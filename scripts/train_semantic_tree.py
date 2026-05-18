"""Train tree models on explicit team-level semantic composition features.

This tests the hypothesis:

  good ARAM/Mayhem teams need a balanced mix of wave clear, CC/engage, and damage.

Inputs:
  - data/raw/*.parquet match rows
  - data/cache/champion_semantic_scores.csv from build_semantic_ability_scores.py

Features:
  - optional signed champion identity
  - semantic score diff/total for each capability
  - derived team balance features: core min/mean/std, lacks flags, lacks count

The train split is swap-augmented so tree models see both team orientations.
"""
from __future__ import annotations

import csv
import json
import pickle
import sys
from pathlib import Path
from typing import Any
import warnings

import click
import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression as SkLogisticRegression

from aram_nn.eval import accuracy_np, ece_np, log_loss_np
from aram_nn.models.logreg import train_and_eval as lr_train_eval

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_empirical_champion_scores import (  # noqa: E402
    blended_percentile_scores,
    collect_empirical_stats_from_rows,
)
from train_ability_nn import load_split_data  # noqa: E402


SCORE_COLUMNS = (
    "wave_clear_score",
    "cc_score",
    "engage_score",
    "damage_score",
    "poke_score",
    "sustain_score",
    "frontline_score",
)

CORE_COLUMNS = (
    "wave_clear_score",
    "cc_score",
    "engage_score",
    "damage_score",
)

# A first-pass "enough to be present in a 5-person team" threshold.  These are
# intentionally moderate because scores are heuristic and Mayhem augments can
# compensate for composition gaps.
LACK_THRESHOLDS = {
    "wave_clear_score": 3.0,
    "cc_score": 3.0,
    "engage_score": 2.2,
    "damage_score": 5.5,
    "poke_score": 2.0,
    "sustain_score": 1.5,
    "frontline_score": 1.8,
}


def load_semantic_scores(path: Path) -> tuple[dict[int, np.ndarray], list[str]]:
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    mapping: dict[int, np.ndarray] = {}
    for row in rows:
        mapping[int(row["champion_id"])] = np.asarray(
            [float(row[col]) for col in SCORE_COLUMNS],
            dtype=np.float32,
        )
    return mapping, list(SCORE_COLUMNS)


def train_frame_for_empirical_scores(
    data: Path,
    patch_prefix: str,
    *,
    min_duration: int = 300,
    val_frac: float = 0.15,
    test_frac: float = 0.15,
) -> pl.DataFrame:
    df = pl.read_parquet(data)
    df = df.filter(pl.col("duration_sec") >= min_duration)
    if patch_prefix:
        df = df.with_columns(
            pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("patch_prefix")
        ).filter(pl.col("patch_prefix") == patch_prefix)

    if df.height == 0:
        raise click.ClickException("No rows after data filters")
    if "participants_json" not in df.columns:
        raise click.ClickException("Cannot build empirical scores: participants_json column is missing")

    df = df.sort("game_creation_ms")
    n = df.height
    n_test = max(1, int(n * test_frac))
    n_val = max(1, int(n * val_frac))
    n_train = n - n_test - n_val
    return df.slice(0, n_train)


def overlay_empirical_combat_scores(
    score_map: dict[int, np.ndarray],
    train_df: pl.DataFrame,
    *,
    min_games: int,
    replace_sustain: bool,
) -> tuple[dict[int, np.ndarray], dict[str, Any]]:
    rows = train_df.select(["blue_wins", "duration_sec", "participants_json"]).iter_rows()
    stats = collect_empirical_stats_from_rows(rows)
    damage_scores = blended_percentile_scores(
        stats, min_games=min_games, metric_a="damage_share", metric_b="damage_per_min"
    )
    cc_scores = blended_percentile_scores(
        stats, min_games=min_games, metric_a="cc_share", metric_b="cc_per_min"
    )
    frontline_scores = blended_percentile_scores(
        stats, min_games=min_games, metric_a="frontline_share", metric_b="frontline_per_min"
    )
    sustain_scores = blended_percentile_scores(
        stats, min_games=min_games, metric_a="sustain_share", metric_b="sustain_per_min"
    )

    col_idx = {name: i for i, name in enumerate(SCORE_COLUMNS)}
    out = {cid: vec.copy() for cid, vec in score_map.items()}
    replaced = 0
    for cid, stat in stats.items():
        if stat.get("games", 0) < min_games or cid not in out:
            continue
        replaced += 1
        if cid in damage_scores:
            out[cid][col_idx["damage_score"]] = damage_scores[cid]
        if cid in cc_scores:
            out[cid][col_idx["cc_score"]] = cc_scores[cid]
        if cid in frontline_scores:
            out[cid][col_idx["frontline_score"]] = frontline_scores[cid]
        if replace_sustain and cid in sustain_scores:
            out[cid][col_idx["sustain_score"]] = sustain_scores[cid]

    eligible = [row for row in stats.values() if row.get("games", 0) >= min_games]
    meta = {
        "train_rows_used": int(train_df.height),
        "champions_with_stats": len(stats),
        "champions_replaced": replaced,
        "eligible_champions": len(eligible),
        "min_games": min_games,
        "replace_sustain": replace_sustain,
    }
    return out, meta


