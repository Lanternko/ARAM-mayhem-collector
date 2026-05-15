"""Real-time ARAM champion recommendation from a trained LR model.

Why LR and not DeepSets:
  At current data scale (~18k games, 2 patches) the LR baseline outperforms
  the DeepSets NN on classification (test acc 55.86% vs 52.72%, see
  models/tier2_mayhem/summary.json).  LR is also analytically convenient
  here — see "opponent-invariant ranking" below.

Why opponent visibility doesn't matter for ranking:
  ARAM champ select hides the opposing team's champions.  But the LR encoding
  is logit = Σ_{c∈blue} w_c − Σ_{c∈red} w_c + b, so swapping my own pick
  Y → X changes the logit by exactly (w_X − w_Y).  The unknown red-team
  contribution cancels out entirely.  The ranking of candidates is therefore
  EXACT even with the opponent hidden — only the displayed absolute prob
  needs an opponent prior.

Absolute probability assumes "average opponent":
  We set the red-team contribution to 0 in the feature vector.  Since LR was
  trained with +1/-1 encoding and L2 regularization, mean coefficient ≈ 0,
  so this is a reasonable point estimate (not a posterior).  The number is
  decorative — the deltas are the load-bearing output.
"""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


@dataclass
class LRModel:
    clf: object  # sklearn.linear_model.LogisticRegression
    champ_to_idx: dict[int, int]
    n_champs: int


def load_lr(lr_pickle: Path, vocab_source: Path) -> LRModel:
    """Load LR pickle + champ_to_idx vocab.

    vocab_source can be either:
      - a .pt checkpoint file (torch.load → dict with 'champ_to_idx' key), or
      - a .json file mapping str(championId) → int index.
    """
    clf = pickle.loads(Path(lr_pickle).read_bytes())

    if str(vocab_source).endswith(".json"):
        raw = json.loads(Path(vocab_source).read_text())
        champ_to_idx = {int(k): int(v) for k, v in raw.items()}
    else:
        # Defer torch import — only needed when reading a .pt checkpoint.
        import torch
        ckpt = torch.load(vocab_source, map_location="cpu", weights_only=False)
        champ_to_idx = {int(k): int(v) for k, v in ckpt["champ_to_idx"].items()}

    return LRModel(clf=clf, champ_to_idx=champ_to_idx, n_champs=len(champ_to_idx))


def _build_feature_vector(
    my_team_ids: Iterable[int],
    model: LRModel,
) -> tuple[np.ndarray, list[int]]:
    """Build +1/-1/0 feature vector with red team = 0 (unknown opponent).

    Returns (X, unknown_ids) where unknown_ids lists championIds not in vocab.
    """
    X = np.zeros(model.n_champs, dtype=np.float32)
    unknown: list[int] = []
    for cid in my_team_ids:
        idx = model.champ_to_idx.get(int(cid))
        if idx is None:
            unknown.append(int(cid))
            continue
        X[idx] = 1.0
    return X, unknown


def predict_blue_prob(
    my_team_ids: Iterable[int],
    model: LRModel,
) -> float:
    """Predicted P(blue wins) given the 5 blue champions, opponent unknown.

    Red contribution is set to 0 — see module docstring on 'average opponent'.
    """
    X, _ = _build_feature_vector(my_team_ids, model)
    return float(model.clf.predict_proba(X.reshape(1, -1))[0, 1])


@dataclass
class Suggestion:
    champion_id: int
    source: str            # "keep" or "bench"
    win_prob: float        # absolute P(blue wins) under "average opponent"
    delta: float           # win_prob - baseline (positive = better than keeping current)
    is_known: bool         # False if championId is outside training vocab


def suggest_for_cell(
    my_team_ids: list[int],
    my_current_id: int,
    bench_ids: list[int],
    model: LRModel,
) -> list[Suggestion]:
    """Rank candidates for the local player's cell.

    Candidates = {my_current} ∪ bench.  For each, swap that champion into the
    local cell, recompute P(blue wins), and sort by descending delta.

    Args:
      my_team_ids : list of 5 championIds currently locked into the blue team
                    (must include my_current_id).
      my_current_id : the championId currently in the local player's cell.
      bench_ids   : championIds sitting on the reroll bench.
    """
    if my_current_id not in my_team_ids:
        raise ValueError(
            f"my_current_id={my_current_id} not found in my_team_ids={my_team_ids}; "
            "session parsing bug."
        )

    baseline = predict_blue_prob(my_team_ids, model)

    seen: set[int] = set()
    out: list[Suggestion] = []
    for source, cid in [("keep", my_current_id)] + [("bench", c) for c in bench_ids]:
        if cid in seen:
            continue
        seen.add(cid)

        idx = model.champ_to_idx.get(int(cid))
        if idx is None:
            out.append(Suggestion(
                champion_id=int(cid), source=source,
                win_prob=float("nan"), delta=float("nan"), is_known=False,
            ))
            continue

        swapped = [c if c != my_current_id else cid for c in my_team_ids]
        prob = predict_blue_prob(swapped, model)
        out.append(Suggestion(
            champion_id=int(cid), source=source,
            win_prob=prob, delta=prob - baseline, is_known=True,
        ))

    out.sort(key=lambda s: (not s.is_known, -s.delta if s.is_known else 0.0))
    return out


# ---------- Session parsing ----------

@dataclass
class ParsedSession:
    my_team_ids: list[int]   # 5 championIds for blue team
    my_current_id: int       # local player's current champion
    my_cell_id: int          # localPlayerCellId
    bench_ids: list[int]     # championIds on reroll bench
    bench_enabled: bool


def parse_session(session: dict) -> ParsedSession | None:
    """Extract the recommender's inputs from a /lol-champ-select/v1/session payload.

    Returns None if the session is incomplete (not all 5 cells have a champion
    locked in yet — recommendations are noise until everyone has a starting champ).
    """
    my_cell = session.get("localPlayerCellId")
    my_team = session.get("myTeam") or []
    bench = session.get("benchChampions") or []

    if my_cell is None or not my_team:
        return None

    my_team_ids: list[int] = []
    my_current_id: int | None = None
    for cell in my_team:
        cid = int(cell.get("championId") or 0)
        if cid == 0:
            return None  # someone hasn't been assigned a champion yet
        my_team_ids.append(cid)
        if cell.get("cellId") == my_cell:
            my_current_id = cid

    if my_current_id is None:
        return None

    bench_ids = [int(b.get("championId") or 0) for b in bench]
    bench_ids = [c for c in bench_ids if c > 0]

    return ParsedSession(
        my_team_ids=my_team_ids,
        my_current_id=my_current_id,
        my_cell_id=int(my_cell),
        bench_ids=bench_ids,
        bench_enabled=bool(session.get("benchEnabled", False)),
    )


def session_state_hash(parsed: ParsedSession) -> tuple:
    """Stable hash so the CLI can detect 'state changed, redraw' vs idle ticks."""
    return (
        tuple(sorted(parsed.my_team_ids)),
        parsed.my_current_id,
        parsed.my_cell_id,
        tuple(sorted(parsed.bench_ids)),
    )
