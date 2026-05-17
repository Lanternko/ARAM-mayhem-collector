# Modeling Notes: ARAM / Mayhem Team Composition Learning

Updated: 2026-05-18

This is the canonical note for the current modeling direction.  Older planning
notes in `PLAN.md` and `TODO.md` still contain useful historical context, but
this file reflects the latest Mayhem/LCU feature work and empirical experiments.

## Core Problem

We are not trying to predict single champion strength only.  The target is team
composition understanding:

- champion identity and baseline strength
- conditional synergy
- frontline / backline balance
- damage profile balance
- wave clear, poke, engage, disengage, CC, sustain
- cases where a pair is good only under a third condition

Example intuition:

```text
Caitlyn + Tristana + real frontline = playable / strong
Caitlyn + Tristana + no frontline   = fragile / bad
```

This is why purely linear logistic regression is an important baseline, but not
the final model family we want to stop at.

## Current High-Level Conclusion

Logistic regression remains very strong for this dataset because champion
identity has a large linear signal.  Neural networks start to improve only when
we give them better structured features:

- raw champion identity alone is not enough
- hand-written ability semantics help a little
- empirical LCU combat averages help more
- role and AD/AP/true damage profile help a bit more

The newest score-based NN improves accuracy over LR slightly, but LR still has
better log-loss.  In plain terms: the NN is starting to rank winners better, but
its probabilities are not as cleanly calibrated yet.

## Data And Leakage Rules

Use time split only.  Never use random split.

For champion average capability stats such as damage, CC, frontline, and
sustain, compute the averages from the training window only when evaluating a
predictive model.  This is not because these averages need huge samples like
win rate.  It is because full-DB averages would let future patch/meta behavior
leak into validation/test.

Current default for empirical capability stats:

- `min_games = 20`
- use only games where all 10 participants have combat stats
- use train-split-only stats inside training scripts
- full-DB merged CSV is only for inspection / sanity checking

Generated data and model artifacts are ignored by git:

- `data/cache/champion_*.csv/json`
- `data/raw/*.parquet`
- `models/*`

## Feature Families

### 1. Champion Identity

Still mandatory.  This captures champion baseline strength and many hidden
factors not yet represented by explicit features.

Representation used by the LR baseline:

```text
blue champion = +1
red champion  = -1
absent        = 0
```

### 2. Ability / Semantic Features

Built from public Data Dragon ability text plus manual review corrections.
These features are subjective, but useful as priors.

Current score columns:

- `wave_clear_score`
- `cc_score`
- `engage_score`
- `damage_score`
- `poke_score`
- `sustain_score`
- `frontline_score`

Important interpretation:

- `wave_clear_score`: ability to clear minion waves, mostly AOE damage / cooldown
- `cc_score`: amount and reliability of crowd control
- `engage_score`: ability to start fights, especially long-range hard CC or forced displacement
- `damage_score`: practical damage pressure, later overwritten by empirical stats when available
- `poke_score`: long-range damage pressure before full engage
- `sustain_score`: healing/shielding value, now preferably empirical
- `frontline_score`: practical tanking/space-making, now preferably empirical

Manual ability scores should not be treated as ground truth.  They are priors.
The model may use them, but empirical stats should replace combat columns when
available.

### 3. Empirical LCU Combat Averages

These come from `participants_json.stats` in `data/lcu/games.db`.

Main empirical replacements:

- `damage_score`: based on damage-to-champions share and per-minute output
- `cc_score`: based on `time_ccing_others` share and per-minute CC
- `frontline_score`: based on `total_damage_taken + 0.5 * damage_self_mitigated`
- `sustain_score`: based on healing stats, with teammate-facing healing valued more

Damage profile features:

- `physical_damage_ratio`
- `magic_damage_ratio`
- `true_damage_ratio`

These let the model recognize AD/AP/true-damage balance instead of forcing it
to infer damage type only from champion identity.

### 4. Riot Role Tags

Data Dragon role tags are fuzzy, but still useful as soft priors.

Current one-hot role features:

- `role_assassin`
- `role_fighter`
- `role_mage`
- `role_marksman`
- `role_support`
- `role_tank`

