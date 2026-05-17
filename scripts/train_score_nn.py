"""Train DeepSets with champion-level composition scores.

This is the same architecture as train_ability_nn.py, but the static champion
features are the compact score columns:

  wave_clear, CC, engage, damage, poke, sustain, frontline

When --empirical-combat is enabled, damage/CC/frontline are built only from the
train split's participants_json so validation/test metrics do not leak future
combat stats into champion priors.
"""
from __future__ import annotations

import json
import sys
import csv
from pathlib import Path

import click
import numpy as np
import torch

from aram_nn.eval import accuracy_np, ece_np, log_loss_np
from aram_nn.models.logreg import train_and_eval as lr_train_eval

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_ability_nn import (  # noqa: E402
    DeepSetsAbility,
    eval_model,
    eval_model_temperature,
    fit_temperature,
    load_split_data,
    make_ability_matrix,
    make_loader,
    train_one,
)
from build_empirical_champion_scores import (  # noqa: E402
    blended_percentile_scores,
    collect_empirical_stats_from_rows,
)
from train_semantic_tree import (  # noqa: E402
    SCORE_COLUMNS,
    train_frame_for_empirical_scores,
)


ROLE_COLUMNS = ("Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank")
EMPIRICAL_PROFILE_COLUMNS = (
    "physical_damage_ratio",
    "magic_damage_ratio",
    "true_damage_ratio",
    "units_healed",
)


def load_score_rows(path: Path) -> dict[int, dict[str, str]]:
    return {
        int(row["champion_id"]): row
        for row in csv.DictReader(path.open(encoding="utf-8-sig"))
    }


