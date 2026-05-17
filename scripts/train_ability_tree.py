"""Train tree-based tabular models with champion ability features.

Compares the existing champion-presence LR baseline against LightGBM and
XGBoost on the same time-based split used by the ability NN experiment.

Feature layout:
  - signed champion presence: +1 blue, -1 red
  - ability diff: sum(blue ability features) - sum(red ability features)
  - ability total: sum(blue ability features) + sum(red ability features)

The training matrix is augmented with swapped teams and flipped labels so the
tree models see the same antisymmetry constraint the NN architecture enforces.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import warnings

import click
import numpy as np
from sklearn.linear_model import LogisticRegression as SkLogisticRegression

from aram_nn.eval import accuracy_np, ece_np, log_loss_np
from aram_nn.models.logreg import train_and_eval as lr_train_eval

from train_ability_nn import build_ability_feature_map, load_split_data, make_ability_matrix


def dataset_to_matrix(
    dataset,
    *,
    n_champs: int,
    ability_matrix: np.ndarray,
    include_champions: bool,
    include_ability: bool,
) -> tuple[np.ndarray, np.ndarray]:
    blocks: list[np.ndarray] = []

    if include_champions:
        x_champ = np.zeros((len(dataset), n_champs), dtype=np.float32)
        for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red, strict=True)):
            x_champ[i, blue] = 1.0
            x_champ[i, red] = -1.0
        blocks.append(x_champ)

    if include_ability:
        n_features = ability_matrix.shape[1]
        x_diff = np.zeros((len(dataset), n_features), dtype=np.float32)
        x_total = np.zeros((len(dataset), n_features), dtype=np.float32)
        for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red, strict=True)):
            blue_sum = ability_matrix[blue].sum(axis=0)
            red_sum = ability_matrix[red].sum(axis=0)
            x_diff[i] = blue_sum - red_sum
            x_total[i] = blue_sum + red_sum
        blocks.extend([x_diff, x_total])

    if not blocks:
        raise click.ClickException("No feature blocks selected")

    x = np.concatenate(blocks, axis=1)
    y = dataset.labels.astype(np.float32)
    return x, y


def augment_swaps(x: np.ndarray, y: np.ndarray, *, n_champs: int, n_ability: int) -> tuple[np.ndarray, np.ndarray]:
    """Append swapped-team rows.

    signed champion presence and ability diff flip sign; ability total stays the
    same.  This matches feature layout from dataset_to_matrix.
    """
    x_swapped = x.copy()
    offset = 0
    if n_champs:
        x_swapped[:, offset : offset + n_champs] *= -1.0
        offset += n_champs
    if n_ability:
        x_swapped[:, offset : offset + n_ability] *= -1.0
        offset += n_ability
        offset += n_ability  # total block stays unchanged
    return np.vstack([x, x_swapped]), np.concatenate([y, 1.0 - y])


def metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    return {
        "log_loss": log_loss_np(y, p),
        "acc": accuracy_np(y, p),
        "ece": ece_np(y, p),
    }


def train_lightgbm(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray,
    y_val: np.ndarray,
    *,
    seed: int,
):
    import lightgbm as lgb

    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=2000,
        learning_rate=0.025,
        num_leaves=31,
        min_child_samples=80,
        subsample=0.9,
        subsample_freq=1,
        colsample_bytree=0.9,
        reg_alpha=0.1,
        reg_lambda=5.0,
        random_state=seed,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(
        x_train,
        y_train,
        eval_set=[(x_val, y_val)],
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(80, verbose=False)],
    )
    return model


def train_xgboost(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray,
    y_val: np.ndarray,
    *,
    seed: int,
):
    import xgboost as xgb

    model = xgb.XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        n_estimators=2500,
        learning_rate=0.02,
        max_depth=2,
        min_child_weight=25,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_alpha=0.1,
        reg_lambda=5.0,
        tree_method="hist",
        random_state=seed,
        n_jobs=-1,
        early_stopping_rounds=100,
    )
    model.fit(x_train, y_train, eval_set=[(x_val, y_val)], verbose=False)
    return model


def predict_proba_1(model: Any, x: np.ndarray) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="X does not have valid feature names")
        return model.predict_proba(x)[:, 1].astype(np.float64)


def platt_calibrated(
    p_val: np.ndarray,
    y_val: np.ndarray,
    p_test: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    eps = 1e-6
    p_val = np.clip(p_val, eps, 1.0 - eps)
    p_test = np.clip(p_test, eps, 1.0 - eps)
    z_val = np.log(p_val / (1.0 - p_val)).reshape(-1, 1)
    z_test = np.log(p_test / (1.0 - p_test)).reshape(-1, 1)
    calibrator = SkLogisticRegression(C=1.0, solver="lbfgs")
    calibrator.fit(z_val, y_val)
    return calibrator.predict_proba(z_val)[:, 1], calibrator.predict_proba(z_test)[:, 1]


def feature_mode_dims(mode: str, n_champs: int, n_ability_features: int) -> tuple[bool, bool, int, int]:
    include_champions = mode in {"champion", "combined"}
    include_ability = mode in {"ability", "combined"}
    champ_dim = n_champs if include_champions else 0
    ability_dim = n_ability_features if include_ability else 0
    return include_champions, include_ability, champ_dim, ability_dim


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--ability-json", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option(
    "--mode",
    type=click.Choice(["ability", "champion", "combined"]),
    multiple=True,
    default=("ability", "combined"),
    show_default=True,
    help="Feature blocks to train tree models on.",
)
@click.option("--seed", default=42, show_default=True, type=int)
def main(data: Path, ability_json: Path, patch_prefix: str, out: Path, mode: tuple[str, ...], seed: int) -> None:
    np.random.seed(seed)

    feature_map, feature_names = build_ability_feature_map(ability_json)
    splits = load_split_data(data, patch_prefix)
    ability_tensor, missing = make_ability_matrix(splits.champ_to_idx, feature_map)
    ability_matrix = ability_tensor.numpy().astype(np.float32)
    n_champs = len(splits.champ_to_idx)
    n_ability_features = int(ability_matrix.shape[1])

    click.echo(
        f"[data] train={len(splits.train)} val={len(splits.val)} test={len(splits.test)} "
        f"n_champs={n_champs} blue_base_rate={splits.blue_base_rate:.4f}"
    )
    click.echo(f"[ability] features={n_ability_features} missing_champions={missing or 'none'}")

    summary: dict[str, Any] = {
        "data": str(data),
        "ability_json": str(ability_json),
        "patch_prefix": patch_prefix,
        "seed": seed,
        "n_champs": n_champs,
        "ability_feature_count": n_ability_features,
        "ability_feature_names": feature_names,
        "missing_ability_champions": missing,
        "train_rows": len(splits.train),
        "val_rows": len(splits.val),
        "test_rows": len(splits.test),
        "blue_base_rate": splits.blue_base_rate,
        "results": {},
    }

    click.echo("\n[baseline/constant]")
    const_val = metrics(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate))
    const_test = metrics(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate))
    summary["results"]["val/Constant"] = const_val
    summary["results"]["test/Constant"] = const_test
    click.echo(f"  val log_loss={const_val['log_loss']:.4f} acc={const_val['acc']:.4f}")

    click.echo("\n[LR baseline]")
    lr_results = lr_train_eval(
        splits.train_lr,
        splits.val_lr,
        splits.test_lr,
        n_champs,
    )
    lr_val = {"log_loss": lr_results["val/log_loss"], "acc": lr_results["val/acc"], "ece": None}
    lr_test = {"log_loss": lr_results["test/log_loss"], "acc": lr_results["test/acc"], "ece": None}
    summary["results"]["val/LR"] = lr_val
    summary["results"]["test/LR"] = lr_test
    click.echo(
        f"  val log_loss={lr_val['log_loss']:.4f} acc={lr_val['acc']:.4f} "
        f"test log_loss={lr_test['log_loss']:.4f} acc={lr_test['acc']:.4f}"
    )

    trained_models: dict[str, Any] = {}
    for feature_mode in mode:
        include_champions, include_ability, champ_dim, ability_dim = feature_mode_dims(
            feature_mode, n_champs, n_ability_features
        )
        x_train, y_train = dataset_to_matrix(
            splits.train,
            n_champs=n_champs,
            ability_matrix=ability_matrix,
            include_champions=include_champions,
            include_ability=include_ability,
        )
        x_val, y_val = dataset_to_matrix(
            splits.val,
            n_champs=n_champs,
            ability_matrix=ability_matrix,
            include_champions=include_champions,
            include_ability=include_ability,
        )
        x_test, y_test = dataset_to_matrix(
            splits.test,
            n_champs=n_champs,
            ability_matrix=ability_matrix,
            include_champions=include_champions,
            include_ability=include_ability,
        )
        x_train_aug, y_train_aug = augment_swaps(
            x_train,
            y_train,
            n_champs=champ_dim,
            n_ability=ability_dim,
        )
        click.echo(
            f"\n[features/{feature_mode}] x_train={x_train.shape} "
            f"x_train_aug={x_train_aug.shape}"
        )

        click.echo("[LightGBM] training...")
        lgbm = train_lightgbm(x_train_aug, y_train_aug, x_val, y_val, seed=seed)
        lgbm_p_val = predict_proba_1(lgbm, x_val)
        lgbm_p_test = predict_proba_1(lgbm, x_test)
        lgbm_val = metrics(y_val, lgbm_p_val)
        lgbm_test = metrics(y_test, lgbm_p_test)
        lgbm_p_val_cal, lgbm_p_test_cal = platt_calibrated(lgbm_p_val, y_val, lgbm_p_test)
        lgbm_val_cal = metrics(y_val, lgbm_p_val_cal)
        lgbm_test_cal = metrics(y_test, lgbm_p_test_cal)
        lgbm_name = f"LightGBM {feature_mode}"
        summary["results"][f"val/{lgbm_name}"] = lgbm_val
        summary["results"][f"test/{lgbm_name}"] = lgbm_test
        summary["results"][f"val/{lgbm_name} cal"] = lgbm_val_cal
        summary["results"][f"test/{lgbm_name} cal"] = lgbm_test_cal
        summary[f"lightgbm_{feature_mode}_best_iteration"] = getattr(lgbm, "best_iteration_", None)
        trained_models[lgbm_name] = lgbm
        click.echo(
            f"  val log_loss={lgbm_val['log_loss']:.4f} acc={lgbm_val['acc']:.4f} "
            f"test log_loss={lgbm_test['log_loss']:.4f} acc={lgbm_test['acc']:.4f} "
            f"test_cal_log_loss={lgbm_test_cal['log_loss']:.4f}"
        )

        click.echo("[XGBoost] training...")
        xgb = train_xgboost(x_train_aug, y_train_aug, x_val, y_val, seed=seed)
        xgb_p_val = predict_proba_1(xgb, x_val)
        xgb_p_test = predict_proba_1(xgb, x_test)
        xgb_val = metrics(y_val, xgb_p_val)
        xgb_test = metrics(y_test, xgb_p_test)
        xgb_p_val_cal, xgb_p_test_cal = platt_calibrated(xgb_p_val, y_val, xgb_p_test)
        xgb_val_cal = metrics(y_val, xgb_p_val_cal)
        xgb_test_cal = metrics(y_test, xgb_p_test_cal)
        xgb_name = f"XGBoost {feature_mode}"
        summary["results"][f"val/{xgb_name}"] = xgb_val
        summary["results"][f"test/{xgb_name}"] = xgb_test
        summary["results"][f"val/{xgb_name} cal"] = xgb_val_cal
        summary["results"][f"test/{xgb_name} cal"] = xgb_test_cal
        summary[f"xgboost_{feature_mode}_best_iteration"] = getattr(xgb, "best_iteration", None)
        trained_models[xgb_name] = xgb
        click.echo(
            f"  val log_loss={xgb_val['log_loss']:.4f} acc={xgb_val['acc']:.4f} "
            f"test log_loss={xgb_test['log_loss']:.4f} acc={xgb_test['acc']:.4f} "
            f"test_cal_log_loss={xgb_test_cal['log_loss']:.4f}"
        )

    click.echo("\n[results]")
    rows = []
    for key, result in summary["results"].items():
        split, name = key.split("/", 1)
        rows.append(
            [
                split,
                name,
                f"{result['log_loss']:.4f}",
                f"{result['acc']:.4f}",
                "-" if result["ece"] is None else f"{result['ece']:.4f}",
            ]
        )
    rows.sort(key=lambda r: (r[0] != "val", r[1]))
    headers = ["split", "model", "log_loss", "acc", "ece"]
    widths = [max(len(h), max(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    click.echo("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    click.echo("  " + "-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        click.echo("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(row))))

    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # Keep models optional and lightweight enough for follow-up inspection.
    import pickle

    (out / "models.pkl").write_bytes(pickle.dumps(trained_models))
    click.echo(f"[saved] {out / 'summary.json'}")
    click.echo(f"[saved] {out / 'models.pkl'}")


if __name__ == "__main__":
    main()