Do not rely on these as hard labels.  Champions like Trundle, Maokai, Sion, or
Kog'Maw can play very differently depending on items and augments.

### 5. Sustain Semantics

Ideal priority order for sustain value:

```text
ally heal > ally shield > self heal > self shield
```

Current DB reality:

- existing stored data has `total_heal`
- existing stored data has `total_units_healed`
- existing stored data does not currently contain teammate heal/shield fields
- parser now attempts to capture teammate heal/shield fields if LCU returns them

Current fallback:

- use `total_heal`
- use `total_units_healed` as a weak proxy for teammate-facing healing
- do not treat `damage_self_mitigated` as shielding, because that belongs more
  to tankiness / frontline

Important distinction:

- `total_heal` is an amount of HP restored
- `total_units_healed` is a count-like field of healed units, not a heal amount

## LCU Field Map

Current fields captured from LCU participant stats include:

| Meaning | Stored key | Raw LCU key |
|---|---|---|
| Kills | `kills` | `kills` |
| Deaths | `deaths` | `deaths` |
| Assists | `assists` | `assists` |
| Largest killing spree | `largest_killing_spree` | `largestKillingSpree` |
| Largest multi kill | `largest_multi_kill` | `largestMultiKill` |
| First blood kill | `first_blood_kill` | `firstBloodKill` |
| First blood assist | `first_blood_assist` | `firstBloodAssist` |
| Total damage to champions | `total_damage_dealt_to_champions` | `totalDamageDealtToChampions` |
| Physical damage to champions | `physical_damage_dealt_to_champions` | `physicalDamageDealtToChampions` |
| Magic damage to champions | `magic_damage_dealt_to_champions` | `magicDamageDealtToChampions` |
| True damage to champions | `true_damage_dealt_to_champions` | `trueDamageDealtToChampions` |
| Total damage dealt, all targets | `total_damage_dealt` | `totalDamageDealt` |
| Physical damage dealt, all targets | `physical_damage_dealt` | `physicalDamageDealt` |
| Magic damage dealt, all targets | `magic_damage_dealt` | `magicDamageDealt` |
| True damage dealt, all targets | `true_damage_dealt` | `trueDamageDealt` |
| Largest critical strike | `largest_critical_strike` | `largestCriticalStrike` |
| Damage to turrets | `damage_dealt_to_turrets` | `damageDealtToTurrets` |
| Damage to objectives | `damage_dealt_to_objectives` | `damageDealtToObjectives` |
| Total damage taken | `total_damage_taken` | `totalDamageTaken` |
| Physical damage taken | `physical_damage_taken` | `physicalDamageTaken` |
| Magic damage taken | `magic_damage_taken` | `magicDamageTaken` / `magicalDamageTaken` |
| True damage taken | `true_damage_taken` | `trueDamageTaken` |
| Self mitigated damage | `damage_self_mitigated` | `damageSelfMitigated` |
| Time CCing others | `time_ccing_others` | `timeCCingOthers` |
| Total CC dealt | `total_time_cc_dealt` | `totalTimeCCDealt` / `totalTimeCrowdControlDealt` |
| Heal amount | `total_heal` | `totalHeal` |
| Units healed | `total_units_healed` | `totalUnitsHealed` |
| Gold earned | `gold_earned` | `goldEarned` |
| Gold spent | `gold_spent` | `goldSpent` |
| Minions killed | `total_minions_killed` | `totalMinionsKilled` |
| Neutral minions killed | `neutral_minions_killed` | `neutralMinionsKilled` |
| Turret kills | `turret_kills` | `turretKills` |
| Inhibitor kills | `inhibitor_kills` | `inhibitorKills` / `inhibKills` |

The parser also tries to capture these if present:

- `total_heals_on_teammates`
- `total_damage_shielded_on_teammates`
- `effective_heal_and_shielding`
- `crowd_control_score`

Existing DB rows do not currently contain teammate heal/shield fields.  New
collector runs or `scripts/lcu_backfill.py` over the last visible games may
capture them if the client endpoint returns them.

## Current Experiments

Dataset used for the latest score NN run:

- `data/raw/mayhem_lcu_latest.parquet`
- queue: Mayhem `2400`
- patch prefix: `16.10`
- train rows: `35,646`
- validation rows: `7,638`
- test rows: `7,638`

