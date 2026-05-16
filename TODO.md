# TODO — aram-winrate-nn

Status as of 2026-05-15, dataset = 27k Mayhem (16.9: 8,334 + 16.10: 18,373).

## Current best (apples-to-apples on 16.10 last-30% test, 2,757 matches)

| Setup | Model | Test Acc | Test Log Loss |
|---|---|---:|---:|
| Both teams visible | **LR combined** | **56.45%** | **0.6809** |
| Both teams visible | DeepSets-reg combined | 55.78% | 0.6854 |
| Solo (single team) | **LR-solo combined** | **54.57%** | **0.6868** |
| Solo (single team) | Residual NN on LR | 54.66% | 0.6869 |

Per-champ signal is essentially saturated by linear models. Pairwise / NN / patch-embed all fall within noise. Tier 1 hypothesis ("comp beats base rate") confirmed ~5-6 pp.

## Validation status

All 6 sanity tests PASS (see `scripts/sanity_tests.py`):
- Antisymmetry, permutation invariance, untrained-near-0.5, label-shuffle (no leak), planted-signal recovery, overfit-tiny.

## High-ROI next steps (do these first)

### 1. Scale data 27k → 50k+
- Continue `snowball-workers` on Mayhem.
- LR plateau is at ~56.5%; estimates of pair effects need ~2× sample to halve SE.
- **Stop criterion**: Mayhem 16.10 reaches 30k single-patch OR signal/noise ratio in anchor-synergy crosses 1.0 (currently 0.4–0.5).

### 2. Bayesian / partial-pooled pair model
- Instead of independent pair estimates, share strength via hierarchical prior.
- Architecture: `champ_strength[a]` + `champ_strength[b]` + `pair_factor[a,b] ~ Normal(0, τ)` with τ learned.
- Implement in `pymc` or `numpyro`. Cheap fit (<1min on this data).
- Goal: identify which pairs are *actually* synergistic above noise.
- File target: `scripts/bayes_pair_model.py`.

### 3. K-fold time-aware validation
- Single split → noisy comparisons (saw +0.22pp "improvements" that are likely noise).
- Use 5 expanding-window folds over time.
- Re-rank current model leaderboard with 5-fold CV mean ± std.
- File target: `scripts/kfold_eval.py`.

## Mid-ROI

### 4. Multi-seed ensemble
- DeepSets-reg: train 5 seeds, average probabilities.
- Likely +0.2 pp test acc, mainly variance reduction.

### 5. Anchor-synergy refresh for top-WR anchors only
- `scripts/anchor_synergy.py` works; useful as a stats tool, not a model.
- Restrict to anchors with ≥1,500 anchor-games (Yasuo, Lillia, Vayne, Jinx, Brand, etc.).
- Output a per-anchor markdown report.

### 6. Cross-patch + patch-embed when ≥4 patches accumulate
- Currently only 2 useful patches (16.9, 16.10) → patch-embed adds noise.
- When 16.11 + 16.12 reach 5k+ each, retry `train_tier2.py` and `scripts/compare_combined.py`.

## Productization

### 7. Pick advisor (done — `scripts/pick_advisor.py`, `scripts/synergy_lift.py`)
- **`pick_advisor.py --team "Alias1,Alias2,Alias3,Alias4"`**: ranks candidates by universal lr_w AND hybrid (lr_w + 0.5 × avg_synergy). Shows per-anchor breakdown with Wilson CIs.
- **`synergy_lift.py --team "..."`**: ranks by 95% lower bound of synergy lift = `(expected_wr_with_team - solo_wr_baseline) - 1.96 × SE`. Surfaces "fun picks" (medium universal strength + high comp-specific synergy) over OP-only ranking.
- Runtime: ~1s end-to-end on 30k data; ~500ms after parquet load. Fast enough for in-game champ-select integration.
- Output: significant lift candidates (z > 1.96) AND negative-synergy avoid list.
- Validated: 6 champs (Kindred, Ambessa, Aurora, Khazix, Ryze, Aphelios) pass z > 1.96 for a sample 4-comp; positive results match domain intuition (e.g. Xin Zhao + Volibear front-line synergy).