def build_score_feature_map(
    score_csv: Path,
    *,
    train_df,
    empirical_combat: bool,
    empirical_min_games: int,
    replace_sustain: bool,
) -> tuple[dict[int, np.ndarray], list[str], dict[str, object] | None]:
    rows = load_score_rows(score_csv)
    stats = {}
    empirical_meta: dict[str, object] | None = None
    damage_scores: dict[int, float] = {}
    cc_scores: dict[int, float] = {}
    frontline_scores: dict[int, float] = {}
    sustain_scores: dict[int, float] = {}
    max_units_healed = 1.0

    if empirical_combat:
        raw_rows = train_df.select(["blue_wins", "duration_sec", "participants_json"]).iter_rows()
        stats = collect_empirical_stats_from_rows(raw_rows)
        damage_scores = blended_percentile_scores(
            stats, min_games=empirical_min_games, metric_a="damage_share", metric_b="damage_per_min"
        )
        cc_scores = blended_percentile_scores(
            stats, min_games=empirical_min_games, metric_a="cc_share", metric_b="cc_per_min"
        )
        frontline_scores = blended_percentile_scores(
            stats, min_games=empirical_min_games, metric_a="frontline_share", metric_b="frontline_per_min"
        )
        sustain_scores = blended_percentile_scores(
            stats, min_games=empirical_min_games, metric_a="sustain_share", metric_b="sustain_per_min"
        )
        max_units_healed = max((row.get("units_healed", 1.0) for row in stats.values()), default=1.0)
        eligible = [cid for cid, row in stats.items() if row.get("games", 0) >= empirical_min_games]
        empirical_meta = {
            "train_rows_used": int(train_df.height),
            "combat_stat_champions": len(stats),
            "eligible_champions": len(eligible),
            "min_games": empirical_min_games,
            "replace_sustain": replace_sustain,
            "empirical_profile_columns": list(EMPIRICAL_PROFILE_COLUMNS),
        }

    feature_names = (
        list(SCORE_COLUMNS)
        + [f"role_{name.lower()}" for name in ROLE_COLUMNS]
        + (list(EMPIRICAL_PROFILE_COLUMNS) if empirical_combat else [])
    )
    feature_map: dict[int, np.ndarray] = {}
    for cid, row in rows.items():
        values = [float(row[col]) for col in SCORE_COLUMNS]
        if empirical_combat and stats.get(cid, {}).get("games", 0) >= empirical_min_games:
            col_idx = {name: i for i, name in enumerate(SCORE_COLUMNS)}
            if cid in damage_scores:
                values[col_idx["damage_score"]] = damage_scores[cid]
            if cid in cc_scores:
                values[col_idx["cc_score"]] = cc_scores[cid]
            if cid in frontline_scores:
                values[col_idx["frontline_score"]] = frontline_scores[cid]
            if replace_sustain and cid in sustain_scores:
                values[col_idx["sustain_score"]] = sustain_scores[cid]

        tags = set((row.get("tags") or "").split("|"))
        values.extend(1.0 if role in tags else 0.0 for role in ROLE_COLUMNS)

        if empirical_combat:
            stat = stats.get(cid, {})
            values.extend(
                [
                    float(stat.get("physical_damage_ratio", 0.0)),
                    float(stat.get("magic_damage_ratio", 0.0)),
                    float(stat.get("true_damage_ratio", 0.0)),
                    min(float(stat.get("units_healed", 1.0)) / max(max_units_healed, 1.0), 1.0),
                ]
            )

        feature_map[cid] = np.asarray(values, dtype=np.float32)

    return feature_map, feature_names, empirical_meta


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--score-csv",
    type=click.Path(exists=True, path_type=Path),
    default=Path("data/cache/champion_semantic_scores.csv"),
    show_default=True,
)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option(
    "--empirical-combat/--static-combat",
    default=True,
    show_default=True,
    help="Replace damage/CC/frontline with train-split participant stats and add empirical profiles.",
)
@click.option("--empirical-min-games", default=20, show_default=True, type=int)
@click.option(
    "--replace-sustain/--keep-static-sustain",
    default=True,
    show_default=True,
    help="Also replace sustain_score with train-split total_heal stats.",
)
@click.option("--embed-dim", default=32, show_default=True)
@click.option("--score-dim", default=12, show_default=True)
@click.option("--hidden", default=96, show_default=True)
@click.option("--dropout", default=0.25, show_default=True, type=float)
@click.option("--lr", default=2e-3, show_default=True, type=float)
@click.option("--weight-decay", default=8e-3, show_default=True, type=float)
@click.option("--epochs", default=45, show_default=True, type=int)
@click.option("--batch-size", default=512, show_default=True, type=int)
@click.option("--patience", default=5, show_default=True, type=int)
@click.option("--eval-every", default=3, show_default=True, type=int)
@click.option("--seed", default=42, show_default=True, type=int)
def main(
    data: Path,
    score_csv: Path,
    patch_prefix: str,
    out: Path,
    empirical_combat: bool,
    empirical_min_games: int,
    replace_sustain: bool,
    embed_dim: int,
    score_dim: int,
    hidden: int,
    dropout: float,
    lr: float,
    weight_decay: float,
    epochs: int,
    batch_size: int,
    patience: int,
    eval_every: int,
    seed: int,
) -> None:
    torch.manual_seed(seed)
    np.random.seed(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    click.echo(f"[device] {device}")

    train_df_for_scores = train_frame_for_empirical_scores(data, patch_prefix)
    score_map, score_names, empirical_meta = build_score_feature_map(
        score_csv,
        train_df=train_df_for_scores,
        empirical_combat=empirical_combat,
        empirical_min_games=empirical_min_games,
        replace_sustain=replace_sustain,
    )
    if empirical_combat and empirical_meta is not None:
        click.echo(
            "[empirical] train-split combat overlay: "
            f"rows={empirical_meta['train_rows_used']} "
            f"eligible_champions={empirical_meta['eligible_champions']}/"
            f"{empirical_meta['combat_stat_champions']} "
            f"min_games={empirical_min_games} replace_sustain={replace_sustain}"
        )

    splits = load_split_data(data, patch_prefix)
    score_matrix, missing = make_ability_matrix(splits.champ_to_idx, score_map)
    click.echo(
        f"[data] train={len(splits.train)} val={len(splits.val)} test={len(splits.test)} "
        f"n_champs={len(splits.champ_to_idx)} blue_base_rate={splits.blue_base_rate:.4f}"
    )
    click.echo(f"[scores] features={score_matrix.shape[1]} missing_champions={missing or 'none'}")

    train_loader = make_loader(splits.train, batch_size, True)
    val_loader = make_loader(splits.val, batch_size, False)
    test_loader = make_loader(splits.test, batch_size, False)

    val_const = {
        "log_loss": log_loss_np(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate)),
        "acc": accuracy_np(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate)),
        "ece": ece_np(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate)),
    }
    test_const = {
        "log_loss": log_loss_np(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate)),
        "acc": accuracy_np(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate)),
        "ece": ece_np(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate)),
    }

    lr_results = lr_train_eval(
        splits.train_lr,
        splits.val_lr,
        splits.test_lr,
        len(splits.champ_to_idx),
    )
    click.echo(
        f"[LR] val_log_loss={lr_results['val/log_loss']:.4f} val_acc={lr_results['val/acc']:.4f} "
        f"test_log_loss={lr_results['test/log_loss']:.4f} test_acc={lr_results['test/acc']:.4f}"
    )

    click.echo("\n[DeepSets embedding-only]")
    base_model = DeepSetsAbility(
        len(splits.champ_to_idx),
        None,
        embed_dim=embed_dim,
        ability_dim=score_dim,
        hidden=hidden,
        dropout=dropout,
    ).to(device)
    click.echo(f"  params={sum(p.numel() for p in base_model.parameters()):,}")
    base_model, base_best_val = train_one(
        base_model,
        train_loader,
        val_loader,
        device=device,
        epochs=epochs,
        lr=lr,
        weight_decay=weight_decay,
        patience=patience,
        eval_every=eval_every,
        swap_aug=True,
    )
    base_val = eval_model(base_model, val_loader, device)
    base_test = eval_model(base_model, test_loader, device)

    click.echo("\n[DeepSets + score features]")
    score_model = DeepSetsAbility(
        len(splits.champ_to_idx),
        score_matrix,
        embed_dim=embed_dim,
        ability_dim=score_dim,
        hidden=hidden,
        dropout=dropout,
    ).to(device)
    click.echo(f"  params={sum(p.numel() for p in score_model.parameters()):,}")
    score_model, score_best_val = train_one(
        score_model,
        train_loader,
        val_loader,
        device=device,
        epochs=epochs,
        lr=lr,
        weight_decay=weight_decay,
        patience=patience,
        eval_every=eval_every,
        swap_aug=True,
    )
    score_val = eval_model(score_model, val_loader, device)
    score_test = eval_model(score_model, test_loader, device)
    temperature = fit_temperature(score_model, val_loader, device)
    score_val_cal = eval_model_temperature(score_model, val_loader, device, temperature)
    score_test_cal = eval_model_temperature(score_model, test_loader, device, temperature)

    rows = [
        ("val", "Constant", val_const),
        ("val", "LR", {"log_loss": lr_results["val/log_loss"], "acc": lr_results["val/acc"], "ece": None}),
        ("val", "DeepSets", base_val),
        ("val", "DeepSets+scores", score_val),
        ("val", "DeepSets+scores cal", score_val_cal),
        ("test", "Constant", test_const),
        ("test", "LR", {"log_loss": lr_results["test/log_loss"], "acc": lr_results["test/acc"], "ece": None}),
        ("test", "DeepSets", base_test),
        ("test", "DeepSets+scores", score_test),
        ("test", "DeepSets+scores cal", score_test_cal),
    ]
    click.echo("\n[results]")
    headers = ["split", "model", "log_loss", "acc", "ece"]
    table = []
    for split, model_name, result in rows:
        ece = result["ece"]
        table.append(
            [
                split,
                model_name,
                f"{result['log_loss']:.4f}",
                f"{result['acc']:.4f}",
                "-" if ece is None else f"{ece:.4f}",
            ]
        )
    widths = [max(len(h), max(len(row[i]) for row in table)) for i, h in enumerate(headers)]
    click.echo("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    click.echo("  " + "-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in table:
        click.echo("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(row))))

    out.mkdir(parents=True, exist_ok=True)
    summary = {
        "data": str(data),
        "score_csv": str(score_csv),
        "patch_prefix": patch_prefix,
        "seed": seed,
        "n_champs": len(splits.champ_to_idx),
        "score_columns": score_names,
        "score_feature_count": int(score_matrix.shape[1]),
        "missing_score_champions": missing,
        "empirical_combat": empirical_combat,
        "empirical_meta": empirical_meta,
        "train_rows": len(splits.train),
        "val_rows": len(splits.val),
        "test_rows": len(splits.test),
        "blue_base_rate": splits.blue_base_rate,
        "base_best_val_log_loss": base_best_val,
        "score_best_val_log_loss": score_best_val,
        "score_temperature": temperature,
        "results": {f"{split}/{name}": result for split, name, result in rows},
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    torch.save(
        {
            "base_model": base_model.state_dict(),
            "score_model": score_model.state_dict(),
            "score_temperature": temperature,
            "champ_to_idx": splits.champ_to_idx,
            "score_feature_names": score_names,
            "score_matrix": score_matrix.cpu(),
        },
        out / "checkpoint.pt",
    )
    click.echo(f"[saved] {out / 'summary.json'}")
    click.echo(f"[saved] {out / 'checkpoint.pt'}")


if __name__ == "__main__":
    main()