Latest test results:

| Model | Test Acc | Test Log Loss | Notes |
|---|---:|---:|---|
| Constant base rate | 52.57% | 0.6920 | baseline |
| LR champion identity | 57.08% | 0.6755 | still best log-loss |
| DeepSets embedding-only | 55.01% | 0.6844 | overfits / underuses signal |
| DeepSets + 7 scores | 57.12% | 0.6783 | scores help |
| DeepSets + 17 score/role/profile features | 57.33% | 0.6771 | best accuracy so far |

The 17 static features are:

- 7 capability scores
- 6 Riot role one-hot features
- 3 empirical damage ratios
- 1 healing target proxy (`units_healed`)

Interpretation:

- Explicit capability features help the NN.
- Role and damage-profile features add a small improvement.
- Accuracy can slightly beat LR.
- Log-loss still trails LR, so calibration and overfit remain issues.

Tree experiments:

- semantic-only features are too weak alone
- LightGBM/XGBoost with champion identity plus semantic/empirical scores are
  close to LR but have not clearly beaten it
- tree models are still worth revisiting with better cross-validation and more
  mature empirical fields

## Current Scripts

Feature generation:

```powershell
python scripts/fetch_champion_abilities.py
python scripts/build_semantic_ability_scores.py
python scripts/build_empirical_champion_scores.py --db data/lcu/games.db --queue 2400 --patch-prefix 16.10
```

Training:

```powershell
python scripts/train_score_nn.py `
  --data data/raw/mayhem_lcu_latest.parquet `
  --score-csv data/cache/champion_semantic_scores.csv `
  --patch-prefix 16.10 `
  --out models/score_nn_16_10_empirical_roles_profile_sustain_weighted `
  --embed-dim 16 `
  --score-dim 8 `
  --hidden 64 `
  --dropout 0.35 `
  --lr 0.0015 `
  --weight-decay 0.02
```

## Modeling Direction

The next useful step is not just adding more raw columns.  The model needs
better conditional composition features.

High-value conditional patterns:

- high backline damage plus low frontline should be penalized
- high poke plus wave clear is different from poke without wave clear
- engage plus follow-up damage matters more than engage alone
- sustain is more valuable when the team has poke/disengage or extended-fight
  champions
- CC value depends on range, reliability, and whether the team can capitalize
- AD/AP/true damage profile matters when the team is one-dimensional

Possible feature blocks:

```text
team_damage * team_frontline
team_poke * team_wave_clear
team_engage * team_followup_damage
lacks_frontline AND double_marksman
lacks_magic_damage
lacks_cc
high_sustain AND high_poke
```

These are interpretable, cheap, and may help both tree models and NN.

## Recommended Experiment Order

1. Keep LR champion-identity baseline as the reference.
2. Rebuild empirical stats after new LCU parser fields accumulate.
3. Add teammate heal/shield fields if they appear in new DB rows.
4. Run time-aware k-fold or expanding-window validation, because single split
   comparisons are noisy.
5. Try LightGBM/XGBoost with champion identity, capability aggregates, role
   aggregates, damage profile, and explicit conditional features.
6. Try smaller/calibrated NN variants:
   - stronger dropout
   - lower embedding dimension
   - temperature scaling
   - multi-seed ensemble
7. Only try attention/team encoders after the tabular baselines are exhausted.

## What Not To Do

- Do not use full-DB empirical averages for evaluation.
- Do not treat Riot role tags as hard truth.
- Do not treat `damage_self_mitigated` as shield.
- Do not assume `total_heal` means ally sustain.
- Do not judge a model only by accuracy.  Track log-loss and calibration.
- Do not rerun old broad pairwise LR as-is unless data/features materially
  change.

## Open Questions

- Does the LCU endpoint ever return teammate healing/shielding for Mayhem, or
  only for some queues / EoG views?
- Are `total_units_healed` values reliable enough as a teammate-heal proxy?
- Does objective/turret damage matter in ARAM/Mayhem, or is it mostly noise?
- Can explicit conditional features close the log-loss gap against LR?
- Will these feature gains survive expanding-window validation?
