"""Build champion scores from empirical LCU participant stats.

This replaces hand-guessed combat columns with observed data when available:

  - damage_score    <- damage to champions share / per-minute output
  - cc_score        <- time_ccing_others share / per-minute CC
  - frontline_score <- damage taken + partial self-mitigated damage
  - sustain_score   <- optional total_heal override; off by default because
                       item/augment healing can swamp kit sustain

The script keeps semantic/ability-derived columns such as wave_clear, poke, and
engage, then overwrites the combat columns for champions with enough games.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import click


COMBAT_COLUMNS = ("damage_score", "cc_score", "frontline_score")


def has_combat_stats(participant: dict[str, Any]) -> bool:
    stats = participant.get("stats") or {}
    return stats.get("total_damage_dealt_to_champions") is not None


def safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def percentile_scores(values: dict[int, float]) -> dict[int, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 1.5}
    return {
        cid: round(3.0 * rank / (n - 1), 2)
        for rank, (cid, _value) in enumerate(ordered)
    }


def patch_where_clause(patch_prefix: str) -> tuple[str, list[Any]]:
    if not patch_prefix:
        return "", []
    return " AND patch LIKE ?", [f"{patch_prefix}.%"]


def load_semantic_rows(path: Path) -> tuple[list[dict[str, str]], dict[int, dict[str, str]]]:
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    by_id = {int(row["champion_id"]): row for row in rows}
    return rows, by_id


def add_match_to_agg(
    agg: dict[int, dict[str, float]],
    *,
    blue_wins: bool | int,
    duration_sec: float | int,
    participants_json: str | None,
) -> None:
    if not participants_json:
        return
    try:
        participants = json.loads(participants_json)
    except Exception:
        return
    if not isinstance(participants, list) or len(participants) != 10:
        return
    minutes = max(float(duration_sec) / 60.0, 1.0)

    by_team: dict[int, list[dict[str, Any]]] = {100: [], 200: []}
    for participant in participants:
        team_id = int(participant.get("teamId", 0) or 0)
        if team_id in by_team:
            by_team[team_id].append(participant)
    if len(by_team[100]) != 5 or len(by_team[200]) != 5:
        return
    if any(not has_combat_stats(participant) for team in by_team.values() for participant in team):
        return

    blue_won = int(bool(blue_wins))
    for team in by_team.values():
        team_damage = 0.0
        team_cc = 0.0
        team_frontline = 0.0
        team_heal = 0.0
        team_sustain = 0.0
        prepared: list[tuple[dict[str, Any], dict[str, float]]] = []

        for participant in team:
            stats = participant.get("stats") or {}
            damage = safe_float(stats.get("total_damage_dealt_to_champions"))
            physical_damage = safe_float(stats.get("physical_damage_dealt_to_champions"))
            magic_damage = safe_float(stats.get("magic_damage_dealt_to_champions"))
            true_damage = safe_float(stats.get("true_damage_dealt_to_champions"))
            # time_ccing_others is the cleaner champion-control stat.
            cc = safe_float(stats.get("time_ccing_others"))
            taken = safe_float(stats.get("total_damage_taken"))
            mitigated = safe_float(stats.get("damage_self_mitigated"))
            frontline = taken + 0.5 * mitigated
            heal = safe_float(stats.get("total_heal"))
            units_healed = max(safe_float(stats.get("total_units_healed")), 1.0)
            ally_heal_raw = stats.get("total_heals_on_teammates")
            ally_shield_raw = stats.get("total_damage_shielded_on_teammates")
            ally_heal = safe_float(ally_heal_raw)
            ally_shield = safe_float(ally_shield_raw)
            effective_heal_shield = safe_float(stats.get("effective_heal_and_shielding"))
            if ally_heal_raw is not None or ally_shield_raw is not None:
                self_heal = max(heal - ally_heal, 0.0)
                sustain = ally_heal + 0.8 * ally_shield + 0.35 * self_heal
            else:
                # Older LCU payloads in our DB only have totalHeal and
                # totalUnitsHealed.  Use healed-unit count as a weak proxy for
                # ally-facing healing while still giving self-heal some value.
                ally_fraction = max(0.0, min(units_healed - 1.0, 4.0)) / min(units_healed, 5.0)
                sustain = heal * (0.35 + 2.0 * ally_fraction)
            damage_den = max(physical_damage + magic_damage + true_damage, damage, 1.0)
            vals = {
                "damage": damage,
                "physical_damage": physical_damage,
                "magic_damage": magic_damage,
                "true_damage": true_damage,
                "physical_damage_ratio": physical_damage / damage_den,
                "magic_damage_ratio": magic_damage / damage_den,
                "true_damage_ratio": true_damage / damage_den,
                "cc": cc,
                "frontline": frontline,
                "heal": heal,
                "ally_heal": ally_heal,
                "ally_shield": ally_shield,
                "effective_heal_shield": effective_heal_shield,
                "units_healed": units_healed,
                "sustain": sustain,
            }
            prepared.append((participant, vals))
            team_damage += damage
            team_cc += cc
            team_frontline += frontline
            team_heal += heal
            team_sustain += sustain

        for participant, vals in prepared:
            cid = int(participant.get("championId", 0) or 0)
            if cid <= 0:
                continue
            row = agg[cid]
            row["games"] += 1
            row["wins"] += 1 if (
                (int(participant.get("teamId", 0)) == 100 and blue_won == 1)
                or (int(participant.get("teamId", 0)) == 200 and blue_won == 0)
            ) else 0
            row["damage_per_min_sum"] += vals["damage"] / minutes
            row["cc_per_min_sum"] += vals["cc"] / minutes
            row["frontline_per_min_sum"] += vals["frontline"] / minutes
            row["heal_per_min_sum"] += vals["heal"] / minutes
            row["ally_heal_per_min_sum"] += vals["ally_heal"] / minutes
            row["ally_shield_per_min_sum"] += vals["ally_shield"] / minutes
            row["effective_heal_shield_per_min_sum"] += vals["effective_heal_shield"] / minutes
            row["sustain_per_min_sum"] += vals["sustain"] / minutes
            row["units_healed_sum"] += vals["units_healed"]
            row["physical_damage_ratio_sum"] += vals["physical_damage_ratio"]
            row["magic_damage_ratio_sum"] += vals["magic_damage_ratio"]
            row["true_damage_ratio_sum"] += vals["true_damage_ratio"]
            row["damage_share_sum"] += vals["damage"] / team_damage if team_damage > 0 else 0.0
            row["cc_share_sum"] += vals["cc"] / team_cc if team_cc > 0 else 0.0
            row["frontline_share_sum"] += (
                vals["frontline"] / team_frontline if team_frontline > 0 else 0.0
            )
            row["heal_share_sum"] += vals["heal"] / team_heal if team_heal > 0 else 0.0
            row["sustain_share_sum"] += vals["sustain"] / team_sustain if team_sustain > 0 else 0.0


def finalize_empirical_stats(agg: dict[int, dict[str, float]]) -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, float]] = {}
    for cid, row in agg.items():
        games = max(row["games"], 1.0)
        out[cid] = {
            "games": row["games"],
            "wins": row["wins"],
            "win_rate": row["wins"] / games,
            "damage_per_min": row["damage_per_min_sum"] / games,
            "cc_per_min": row["cc_per_min_sum"] / games,
            "frontline_per_min": row["frontline_per_min_sum"] / games,
            "heal_per_min": row["heal_per_min_sum"] / games,
            "ally_heal_per_min": row["ally_heal_per_min_sum"] / games,
            "ally_shield_per_min": row["ally_shield_per_min_sum"] / games,
            "effective_heal_shield_per_min": row["effective_heal_shield_per_min_sum"] / games,
            "sustain_per_min": row["sustain_per_min_sum"] / games,
            "units_healed": row["units_healed_sum"] / games,
            "physical_damage_ratio": row["physical_damage_ratio_sum"] / games,
            "magic_damage_ratio": row["magic_damage_ratio_sum"] / games,
            "true_damage_ratio": row["true_damage_ratio_sum"] / games,
            "damage_share": row["damage_share_sum"] / games,
            "cc_share": row["cc_share_sum"] / games,
            "frontline_share": row["frontline_share_sum"] / games,
            "heal_share": row["heal_share_sum"] / games,
            "sustain_share": row["sustain_share_sum"] / games,
        }
    return out


def collect_empirical_stats_from_rows(
    rows: Iterable[tuple[bool | int, float | int, str | None]],
) -> dict[int, dict[str, float]]:
    agg: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for blue_wins, duration_sec, participants_json in rows:
        add_match_to_agg(
            agg,
            blue_wins=blue_wins,
            duration_sec=duration_sec,
            participants_json=participants_json,
        )
    return finalize_empirical_stats(agg)


def collect_empirical_stats(
    db: Path,
    *,
    queue: int,
    patch_prefix: str,
    min_duration: int,
) -> dict[int, dict[str, float]]:
    where_patch, patch_params = patch_where_clause(patch_prefix)
    sql = (
        "SELECT blue_wins, duration_sec, participants_json FROM games "
        "WHERE queue_id = ? AND duration_sec >= ? AND participants_json IS NOT NULL"
        f"{where_patch}"
    )
    params: list[Any] = [queue, min_duration, *patch_params]

    with sqlite3.connect(db) as con:
        return collect_empirical_stats_from_rows(con.execute(sql, params))


def blended_percentile_scores(
    stats: dict[int, dict[str, float]],
    *,
    min_games: int,
    metric_a: str,
    metric_b: str,
    weight_a: float = 0.65,
) -> dict[int, float]:
    eligible = {
        cid: row for cid, row in stats.items()
        if row.get("games", 0) >= min_games
    }
    pa = percentile_scores({cid: row[metric_a] for cid, row in eligible.items()})
    pb = percentile_scores({cid: row[metric_b] for cid, row in eligible.items()})
    return {
        cid: round(weight_a * pa.get(cid, 0.0) + (1.0 - weight_a) * pb.get(cid, 0.0), 2)
        for cid in eligible
    }


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        raise click.ClickException("No rows to write")
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


@click.command()
@click.option("--db", type=click.Path(exists=True, path_type=Path), default=Path("data/lcu/games.db"), show_default=True)
@click.option("--semantic-csv", type=click.Path(exists=True, path_type=Path), default=Path("data/cache/champion_semantic_scores.csv"), show_default=True)
@click.option("--queue", default=2400, show_default=True, type=int)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--min-duration", default=300, show_default=True, type=int)
@click.option("--min-games", default=20, show_default=True, type=int)
@click.option(
    "--replace-sustain/--keep-semantic-sustain",
    default=True,
    show_default=True,
    help="Use empirical total_heal and total_units_healed for sustain_score.",
)
@click.option("--out-csv", type=click.Path(path_type=Path), default=Path("data/cache/champion_scores_empirical_merged.csv"), show_default=True)
@click.option("--out-json", type=click.Path(path_type=Path), default=Path("data/cache/champion_scores_empirical_merged.json"), show_default=True)
def main(
    db: Path,
    semantic_csv: Path,
    queue: int,
    patch_prefix: str,
    min_duration: int,
    min_games: int,
    replace_sustain: bool,
    out_csv: Path,
    out_json: Path,
) -> None:
    semantic_rows, _semantic_by_id = load_semantic_rows(semantic_csv)
    stats = collect_empirical_stats(
        db,
        queue=queue,
        patch_prefix=patch_prefix,
        min_duration=min_duration,
    )
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

    merged: list[dict[str, Any]] = []
    empirical_count = 0
    for row in semantic_rows:
        cid = int(row["champion_id"])
        out = dict(row)
        stat = stats.get(cid, {})
        games = int(stat.get("games", 0))
        out["empirical_games"] = games
        for metric in (
            "damage_share", "damage_per_min",
            "cc_share", "cc_per_min",
            "frontline_share", "frontline_per_min",
            "heal_share", "heal_per_min", "ally_heal_per_min", "ally_shield_per_min",
            "effective_heal_shield_per_min", "sustain_share", "sustain_per_min",
            "units_healed", "physical_damage_ratio", "magic_damage_ratio", "true_damage_ratio",
            "win_rate",
        ):
            out[f"empirical_{metric}"] = round(float(stat.get(metric, 0.0)), 6)

        if games >= min_games:
            empirical_count += 1
            if cid in damage_scores:
                out["damage_score"] = damage_scores[cid]
            if cid in cc_scores:
                out["cc_score"] = cc_scores[cid]
            if cid in frontline_scores:
                out["frontline_score"] = frontline_scores[cid]
            if replace_sustain and cid in sustain_scores:
                out["sustain_score"] = sustain_scores[cid]
            out["score_source"] = "empirical+semantic"
        else:
            out["score_source"] = "semantic_fallback"
        merged.append(out)

    write_csv(merged, out_csv)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    click.echo(
        f"[empirical] champions={len(merged)} empirical_overrides={empirical_count} "
        f"min_games={min_games} queue={queue} patch_prefix={patch_prefix}"
    )
    click.echo(f"[empirical] csv : {out_csv}")
    click.echo(f"[empirical] json: {out_json}")


if __name__ == "__main__":
    main()
