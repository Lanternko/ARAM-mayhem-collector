"""Fetch champion Q/W/E/R ability data from Riot Data Dragon.

This is intentionally a raw catalogue, not a hand-written role model.  The
outputs are meant to seed later ability-type annotation and feature work:

    python scripts/fetch_champion_abilities.py
    python scripts/fetch_champion_abilities.py --version 16.10.1
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import click
import httpx


DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CHAMPION_FULL = (
    "https://ddragon.leagueoflegends.com/cdn/{version}/data/{locale}/championFull.json"
)
SPELL_SLOTS = ("Q", "W", "E", "R")


TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")

HEURISTIC_TAG_PATTERNS: dict[str, tuple[str, ...]] = {
    "hard_cc": (
        r"\bstun(?:s|ned|ning)?\b",
        r"\broot(?:s|ed|ing)?\b",
        r"\bknock(?:s|ed|ing)?\s+(?:up|back|aside)\b",
        r"\bairborne\b",
        r"\bsuppress(?:es|ed|ing|ion)?\b",
        r"\bsilence(?:s|d)?\b",
        r"\bcharm(?:s|ed|ing)?\b",
        r"\btaunt(?:s|ed|ing)?\b",
        r"\bfear(?:s|ed|ing)?\b",
        r"\bflee(?:s|ing)?\b",
        r"\bsleep(?:s|ing)?\b",
        r"\bpolymorph(?:s|ed|ing)?\b",
        r"\bdisplace(?:s|d|ment)?\b",
    ),
    "soft_cc": (
        r"\bslow(?:s|ed|ing)?\b",
        r"\bcripple(?:s|d)?\b",
        r"\bground(?:s|ed|ing)?\b",
        r"\bnearsight(?:s|ed|ing)?\b",
        r"\bdrowsy\b",
    ),
    "mobility": (
        r"\bdash(?:es|ed|ing)?\b",
        r"\bblink(?:s|ed|ing)?\b",
        r"\bleap(?:s|ed|ing)?\b",
        r"\bjump(?:s|ed|ing)?\b",
        r"\bteleport(?:s|ed|ing)?\b",
        r"\brush(?:es|ed|ing)?\b",
        r"\bcharge(?:s|d|ing)?\s+(?:to|toward|at|through|forward|into)\b",
        r"\bvault(?:s|ed|ing)?\b",
    ),
    "aoe_or_multitarget": (
        r"\barea\b",
        r"\bnearby\b",
        r"\baround\b",
        r"\bcone\b",
        r"\bline\b",
        r"\bexplod(?:e|es|ed|ing)\b",
        r"\bexplosion\b",
        r"\bshockwave\b",
        r"\bzone\b",
        r"\ball enemies\b",
        r"\benemies hit\b",
        r"\beach enemy\b",
        r"\benemy champions hit\b",
    ),
    "shield": (
        r"\bshield(?:s|ed|ing)?\b",
    ),
    "heal_or_sustain": (
        r"\bheal(?:s|ed|ing)?\b",
        r"\brestore(?:s|d|ing)? health\b",
        r"\bregenerate(?:s|d|ing)?\b",
        r"\blife steal\b",
        r"\bomnivamp\b",
    ),
    "poke_hint": (
        r"\bmissile\b",
        r"\bprojectile\b",
        r"\bbeam\b",
        r"\bbolt\b",
        r"\bshot\b",
        r"\bskillshot\b",
        r"\bfrom afar\b",
    ),
    "execute_or_missing_health": (
        r"\bexecute(?:s|d|ing)?\b",
        r"\bmissing health\b",
    ),
}


def fetch_json(url: str, *, timeout: float = 30.0) -> Any:
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def latest_version() -> str:
    versions = fetch_json(DDRAGON_VERSIONS)
    if not isinstance(versions, list) or not versions:
        raise click.ClickException("Data Dragon versions response was empty")
    return str(versions[0])


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    text = TAG_RE.sub("", text)
    text = PLACEHOLDER_RE.sub(r"{{\1}}", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def infer_heuristic_tags(spell: dict[str, Any]) -> list[str]:
    text = " ".join(
        clean_text(str(spell.get(field) or ""))
        for field in ("name", "description", "tooltip")
    ).lower()
    tags: list[str] = []
    for tag, patterns in HEURISTIC_TAG_PATTERNS.items():
        if any(re.search(pattern, text) for pattern in patterns):
            tags.append(tag)
    return tags


def load_champion_full(version: str, locale: str) -> dict[str, Any]:
    url = DDRAGON_CHAMPION_FULL.format(version=version, locale=locale)
    raw = fetch_json(url, timeout=60.0)
    data = raw.get("data")
    if not isinstance(data, dict):
        raise click.ClickException(f"{url} did not contain a champion data map")
    return data


def ability_image_url(version: str, image: dict[str, Any] | None) -> str:
    full = (image or {}).get("full")
    if not full:
        return ""
    return f"https://ddragon.leagueoflegends.com/cdn/{version}/img/spell/{full}"


def build_catalogue(
    *,
    version: str,
    primary_locale: str,
    secondary_locale: str,
) -> dict[str, Any]:
    primary = load_champion_full(version, primary_locale)
    secondary = load_champion_full(version, secondary_locale)

    champions: list[dict[str, Any]] = []
    spell_count = 0
    non_four_spell_champions: list[str] = []

    for alias, entry in sorted(primary.items(), key=lambda item: int(item[1]["key"])):
        localized = secondary.get(alias, {})
        spells = entry.get("spells") or []
        localized_spells = localized.get("spells") or []
        if len(spells) != 4:
            non_four_spell_champions.append(alias)

        abilities: list[dict[str, Any]] = []
        for idx, spell in enumerate(spells[:4]):
            localized_spell = localized_spells[idx] if idx < len(localized_spells) else {}
            slot = SPELL_SLOTS[idx]
            ability = {
                "slot": slot,
                "spell_id": spell.get("id", ""),
                "name_en": spell.get("name", ""),
                "name_zh": localized_spell.get("name") or spell.get("name", ""),
                "description_en": spell.get("description", ""),
                "description_zh": localized_spell.get("description", ""),
                "description_en_clean": clean_text(spell.get("description")),
                "description_zh_clean": clean_text(localized_spell.get("description")),
                "tooltip_en": spell.get("tooltip", ""),
                "tooltip_zh": localized_spell.get("tooltip", ""),
                "tooltip_en_clean": clean_text(spell.get("tooltip")),
                "tooltip_zh_clean": clean_text(localized_spell.get("tooltip")),
                "maxrank": spell.get("maxrank"),
                "cooldown": spell.get("cooldown") or [],
                "cooldown_burn": spell.get("cooldownBurn", ""),
                "cost": spell.get("cost") or [],
                "cost_burn": spell.get("costBurn", ""),
                "cost_type_en": spell.get("costType", ""),
                "cost_type_zh": localized_spell.get("costType", ""),
                "resource_en": spell.get("resource", ""),
                "resource_zh": localized_spell.get("resource", ""),
                "range": spell.get("range") or [],
                "range_burn": spell.get("rangeBurn", ""),
                "effect": spell.get("effect") or [],
                "effect_burn": spell.get("effectBurn") or [],
                "vars": spell.get("vars") or [],
                "image": spell.get("image") or {},
                "image_url": ability_image_url(version, spell.get("image")),
                "heuristic_tags": infer_heuristic_tags(spell),
            }
            abilities.append(ability)
            spell_count += 1

        champions.append(
            {
                "champion_id": int(entry["key"]),
                "alias": alias,
                "name_en": entry.get("name", alias),
                "name_zh": localized.get("name") or entry.get("name", alias),
                "title_en": entry.get("title", ""),
                "title_zh": localized.get("title", ""),
                "tags": entry.get("tags") or [],
                "partype_en": entry.get("partype", ""),
                "partype_zh": localized.get("partype", ""),
                "passive": {
                    "name_en": (entry.get("passive") or {}).get("name", ""),
                    "name_zh": (localized.get("passive") or {}).get("name", ""),
                    "description_en": (entry.get("passive") or {}).get("description", ""),
                    "description_zh": (localized.get("passive") or {}).get("description", ""),
                    "description_en_clean": clean_text(
                        (entry.get("passive") or {}).get("description")
                    ),
                    "description_zh_clean": clean_text(
                        (localized.get("passive") or {}).get("description")
                    ),
                },
                "abilities": abilities,
            }
        )

    return {
        "source": "Riot Data Dragon",
        "version": version,
        "primary_locale": primary_locale,
        "secondary_locale": secondary_locale,
        "generated_at_utc": dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "source_urls": {
            "versions": DDRAGON_VERSIONS,
            "champion_full_primary": DDRAGON_CHAMPION_FULL.format(
                version=version, locale=primary_locale
            ),
            "champion_full_secondary": DDRAGON_CHAMPION_FULL.format(
                version=version, locale=secondary_locale
            ),
        },
        "summary": {
            "champion_count": len(champions),
            "ability_count": spell_count,
            "expected_slots": list(SPELL_SLOTS),
            "non_four_spell_champions": non_four_spell_champions,
            "heuristic_tags_are_ground_truth": False,
        },
        "champions": champions,
    }


def iter_ability_rows(catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for champion in catalogue["champions"]:
        for ability in champion["abilities"]:
            rows.append(
                {
                    "champion_id": champion["champion_id"],
                    "champion_alias": champion["alias"],
                    "champion_name_en": champion["name_en"],
                    "champion_name_zh": champion["name_zh"],
                    "champion_tags": compact_json(champion["tags"]),
                    "slot": ability["slot"],
                    "spell_id": ability["spell_id"],
                    "ability_name_en": ability["name_en"],
                    "ability_name_zh": ability["name_zh"],
                    "description_en": ability["description_en_clean"],
                    "description_zh": ability["description_zh_clean"],
                    "tooltip_en": ability["tooltip_en_clean"],
                    "tooltip_zh": ability["tooltip_zh_clean"],
                    "maxrank": ability["maxrank"],
                    "cooldown": compact_json(ability["cooldown"]),
                    "cooldown_burn": ability["cooldown_burn"],
                    "cost": compact_json(ability["cost"]),
                    "cost_burn": ability["cost_burn"],
                    "range": compact_json(ability["range"]),
                    "range_burn": ability["range_burn"],
                    "resource_en": ability["resource_en"],
                    "resource_zh": ability["resource_zh"],
                    "effect_burn": compact_json(ability["effect_burn"]),
                    "vars": compact_json(ability["vars"]),
                    "heuristic_tags": compact_json(ability["heuristic_tags"]),
                    "image_url": ability["image_url"],
                }
            )
    return rows


def write_csv(rows: list[dict[str, Any]], out_csv: Path) -> None:
    if not rows:
        raise click.ClickException("No ability rows to write")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


@click.command()
@click.option("--version", default=None, help="Data Dragon version; default: latest")
@click.option("--primary-locale", default="en_US", show_default=True)
@click.option("--secondary-locale", default="zh_TW", show_default=True)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=Path("data/cache/champion_abilities.json"),
    show_default=True,
)
@click.option(
    "--out-csv",
    type=click.Path(path_type=Path),
    default=Path("data/cache/champion_abilities.csv"),
    show_default=True,
)
def main(
    version: str | None,
    primary_locale: str,
    secondary_locale: str,
    out_json: Path,
    out_csv: Path,
) -> None:
    """Fetch champion ability catalogue and write JSON + CSV outputs."""
    version = version or latest_version()
    click.echo(f"[abilities] fetching Data Dragon {version}")
    catalogue = build_catalogue(
        version=version,
        primary_locale=primary_locale,
        secondary_locale=secondary_locale,
    )
    rows = iter_ability_rows(catalogue)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(rows, out_csv)

    summary = catalogue["summary"]
    click.echo(
        "[abilities] wrote "
        f"{summary['champion_count']} champions / {summary['ability_count']} abilities"
    )
    click.echo(f"[abilities] json: {out_json}")
    click.echo(f"[abilities] csv : {out_csv}")
    if summary["non_four_spell_champions"]:
        click.echo(
            "[abilities] warning: non-4-spell champions: "
            + ", ".join(summary["non_four_spell_champions"])
        )


if __name__ == "__main__":
    main()
