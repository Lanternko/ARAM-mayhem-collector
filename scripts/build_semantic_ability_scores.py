"""Build reviewable champion-level semantic ability scores.

Inputs:
  data/cache/champion_abilities.json from scripts/fetch_champion_abilities.py

Outputs:
  data/cache/champion_semantic_scores.csv
  data/cache/champion_semantic_scores.json

Scores are heuristic 0..3 drafts, not ground truth.  They are intended to make
team-composition hypotheses explicit and easy to audit:

  wave_clear_score, cc_score, engage_score, damage_score, poke_score,
  sustain_score, frontline_score
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

import click


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

HARD_CC_WORDS = (
    "stun",
    "stuns",
    "stunning",
    "stunned",
    "root",
    "roots",
    "rooted",
    "airborne",
    "knock up",
    "knocks up",
    "knock back",
    "knocks back",
    "knocked up",
    "knocked back",
    "suppress",
    "suppression",
    "silence",
    "silenced",
    "charm",
    "charmed",
    "taunt",
    "taunted",
    "fear",
    "feared",
    "flee",
    "flees",
    "sleep",
    "asleep",
    "polymorph",
    "pull",
    "pulls",
    "pulled",
    "drag",
    "drags",
    "dragged",
    "hook",
    "hooks",
    "hooked",
    "bind",
    "binds",
    "binding",
    "bound",
    "snare",
    "snares",
    "snared",
    "entangle",
    "entangles",
    "entangled",
    "immobilize",
    "immobilizes",
    "immobilized",
    "impale",
    "impales",
    "impaled",
)

SOFT_CC_WORDS = (
    "slow",
    "cripple",
    "ground",
    "nearsight",
    "drowsy",
)

MOBILITY_WORDS = (
    "dash",
    "leap",
    "blink",
    "jump",
    "teleport",
    "rush",
    "charge toward",
    "charge to",
    "vault",
)

AOE_WORDS = (
    "area",
    "nearby",
    "around",
    "cone",
    "line",
    "explosion",
    "explode",
    "shockwave",
    "zone",
    "all enemies",
    "enemies hit",
    "each enemy",
    "bounce",
    "chain",
)

POKE_WORDS = (
    "missile",
    "projectile",
    "beam",
    "bolt",
    "shot",
    "skillshot",
    "orb",
    "spear",
    "rocket",
)

SUSTAIN_TERMS = (
    "heal",
    "heals",
    "healing",
    "healed",
    "shield",
    "shields",
    "shielding",
    "shielded",
    "restore",
    "restores",
    "restoring",
    "restored",
    "regenerate",
    "regenerates",
    "regeneration",
    "life steal",
    "lifesteal",
    "omnivamp",
    "drain",
    "drains",
    "draining",
)

FRONTLINE_WORDS = (
    "damage reduction",
    "armor",
    "magic resist",
    "resistances",
    "unstoppable",
    "tenacity",
    "maximum health",
    "bonus health",
)


# User-reviewed corrections for obvious heuristic misses.  Keep these sparse:
# broad fixes should go into the rules above, while champion-specific judgment
# can live here after manual review.
REVIEWED_OVERRIDES: dict[str, dict[str, float]] = {
    # Ziggs is elite wave/poke, but E minefield is not real engage and he is not frontline.
    "Ziggs": {"wave_clear_score": 2.8, "poke_score": 2.8, "engage_score": 0.0, "frontline_score": 0.0},
    # Two-target Q plus aura utility is not real ARAM wave clear.
    "Sona": {"wave_clear_score": 0.25, "engage_score": 1.2, "damage_score": 1.3, "sustain_score": 2.2},
    "Janna": {"cc_score": 1.8, "sustain_score": 2.2},
    # Soraka's output is mostly utility pressure, not sustained team damage.
    "Soraka": {"damage_score": 1.1},
    # Sivir is a sustained marksman; E is a spell shield, not meaningful team sustain.
    "Sivir": {"damage_score": 2.35, "poke_score": 1.0, "sustain_score": 0.2},
    # Lux shield is utility, but should not count as real sustain for composition balance.
    "Lux": {"cc_score": 1.9, "sustain_score": 0.0},
    # Xerath Q is a fast, wide, long-range line clear in ARAM.
    "Xerath": {"wave_clear_score": 2.2, "cc_score": 1.35, "poke_score": 3.0},
    # Incidental tank damage should not look like carry output.
    "Leona": {"wave_clear_score": 0.25, "poke_score": 0.0, "damage_score": 1.0, "engage_score": 3.0},
    "Nautilus": {"damage_score": 1.2, "cc_score": 2.8},
    # Hook displacement is high-value engage/peel even though Pyke is fragile.
    "Pyke": {"cc_score": 2.2, "engage_score": 2.0, "damage_score": 1.55},
    # Kog'Maw's poke is mostly R/AP-dependent; his main threat is W DPS.
    "KogMaw": {"poke_score": 1.55, "frontline_score": 0.0},
    # Sapling poke exists, but Sivir's repeatable lane pressure should rank higher.
    "Maokai": {"engage_score": 2.5, "poke_score": 0.75, "sustain_score": 1.2},
}


def clamp_score(value: float) -> float:
    return round(max(0.0, min(3.0, value)), 2)


def text_of(ability: dict[str, Any]) -> str:
    parts = [
        ability.get("name_en", ""),
        ability.get("description_en_clean", ""),
        ability.get("tooltip_en_clean", ""),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def passive_text(champion: dict[str, Any]) -> str:
    passive = champion.get("passive") or {}
    return " ".join(
        str(passive.get(k, ""))
        for k in ("name_en", "description_en_clean")
        if passive.get(k)
    ).lower()


def contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def contains_terms(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        pattern = r"\b" + re.escape(term).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, text):
            return True
    return False


def number_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        if isinstance(item, (int, float)) and math.isfinite(float(item)):
            out.append(float(item))
    return out


def spell_range(ability: dict[str, Any]) -> float:
    xs = [x for x in number_list(ability.get("range")) if 0 < x < 10000]
    if xs:
        return max(xs)
    raw = str(ability.get("range_burn") or "")
    nums: list[float] = []
    for part in raw.split("/"):
        try:
            value = float(part)
        except ValueError:
            continue
        if 0 < value < 10000:
            nums.append(value)
    return max(nums) if nums else 0.0


def cooldown_min(ability: dict[str, Any]) -> float:
    xs = [x for x in number_list(ability.get("cooldown")) if x > 0]
    return min(xs) if xs else 99.0


def has_damage(text: str) -> bool:
    return bool(
        re.search(r"\b(?:deal|deals|dealing|dealt)\b.{0,80}\bdamage\b", text)
        or "damages " in text
        or re.search(r"\bdamage\b.{0,40}\benem", text)
    )


def is_aoe(ability: dict[str, Any], text: str) -> bool:
    tags = set(ability.get("heuristic_tags") or [])
    return "aoe_or_multitarget" in tags or contains_any(text, AOE_WORDS)


def has_hard_cc(ability: dict[str, Any], text: str) -> bool:
    # Ignore self-roots/channel restrictions such as Xerath R.
    text = (
        text.replace("immobilizes himself", "")
        .replace("roots himself", "")
        .replace("stuns himself", "")
    )
    return contains_terms(text, HARD_CC_WORDS)


def has_soft_cc(ability: dict[str, Any], text: str) -> bool:
    return contains_terms(text, SOFT_CC_WORDS)


def has_mobility(ability: dict[str, Any], text: str) -> bool:
    return contains_terms(text, MOBILITY_WORDS)


def has_sustain(text: str) -> bool:
    if contains_terms(text, ("shield", "shields", "shielding", "shielded")):
        return True
    if contains_terms(text, ("heal", "heals", "healing", "healed")):
        return True
    if contains_terms(
        text,
        ("life steal", "lifesteal", "omnivamp", "regenerate", "regenerates", "regeneration"),
    ):
        return True
    if re.search(r"\brestore(?:s|d|ing)?\b.{0,40}\bhealth\b", text):
        return True
    if re.search(r"\bdrain(?:s|ed|ing)?\b.{0,60}\bhealth\b", text):
        return True
    return False


def score_champion(champion: dict[str, Any]) -> dict[str, Any]:
    tags = set(champion.get("tags") or [])
    ability_rows = champion.get("abilities") or []

    wave = 0.0
    cc = 0.0
    engage = 0.0
    damage = 0.0
    poke = 0.0
    sustain = 0.0
    frontline = 0.0
    evidence: dict[str, list[str]] = {col: [] for col in SCORE_COLUMNS}

    champ_hard_cc = False
    champ_soft_cc = False
    champ_mobility = False

    for ability in ability_rows:
        slot = str(ability.get("slot") or "?")
        text = text_of(ability)
        rng = spell_range(ability)
        cd = cooldown_min(ability)
        dmg = has_damage(text)
        aoe = is_aoe(ability, text)
        hard = has_hard_cc(ability, text)
        soft = has_soft_cc(ability, text)
        mobility = has_mobility(ability, text)
        sustainy = has_sustain(text)

        champ_hard_cc = champ_hard_cc or hard
        champ_soft_cc = champ_soft_cc or soft
        champ_mobility = champ_mobility or mobility

        slot_weight = 0.75 if slot == "R" else 1.0
        if dmg:
            spell_damage = 0.45
            if cd <= 7:
                spell_damage += 0.25
            if aoe:
                spell_damage += 0.2
            if rng >= 800:
                spell_damage += 0.15
            if slot == "R":
                spell_damage += 0.25
            damage += min(0.9, spell_damage)
            evidence["damage_score"].append(slot)

        if dmg and aoe:
            spell_wave = 0.25
            if cd <= 8:
                spell_wave += 0.3
            elif cd <= 12:
                spell_wave += 0.15
            if rng >= 650:
                spell_wave += 0.2
            if any(word in text for word in ("minion", "wave", "line", "cone", "all enemies", "nearby targets")):
                spell_wave += 0.2
            if "two nearby enemies" in text or "single target" in text:
                spell_wave *= 0.35
            if "damage reduction" in text or "armor" in text or "magic resist" in text:
                spell_wave *= 0.35
            if "Tank" in tags and "Mage" not in tags:
                spell_wave *= 0.55
            if slot == "R":
                spell_wave *= 0.55
            wave += min(1.0, spell_wave) * slot_weight
            evidence["wave_clear_score"].append(slot)

        if hard:
            cc += 0.85 if slot != "R" else 0.75
            if rng >= 650:
                cc += 0.15
            if cd <= 14:
                cc += 0.1
            if rng >= 900 and contains_terms(text, ("pull", "pulls", "hook", "hooks", "knock back", "knocks back")):
                cc += 0.2
            evidence["cc_score"].append(f"{slot}:hard")
        elif soft:
            cc += 0.35 if slot != "R" else 0.3
            if rng >= 700:
                cc += 0.1
            evidence["cc_score"].append(f"{slot}:soft")

        if hard and (mobility or rng >= 650 or contains_terms(text, ("pull", "pulls", "hook", "hooks", "dash"))):
            engage += 0.75
            if rng >= 900:
                engage += 0.25
            if contains_terms(text, ("pull", "pulls", "hook", "hooks", "knock back", "knocks back")):
                engage += 0.35
            evidence["engage_score"].append(slot)
        elif soft and mobility:
            engage += 0.35
            evidence["engage_score"].append(slot)
        elif mobility and slot != "R":
            engage += 0.15

        if dmg and rng >= 850:
            spell_poke = 0.35
            if rng >= 1000:
                spell_poke += 0.25
            if cd <= 10:
                spell_poke += 0.2
            if contains_any(text, POKE_WORDS):
                spell_poke += 0.15
            poke += min(0.9, spell_poke)
            evidence["poke_score"].append(slot)

        if sustainy:
            sustain += 0.55
            if contains_terms(text, ("shield", "shields", "shielded", "shielding")):
                sustain += 0.2
            if contains_terms(text, ("heal", "heals", "healing", "restore", "restores", "drain", "drains", "draining")):
                sustain += 0.2
            evidence["sustain_score"].append(slot)

        if contains_any(text, FRONTLINE_WORDS):
            frontline += 0.25
            evidence["frontline_score"].append(slot)
        if hard and (slot == "R" or rng >= 550):
            # Peel CC can function as pseudo-frontline by stopping divers, but
            # it should stay far below real durability.
            frontline += 0.18
            evidence["frontline_score"].append(f"{slot}:peel")

    ptext = passive_text(champion)
    if has_sustain(ptext):
        sustain += 0.35
        evidence["sustain_score"].append("P")
    if contains_any(ptext, FRONTLINE_WORDS):
        frontline += 0.35
        evidence["frontline_score"].append("P")

    if champ_hard_cc and champ_mobility:
        engage += 0.35
    elif champ_hard_cc and (tags & {"Tank", "Fighter"}):
        engage += 0.25

    if "Marksman" in tags:
        damage += 0.75
        poke += 0.15
    if "Mage" in tags:
        damage += 0.1
        wave += 0.15
        poke += 0.15
    if "Tank" in tags:
        frontline += 1.0
        engage += 0.2 if champ_hard_cc or champ_soft_cc else 0.0
    if "Fighter" in tags:
        frontline += 0.45
        damage += 0.15
    if "Support" in tags and sustain > 0:
        sustain += 0.15
        if sustain >= 1.2:
            damage -= 0.35

    row = {
        "champion_id": int(champion["champion_id"]),
        "champion_alias": champion.get("alias", ""),
        "champion_name_en": champion.get("name_en", ""),
        "champion_name_zh": champion.get("name_zh", ""),
        "tags": "|".join(champion.get("tags") or []),
        "wave_clear_score": clamp_score(wave),
        "cc_score": clamp_score(cc),
        "engage_score": clamp_score(engage),
        "damage_score": clamp_score(damage),
        "poke_score": clamp_score(poke),
        "sustain_score": clamp_score(sustain),
        "frontline_score": clamp_score(frontline),
        "notes": "; ".join(
            f"{col.replace('_score', '')}={','.join(vals[:5])}"
            for col, vals in evidence.items()
            if vals
        ),
    }
    for key, value in REVIEWED_OVERRIDES.get(str(champion.get("alias", "")), {}).items():
        row[key] = float(value)
    row["core_min_score"] = min(float(row[col]) for col in CORE_COLUMNS)
    row["core_mean_score"] = round(
        sum(float(row[col]) for col in CORE_COLUMNS) / len(CORE_COLUMNS),
        2,
    )
    return row


def build_scores(ability_json: Path) -> list[dict[str, Any]]:
    raw = json.loads(ability_json.read_text(encoding="utf-8"))
    rows = [score_champion(champion) for champion in raw.get("champions", [])]
    return sorted(rows, key=lambda row: int(row["champion_id"]))


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        raise click.ClickException("No rows to write")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


@click.command()
@click.option(
    "--ability-json",
    type=click.Path(exists=True, path_type=Path),
    default=Path("data/cache/champion_abilities.json"),
    show_default=True,
)
@click.option(
    "--out-csv",
    type=click.Path(path_type=Path),
    default=Path("data/cache/champion_semantic_scores.csv"),
    show_default=True,
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=Path("data/cache/champion_semantic_scores.json"),
    show_default=True,
)
def main(ability_json: Path, out_csv: Path, out_json: Path) -> None:
    rows = build_scores(ability_json)
    write_csv(rows, out_csv)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    click.echo(f"[semantic] wrote {len(rows)} champion rows")
    click.echo(f"[semantic] csv : {out_csv}")
    click.echo(f"[semantic] json: {out_json}")


if __name__ == "__main__":
    main()