### 8. Tier-list cron / weekly refresh
- `scripts/tier_list.py` already works.
- Schedule: when DB grows by ≥1k games, regenerate `models/tier_list_solo_latest.csv` + diff vs last week.

### 9. In-game champ-select integration (next)
- LCU poller → on champ-select phase change → call `pick_advisor.py` for current 4 known team members → display top-3 picks with lift + confidence.
- Daemon mode: pre-load parquet once, serve queries via stdin/socket → <100ms latency per query.
- Output to overlay HUD or stdout for OBS capture.

## Verified DEAD ENDS (don't redo without new context)

- **Tier 1 DeepSets (default params)** — overfits; reg version is the only useful one.
- **Patch embedding at 2 patches** — adds noise > signal. Revisit at 4+ patches.
- **Pairwise LR (172 + 14,706 features) for full-corpus prediction acc** — best C collapses all pair weights to 0; no acc gain. Revisit at 50k+.
  - **BUT** anchor-conditional synergy (`scripts/synergy_lift.py`) DOES surface significant pair effects for specific team queries — the trick is to ask the *narrow* question (given THIS team, which pair is best) instead of the *broad* one (rank all pairs in corpus).

- **NN-based synergy ranking (`scripts/synergy_lift_nn.py`) for "which 5th pick has best chemistry"** — trains DeepSetsSolo on 30k mirror, queries P(win | team + X) − P(win | random + X). Result: NN smooths champion-specific quirks into role-based recommendations (tanks, sustain, ADC) and **disagrees with conditional-WR stats on direction for unusual pairs** (e.g. Diana: stats says −9pp drag, NN says +9.7pp boost).
  - **Verdict**: NN is 350× slower (~5 min train + 10s score vs 0.85s pure stats) AND **less accurate for specific chemistry** — its smoothing kills the empirical signals that the user wants. Stats wins for "fun pick" use case at this data scale.
  - NN's role-based output (Maokai / Alistar / Soraka for our test comp) is intuitive but generic — not the data-driven quirks (Kindred / Ambessa / Aurora) that conditional WR surfaces.
  - Only **Aphelios** appears in both methods' top 6 — that's the cross-validated "safe synergy" pick.
- **Token-masking SSL** — pretext task ("predict masked champion") learns co-occurrence ≠ win signal. Target misaligned.
- **Apex / ladder / riot-tier seed families** — verified dead (per CLAUDE.md, 0 transitive captures).

## Verified IRRELEVANT for our use case

- **Augment features** — augments unknown at pick time, so excluded.
- **Riot API queueId=2400** — Riot blocked, LCU-only.

## Datasets / artifacts

- `data/raw/mayhem_27k.parquet` — current frozen export.
- `data/lcu/games.db` — live DB (~27k games, growing).
- `models/tier_list_solo_27k.csv` — current 172-champion tier list.
- `models/tier2_mayhem_27k/`, `models/tier2_mayhem_27k_reg/` — Tier 2 checkpoints (NOT recommended for prod; LR-combined wins).
- `scripts/sanity_tests.py` — regression test for pipeline correctness; run before any architecture change.

## Theoretical ceiling estimate

| Quantity | Value | Source |
|---|---:|---|
| Base rate (blue WR) | 51.6% | data |
| Solo (per-champ only) achieved | 54.57% test acc | LR-solo combined |
| Both-teams (per-champ relative) achieved | 56.45% test acc | LR combined |
| Per-champ contribution | +4.6 pp | solo − base |
| Matchup (relative) contribution | +1.9 pp | combined − solo |
| Estimated remaining (synergy + counter + skill) | ~3–9 pp | back-of-envelope |
| Skill-cap ceiling (random matchmaking) | ~60–65% | literature for similar tasks |

**Implication**: even a perfect synergy/matchup model probably caps at ~60% test acc on this data without additional features (player skill, account history, etc., which we can't access).