def score_matrix_for_vocab(
    champ_to_idx: dict[int, int],
    score_map: dict[int, np.ndarray],
) -> tuple[np.ndarray, list[int]]:
    mat = np.zeros((len(champ_to_idx), len(SCORE_COLUMNS)), dtype=np.float32)
    missing: list[int] = []
    for cid, idx in champ_to_idx.items():
        vec = score_map.get(cid)
        if vec is None:
            missing.append(cid)
        else:
            mat[idx] = vec
    return mat, missing


def team_summary(indices: list[int], score_matrix: np.ndarray) -> np.ndarray:
    team = score_matrix[indices]
    sums = team.sum(axis=0)
    maxs = team.max(axis=0)
    mins = team.min(axis=0)
    means = team.mean(axis=0)

    core_idx = [SCORE_COLUMNS.index(col) for col in CORE_COLUMNS]
    core = sums[core_idx]
    lacks = np.asarray(
        [
            1.0 if sums[i] < LACK_THRESHOLDS[col] else 0.0
            for i, col in enumerate(SCORE_COLUMNS)
        ],
        dtype=np.float32,
    )

    derived = np.asarray(
        [
            float(core.min()),
            float(core.mean()),
            float(core.std()),
            float(lacks[: len(CORE_COLUMNS)].sum()),
            float(lacks.sum()),
        ],
        dtype=np.float32,
    )
    return np.concatenate([sums, means, maxs, mins, lacks, derived]).astype(np.float32)


def semantic_feature_names() -> list[str]:
    names: list[str] = []
    for prefix in ("sum", "mean", "max", "min", "lacks"):
        names.extend(f"{prefix}_{col.replace('_score', '')}" for col in SCORE_COLUMNS)
    names.extend(["core_min", "core_mean", "core_std", "core_lacks_count", "all_lacks_count"])

    out: list[str] = []
    out.extend(f"diff_{name}" for name in names)
    out.extend(f"total_{name}" for name in names)
    out.extend(f"absdiff_{name}" for name in names)
    return out


def dataset_to_semantic_matrix(
    dataset,
    *,
    n_champs: int,
    score_matrix: np.ndarray,
    include_champions: bool,
) -> tuple[np.ndarray, np.ndarray, int]:
    blocks: list[np.ndarray] = []
    champ_dim = n_champs if include_champions else 0
    if include_champions:
        x_champ = np.zeros((len(dataset), n_champs), dtype=np.float32)
        for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red, strict=True)):
            x_champ[i, blue] = 1.0
            x_champ[i, red] = -1.0
        blocks.append(x_champ)

    n_semantic = len(semantic_feature_names())
    x_sem = np.zeros((len(dataset), n_semantic), dtype=np.float32)
    for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red, strict=True)):
        blue_s = team_summary(blue, score_matrix)
        red_s = team_summary(red, score_matrix)
        diff = blue_s - red_s
        total = blue_s + red_s
        absdiff = np.abs(diff)
        x_sem[i] = np.concatenate([diff, total, absdiff]).astype(np.float32)
    blocks.append(x_sem)

    return np.concatenate(blocks, axis=1), dataset.labels.astype(np.float32), champ_dim


