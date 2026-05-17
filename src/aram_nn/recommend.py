"""Real-time ARAM champion recommendation from LR + pair synergy stats.

Why LR and not DeepSets:
  At current data scale (~18k games, 2 patches) the LR baseline outperforms
  the DeepSets NN on classification (test acc 55.86% vs 52.72%, see
  models/tier2_mayhem/summary.json).  LR remains useful as a universal
  champion-strength prior, but same-team pair stats now dominate ranking.

Why opponent visibility doesn't matter for the LR component:
  ARAM champ select hides the opposing team's champions.  But the LR encoding
  is logit = Σ_{c∈blue} w_c − Σ_{c∈red} w_c + b, so swapping my own pick
  Y → X changes the logit by exactly (w_X − w_Y).  The unknown red-team
  contribution cancels out entirely.  Pair synergy is computed only from the
  visible ally anchors, so it also does not require opponent visibility.

Absolute probability assumes "average opponent":
  We set the red-team contribution to 0 in the feature vector.  Since LR was
  trained with +1/-1 encoding and L2 regularization, mean coefficient ≈ 0,
  so this is a reasonable point estimate (not a posterior).  The number is
  decorative; the blended score is the load-bearing output.
"""
from __future__ import annotations

import json
import math
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from aram_nn.pair_synergy import PairSynergyStats


@dataclass
class LRModel:
    """Logistic Regression weights + champion vocab.

    Stores plain numpy arrays so inference doesn't touch sklearn at runtime.
    This matters because pulling in sklearn -> scipy can crash during import
    on Python 3.13 (scipy.spatial.distance fails inside @dataclass
    construction with MemoryError) and even when it succeeds it adds 30+s
    of cold-start latency.

    coef_mean / coef_std are precomputed so the recommender can show each
    champion's strength as a z-score in the current meta — much more
    intuitive than P(win) for a 51-55% base-rate game where absolute
    probabilities all look similar.
    """
    coef: np.ndarray             # shape (n_champs,)
    intercept: float
    champ_to_idx: dict[int, int]
    n_champs: int
    coef_mean: float = 0.0
    coef_std: float = 1.0

    def z_score(self, champ_idx: int) -> float:
        """Standardized champion strength: (w - mean(w)) / std(w)."""
        if self.coef_std <= 1e-12:
            return 0.0
        return float((self.coef[champ_idx] - self.coef_mean) / self.coef_std)


def _sigmoid(x: float | np.ndarray) -> float | np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _vocab_sidecar_path(pt_path: Path) -> Path:
    """Return the JSON sidecar path for a given .pt vocab source.

    e.g. models/tier2_mayhem/tier2_checkpoint.pt
       -> models/tier2_mayhem/tier2_checkpoint.champ_to_idx.json
    """
    return pt_path.with_name(pt_path.stem + ".champ_to_idx.json")


def _load_vocab(vocab_source: Path) -> dict[int, int]:
    """Load champion-id -> index vocab.

    Path is tried in order:
      1. If the file is a .json, parse directly.
      2. If a .pt was passed but a JSON sidecar exists next to it, use the
         sidecar — avoids the slow `import torch` (30+s on Windows cold start
         with antivirus scanning, which is most of the recommender's boot
         time on this machine).
      3. Otherwise import torch, load the .pt, AND write a JSON sidecar
         next to it so the next startup hits the fast path.
    """
    vocab_source = Path(vocab_source)
    if vocab_source.suffix == ".json":
        raw = json.loads(vocab_source.read_text())
        return {int(k): int(v) for k, v in raw.items()}

    sidecar = _vocab_sidecar_path(vocab_source)
    if sidecar.exists():
        raw = json.loads(sidecar.read_text())
        return {int(k): int(v) for k, v in raw.items()}

    # Cold path — needs torch, writes sidecar for next time.
    import torch
    ckpt = torch.load(vocab_source, map_location="cpu", weights_only=False)
    vocab = {int(k): int(v) for k, v in ckpt["champ_to_idx"].items()}
    try:
        sidecar.write_text(json.dumps({str(k): v for k, v in vocab.items()}))
    except Exception:
        # Sidecar caching is best-effort; failures here shouldn't break loading.
        pass
    return vocab


# ---------- sklearn-free pickle loading ----------

class _LRStub:
    """Pickle stub for sklearn estimators.

    sklearn's pickle format calls __setstate__(dict) with the instance's
    attribute dictionary.  We only need 'coef_' and 'intercept_' off that
    dict, so the stub stores everything and the caller pulls what it needs.
    Crucially, no sklearn classes are imported during unpickling.
    """
    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)


class _NoSklearnUnpickler(pickle.Unpickler):
    """Unpickler that swaps sklearn class references for _LRStub.

    Numpy classes still resolve normally — they're needed to materialize
    the coef_/intercept_ arrays.
    """
    def find_class(self, module: str, name: str):
        if module.startswith("sklearn"):
            return _LRStub
        return super().find_class(module, name)


def _load_pickle_no_sklearn(pkl_path: Path) -> tuple[np.ndarray, float]:
    with open(pkl_path, "rb") as f:
        obj = _NoSklearnUnpickler(f).load()
    if not hasattr(obj, "coef_") or not hasattr(obj, "intercept_"):
        raise ValueError(
            f"Pickle at {pkl_path} has no coef_/intercept_ — not a fitted LR model?"
        )
    coef = np.asarray(obj.coef_, dtype=np.float64).reshape(-1)
    intercept = float(np.asarray(obj.intercept_).reshape(-1)[0])
    return coef, intercept


