"""Anchor-conditional same-team champion synergy stats.

For an already-picked anchor champion A and a candidate X:

    delta(A, X) = WR(team contains A and X) - WR(team contains A and not X)

Rows are ordered by anchor, so (A -> X) and (X -> A) are different
conditionals even though they come from the same co-games.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class PairSynergyRow:
    anchor_id: int
    candidate_id: int
    games: int
    wins: int
    rest_games: int
    rest_wins: int
    pair_wr: float
    rest_wr: float
    raw_delta: float
    delta: float
    se: float


@dataclass
class PairSynergyStats:
    rows: dict[tuple[int, int], PairSynergyRow]
    min_pair: int
    shrink_k: float
    queue_id: int | None = None
    patch_prefix: str | None = None
    total_matches: int | None = None

    def get(self, anchor_id: int, candidate_id: int) -> PairSynergyRow | None:
        return self.rows.get((int(anchor_id), int(candidate_id)))


def _iter_team_rows(
    db_path: Path,
    *,
    queue_id: int,
    patch_prefix: str | None,
) -> Iterable[tuple[list[int], int]]:
    con = sqlite3.connect(str(db_path))
    try:
        if patch_prefix:
            rows = con.execute(
                "SELECT blue_champs, red_champs, blue_wins FROM games "
                "WHERE queue_id=? AND patch LIKE ?",
                (queue_id, f"{patch_prefix}%"),
            )
        else:
            rows = con.execute(
                "SELECT blue_champs, red_champs, blue_wins FROM games WHERE queue_id=?",
                (queue_id,),
            )

        for blue, red, blue_wins in rows:
            bw = int(blue_wins)
            yield [int(c) for c in json.loads(blue)], bw
            yield [int(c) for c in json.loads(red)], 1 - bw
    finally:
        con.close()


def build_pair_synergy(
    db_path: Path,
    *,
    queue_id: int = 2400,
    patch_prefix: str | None = "16.10",
    min_pair: int = 30,
    shrink_k: float = 40.0,
) -> PairSynergyStats:
    """Build ordered anchor -> candidate synergy rows from the LCU DB."""
    anchor_games: Counter[int] = Counter()
    anchor_wins: Counter[int] = Counter()
    pair_games: Counter[tuple[int, int]] = Counter()
    pair_wins: Counter[tuple[int, int]] = Counter()
    total_team_rows = 0

    for team, won in _iter_team_rows(db_path, queue_id=queue_id, patch_prefix=patch_prefix):
        total_team_rows += 1
        team_ids = sorted({int(c) for c in team if int(c) > 0})
        for anchor in team_ids:
            anchor_games[anchor] += 1
            anchor_wins[anchor] += won
            for candidate in team_ids:
                if candidate == anchor:
                    continue
                key = (anchor, candidate)
                pair_games[key] += 1
                pair_wins[key] += won

    out: dict[tuple[int, int], PairSynergyRow] = {}
    for (anchor, candidate), n_pair in pair_games.items():
        if n_pair < min_pair:
            continue

        w_pair = pair_wins[(anchor, candidate)]
        n_rest = anchor_games[anchor] - n_pair
        if n_rest <= 0:
            continue

        w_rest = anchor_wins[anchor] - w_pair
        pair_wr = w_pair / n_pair
        rest_wr = w_rest / n_rest
        raw_delta = pair_wr - rest_wr
        shrunk_delta = raw_delta * (n_pair / (n_pair + shrink_k))
        var_pair = pair_wr * (1.0 - pair_wr) / max(n_pair, 1)
        var_rest = rest_wr * (1.0 - rest_wr) / max(n_rest, 1)
        se = math.sqrt(var_pair + var_rest)

        out[(anchor, candidate)] = PairSynergyRow(
            anchor_id=anchor,
            candidate_id=candidate,
            games=n_pair,
            wins=w_pair,
            rest_games=n_rest,
            rest_wins=w_rest,
            pair_wr=pair_wr,
            rest_wr=rest_wr,
            raw_delta=raw_delta,
            delta=shrunk_delta,
            se=se,
        )

    return PairSynergyStats(
        rows=out,
        min_pair=min_pair,
        shrink_k=shrink_k,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
        total_matches=total_team_rows // 2,
    )


def save_pair_synergy(stats: PairSynergyStats, path: Path) -> None:
    payload = {
        "version": 1,
        "queue_id": stats.queue_id,
        "patch_prefix": stats.patch_prefix,
        "min_pair": stats.min_pair,
        "shrink_k": stats.shrink_k,
        "total_matches": stats.total_matches,
        "pairs": [
            {
                "anchor_id": row.anchor_id,
                "candidate_id": row.candidate_id,
                "games": row.games,
                "wins": row.wins,
                "rest_games": row.rest_games,
                "rest_wins": row.rest_wins,
                "pair_wr": row.pair_wr,
                "rest_wr": row.rest_wr,
                "raw_delta": row.raw_delta,
                "delta": row.delta,
                "se": row.se,
            }
            for row in sorted(stats.rows.values(), key=lambda r: (r.anchor_id, r.candidate_id))
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_pair_synergy(path: Path) -> PairSynergyStats:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows: dict[tuple[int, int], PairSynergyRow] = {}
    for item in payload.get("pairs", []):
        row = PairSynergyRow(
            anchor_id=int(item["anchor_id"]),
            candidate_id=int(item["candidate_id"]),
            games=int(item["games"]),
            wins=int(item["wins"]),
            rest_games=int(item["rest_games"]),
            rest_wins=int(item["rest_wins"]),
            pair_wr=float(item["pair_wr"]),
            rest_wr=float(item["rest_wr"]),
            raw_delta=float(item.get("raw_delta", item["delta"])),
            delta=float(item["delta"]),
            se=float(item["se"]),
        )
        rows[(row.anchor_id, row.candidate_id)] = row

    return PairSynergyStats(
        rows=rows,
        min_pair=int(payload.get("min_pair", 0)),
        shrink_k=float(payload.get("shrink_k", 40.0)),
        queue_id=payload.get("queue_id"),
        patch_prefix=payload.get("patch_prefix"),
        total_matches=payload.get("total_matches"),
    )