def augment_swaps(
    x: np.ndarray,
    y: np.ndarray,
    *,
    champ_dim: int,
    semantic_dim: int,
) -> tuple[np.ndarray, np.ndarray]:
    x_swapped = x.copy()
    offset = 0
    if champ_dim:
        x_swapped[:, :champ_dim] *= -1.0
        offset += champ_dim
    # semantic layout: diff, total, absdiff.  Only diff flips.
    x_swapped[:, offset : offset + semantic_dim // 3] *= -1.0
    return np.vstack([x, x_swapped]), np.concatenate([y, 1.0 - y])


def metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    return {
        "log_loss": log_loss_np(y, p),
        "acc": accuracy_np(y, p),
        "ece": ece_np(y, p),
    }


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
    cal = SkLogisticRegression(C=1.0, solver="lbfgs")
    cal.fit(z_val, y_val)
    return cal.predict_proba(z_val)[:, 1], cal.predict_proba(z_test)[:, 1]


def train_lightgbm(x_train, y_train, x_val, y_val, *, seed: int):
    import lightgbm as lgb

    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=1000,
        learning_rate=0.025,
        num_leaves=31,
        min_child_samples=80,
        subsample=0.9,
        subsample_freq=1,
        colsample_bytree=0.9,
        reg_alpha=0.1,
        reg_lambda=5.0,
        random_state=seed,
        n_jobs=4,
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


def train_xgboost(x_train, y_train, x_val, y_val, *, seed: int):
    import xgboost as xgb

    model = xgb.XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        n_estimators=1000,
        learning_rate=0.02,
        max_depth=2,
        min_child_weight=25,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_alpha=0.1,
        reg_lambda=5.0,
        tree_method="hist",
        random_state=seed,
        n_jobs=4,
        early_stopping_rounds=100,
    )
    model.fit(x_train, y_train, eval_set=[(x_val, y_val)], verbose=False)
    return model