def load_lr(lr_path: Path, vocab_source: Path) -> LRModel:
    """Load LR coefficients + champ_to_idx vocab without importing sklearn.

    lr_path can be either:
      - lr_weights.json — bare {coef, intercept} JSON; fastest.
      - lr_model.pkl — sklearn LogisticRegression pickle; loaded via a
        custom Unpickler that stubs out sklearn classes so scipy/sklearn
        are never imported.  Still slightly slower than the JSON path
        because numpy unpacks the pickled array buffers.

    vocab_source can be a .pt checkpoint or a champ_to_idx.json file.
    """
    champ_to_idx = _load_vocab(vocab_source)

    lr_path = Path(lr_path)
    if lr_path.suffix == ".json":
        payload = json.loads(lr_path.read_text())
        coef = np.asarray(payload["coef"], dtype=np.float64)
        intercept = float(payload["intercept"])
    else:
        coef, intercept = _load_pickle_no_sklearn(lr_path)

    if coef.shape[0] != len(champ_to_idx):
        raise ValueError(
            f"LR coef length ({coef.shape[0]}) != vocab size ({len(champ_to_idx)}); "
            "model and vocab were trained on different splits."
        )

    return LRModel(
        coef=coef, intercept=intercept,
        champ_to_idx=champ_to_idx, n_champs=len(champ_to_idx),
        coef_mean=float(coef.mean()),
        coef_std=float(coef.std()),
    )


def _build_feature_vector(
    my_team_ids: Iterable[int],
    model: LRModel,
) -> tuple[np.ndarray, list[int]]:
    """Build +1/-1/0 feature vector with red team = 0 (unknown opponent).

    Returns (X, unknown_ids) where unknown_ids lists championIds not in vocab.
    """
    X = np.zeros(model.n_champs, dtype=np.float64)
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
    logit = float(X @ model.coef + model.intercept)
    return float(_sigmoid(logit))


@dataclass
class Suggestion:
    champion_id: int
    source: str            # "keep" or "bench"
    win_prob: float        # absolute P(blue wins) under "average opponent"
    delta: float           # legacy display alias for score
    prob_delta_lr: float   # LR win_prob - baseline
    synergy_delta: float   # candidate anchor synergy minus current anchor synergy
    synergy_se: float      # combined SE for the synergy estimate
    anchors_covered: int   # number of teammate anchors with usable pair stats
    score: float           # 70% synergy + 30% LR prob delta
    z_score: float         # standardized champion strength in the current meta:
                           #   (coef[champ] - mean(coef)) / std(coef)
                           # ~ +1 means roughly top 16%, ~ +2 means top 2.5%.
    is_known: bool         # False if championId is outside training vocab


def _combine_synergy(
    anchors: Iterable[int],
    candidate_id: int,
    pair_stats: PairSynergyStats | None,
) -> tuple[float, float, int]:
    """Return (synergy_delta, combined_se, anchors_covered)."""
    if pair_stats is None:
        return 0.0, float("inf"), 0

    weighted_sum = 0.0
    weight_sum = 0.0
    se_sq_sum = 0.0
    deltas: list[float] = []

    for anchor in anchors:
        row = pair_stats.get(anchor, candidate_id)
        if row is None:
            continue
        weight = 1.0 / (row.se * row.se + 0.0004)
        weighted_sum += row.delta * weight
        weight_sum += weight
        se_sq_sum += row.se * row.se
        deltas.append(row.delta)

    anchors_covered = len(deltas)
    if anchors_covered == 0:
        return 0.0, float("inf"), 0

    combined_se = math.sqrt(se_sq_sum) / anchors_covered
    if anchors_covered >= 2:
        synergy = weighted_sum / weight_sum if weight_sum > 0 else float(np.mean(deltas))
    else:
        synergy = 0.5 * deltas[0]

    if combined_se > 0.04:
        synergy *= 0.5

    return float(synergy), float(combined_se), anchors_covered


def suggest_for_cell(
    my_team_ids: list[int],
    my_current_id: int,
    bench_ids: list[int],
    model: LRModel,
    pair_stats: PairSynergyStats | None = None,
) -> list[Suggestion]:
    """Rank candidates for the local player's cell.

    Candidates = {my_current} ∪ bench.  For each, swap that champion into the
    local cell, compute ally-anchor synergy, blend it with the LR probability
    delta, and sort by descending score.

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
    anchors = [int(c) for c in my_team_ids if int(c) != int(my_current_id)]
    current_synergy, _, _ = _combine_synergy(anchors, int(my_current_id), pair_stats)

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
                win_prob=float("nan"), delta=float("nan"),
                prob_delta_lr=float("nan"),
                synergy_delta=float("nan"), synergy_se=float("nan"),
                anchors_covered=0, score=float("nan"),
                z_score=float("nan"), is_known=False,
            ))
            continue

        swapped = [c if c != my_current_id else cid for c in my_team_ids]
        prob = predict_blue_prob(swapped, model)
        prob_delta_lr = prob - baseline
        candidate_synergy, synergy_se, anchors_covered = _combine_synergy(
            anchors, int(cid), pair_stats
        )
        synergy_delta = (
            candidate_synergy - current_synergy
            if anchors_covered > 0 else 0.0
        )
        score = 0.7 * synergy_delta + 0.3 * prob_delta_lr
        out.append(Suggestion(
            champion_id=int(cid), source=source,
            win_prob=prob, delta=score,
            prob_delta_lr=prob_delta_lr,
            synergy_delta=synergy_delta,
            synergy_se=synergy_se,
            anchors_covered=anchors_covered,
            score=score,
            z_score=model.z_score(idx), is_known=True,
        ))

    out.sort(key=lambda s: (not s.is_known, -s.score if s.is_known else 0.0))
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
