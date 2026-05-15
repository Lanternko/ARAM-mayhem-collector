"""Generate a tier-list HTML from Mayhem (or ARAM) winrates.

Reads winrates from data/lcu/games.db, fetches champion id->name mapping from
Riot's Data Dragon CDN, applies Bayesian smoothing, and renders an HTML grid
where each champion icon carries a tier badge (OP / T1..T5) in the top-right.

Clicking a champion expands an inline panel below its tier-row showing the
top-5 best and bottom-5 worst augments (by Bayesian-smoothed winrate using
that champion's own baseline winrate as the prior).

Usage:
    python scripts/build_tier_list.py
    python scripts/build_tier_list.py --queue 2400 --patch-prefix 16.10 --out tier_list.html
    python scripts/build_tier_list.py --queue 450  --patch-prefix 16.9
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import click
import httpx


TIER_ORDER = ["OP", "T1", "T2", "T3", "T4", "T5"]
TIER_COLOR = {
    "OP": "#d8b8ff",
    "T1": "#ff5a3c",
    "T2": "#f5c518",
    "T3": "#8ec441",
    "T4": "#3aa0ff",
    "T5": "#7a7f8a",
}
# OP gets a prismatic/iridescent look with shine + glow (see CSS below).
# Other tiers stay solid.
TIER_LABEL_BG = {
    "OP": (
        "linear-gradient(135deg,"
        "#ffffff 0%,#e7d5ff 18%,#bcd6ff 36%,"
        "#ffd5ec 58%,#fff1c8 78%,#ffffff 100%)"
    ),
    "T1": "#ff5a3c",
    "T2": "#f5c518",
    "T3": "#8ec441",
    "T4": "#3aa0ff",
    "T5": "#7a7f8a",
}

CDRAGON_BASE = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default"


def assign_tier(bayes_wr: float) -> str:
    if bayes_wr >= 0.55:
        return "OP"
    if bayes_wr >= 0.52:
        return "T1"
    if bayes_wr >= 0.50:
        return "T2"
    if bayes_wr >= 0.48:
        return "T3"
    if bayes_wr >= 0.46:
        return "T4"
    return "T5"


def load_champion_metadata(version: str | None) -> tuple[str, dict[int, dict]]:
    if version is None:
        r = httpx.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=15)
        r.raise_for_status()
        version = r.json()[0]
    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_TW/champion.json"
    r = httpx.get(url, timeout=30)
    if r.status_code != 200:
        url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
        r = httpx.get(url, timeout=30)
    r.raise_for_status()
    raw = r.json()["data"]
    by_id: dict[int, dict] = {}
    for _, entry in raw.items():
        by_id[int(entry["key"])] = {
            "name": entry["name"],
            "alias": entry["id"],
            "tags": entry.get("tags") or [],
            "image": f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{entry['id']}.png",
        }
    return version, by_id


def _icon_url(lcu_path: str) -> str:
    """Convert an LCU asset path to a CommunityDragon URL."""
    stripped = lcu_path.replace("/lol-game-data/assets/", "", 1).lower()
    return f"{CDRAGON_BASE}/{stripped}"


def _cached_get_json(url: str, cache_path: Path, timeout: float = 60) -> dict | list:
    """Fetch JSON with on-disk caching (the kiwi.bin.json + stringtable are large)."""
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(r.text, encoding="utf-8")
    return r.json()


# Strips Riot's inline markup so an augment description can be shown as plain
# text in a hover tooltip:
#   * `<speed>跑速</speed>`     -> `跑速`            (keep inner text)
#   * `<br>` / `<br />`         -> ` ` / newline
#   * `@MovespeedMod*100@%`     -> `[數值]`          (numeric placeholders)
#   * `%i:scaleCrit%`           -> ``                (inline UI icons)
_TAG_RE = re.compile(r"<[^>]+>")
_PLACEHOLDER_RE = re.compile(r"@[A-Za-z0-9_*+\-./]+@%?")
_ICON_REF_RE = re.compile(r"%i:[A-Za-z0-9_]+%")


def _clean_desc(text: str) -> str:
    if not text:
        return ""
    s = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    s = _PLACEHOLDER_RE.sub("[數值]", s)
    s = _ICON_REF_RE.sub("", s)
    s = _TAG_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_augment_descriptions(cache_dir: Path) -> dict[int, str]:
    """Resolve Mayhem augment descriptions via:

        kiwi.bin.json (AugmentPlatformId -> DescriptionTra)
        +  lol.stringtable.json zh_tw (lowercase key -> zh_tw text)

    Returns dict mapping augment ID (matches our DB) -> cleaned zh-TW summary.
    """
    kiwi = _cached_get_json(
        "https://raw.communitydragon.org/latest/game/maps/modespecificdata/kiwi.bin.json",
        cache_dir / "kiwi.bin.json",
    )
    plat: dict[int, tuple[str | None, str | None]] = {}
    for entry in kiwi.values() if isinstance(kiwi, dict) else []:
        if not isinstance(entry, dict) or entry.get("__type") != "AugmentData":
            continue
        pid = entry.get("AugmentPlatformId")
        if pid is None:
            continue
        desc_key = (entry.get("DescriptionTra") or "").lower() or None
        tip_key = (entry.get("AugmentTooltipTra") or "").lower() or None
        plat[int(pid)] = (desc_key, tip_key)

    st = _cached_get_json(
        "https://raw.communitydragon.org/latest/game/zh_tw/data/menu/en_us/lol.stringtable.json",
        cache_dir / "lol_stringtable_zh_tw.json",
    )
    entries = st["entries"] if isinstance(st, dict) and "entries" in st else {}

    out: dict[int, str] = {}
    for pid, (desc_key, tip_key) in plat.items():
        # Prefer the *Summary (DescriptionTra) — it tends to be a short clean
        # blurb with no @placeholders.  Fall back to Tooltip if missing.
        raw = ""
        if desc_key and desc_key in entries:
            raw = entries[desc_key]
        if not raw and tip_key and tip_key in entries:
            raw = entries[tip_key]
        cleaned = _clean_desc(raw)
        if cleaned:
            out[pid] = cleaned
    return out


def load_augment_metadata(cache_dir: Path | None = None) -> dict[int, dict]:
    # Try zh-TW first; fall back to default (English) if the field is empty.
    try:
        r_tw = httpx.get(f"{CDRAGON_BASE.replace('/default', '/zh_tw')}/v1/cherry-augments.json", timeout=20)
        r_tw.raise_for_status()
        tw_rows = r_tw.json()
    except Exception:
        tw_rows = []
    tw_by_id = {int(r["id"]): r for r in tw_rows if "id" in r}

    r = httpx.get(f"{CDRAGON_BASE}/v1/cherry-augments.json", timeout=20)
    r.raise_for_status()
    rows = r.json()

    by_id: dict[int, dict] = {}
    for entry in rows:
        aug_id = entry.get("id")
        if aug_id is None:
            continue
        aug_id = int(aug_id)
        tw_entry = tw_by_id.get(aug_id, {})
        tw_name = tw_entry.get("nameTRA") or tw_entry.get("name")
        en_name = entry.get("nameTRA") or entry.get("name") or entry.get("simpleNameTRA")
        name = tw_name if tw_name and tw_name.strip() else en_name
        icon_path = (
            entry.get("augmentSmallIconPath")
            or entry.get("augmentLargeIconPath")
        )
        by_id[aug_id] = {
            "name": name or f"#{aug_id}",
            "icon": _icon_url(icon_path) if icon_path else "",
            "rarity": entry.get("rarity", ""),
            "desc": "",
        }

    if cache_dir is not None:
        try:
            descs = load_augment_descriptions(cache_dir)
            for aid, txt in descs.items():
                if aid in by_id:
                    by_id[aid]["desc"] = txt
        except Exception as exc:
            click.echo(f"[tierlist] WARN: augment description fetch failed: {exc}")

    return by_id


def compute_winrates(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    prior: float = 0.5,
    k: int = 200,
):
    """Compute champion winrates + per-(champion, augment) winrates.

    Returns: (champ_records, champ_aug_records)
      champ_records: list of dicts with champion_id, games, wins, raw_wr, bayes_wr
      champ_aug_records: list of dicts with champion_id, augment_id, games, wins,
                        raw_wr, smoothed_wr, lift (smoothed_wr - champ_baseline_wr)
    """
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = list(
            con.execute(
                "SELECT blue_champs, red_champs, blue_wins, participants_json FROM games "
                "WHERE queue_id=? AND patch LIKE ?",
                (queue_id, f"{patch_prefix}%"),
            )
        )
    else:
        rows = list(
            con.execute(
                "SELECT blue_champs, red_champs, blue_wins, participants_json FROM games "
                "WHERE queue_id=?",
                (queue_id,),
            )
        )
    con.close()

    games: Counter[int] = Counter()
    wins: Counter[int] = Counter()
    ca_games: Counter[tuple[int, int]] = Counter()
    ca_wins: Counter[tuple[int, int]] = Counter()

    for blue, red, bw, pj in rows:
        bw_bool = bool(bw)
        for c in json.loads(blue):
            games[c] += 1
            if bw_bool:
                wins[c] += 1
        for c in json.loads(red):
            games[c] += 1
            if not bw_bool:
                wins[c] += 1
        if not pj:
            continue
        for p in json.loads(pj):
            cid = int(p.get("championId", 0))
            if cid <= 0:
                continue
            player_won = 1 if (int(p.get("teamId", 0)) == 100) == bw_bool else 0
            for a in p.get("augments") or []:
                a = int(a)
                if a <= 0:
                    continue
                ca_games[(cid, a)] += 1
                ca_wins[(cid, a)] += player_won

    champ_records = []
    for cid, g in games.items():
        w = wins[cid]
        raw = w / g if g else 0.0
        bayes = (w + prior * k) / (g + k)
        champ_records.append({
            "champion_id": cid,
            "games": g,
            "wins": w,
            "raw_wr": raw,
            "bayes_wr": bayes,
        })
    champ_records.sort(key=lambda d: -d["bayes_wr"])

    # Per-pair smoothing uses *that champion's* baseline winrate as the prior.
    # This way the comparison is "does this augment lift the champ above its
    # own baseline?", which is what we actually want for best/worst-fit picks.
    raw_wr_by_champ = {cid: (wins[cid] / games[cid]) if games[cid] else 0.5 for cid in games}
    pair_k = 20
    champ_aug_records = []
    for (cid, aid), g in ca_games.items():
        w = ca_wins[(cid, aid)]
        raw = w / g if g else 0.0
        baseline = raw_wr_by_champ.get(cid, 0.5)
        smoothed = (w + baseline * pair_k) / (g + pair_k)
        champ_aug_records.append({
            "champion_id": cid,
            "augment_id": aid,
            "games": g,
            "wins": w,
            "raw_wr": raw,
            "smoothed_wr": smoothed,
            "baseline_wr": baseline,
            "lift": smoothed - baseline,
        })

    return champ_records, champ_aug_records


RARITY_ORDER = ["kPrismatic", "kGold", "kSilver"]


def build_champ_augment_picks(
    champ_aug: list[dict],
    aug_meta: dict[int, dict],
    *,
    min_games_per_pair: int,
    top_n: int,
    bot_n: int,
) -> dict[int, dict]:
    """For each champion, pick top-N best and bot-N worst augments by smoothed WR,
    bucketed by rarity (Prismatic / Gold / Silver)."""
    by_champ_rarity: dict[int, dict[str, list[dict]]] = {}
    for row in champ_aug:
        if row["games"] < min_games_per_pair:
            continue
        meta = aug_meta.get(row["augment_id"])
        if meta is None:
            continue
        rarity = meta.get("rarity", "")
        if rarity not in RARITY_ORDER:
            continue
        bucket = by_champ_rarity.setdefault(
            row["champion_id"], {r: [] for r in RARITY_ORDER}
        )
        bucket[rarity].append(row)

    out: dict[int, dict] = {}
    for cid, buckets in by_champ_rarity.items():
        top, bot = {}, {}
        for rarity, rows in buckets.items():
            rows.sort(key=lambda r: -r["smoothed_wr"])
            top[rarity] = rows[:top_n]
            bot[rarity] = rows[-bot_n:][::-1] if rows else []
        out[cid] = {"top": top, "bot": bot}
    return out


def render_html(
    records: list[dict],
    champ_meta: dict[int, dict],
    champ_picks: dict[int, dict],
    aug_meta: dict[int, dict],
    *,
    queue_id: int,
    patch_prefix: str | None,
    ddragon_version: str,
    total_games: int,
    min_games_per_pair: int,
    site_url: str = "",
    og_image: str = "",
    build_date: str = "",
) -> str:
    # Group champions by tier
    by_tier: dict[str, list[dict]] = {t: [] for t in TIER_ORDER}
    for r in records:
        tier = assign_tier(r["bayes_wr"])
        meta = champ_meta.get(r["champion_id"])
        if meta is None:
            continue
        by_tier[tier].append({**r, **meta})

    # ARAM tier list is unambiguous in zh-Hant; queue 2400 was Mayhem's queueId
    # during the 16.x cycle.  Make the header explicit so people don't think
    # the data is for queue 450.
    if queue_id == 2400:
        header_title = "ARAM Mayhem 增幅勝率 Tier List"
        queue_label = "ARAM Mayhem (queueId 2400)"
    elif queue_id == 450:
        header_title = "ARAM 勝率 Tier List"
        queue_label = "ARAM (queueId 450)"
    else:
        header_title = f"Tier List (queueId {queue_id})"
        queue_label = f"queueId {queue_id}"
    patch_label = f"patch {patch_prefix}.*" if patch_prefix else "all patches"

    # Build the JS data payload. Keep it slim: only champs we render + their
    # picked augments (bucketed by rarity) + the augment metadata for ids that
    # actually appear.
    used_aug_ids: set[int] = set()
    js_champs: dict[str, dict] = {}

    def _pack(r: dict) -> dict:
        return {
            "id": r["augment_id"],
            "g": r["games"],
            "wr": round(r["smoothed_wr"], 4),
            "lift": round(r["lift"], 4),
        }

    for cid, picks in champ_picks.items():
        meta = champ_meta.get(cid)
        if meta is None:
            continue
        top_buckets = {}
        bot_buckets = {}
        for rarity in RARITY_ORDER:
            top_rows = picks["top"].get(rarity, [])
            bot_rows = picks["bot"].get(rarity, [])
            for r in top_rows + bot_rows:
                used_aug_ids.add(r["augment_id"])
            top_buckets[rarity] = [_pack(r) for r in top_rows]
            bot_buckets[rarity] = [_pack(r) for r in bot_rows]
        js_champs[str(cid)] = {
            "name": meta["name"],
            "alias": meta.get("alias", ""),
            "tags": meta.get("tags") or [],
            "top": top_buckets,
            "bot": bot_buckets,
        }
    js_augs = {
        str(aid): {
            "name": aug_meta[aid]["name"],
            "icon": aug_meta[aid]["icon"],
            "rarity": aug_meta[aid].get("rarity", ""),
            "desc": aug_meta[aid].get("desc", ""),
        }
        for aid in used_aug_ids
        if aid in aug_meta
    }

    css = """
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
        margin: 0;
        background: #0e1116;
        color: #e6e8eb;
        font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
        padding: 32px 24px 64px;
    }
    h1 { margin: 0 0 4px; font-weight: 600; font-size: 22px; }
    .subtitle { color: #9aa0a6; font-size: 13px; }
    /* Top header row — title on the left, GitHub star CTA on the right. */
    .page-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        margin-bottom: 16px;
    }
    .gh-star {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 12px;
        background: #21262d;
        color: #c9d1d9;
        border: 1px solid #30363d;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 500;
        text-decoration: none;
        white-space: nowrap;
        transition: background 0.12s, border-color 0.12s;
    }
    .gh-star:hover { background: #30363d; border-color: #58606b; }
    .gh-star svg { flex-shrink: 0; }
    /* Filter bar: role chips + free-text search + live count. */
    .filter-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
        margin: 0 0 20px;
        padding: 10px 12px;
        background: #161a22;
        border-radius: 10px;
    }
    .role-chips { display: flex; flex-wrap: wrap; gap: 6px; }
    .chip {
        padding: 5px 12px;
        background: #1f2530;
        color: #c5cad3;
        border: 1px solid transparent;
        border-radius: 18px;
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        font-family: inherit;
        transition: background 0.1s;
    }
    .chip:hover { background: #2a3142; }
    .chip.active {
        background: var(--role-color, #f5c518);
        color: #0e1116;
        border-color: var(--role-color, #f5c518);
    }
    .chip[data-role=""]              { --role-color: #f5c518; }
    .chip[data-role="Assassin"]      { --role-color: #ef4444; }
    .chip[data-role="Fighter"]       { --role-color: #f97316; }
    .chip[data-role="Mage"]          { --role-color: #3b82f6; }
    .chip[data-role="Marksman"]      { --role-color: #22c55e; }
    .chip[data-role="Support"]       { --role-color: #ec4899; }
    .chip[data-role="Tank"]          { --role-color: #a855f7; }
    .chip-reset {
        margin-left: 4px;
        padding: 5px 10px;
        background: transparent;
        color: #9aa0a6;
        border: 1px solid #30363d;
        border-radius: 6px;
        font-size: 12px;
        cursor: pointer;
        font-family: inherit;
    }
    .chip-reset:hover { color: #e6e8eb; border-color: #58606b; }
    .filter-tools {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-left: auto;
        flex: 1;
        justify-content: flex-end;
    }
    .search {
        flex: 1;
        max-width: 280px;
        min-width: 160px;
        padding: 6px 12px;
        background: #0e1116;
        color: #e6e8eb;
        border: 1px solid #30363d;
        border-radius: 6px;
        font-size: 13px;
        font-family: inherit;
        outline: none;
    }
    .search:focus { border-color: #58606b; }
    .shown-count { color: #9aa0a6; font-size: 12px; white-space: nowrap; }
    .tier-block { margin-bottom: 14px; }
    .tier-block.hidden { display: none; }
    /* Tier name on its own line above the grid (replaces the old left-side
       full-height ornament bar). */
    .tier-heading {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 8px 0;
        font-size: 14px;
        font-weight: 600;
    }
    .tier-pill {
        position: relative;
        overflow: hidden;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 4px 16px;
        border-radius: 6px;
        color: #0e1116;
        background: var(--tier-bg);
        font-size: 16px;
        font-weight: 700;
        text-shadow: 0 1px 0 rgba(255,255,255,0.25);
        letter-spacing: 0.3px;
    }
    .tier-pill > span { position: relative; z-index: 2; }
    .tier-count { color: #9aa0a6; font-size: 12px; font-weight: 400; }
    /* Prismatic / pearl shine for the OP tier — animated highlight sweep +
       outer halo glow, matching the iridescent augment-card look. */
    .tier-block[data-tier="OP"] .tier-pill {
        background-size: 200% 200%;
        animation: prismShift 6s ease-in-out infinite;
        box-shadow:
            0 0 12px rgba(220,180,255,0.55),
            0 0 28px rgba(170,210,255,0.30),
            inset 0 0 0 1px rgba(255,255,255,0.55);
        color: #2a1a4a;
        text-shadow: 0 1px 0 rgba(255,255,255,0.8);
    }
    .tier-block[data-tier="OP"] .tier-pill::before {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(115deg,
            transparent 35%,
            rgba(255,255,255,0.75) 50%,
            transparent 65%);
        background-size: 220% 100%;
        animation: shineSweep 3.2s linear infinite;
        z-index: 1;
        pointer-events: none;
    }
    @keyframes prismShift {
        0%   { background-position: 0% 50%; }
        50%  { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
    }
    @keyframes shineSweep {
        from { background-position: 220% 0; }
        to   { background-position: -120% 0; }
    }
    .tier-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
        gap: 10px;
    }
    .champ {
        position: relative;
        aspect-ratio: 1 / 1;
        border-radius: 8px;
        overflow: hidden;
        background: #1f2530;
        box-shadow: 0 0 0 1px rgba(255,255,255,0.05);
        cursor: pointer;
        transition: transform .08s, box-shadow .08s;
    }
    .champ:hover { transform: translateY(-1px); }
    .champ.selected {
        box-shadow: 0 0 0 2px var(--tier-color), 0 4px 12px rgba(0,0,0,0.5);
        transform: translateY(-2px);
    }
    .champ img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
    }
    .champ .badge {
        position: absolute;
        top: 2px;
        right: 2px;
        font-size: 10px;
        font-weight: 700;
        padding: 2px 5px;
        border-radius: 4px;
        color: #0e1116;
        background: var(--tier-bg);
        box-shadow: 0 1px 2px rgba(0,0,0,0.4);
        letter-spacing: 0.3px;
    }
    .tier-block[data-tier="OP"] .champ .badge {
        color: #2a1a4a;
        text-shadow: 0 1px 0 rgba(255,255,255,0.55);
        box-shadow:
            0 0 6px rgba(220,180,255,0.55),
            inset 0 0 0 1px rgba(255,255,255,0.55);
    }
    .champ.hidden { display: none; }
    .champ .wr {
        position: absolute;
        left: 2px;
        bottom: 2px;
        font-size: 10px;
        font-weight: 600;
        padding: 1px 4px;
        border-radius: 3px;
        color: #e6e8eb;
        background: rgba(14,17,22,0.78);
    }
    .champ .name {
        position: absolute;
        left: 0; right: 0; bottom: 0;
        padding: 2px 4px;
        font-size: 10px;
        text-align: center;
        background: linear-gradient(to top, rgba(0,0,0,0.85), rgba(0,0,0,0));
        color: #e6e8eb;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        pointer-events: none;
        opacity: 0;
        transition: opacity .15s;
    }
    .champ:hover .name { opacity: 1; }
    .detail-host {
        /* Sits inside .tier-grid; when populated, spans every grid column so
           it appears as a full-width row right after the clicked champion. */
        grid-column: 1 / -1;
    }
    .detail-host:empty { display: none; }
    .detail {
        margin: 6px 0 4px;
        background: #1b2030;
        border-radius: 10px;
        padding: 14px 16px 16px;
        animation: slideDown .18s ease-out;
    }
    @keyframes slideDown {
        from { opacity: 0; transform: translateY(-4px); }
        to { opacity: 1; transform: translateY(0); }
    }
    .detail-head {
        display: flex;
        align-items: baseline;
        gap: 10px;
        margin-bottom: 12px;
    }
    .detail-head .cname { font-size: 16px; font-weight: 600; }
    .detail-head .cmeta { font-size: 12px; color: #9aa0a6; }
    .detail-cols {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 18px;
    }
    .detail-col h3 {
        margin: 0 0 8px;
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }
    .detail-col.best h3 { color: #6bd16b; }
    .detail-col.worst h3 { color: #ff6b6b; }
    .rarity-row {
        display: grid;
        grid-template-columns: 56px 1fr;
        gap: 10px;
        align-items: start;
        margin-bottom: 10px;
    }
    .rlabel {
        font-size: 11px;
        font-weight: 700;
        padding: 5px 6px;
        border-radius: 5px;
        text-align: center;
        color: #0e1116;
        letter-spacing: 0.3px;
        align-self: stretch;
        display: flex;
        align-items: center;
        justify-content: center;
        position: relative;
        overflow: hidden;
    }
    .rlabel.prismatic {
        background: linear-gradient(135deg,#ffffff 0%,#e7d5ff 25%,#bcd6ff 50%,#ffd5ec 75%,#fff1c8 100%);
        background-size: 220% 220%;
        animation: prismShift 6s ease-in-out infinite;
        color: #2a1a4a;
        box-shadow: 0 0 6px rgba(220,180,255,0.5), inset 0 0 0 1px rgba(255,255,255,0.6);
    }
    .rlabel.gold     { background: linear-gradient(135deg,#ffe87a,#f5c518,#d99908); color: #3a2600; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.35); }
    .rlabel.silver   { background: linear-gradient(135deg,#eef0f4,#c0c5cc,#9aa0a6); color: #2a2e35; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.35); }
    .aug-list {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(86px, 1fr));
        gap: 10px;
    }
    .aug-list.empty-list { color: #6b7280; font-size: 11px; padding: 8px 0; }
    .aug {
        background: #11151d;
        border-radius: 8px;
        padding: 8px 6px;
        text-align: center;
        position: relative;
        border: 1px solid rgba(255,255,255,0.04);
    }
    .aug img {
        width: 48px; height: 48px;
        display: block;
        margin: 0 auto 4px;
        border-radius: 6px;
        background: #2a3142;
    }
    .aug .aname {
        font-size: 10px;
        color: #e6e8eb;
        line-height: 1.25;
        margin-bottom: 4px;
        min-height: 24px;
        overflow: hidden;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
    }
    .aug .awr {
        font-size: 11px;
        font-weight: 700;
    }
    .aug.good .awr { color: #6bd16b; }
    .aug.bad  .awr { color: #ff6b6b; }
    .aug .alift {
        font-size: 9px;
        color: #9aa0a6;
        margin-top: 1px;
    }
    /* Custom hover popup with augment description.  Native title is kept too
       as an accessibility/fallback path. */
    .aug-tip {
        position: absolute;
        left: 50%;
        bottom: calc(100% + 8px);
        transform: translateX(-50%);
        width: 220px;
        padding: 8px 10px;
        background: #0b0e13;
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 8px;
        box-shadow: 0 6px 18px rgba(0,0,0,0.55);
        color: #e6e8eb;
        font-size: 11px;
        line-height: 1.45;
        text-align: left;
        z-index: 50;
        pointer-events: none;
        opacity: 0;
        transition: opacity 0.12s ease-out;
    }
    .aug-tip::after {
        content: "";
        position: absolute;
        top: 100%;
        left: 50%;
        transform: translateX(-50%);
        border: 6px solid transparent;
        border-top-color: #0b0e13;
    }
    .aug:hover .aug-tip { opacity: 1; }
    .aug-tip-name {
        font-weight: 700;
        font-size: 12px;
        margin-bottom: 4px;
        color: #f5d780;
    }
    .aug-tip-desc {
        color: #c5cad3;
        margin-bottom: 6px;
        white-space: normal;
    }
    .aug-tip-stat {
        color: #9aa0a6;
        font-size: 10px;
        border-top: 1px solid rgba(255,255,255,0.08);
        padding-top: 4px;
    }
    .aug.rarity-kGold   { box-shadow: inset 0 0 0 2px #f5c518; }
    .aug.rarity-kSilver { box-shadow: inset 0 0 0 2px #c0c5cc; }
    .aug.rarity-kPrismatic { box-shadow: inset 0 0 0 2px #d36bff; }
    .empty { color: #6b7280; font-size: 12px; }
    .footer {
        margin-top: 24px;
        color: #6b7280;
        font-size: 11px;
        text-align: center;
        line-height: 1.6;
    }
    .footer .disclaimer {
        max-width: 760px;
        margin: 12px auto 0;
        color: #555a63;
        font-size: 10px;
    }
    /* Mobile / narrow viewport: switch the detail panel from two columns
       (best / worst) to a single stack so prismatic / gold / silver rows
       stay readable, and shrink the tier-row label so champions get more
       space.  ~700px is around where the two-column layout starts looking
       cramped on most phones. */
    @media (max-width: 700px) {
        body { padding: 18px 10px 40px; }
        h1 { font-size: 18px; }
        .subtitle { font-size: 12px; }
        /* Header stacks: title row, then GitHub button below at full width. */
        .page-header { flex-direction: column; gap: 8px; margin-bottom: 12px; }
        .gh-star { align-self: flex-start; }
        /* Filter bar wraps tighter; search input becomes full-width on
           its own row. */
        .filter-bar { padding: 8px; gap: 8px; }
        .role-chips { gap: 4px; }
        .chip { padding: 4px 10px; font-size: 11px; }
        .filter-tools {
            margin-left: 0;
            width: 100%;
            justify-content: space-between;
        }
        .search { max-width: none; min-width: 0; }
        /* Tier heading slimmer; pill stays inline. */
        .tier-heading { margin: 6px 0; gap: 6px; }
        .tier-pill { padding: 3px 12px; font-size: 14px; }
        .tier-count { font-size: 11px; }
        /* Lock to 6 champions per row on mobile (instead of auto-fill which
           packs 7-8 in and makes icons tiny). */
        .tier-grid { grid-template-columns: repeat(6, 1fr); gap: 5px; }
        /* OP / T1 / ... badges on champion thumbnails are noisy on mobile;
           the tier is already implied by the pill above the row. */
        .champ .badge { display: none; }
        .detail-cols { grid-template-columns: 1fr; gap: 14px; }
        /* Drop the rarity colored bar (label) on mobile to recover horizontal
           space.  Each augment card still has a rarity-coloured border, so
           which row is which is obvious. */
        .rarity-row { grid-template-columns: 1fr; gap: 4px; }
        .rlabel { display: none; }
        /* Each rarity row shows exactly the same 5 augments (top / bot),
           so force 5 columns and let each card shrink to fit. */
        .aug-list { grid-template-columns: repeat(5, 1fr); gap: 4px; }
        .aug { padding: 5px 3px; }
        .aug img { width: 36px; height: 36px; }
        .aug .aname { font-size: 9px; min-height: 22px; }
        .aug .awr { font-size: 10px; }
        /* Hide the lift% / games count on mobile — keep cards compact.
           Numbers still available on hover (tooltip) and via the title attr. */
        .aug .alift { display: none; }
        .aug-tip { width: 170px; font-size: 10px; }
    }
    """

    payload = {
        "champs": js_champs,
        "augs": js_augs,
        "min_games_per_pair": min_games_per_pair,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    # Pick a default OG image: highest-WR champion's icon — gives Discord /
    # Twitter / LINE a recognizable preview without us hosting an image.
    if not og_image and records:
        top_meta = champ_meta.get(records[0]["champion_id"])
        if top_meta:
            og_image = top_meta["image"]

    og_title = f"{header_title} ({patch_label}, {total_games:,} 場樣本)"
    og_desc = (
        f"基於 {total_games:,} 場 LCU 抓取的 {queue_label} 對戰，"
        "每位英雄分別給出最佳 / 最差的 彩色 / 金色 / 銀色 augment（含中文效果說明）。"
    )

    meta_lines: list[str] = []
    meta_lines.append("<meta charset='utf-8'>")
    meta_lines.append(
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    )
    meta_lines.append(f"<title>{og_title}</title>")
    meta_lines.append(f"<meta name='description' content=\"{og_desc}\">")
    if site_url:
        meta_lines.append(f"<link rel='canonical' href='{site_url}'>")
        meta_lines.append(f"<meta property='og:url' content='{site_url}'>")
    meta_lines.append("<meta property='og:type' content='website'>")
    meta_lines.append(f"<meta property='og:title' content=\"{og_title}\">")
    meta_lines.append(f"<meta property='og:description' content=\"{og_desc}\">")
    if og_image:
        meta_lines.append(f"<meta property='og:image' content='{og_image}'>")
        meta_lines.append("<meta name='twitter:card' content='summary_large_image'>")
        meta_lines.append(f"<meta name='twitter:image' content='{og_image}'>")
    else:
        meta_lines.append("<meta name='twitter:card' content='summary'>")
    meta_lines.append(f"<meta name='twitter:title' content=\"{og_title}\">")
    meta_lines.append(f"<meta name='twitter:description' content=\"{og_desc}\">")

    parts: list[str] = []
    parts.append("<!doctype html><html lang='zh-Hant'><head>")
    parts.extend(meta_lines)
    parts.append(f"<style>{css}</style></head><body>")
    # Header: title + subtitle on the left, "Star on GitHub" CTA on the right.
    # The repo name is the canonical project URL; if the user later forks /
    # renames, update REPO_URL below.
    REPO_URL = "https://github.com/Lanternko/ARAM-Mayhem-Database"
    short_patch = f"patch {patch_prefix}" if patch_prefix else "全 patch"
    date_str = f"更新於 {build_date}" if build_date else "日期未標"
    gh_icon = (
        "<svg viewBox='0 0 16 16' width='14' height='14' fill='currentColor' "
        "aria-hidden='true'><path d='M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1"
        "-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1."
        "23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-."
        "2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0"
        "-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12"
        "-.51.56-.82 1.27-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-"
        ".51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.2"
        "7.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.0"
        "1 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8"
        "Z'></path></svg>"
    )
    parts.append("<div class='page-header'>")
    parts.append("<div>")
    parts.append(f"<h1>{header_title}</h1>")
    parts.append(
        f"<div class='subtitle'>"
        f"{short_patch} · {date_str} ({total_games:,} games) · "
        "點擊英雄展開 augment"
        f"</div>"
    )
    parts.append("</div>")
    parts.append(
        f"<a class='gh-star' href='{REPO_URL}' target='_blank' rel='noopener' "
        f"title='覺得有用請幫忙按 Star ⭐'>"
        f"{gh_icon} ⭐ Star on GitHub"
        f"</a>"
    )
    parts.append("</div>")  # /page-header

    # Filter bar: role chips + free-text search + live "N shown" counter.
    parts.append("<div class='filter-bar'>")
    parts.append("<div class='role-chips'>")
    parts.append('<button class="chip active" data-role="">★ All</button>')
    for role_en, role_zh in [
        ("Assassin", "刺客"),
        ("Fighter", "戰士"),
        ("Mage", "法師"),
        ("Marksman", "射手"),
        ("Support", "輔助"),
        ("Tank", "坦克"),
    ]:
        parts.append(
            f'<button class="chip" data-role="{role_en}">{role_zh}</button>'
        )
    parts.append('<button class="chip-reset" id="reset-filters">重設</button>')
    parts.append("</div>")  # /role-chips
    parts.append("<div class='filter-tools'>")
    parts.append(
        '<input class="search" id="champ-search" type="search" '
        'placeholder="搜尋英雄（中 / 英 / 角色）…" autocomplete="off" '
        'aria-label="搜尋英雄">'
    )
    parts.append(
        f'<span class="shown-count"><span id="shown-n">{len(records)}</span> 隻顯示</span>'
    )
    parts.append("</div>")  # /filter-tools
    parts.append("</div>")  # /filter-bar

    for tier in TIER_ORDER:
        entries = by_tier[tier]
        if not entries:
            continue
        entries.sort(key=lambda d: -d["bayes_wr"])
        color = TIER_COLOR[tier]
        bg = TIER_LABEL_BG[tier]
        parts.append(
            f"<div class='tier-block' data-tier='{tier}' "
            f"style='--tier-color:{color}; --tier-bg:{bg};'>"
        )
        # New layout: tier name on its own heading row (no side bar), grid
        # takes the full row below.  Same look on desktop + mobile.
        parts.append("<h2 class='tier-heading'>")
        parts.append(f"<span class='tier-pill'><span>{tier}</span></span>")
        parts.append(
            f"<span class='tier-count'>"
            f"<span class='tier-count-num' data-tier='{tier}'>{len(entries)}</span>"
            " 隻"
            "</span>"
        )
        parts.append("</h2>")
        parts.append("<div class='tier-grid'>")
        for r in entries:
            wr_pct = f"{r['bayes_wr'] * 100:.1f}%"
            meta = champ_meta.get(r["champion_id"], {})
            tag_str = " ".join(meta.get("tags") or [])
            alias = meta.get("alias", "")
            search_blob = f"{r['name']} {alias} {tag_str}".lower()
            title = (
                f"{r['name']} · WR {wr_pct} · games {r['games']:,} · "
                f"raw {r['raw_wr']*100:.1f}%"
            )
            parts.append(
                f"<div class='champ' data-cid='{r['champion_id']}' "
                f"data-tags='{tag_str}' data-search=\"{search_blob}\" "
                f"title=\"{title}\">"
                f"<img loading='lazy' src='{r['image']}' alt='{r['name']}'>"
                f"<span class='badge'>{tier}</span>"
                f"<span class='wr'>{wr_pct}</span>"
                f"<span class='name'>{r['name']}</span>"
                f"</div>"
            )
        # Detail host lives INSIDE .tier-grid so it can grid-span all columns
        # and be inserted right after the clicked champion's visual row.
        parts.append(f"<div class='detail-host' data-tier='{tier}'></div>")
        parts.append("</div>")  # /tier-grid
        parts.append("</div>")  # /tier-block

    parts.append("<div class='footer'>")
    parts.append(
        "Tier cutoffs (Bayes WR): OP ≥ 55% · T1 ≥ 52% · T2 ≥ 50% · "
        "T3 ≥ 48% · T4 ≥ 46% · T5 &lt; 46%"
    )
    if build_date:
        parts.append(f"<br>資料截至 {build_date}（{patch_label}）")
    parts.append(
        "<div class='disclaimer'>"
        "This site isn't endorsed by Riot Games and doesn't reflect the views "
        "or opinions of Riot Games or anyone officially involved in producing "
        "or managing League of Legends. League of Legends and Riot Games are "
        "trademarks or registered trademarks of Riot Games, Inc. "
        "League of Legends © Riot Games, Inc."
        "</div>"
    )
    parts.append("</div>")

    js = """
    const DATA = __PAYLOAD__;
    const pct = x => (x * 100).toFixed(1) + '%';
    const signed = x => (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%';
    const escHtml = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    function buildAugCard(entry, kind) {
        const aug = DATA.augs[entry.id];
        const name = aug ? aug.name : '#' + entry.id;
        const icon = aug && aug.icon ? aug.icon : '';
        const rarity = aug ? (aug.rarity || '') : '';
        const desc = aug && aug.desc ? aug.desc : '';
        const titleAttr = `${name} · WR ${pct(entry.wr)} · ${entry.g}場${desc ? ' — ' + desc : ''}`;
        const tooltip = `
            <div class="aug-tip">
                <div class="aug-tip-name">${escHtml(name)}</div>
                ${desc ? `<div class="aug-tip-desc">${escHtml(desc)}</div>` : ''}
                <div class="aug-tip-stat">WR ${pct(entry.wr)} · ${signed(entry.lift)} · ${entry.g}場</div>
            </div>
        `;
        return `
            <div class="aug ${kind} rarity-${rarity}" title="${escHtml(titleAttr)}">
                ${icon ? `<img loading="lazy" src="${icon}" alt="${escHtml(name)}">` : '<div style="width:48px;height:48px;margin:0 auto 4px;background:#2a3142;border-radius:6px"></div>'}
                <div class="aname">${escHtml(name)}</div>
                <div class="awr">${pct(entry.wr)}</div>
                <div class="alift">${signed(entry.lift)} · ${entry.g}場</div>
                ${tooltip}
            </div>
        `;
    }

    const RARITIES = [
        { key: 'kPrismatic', label: '彩色', css: 'prismatic' },
        { key: 'kGold',      label: '金色', css: 'gold' },
        { key: 'kSilver',    label: '銀色', css: 'silver' },
    ];

    function buildRarityRow(items, kind, r) {
        const cards = (items || []).map(e => buildAugCard(e, kind)).join('');
        const body = cards
            ? `<div class="aug-list">${cards}</div>`
            : `<div class="aug-list empty-list">資料不足</div>`;
        return `
            <div class="rarity-row">
                <div class="rlabel ${r.css}">${r.label}</div>
                ${body}
            </div>
        `;
    }

    function renderDetail(cid) {
        const info = DATA.champs[cid];
        if (!info) {
            return `<div class="empty">該英雄沒有 augment 資料（每組需 >= ${DATA.min_games_per_pair} 場）。</div>`;
        }
        const top = info.top || {};
        const bot = info.bot || {};
        const topRows = RARITIES.map(r => buildRarityRow(top[r.key], 'good', r)).join('');
        const botRows = RARITIES.map(r => buildRarityRow(bot[r.key], 'bad', r)).join('');
        return `
            <div class="detail-head">
                <span class="cname">${info.name}</span>
                <span class="cmeta">每種稀有度各取最佳 / 最差 5 個</span>
            </div>
            <div class="detail-cols">
                <div class="detail-col best">
                    <h3>最佳</h3>
                    ${topRows}
                </div>
                <div class="detail-col worst">
                    <h3>最差</h3>
                    ${botRows}
                </div>
            </div>
        `;
    }

    let selected = null;

    // Find the last .champ in the same visual row as `clicked` (same offsetTop).
    // Tier-grid is a CSS grid so offsetTop tells us the row reliably across
    // viewport widths.
    function lastChampInRow(clicked) {
        const grid = clicked.parentElement;
        const topPx = clicked.offsetTop;
        const champs = grid.querySelectorAll(':scope > .champ');
        let last = clicked;
        for (const c of champs) {
            if (Math.abs(c.offsetTop - topPx) < 2) last = c;
        }
        return last;
    }

    document.addEventListener('click', (ev) => {
        const champ = ev.target.closest('.champ');
        if (!champ) return;
        const cid = champ.getAttribute('data-cid');
        const block = champ.closest('.tier-block');
        const grid  = block.querySelector('.tier-grid');
        const host  = block.querySelector('.detail-host');

        // Clear any previously selected highlight + detail elsewhere.
        document.querySelectorAll('.champ.selected').forEach(el => {
            if (el !== champ) el.classList.remove('selected');
        });
        document.querySelectorAll('.detail-host').forEach(el => {
            if (el !== host) el.innerHTML = '';
        });

        if (selected === cid && host.firstChild) {
            host.innerHTML = '';
            champ.classList.remove('selected');
            selected = null;
            return;
        }

        // Position the detail host right after the last champ in the clicked
        // row, so the panel always pops up directly under the champion you
        // tapped — never hidden far below by other champs.
        const anchor = lastChampInRow(champ);
        if (anchor.nextSibling !== host) {
            anchor.after(host);
        }

        host.innerHTML = `<div class="detail">${renderDetail(cid)}</div>`;
        champ.classList.add('selected');
        selected = cid;
    });

    // When viewport width changes, the row containing the selected champ
    // shifts — re-anchor the detail host so it stays directly under that
    // champ on the new layout.
    let resizeT = null;
    window.addEventListener('resize', () => {
        if (!selected) return;
        clearTimeout(resizeT);
        resizeT = setTimeout(() => {
            const champ = document.querySelector(`.champ[data-cid="${selected}"].selected`);
            if (!champ) return;
            const host = champ.closest('.tier-block').querySelector('.detail-host');
            const anchor = lastChampInRow(champ);
            if (anchor.nextSibling !== host) anchor.after(host);
        }, 120);
    });

    /* -----  Filter / search  --------------------------------------- */

    const filterState = { role: '', q: '' };

    function applyFilters() {
        const role = filterState.role;
        const q = filterState.q.trim().toLowerCase();
        let shown = 0;
        document.querySelectorAll('.tier-block').forEach(block => {
            let tierShown = 0;
            const champs = block.querySelectorAll(':scope > .tier-grid > .champ');
            champs.forEach(c => {
                const tags = (c.getAttribute('data-tags') || '').split(' ');
                const blob = c.getAttribute('data-search') || '';
                const matchRole = !role || tags.includes(role);
                const matchQ = !q || blob.includes(q);
                const hide = !(matchRole && matchQ);
                c.classList.toggle('hidden', hide);
                if (!hide) tierShown++;
            });
            // Update tier count number
            const tier = block.getAttribute('data-tier');
            const numEl = block.querySelector(`.tier-count-num[data-tier="${tier}"]`);
            if (numEl) numEl.textContent = tierShown;
            // Hide whole tier-block when empty
            block.classList.toggle('hidden', tierShown === 0);
            shown += tierShown;
        });
        const shownN = document.getElementById('shown-n');
        if (shownN) shownN.textContent = shown;

        // If the currently-selected champ got hidden, close its detail panel.
        if (selected) {
            const sel = document.querySelector(`.champ[data-cid="${selected}"].selected`);
            if (!sel || sel.classList.contains('hidden')) {
                document.querySelectorAll('.detail-host').forEach(h => h.innerHTML = '');
                document.querySelectorAll('.champ.selected').forEach(el => el.classList.remove('selected'));
                selected = null;
            }
        }
    }

    function setActiveChip(role) {
        document.querySelectorAll('.chip').forEach(chip => {
            chip.classList.toggle('active', chip.getAttribute('data-role') === role);
        });
    }

    // Role chip clicks (event delegation).
    document.addEventListener('click', (ev) => {
        const chip = ev.target.closest('.chip');
        if (chip) {
            filterState.role = chip.getAttribute('data-role') || '';
            setActiveChip(filterState.role);
            applyFilters();
            return;
        }
        if (ev.target.id === 'reset-filters') {
            filterState.role = '';
            filterState.q = '';
            setActiveChip('');
            const s = document.getElementById('champ-search');
            if (s) s.value = '';
            applyFilters();
        }
    });

    // Live search.
    const searchEl = document.getElementById('champ-search');
    if (searchEl) {
        searchEl.addEventListener('input', () => {
            filterState.q = searchEl.value || '';
            applyFilters();
        });
    }
    """
    js = js.replace("__PAYLOAD__", payload_json)
    parts.append(f"<script>{js}</script>")
    parts.append("</body></html>")
    return "".join(parts)


@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/lcu/games.db"))
@click.option("--queue", "queue_id", type=int, default=2400, help="450=ARAM, 2400=Mayhem")
@click.option("--patch-prefix", default="16.10", help='e.g. "16.10" or "" for all patches')
@click.option("--ddragon-version", default=None, help="Override Data Dragon version (default: latest)")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=Path("docs/index.html"),
              help="Output HTML path (default: docs/index.html — the only non-root folder GitHub Pages serves from)")
@click.option("--min-games", type=int, default=50, help="Drop champions below this game count")
@click.option("--min-pair-games", type=int, default=15, help="Min games per (champ, augment) pair")
@click.option("--top-n", type=int, default=5)
@click.option("--bot-n", type=int, default=5)
@click.option("--site-url", default="",
              help="Canonical URL (used for OG og:url + <link rel=canonical>), e.g. https://user.github.io/repo/")
@click.option("--og-image", default="",
              help="Override the og:image URL (default: top champion's icon)")
@click.option("--build-date", default="",
              help="Date stamp shown in footer (default: today, YYYY-MM-DD)")
def main(
    db: Path,
    queue_id: int,
    patch_prefix: str,
    ddragon_version: str | None,
    out_path: Path,
    min_games: int,
    min_pair_games: int,
    top_n: int,
    bot_n: int,
    site_url: str,
    og_image: str,
    build_date: str,
) -> None:
    patch_prefix = patch_prefix or None
    click.echo(f"[tierlist] db={db}  queue={queue_id}  patch_prefix={patch_prefix}")

    champ_records, champ_aug = compute_winrates(db, queue_id, patch_prefix)
    total_games = sum(r["games"] for r in champ_records) // 10
    champ_records = [r for r in champ_records if r["games"] >= min_games]
    click.echo(f"[tierlist] {len(champ_records)} champions after min_games={min_games}")
    click.echo(f"[tierlist] {len(champ_aug):,} (champ, augment) pairs total")

    version, champ_meta = load_champion_metadata(ddragon_version)
    click.echo(f"[tierlist] data dragon version: {version}")

    aug_meta = load_augment_metadata(cache_dir=Path("data/cache"))
    desc_n = sum(1 for v in aug_meta.values() if v.get("desc"))
    click.echo(
        f"[tierlist] augment catalogue: {len(aug_meta)} entries "
        f"({desc_n} with zh-TW description)"
    )

    picks = build_champ_augment_picks(
        champ_aug,
        aug_meta,
        min_games_per_pair=min_pair_games,
        top_n=top_n,
        bot_n=bot_n,
    )
    click.echo(
        f"[tierlist] {len(picks)} champions have >= 1 rarity-bucketed pair "
        f"(games >= {min_pair_games})"
    )

    if not build_date:
        build_date = _dt.date.today().isoformat()

    html = render_html(
        champ_records,
        champ_meta,
        picks,
        aug_meta,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
        ddragon_version=version,
        total_games=total_games,
        min_games_per_pair=min_pair_games,
        site_url=site_url,
        og_image=og_image,
        build_date=build_date,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    click.echo(f"[tierlist] wrote {out_path}  ({out_path.stat().st_size:,} bytes)")

    # GitHub Pages: prevent Jekyll preprocessing (we don't have any _-prefixed
    # files today, but adding the marker keeps it that way as we evolve).
    nojekyll = out_path.parent / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.write_text("", encoding="utf-8")
        click.echo(f"[tierlist] wrote {nojekyll}")


if __name__ == "__main__":
    main()