def add_result(
    summary: dict[str, Any],
    name: str,
    model: Any,
    x_val: np.ndarray,
    y_val: np.ndarray,
    x_test: np.ndarray,
    y_test: np.ndarray,
) -> None:
    p_val = predict_proba_1(model, x_val)
    p_test = predict_proba_1(model, x_test)
    val = metrics(y_val, p_val)
    test = metrics(y_test, p_test)
    p_val_cal, p_test_cal = platt_calibrated(p_val, y_val, p_test)
    val_cal = metrics(y_val, p_val_cal)
    test_cal = metrics(y_test, p_test_cal)
    summary["results"][f"val/{name}"] = val
    summary["results"][f"test/{name}"] = test
    summary["results"][f"val/{name} cal"] = val_cal
    summary["results"][f"test/{name} cal"] = test_cal
    click.echo(
        f"  {name}: val_log_loss={val['log_loss']:.4f} val_acc={val['acc']:.4f} "
        f"test_log_loss={test['log_loss']:.4f} test_acc={test['acc']:.4f} "
        f"test_cal_log_loss={test_cal['log_loss']:.4f}"
    )


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--semantic-csv",
    type=click.Path(exists=True, path_type=Path),
    default=Path("data/cache/champion_semantic_scores.csv"),
    show_default=True,
)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--include-champions/--semantic-only", default=True, show_default=True)
@click.option(
    "--empirical-combat/--static-combat",
    default=False,
    show_default=True,
    help="Replace damage/CC/frontline scores with train-split participant stats.",
)
@click.option("--empirical-min-games", default=20, show_default=True, type=int)
@click.option(
    "--replace-sustain/--keep-static-sustain",
    default=True,
    show_default=True,
    help="Also replace sustain_score with train-split total_heal stats.",
)
@click.option("--seed", default=42, show_default=True, type=int)
def main(
    data: Path,
    semantic_csv: Path,
    patch_prefix: str,
    out: Path,
    include_champions: bool,
    empirical_combat: bool,
    empirical_min_games: int,
    replace_sustain: bool,
    seed: int,
) -> None:
    score_map, score_names = load_semantic_scores(semantic_csv)
    splits = load_split_data(data, patch_prefix)
    empirical_meta: dict[str, Any] | None = None
    if empirical_combat:
        train_df_for_scores = train_frame_for_empirical_scores(data, patch_prefix)
        score_map, empirical_meta = overlay_empirical_combat_scores(
            score_map,
            train_df_for_scores,
            min_games=empirical_min_games,
            replace_sustain=replace_sustain,
        )
        click.echo(
            "[empirical] train-split combat overlay: "
            f"rows={empirical_meta['train_rows_used']} "
            f"champions={empirical_meta['champions_replaced']}/{empirical_meta['champions_with_stats']} "
            f"min_games={empirical_min_games} replace_sustain={replace_sustain}"
        )

    n_champs = len(splits.champ_to_idx)
    score_matrix, missing = score_matrix_for_vocab(splits.champ_to_idx, score_map)
    sem_names = semantic_feature_names()

    click.echo(
        f"[data] train={len(splits.train)} val={len(splits.val)} test={len(splits.test)} "
        f"n_champs={n_champs} blue_base_rate={splits.blue_base_rate:.4f}"
    )
    click.echo(f"[semantic] score_cols={score_names} team_features={len(sem_names)} missing={missing or 'none'}")

    x_train, y_train, champ_dim = dataset_to_semantic_matrix(
        splits.train,
        n_champs=n_champs,
        score_matrix=score_matrix,
        include_champions=include_champions,
    )
    x_val, y_val, _ = dataset_to_semantic_matrix(
        splits.val,
        n_champs=n_champs,
        score_matrix=score_matrix,
        include_champions=include_champions,
    )
    x_test, y_test, _ = dataset_to_semantic_matrix(
        splits.test,
        n_champs=n_champs,
        score_matrix=score_matrix,
        include_champions=include_champions,
    )
    x_train_aug, y_train_aug = augment_swaps(
        x_train,
        y_train,
        champ_dim=champ_dim,
        semantic_dim=len(sem_names),
    )
    click.echo(f"[features] x_train={x_train.shape} x_train_aug={x_train_aug.shape}")

    summary: dict[str, Any] = {
        "data": str(data),
        "semantic_csv": str(semantic_csv),
        "patch_prefix": patch_prefix,
        "include_champions": include_champions,
        "seed": seed,
        "n_champs": n_champs,
        "semantic_score_columns": score_names,
        "semantic_feature_names": sem_names,
        "missing_semantic_champions": missing,
        "empirical_combat": empirical_combat,
        "empirical_meta": empirical_meta,
        "train_rows": len(splits.train),
        "val_rows": len(splits.val),
        "test_rows": len(splits.test),
        "blue_base_rate": splits.blue_base_rate,
        "lack_thresholds": LACK_THRESHOLDS,
        "results": {},
    }

    const_val = metrics(y_val, np.full(len(y_val), splits.blue_base_rate))
    const_test = metrics(y_test, np.full(len(y_test), splits.blue_base_rate))
    summary["results"]["val/Constant"] = const_val
    summary["results"]["test/Constant"] = const_test

    lr_results = lr_train_eval(splits.train_lr, splits.val_lr, splits.test_lr, n_champs)
    summary["results"]["val/LR"] = {
        "log_loss": lr_results["val/log_loss"],
        "acc": lr_results["val/acc"],
        "ece": None,
    }
    summary["results"]["test/LR"] = {
        "log_loss": lr_results["test/log_loss"],
        "acc": lr_results["test/acc"],
        "ece": None,
    }
    click.echo(
        f"[LR] val_log_loss={lr_results['val/log_loss']:.4f} val_acc={lr_results['val/acc']:.4f} "
        f"test_log_loss={lr_results['test/log_loss']:.4f} test_acc={lr_results['test/acc']:.4f}"
    )

    models: dict[str, Any] = {}
    click.echo("[LightGBM] training...")
    lgbm = train_lightgbm(x_train_aug, y_train_aug, x_val, y_val, seed=seed)
    models["LightGBM semantic"] = lgbm
    add_result(summary, "LightGBM semantic", lgbm, x_val, y_val, x_test, y_test)

    click.echo("[XGBoost] training...")
    xgb = train_xgboost(x_train_aug, y_train_aug, x_val, y_val, seed=seed)
    models["XGBoost semantic"] = xgb
    add_result(summary, "XGBoost semantic", xgb, x_val, y_val, x_test, y_test)

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
    (out / "models.pkl").write_bytes(pickle.dumps(models))
    click.echo(f"[saved] {out / 'summary.json'}")
    click.echo(f"[saved] {out / 'models.pkl'}")


if __name__ == "__main__":
    main()
