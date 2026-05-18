"""Pick advisor for a specific 4-champ team. For each candidate 5th pick X:
  - Compute X's solo strength (LR-solo lr_w)
  - Compute X's conditional WR with each of the 4 known teammates
  - Score = sum of conditional deltas (vs anchor's average) across the 4

Output: ranked candidate list with both universal and synergy-aware scores.
"""
from __future__ import annotations

import math
from pathlib import Path

import click
import numpy as np
import polars as pl


def wilson_ci(k: int, n: int, z: float = 1.96):
    if n == 0: return 0.5, 0.5
    p = k / n
    denom = 1 + z*z/n
    centre = (p + z*z/(2*n)) / denom
    spread = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denom
    return centre - spread, centre + spread


def _load_name_map():
    try:
        from aram_nn.lcu.client import LCUClient, get_champion_summary
        from aram_nn.lcu.process import get_credentials
        creds = get_credentials()
        if creds is None: return {}
        with LCUClient(creds) as lcu:
            summary = get_champion_summary(lcu)
        m = {}
        for row in summary:
            cid = row.get("id"); name = row.get("alias") or row.get("name")
            if cid is None or not name: continue
            m[int(cid)] = str(name)
        return m
    except Exception:
        return {}


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--patches", multiple=True, default=("16.9", "16.10"))
@click.option("--team", required=True, help="comma-separated champion aliases on the team")
@click.option("--tier-csv", default=Path("models/tier_list_solo_30k.csv"), type=click.Path(path_type=Path))
@click.option("--top", default=20, type=int)
@click.option("--min-pair", default=30, type=int, help="min co-games for conditional WR")
def main(data, patches, team, tier_csv, top, min_pair):
    name_map = _load_name_map()
    alias_to_id = {v: k for k, v in name_map.items()}

    team_aliases = [s.strip() for s in team.split(",")]
    team_ids = []
    for a in team_aliases:
        if a not in alias_to_id:
            click.echo(f"[err] unknown alias: {a}"); return
        team_ids.append(alias_to_id[a])
    team_set = set(team_ids)
    click.echo(f"[team] {team_aliases} -> ids {team_ids}")

    # Load data
    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300)
    df = df.with_columns(pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("p"))
    df = df.filter(pl.col("p").is_in(list(patches)))
    click.echo(f"[data] n_matches={df.height}  patches={list(patches)}")

    # Mirror to solo
    rows = []  # (frozenset, win)
    for b, r, w in zip(df["blue_champions"].to_list(), df["red_champions"].to_list(), df["blue_wins"].to_list()):
        rows.append((frozenset(b), int(w)))
        rows.append((frozenset(r), int(1 - w)))

    # Build conditional WR per (anchor, partner): only games where BOTH are on team
    # We need for each candidate X != team, and each team_id A:
    #   n_pair(A, X), wins_pair(A, X), n_solo_A (anchor games), n_anchor_no_partner = n_solo_A - n_pair(A,X)
    # baseline_A = WR of A solo (over all team-with-A rows)
    # delta(A, X) = WR(A AND X games) - WR(A games NOT containing X)
    anchor_total = {a: 0 for a in team_ids}
    anchor_wins  = {a: 0 for a in team_ids}
    co = {a: {} for a in team_ids}  # co[A][X] = [n, wins]
    for t, w in rows:
        for a in team_ids:
            if a in t:
                anchor_total[a] += 1
                anchor_wins[a]  += w
                for c in t:
                    if c == a or c in team_set: continue
                    bucket = co[a].setdefault(c, [0, 0])
                    bucket[0] += 1; bucket[1] += w

    for a in team_ids:
        n = anchor_total[a]; w = anchor_wins[a]
        click.echo(f"  anchor {name_map.get(a, a)}: {w}/{n} = {w/n*100:.2f}%")

    # Load LR-solo tier (for universal score)
    tier = pl.read_csv(tier_csv)
    lr_w_map = {int(r["champion_id"]): float(r["lr_weight"]) for r in tier.iter_rows(named=True)}
    games_map = {int(r["champion_id"]): int(r["games"]) for r in tier.iter_rows(named=True)}

    # Score each candidate
    all_candidates = set()
    for a in team_ids:
        all_candidates.update(co[a].keys())
    all_candidates -= team_set

    cand_rows = []
    for cid in all_candidates:
        if cid not in lr_w_map: continue
        lr_w = lr_w_map[cid]
        total_games = games_map[cid]
        if total_games < 200: continue  # need decent sample

        # Conditional delta for each anchor
        deltas = []
        pairs_info = []
        for a in team_ids:
            if cid not in co[a]: continue
            n_pair, w_pair = co[a][cid]
            if n_pair < min_pair: continue
            wr_pair = w_pair / n_pair
            # rest = anchor games without this candidate
            n_rest = anchor_total[a] - n_pair
            w_rest = anchor_wins[a]  - w_pair
            wr_rest = w_rest / max(n_rest, 1)
            delta = wr_pair - wr_rest
            # SE for the delta
            var_pair = wr_pair*(1-wr_pair)/max(n_pair, 1)
            var_rest = wr_rest*(1-wr_rest)/max(n_rest, 1)
            se = math.sqrt(var_pair + var_rest)
            deltas.append(delta)
            pairs_info.append((name_map.get(a, a), n_pair, wr_pair, delta, se))

        anchors_covered = len(deltas)
        if anchors_covered >= 2:
            weighted_sum = 0.0
            weight_sum = 0.0
            for _, _, _, delta, se in pairs_info:
                weight = 1.0 / (se * se + 0.0004)
                weighted_sum += delta * weight
                weight_sum += weight
            avg_delta = weighted_sum / weight_sum if weight_sum > 0 else float(np.mean(deltas))
        elif anchors_covered == 1:
            avg_delta = 0.5 * deltas[0]
        else:
            avg_delta = 0.0
        # Combined SE (pessimistic: sqrt(sum_se^2)/n)
        combined_se = (
            math.sqrt(sum(se*se for _,_,_,_,se in pairs_info)) / anchors_covered
            if anchors_covered else float("inf")
        )

        cand_rows.append({
            "id": cid,
            "name": name_map.get(cid, f"id_{cid}"),
            "lr_w": lr_w,
            "n_total": total_games,
            "n_anchors_covered": anchors_covered,
            "avg_delta_pp": avg_delta * 100,
            "combined_se_pp": combined_se * 100,
            "pairs": pairs_info,
        })

    # Two rankings
    by_universal = sorted(cand_rows, key=lambda r: -r["lr_w"])[:top]
    by_synergy   = sorted(cand_rows, key=lambda r: -(0.7 * r["avg_delta_pp"]/100 + 0.3 * r["lr_w"]))[:top]
    # Hybrid score: 70% synergy + 30% LR universal strength.

    click.echo(f"\n========== A. By UNIVERSAL strength (LR-solo lr_w) ==========")
    click.echo(f"  {'name':<14} {'lr_w':>8} {'n_total':>7} {'avg_synergy':>12} {'±SE':>6}")
    for r in by_universal:
        click.echo(f"  {r['name']:<14} {r['lr_w']:>+8.4f} {r['n_total']:>7} {r['avg_delta_pp']:>+10.2f}pp {r['combined_se_pp']:>5.1f}pp")

    click.echo(f"\n========== B. By HYBRID (0.7 * synergy + 0.3 * lr_w) ==========")
    click.echo(f"  {'name':<14} {'hybrid_score':>10} {'lr_w':>8} {'avg_synergy':>12} {'±SE':>6}")
    for r in by_synergy:
        score = 0.7 * r['avg_delta_pp']/100 + 0.3 * r['lr_w']
        click.echo(f"  {r['name']:<14} {score:>+10.4f} {r['lr_w']:>+8.4f} {r['avg_delta_pp']:>+10.2f}pp {r['combined_se_pp']:>5.1f}pp")

    # Top-5 detailed breakdown
    click.echo(f"\n========== C. Top 5 by synergy: per-anchor breakdown ==========")
    top5_by_synergy = sorted(cand_rows, key=lambda r: -r["avg_delta_pp"])[:5]
    for r in top5_by_synergy:
        click.echo(f"\n  {r['name']} (lr_w={r['lr_w']:+.4f}, total_games={r['n_total']})")
        for (anchor_name, n_pair, wr_pair, delta, se) in r['pairs']:
            ci_lo, ci_hi = delta - 1.96*se, delta + 1.96*se
            sig = "*" if (ci_lo > 0 or ci_hi < 0) else " "
            click.echo(f"    with {anchor_name:<12}: n={n_pair:>3}  wr={wr_pair*100:>5.1f}%  delta={delta*100:+6.2f}pp  CI95=[{ci_lo*100:+5.1f}, {ci_hi*100:+5.1f}] {sig}")

    click.echo(f"\n========== D. Top 5 worst by synergy ==========")
    bot5_by_synergy = sorted(cand_rows, key=lambda r: r["avg_delta_pp"])[:5]
    for r in bot5_by_synergy:
        click.echo(f"  {r['name']:<14} lr_w={r['lr_w']:+.4f}  avg_delta={r['avg_delta_pp']:+.2f}pp ±{r['combined_se_pp']:.1f}pp  ({r['n_anchors_covered']}/4 anchors)")


if __name__ == "__main__":
    main()
