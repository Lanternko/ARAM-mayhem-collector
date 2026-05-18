"""Generate a tier-list HTML from Mayhem (or ARAM) winrates.

Reads winrates from data/lcu/games.db, fetches champion id->name mapping from
Riot's Data Dragon CDN, applies Bayesian smoothing, and renders an HTML grid
where each champion icon carries a tier badge (OP / T1..T5) in the top-right.

Clicking a champion expands an inline panel below its tier-row showing the
top-5 best and bottom-5 worst augments (by empirical-Bayes lower-bound lift;
peer-relative pick-rate is kept as diagnostics), plus best/worst same-team
teammate synergies.  A right-side panel also lets users pick 1-4 champions and
        rank recommended teammates by aggregated anchor-conditional synergy.

Usage:
    python scripts/build_tier_list.py
    python scripts/build_tier_list.py --queue 2400 --patch-prefix 16.10 --out tier_list.html
    python scripts/build_tier_list.py --queue 450  --patch-prefix 16.9
"""
from __future__ import annotations

import datetime as _dt
import csv
import html
from io import BytesIO
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import click
import httpx

try:
    from scipy.optimize import minimize_scalar
    from scipy.special import betaln, betaincinv
except Exception:  # pragma: no cover - scipy is installed through sklearn locally.
    minimize_scalar = None
    betaln = None
    betaincinv = None


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
AUGMENT_PRIOR_DEFAULT = 350.0
AUGMENT_POSTERIOR_Q = 0.10
AUGMENT_LCB_Z = 1.2815515655446004
AUGMENT_PICK_LIFT_WEIGHT = 0.0
AUGMENT_PICK_LIFT_CAP = 3.0
EMPIRICAL_CHAMPION_SCORES = Path("data/cache/champion_scores_empirical_merged.csv")
ITEM_MIN_TOTAL_GOLD = 1800
CATEGORY_PRIOR_DEFAULT = AUGMENT_PRIOR_DEFAULT
ITEM_STYLE_MIN_GAMES = 150
ITEM_STYLE_FALLBACK_MIN_GAMES = 100
AUGMENT_TYPE_MIN_GAMES = 100

ITEM_STYLE_LABELS = {
    "ap_burn": {"zh": "AP燃燒", "en": "AP burn"},
    "ap_burst": {"zh": "AP爆發", "en": "AP burst"},
    "ap_bruiser": {"zh": "法坦", "en": "AP bruiser"},
    "ap_onhit": {"zh": "混傷命中", "en": "Hybrid on-hit"},
    "ad_bruiser": {"zh": "AD鬥士", "en": "AD bruiser"},
    "ad_assassin": {"zh": "物穿刺客", "en": "Lethality assassin"},
    "ad_poke": {"zh": "AD poke", "en": "AD poke"},
    "crit": {"zh": "暴擊", "en": "Crit"},
    "onhit": {"zh": "攻速命中", "en": "AS / on-hit"},
    "tank": {"zh": "坦克", "en": "Tank"},
    "support": {"zh": "輔助", "en": "Support"},
}

AP_BURN_ITEM_KEYWORDS = (
    "liandry",
    "blackfire torch",
    "demonic embrace",
    "malignance",
    "pyromancer",
)

AP_BRUISER_ITEM_KEYWORDS = (
    "abyssal mask",
    "banshee",
    "bloodletter",
    "cosmic drive",
    "crown of the shattered queen",
    "cruelty",
    "demon king",
    "everfrost",
    "innervating locket",
    "lightning braid",
    "moonflair",
    "morellonomicon",
    "riftmaker",
    "rod of ages",
    "rylai",
    "sanguine gift",
    "twin mask",
    "twilight's edge",
    "zhonya",
)

AP_BURST_ITEM_KEYWORDS = (
    "actualizer",
    "archangel",
    "cryptbloom",
    "deathfire",
    "detonation orb",
    "flesheater",
    "hextech gunblade",
    "hextech rocketbelt",
    "horizon focus",
    "luden",
    "night harvester",
    "perplexity",
    "rabadon",
    "runecarver",
    "seraph",
    "shadowflame",
    "stormsurge",
    "void staff",
    "wooglet",
    "wordless promise",
)

AP_ONHIT_ITEM_KEYWORDS = (
    "dusk and dawn",
    "guinsoo",
    "lich bane",
    "nashor",
    "reality fracture",
    "reaper's toll",
    "statikk",
)

SUPPORT_ITEM_KEYWORDS = (
    "ardent",
    "chemtech putrifier",
    "dawncore",
    "echoes of helia",
    "empirean promise",
    "imperial mandate",
    "locket",
    "mikael",
    "moonstone",
    "puppeteer",
    "redemption",
    "shurelya",
    "staff of flowing",
    "sword of blossoming dawn",
)

AD_POKE_ITEM_KEYWORDS = (
    "bastionbreaker",
    "diamond-tipped spear",
    "hellfire hatchet",
    "manamune",
    "muramana",
    "serylda",
)

AD_ASSASSIN_ITEM_KEYWORDS = (
    "axiom arc",
    "duskblade",
    "edge of night",
    "gambler's blade",
    "hubris",
    "opportunity",
    "profane hydra",
    "prowler",
    "regicide",
    "serpent",
    "spectral cutlass",
    "umbral glaive",
    "voltaic",
    "youmuu",
)

AD_BRUISER_ITEM_KEYWORDS = (
    "black cleaver",
    "bloodthirster",
    "blade of the ruined king",
    "chempunk",
    "death's dance",
    "divine sunderer",
    "eclipse",
    "endless hunger",
    "experimental hexplate",
    "frozen mallet",
    "goredrinker",
    "guardian angel",
    "hemomancer",
    "hullbreaker",
    "innervating locket",
    "maw of malmortius",
    "mercurial scimitar",
    "overlord",
    "ravenous hydra",
    "sanguine blade",
    "shield of the rakkor",
    "silvermere dawn",
    "spear of shojin",
    "sterak",
    "stridebreaker",
    "sundered sky",
    "titanic hydra",
    "trinity force",
)

AUGMENT_TYPE_LABELS = {
    "damage": {"zh": "傷害", "en": "Damage"},
    "spell": {"zh": "技能 / AP", "en": "Spell / AP"},
    "attack": {"zh": "普攻 / AD", "en": "Attack / AD"},
    "crit": {"zh": "暴擊", "en": "Crit"},
    "tank": {"zh": "坦克", "en": "Tank"},
    "sustain": {"zh": "治療護盾", "en": "Heal / Shield"},
    "mobility": {"zh": "機動進場", "en": "Mobility"},
    "snowball": {"zh": "雪球", "en": "Snowball"},
    "economy": {"zh": "經濟", "en": "Economy"},
    "stacking": {"zh": "疊層成長", "en": "Stacking"},
    "utility": {"zh": "控制輔助", "en": "Utility"},
    "auto": {"zh": "自動觸發", "en": "Automated"},
}

MAYHEM_AUGMENT_SETS = {
    "Archmage": [
        "Buff Buddies",
        "Juiced",
        "Mind to Matter",
        "Ocean Soul",
        "Overflow",
    ],
    "Dive Bomb": [
        "Clown College",
        "Dive Bomber",
        "Final City Transit",
        "Self Destruct",
    ],
    "Firecracker": [
        "Critical Missile",
        "Fan the Hammer",
        "Light 'em Up!",
        "Magic Missile",
        "Twin Fire",
        "Typhoon",
    ],
    "Fully Automated": [
        "Divine Intervention",
        "Firefox",
        "Frost Wraith",
        "OK Boomerang",
        "Prom Queen",
        "Quantum Computing",
        "Self Destruct",
        "Sonata",
    ],
    "High Roller": [
        "Pandora's Box",
        "Stats!",
        "Stats on Stats!",
        "Stats on Stats on Stats!",
        "Transmute: Chaos",
        "Transmute: Gold",
        "Transmute: Prismatic",
    ],
    "Make it Rain": [
        "Donation",
        "From Beginning to End",
        "Goldrend",
        "Heads Up Cupcake!",
        "Red Envelopes",
        "Upgrade: Collector",
        "Upgrade: Immolate",
    ],
    "Snowday": [
        "Biggest Snowball Ever",
        "Holy Snowball",
        "Pinball",
        "Snowball Roulette",
        "Snowball Upgrade",
    ],
    "Stackosaurus Rex": [
        "Infinite Recursion",
        "Master of Duality",
        "Phenomenal Evil",
        "Quest: Steel Your Heart",
        "Shrink Engine",
        "Slap Around",
        "Soul Eater",
        "Tap Dancer",
        "Upgrade: Hubris",
    ],
    "Wee Woo Wee Woo": [
        "All For You",
        "Critical Healing",
        "First-Aid Kit",
        "I'm a Baby Kitty Where is Mama",
        "Sonata",
        "Upgrade Mikael's Blessing",
        "Windspeaker's Blessing",
    ],
}

MAYHEM_AUGMENT_SET_LABELS = {
    "Archmage": {"zh": "大法師", "en": "Archmage"},
    "Dive Bomb": {"zh": "俯衝轟炸", "en": "Dive Bomb"},
    "Firecracker": {"zh": "爆竹", "en": "Firecracker"},
    "Fully Automated": {"zh": "全自動", "en": "Fully Automated"},
    "High Roller": {"zh": "豪賭", "en": "High Roller"},
    "Make it Rain": {"zh": "天降財雨", "en": "Make it Rain"},
    "Snowday": {"zh": "雪球日", "en": "Snowday"},
    "Stackosaurus Rex": {"zh": "疊疊暴龍", "en": "Stackosaurus Rex"},
    "Wee Woo Wee Woo": {"zh": "警笛大響", "en": "Wee Woo Wee Woo"},
}


def render_analytics_tags(
    *,
    cloudflare_token: str = "",
    ga_measurement_id: str = "",
) -> list[str]:
    tags: list[str] = []
    cloudflare_token = cloudflare_token.strip()
    ga_measurement_id = ga_measurement_id.strip()

    if cloudflare_token:
        cf_config = html.escape(json.dumps({"token": cloudflare_token}), quote=True)
        tags.append(
            "<script defer src='https://static.cloudflareinsights.com/beacon.min.js' "
            f"data-cf-beacon='{cf_config}'></script>"
        )

    if ga_measurement_id:
        ga_id = html.escape(ga_measurement_id, quote=True)
        ga_id_js = json.dumps(ga_measurement_id)
        tags.append(
            f"<script async src='https://www.googletagmanager.com/gtag/js?id={ga_id}'></script>"
            "<script>"
            "window.dataLayer=window.dataLayer||[];"
            "function gtag(){dataLayer.push(arguments);}"
            "gtag('js',new Date());"
            f"gtag('config',{ga_id_js});"
            "</script>"
        )

    return tags


def _slugify_set_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _normalize_augment_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _augment_set_lookup() -> dict[str, list[dict[str, str]]]:
    lookup: dict[str, list[dict[str, str]]] = {}
    for set_name, aug_names in MAYHEM_AUGMENT_SETS.items():
        slug = _slugify_set_name(set_name)
        labels = MAYHEM_AUGMENT_SET_LABELS.get(
            set_name,
            {"zh": set_name, "en": set_name},
        )
        for aug_name in aug_names:
            info = {
                "name": set_name,
                "name_zh": labels["zh"],
                "name_en": labels["en"],
                "slug": slug,
            }
            lookup.setdefault(_normalize_augment_name(aug_name), []).append(info)
            if aug_name.startswith("Upgrade: "):
                lookup.setdefault(
                    _normalize_augment_name(aug_name.replace("Upgrade: ", "Upgrade ")),
                    [],
                ).append(info)
    return lookup


def _queue_copy(queue_id: int) -> tuple[str, str]:
    # queue 2400 was Mayhem's queueId during the 16.x cycle.
    if queue_id == 2400:
        return "ARAM 大亂鬥", "ARAM Mayhem (queueId 2400)"
    if queue_id == 450:
        return "ARAM 勝率 Tier List", "ARAM (queueId 450)"
    return f"Tier List (queueId {queue_id})", f"queueId {queue_id}"


def _load_font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        Path("C:/Windows/Fonts/msjhbd.ttc" if bold else "C:/Windows/Fonts/msjh.ttc"),
        Path("C:/Windows/Fonts/NotoSansTC-Bold.otf" if bold else "C:/Windows/Fonts/NotoSansTC-Regular.otf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _draw_text_fit(draw, xy: tuple[int, int], text: str, font, fill: str, max_width: int) -> None:
    # Pillow can hang measuring some CJK fonts on Windows, so keep this
    # deliberately simple for the fixed-size OG canvas.
    char_budget = max(8, max_width // 20)
    if len(text) > char_budget:
        text = text[: char_budget - 3].rstrip() + "..."
    draw.text(xy, text, font=font, fill=fill)


def _draw_prismatic_frame(img, box: tuple[int, int, int, int], radius: int) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    x1, y1, x2, y2 = box
    border_w = 14
    stops = [
        (0.00, (216, 184, 255)),
        (0.28, (188, 214, 255)),
        (0.56, (255, 213, 236)),
        (0.82, (231, 213, 255)),
        (1.00, (216, 184, 255)),
    ]

    def sample(t: float) -> tuple[int, int, int, int]:
        for idx in range(len(stops) - 1):
            left_t, left = stops[idx]
            right_t, right = stops[idx + 1]
            if t <= right_t:
                local = 0.0 if right_t == left_t else (t - left_t) / (right_t - left_t)
                rgb = tuple(int(left[c] + (right[c] - left[c]) * local) for c in range(3))
                return (*rgb, 255)
        return (*stops[-1][1], 255)

    ring_mask = Image.new("L", img.size, 0)
    ring_draw = ImageDraw.Draw(ring_mask)
    ring_draw.rounded_rectangle(box, radius=radius, fill=255)
    ring_draw.rounded_rectangle(
        (x1 + border_w, y1 + border_w, x2 - border_w, y2 - border_w),
        radius=radius - border_w,
        fill=0,
    )

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.rounded_rectangle(
        (x1 - 5, y1 - 5, x2 + 5, y2 + 5),
        radius=radius + 5,
        outline=(216, 184, 255, 140),
        width=9,
    )
    glow_draw.rounded_rectangle(
        (x1 - 10, y1 - 10, x2 + 10, y2 + 10),
        radius=radius + 10,
        outline=(188, 214, 255, 80),
        width=7,
    )
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(7)))

    gradient = Image.new("RGBA", img.size, (0, 0, 0, 0))
    px = gradient.load()
    denom = max(1, (x2 - x1) + (y2 - y1))
    for y in range(y1, y2 + 1):
        for x in range(x1, x2 + 1):
            if ring_mask.getpixel((x, y)):
                px[x, y] = sample(((x - x1) + (y - y1)) / denom)
    img.alpha_composite(gradient)

    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (x1 + border_w + 2, y1 + border_w + 2, x2 - border_w - 2, y2 - border_w - 2),
        radius=radius - border_w - 2,
        outline="#090c12",
        width=4,
    )


def write_og_image(
    out_path: Path,
    records: list[dict],
    champ_meta: dict[int, dict],
    *,
    queue_id: int,
    patch_prefix: str | None,
    total_games: int,
) -> None:
    """Write a square top-champion thumbnail for Open Graph cards."""
    from PIL import Image, ImageDraw

    top_record = records[0] if records else None
    top_meta = champ_meta.get(top_record["champion_id"]) if top_record else None
    top_wr = float(top_record.get("bayes_wr", 0.0)) if top_record else 0.0

    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    badge_font = _load_font(58, bold=True)

    card_x, card_y, card_size = 58, 58, 396
    frame_box = (card_x - 24, card_y - 24, card_x + card_size + 24, card_y + card_size + 24)
    draw.rounded_rectangle(frame_box, radius=36, fill="#080a10")
    _draw_prismatic_frame(img, frame_box, 36)
    if top_meta and top_meta.get("image"):
        try:
            resp = httpx.get(top_meta["image"], timeout=5)
            resp.raise_for_status()
            icon = Image.open(BytesIO(resp.content)).convert("RGB").resize((card_size, card_size))
            mask = Image.new("L", (card_size, card_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle((0, 0, card_size, card_size), radius=24, fill=255)
            img.paste(icon, (card_x, card_y), mask)
        except Exception:
            draw.rounded_rectangle((card_x, card_y, card_x + card_size, card_y + card_size), radius=24, fill="#242b3a")
    else:
        draw.rounded_rectangle(
            (card_x, card_y, card_x + card_size, card_y + card_size),
            radius=24,
            fill="#242b3a",
        )
    badge_text = f"{top_wr * 100:.1f}%"
    draw.rounded_rectangle((card_x, card_y + card_size - 102, card_x + 190, card_y + card_size), radius=22, fill="#0d111a")
    draw.text((card_x + 22, card_y + card_size - 86), badge_text, font=badge_font, fill="#f8fbff")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "PNG", optimize=True)


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


# Data Dragon's `tags` field is Riot's *SR / general* classification, which
# doesn't always match how ARAM/Mayhem players think about a champion.
# These overrides REPLACE the DDragon tag list for the listed aliases.
#
# Codex audit #1 (2026-05-15): 10 entries — mage-supports + Nilah.
# Codex audit #2 (2026-05-17): ~50 entries — full role-chip noise cleanup.
#   Dominant patterns: Fighter↔Tank cross-pollution, Marksman mislabeled Mage,
#   Mage/Support & Support/Mage chip bleed. User-reviewed per-champion.
# Codex audit #3 (2026-05-17): narrow remaining broad DDragon secondary tags.
TAG_OVERRIDES: dict[str, list[str]] = {
    # --- Assassin ---
    # Pure burst assassins whose Fighter secondary pollutes 戰士 chip.
    "Akali":    ["Assassin"],
    "Diana":    ["Assassin"],   # AP diver; Fighter tag is a relic
    "Ekko":     ["Assassin"],
    "Evelynn":  ["Assassin"],
    "Fizz":     ["Assassin"],
    "Kassadin": ["Assassin"],
    "Katarina": ["Assassin"],
    "Leblanc":  ["Assassin"],
    "Naafiri":  ["Assassin"],
    "Nocturne": ["Assassin"],
    "Qiyana":   ["Assassin"],
    "Rengar":   ["Assassin"],

    # --- Fighter ---
    # Duelists/skirmishers tagged Fighter+Assassin — Assassin chip is noisy.
    "Briar":   ["Fighter"],
    "Fiora":   ["Fighter"],
    "Irelia":  ["Fighter"],
    "Jax":     ["Fighter"],
    "Kayn":    ["Fighter"],
    "LeeSin":  ["Fighter"],
    "MasterYi":["Fighter"],
    "Pantheon":["Fighter"],
    "Riven":   ["Fighter"],
    "Tryndamere":["Fighter"],
    "Vi":      ["Fighter"],
    "Viego":   ["Fighter"],
    "XinZhao": ["Fighter"],
    "Yasuo":   ["Fighter"],
    "Yone":    ["Fighter"],
    "Zaahen":  ["Fighter"],
    # Bruisers tagged Fighter+Tank — Tank chip is noisy for these.
    "Aatrox":   ["Fighter"],
    "Ambessa":  ["Fighter"],
    "Camille":  ["Fighter"],
    "Darius":   ["Fighter"],
    "Garen":    ["Fighter"],
    "Gnar":     ["Fighter"],
    "Hecarim":  ["Fighter"],
    "Illaoi":   ["Fighter"],
    "JarvanIV": ["Fighter"],
    "Jayce":    ["Fighter"],
    "Kled":     ["Fighter"],
    "MonkeyKing":["Fighter"],
    "Mordekaiser":["Fighter"],
    "Olaf":     ["Fighter"],
    "RekSai":   ["Fighter"],
    "Renekton": ["Fighter"],
    "Sett":     ["Fighter"],
    "Shyvana":  ["Fighter"],
    "Trundle":  ["Fighter"],
    "Udyr":     ["Fighter"],
    "Urgot":    ["Fighter"],
    "Warwick":  ["Fighter"],
    "Yorick":   ["Fighter"],
    # Tank/Fighter — primary identity is Fighter in Mayhem.
    "Poppy":    ["Fighter"],

    # --- Tank ---
    # True frontline tanks whose Fighter secondary pollutes 戰士 chip.
    "Malphite": ["Tank"],
    "Maokai":   ["Tank"],
    "DrMundo":  ["Tank"],
    "KSante":   ["Tank"],
    "Nunu":     ["Tank"],
    "Ornn":     ["Tank"],
    "Rammus":   ["Tank"],
    "Sejuani":  ["Tank"],
    "Sion":     ["Tank"],
    "Skarner":  ["Tank"],
    "Zac":      ["Tank"],
    # AP tanks — Mage tag is misleading for role filter purposes.
    "Amumu":    ["Tank"],
    "Chogath":  ["Tank"],
    "Galio":    ["Tank"],
    "Singed":   ["Tank"],
    # Fighter/Tank — these play as frontline tanks in Mayhem.
    "Nasus":    ["Tank"],
    "Volibear": ["Tank"],

    # --- Support + Tank (engage supports) ---
    "TahmKench": ["Tank", "Support"],
    "Taric":     ["Tank", "Support"],
    "Thresh":    ["Support", "Tank"],

    # --- Marksman ---
    # ADCs with AP builds — Mage tag causes them to appear under 法師.
    "Akshan":  ["Marksman"],
    "Ashe":    ["Marksman"],
    "Corki":   ["Marksman"],
    "Ezreal":  ["Marksman"],
    "Jhin":    ["Marksman"],
    "Kaisa":   ["Marksman"],
    "Kayle":   ["Marksman"],   # Fighter/Support tags are completely wrong
    "KogMaw":  ["Marksman"],
    "Lucian":  ["Marksman"],
    "MissFortune":["Marksman"],
    "Nilah":   ["Marksman"],   # Officially Fighter/Assassin; melee ADC in practice
    "Quinn":   ["Marksman"],
    "Samira":  ["Marksman"],
    "Smolder": ["Marksman"],
    "Tristana":["Marksman"],
    "Twitch":  ["Marksman"],
    "Varus":   ["Marksman"],
    "Vayne":   ["Marksman"],

    # --- Mage ---
    # Poke/control mages with Support secondary — pollutes 輔助 chip.
    "Azir":     ["Mage"],
    "Aurora":   ["Mage"],
    "Fiddlesticks":["Mage"],
    "Karma":    ["Mage"],
    "Lux":      ["Mage"],
    "Mel":      ["Mage"],
    "Morgana":  ["Mage"],
    "Nidalee":  ["Mage"],
    "Orianna":  ["Mage"],
    "Rumble":   ["Mage"],
    "Seraphine":["Mage"],
    "Swain":    ["Mage"],      # Fighter secondary is noisy
    "Taliyah":  ["Mage"],
    "Teemo":    ["Mage"],      # Marksman/Assassin tags; trap mage in practice
    "Zoe":      ["Mage"],
    "Zyra":     ["Mage"],
    # Mage-supports — already present from audit #1; Support tag was noisy.
    "Annie":        ["Mage"],
    "Brand":        ["Mage"],
    "Heimerdinger": ["Mage"],
    "Hwei":         ["Mage"],
    "Neeko":        ["Mage"],
    "Velkoz":       ["Mage"],
    "Xerath":       ["Mage"],
    "TwistedFate":  ["Mage"],  # Marksman tag is a relic
    "Vladimir":     ["Mage"],  # Fighter tag is misleading

    # --- Support ---
    # Enchanters with Mage secondary — pollutes 法師 chip.
    "Bard":    ["Support"],
    "Janna":   ["Support"],
    "Lulu":    ["Support"],
    "Nami":    ["Support"],
    "Sona":    ["Support"],
    "Soraka":  ["Support"],
    "Yuumi":   ["Support"],
    "Zilean":  ["Support"],
    "Ivern":   ["Support"],
    "Milio":   ["Support"],
    "Renata":  ["Support"],
}


def load_champion_metadata(version: str | None) -> tuple[str, dict[int, dict]]:
    if version is None:
        r = httpx.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=15)
        r.raise_for_status()
        version = r.json()[0]
    url_zh = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_TW/champion.json"
    r_zh = httpx.get(url_zh, timeout=30)
    url_en = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    r_en = httpx.get(url_en, timeout=30)
    if r_zh.status_code != 200 and r_en.status_code != 200:
        r_zh.raise_for_status()
        r_en.raise_for_status()
    raw_zh = r_zh.json()["data"] if r_zh.status_code == 200 else {}
    raw_en = r_en.json()["data"] if r_en.status_code == 200 else {}
    by_id: dict[int, dict] = {}
    applied: list[tuple[str, list[str], list[str]]] = []
    source = raw_en or raw_zh
    for alias, base_entry in source.items():
        entry_en = raw_en.get(alias, base_entry)
        entry_zh = raw_zh.get(alias, base_entry)
        tags = entry_en.get("tags") or entry_zh.get("tags") or []
        if alias in TAG_OVERRIDES:
            applied.append((alias, list(tags), list(TAG_OVERRIDES[alias])))
            tags = list(TAG_OVERRIDES[alias])
        by_id[int(base_entry["key"])] = {
            "name": entry_zh.get("name") or entry_en.get("name") or alias,
            "name_zh": entry_zh.get("name") or entry_en.get("name") or alias,
            "name_en": entry_en.get("name") or alias,
            "alias": alias,
            "tags": tags,
            "image": f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{alias}.png",
        }
    if applied:
        click.echo(f"[tierlist] applied {len(applied)} TAG_OVERRIDES (DDragon -> Mayhem mental model):")
        for alias, before, after in applied:
            click.echo(f"  {alias:14s} {before} -> {after}")
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


def load_augment_descriptions(
    cache_dir: Path,
    *,
    locale: str,
    cache_name: str,
) -> dict[int, str]:
    """Resolve Mayhem augment descriptions via:

        kiwi.bin.json (AugmentPlatformId -> DescriptionTra)
        +  lol.stringtable.json (lowercase key -> localized text)

    Returns dict mapping augment ID (matches our DB) -> cleaned localized summary.
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
        f"https://raw.communitydragon.org/latest/game/{locale}/data/menu/en_us/lol.stringtable.json",
        cache_dir / cache_name,
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


# CommunityDragon `zh_tw` augment names don't always match Garena's live
# Traditional Chinese client.  Drop manual TW overrides here as users
# report mistranslations.  Key = augment ID (== `AugmentPlatformId`).
#
# Format: aid -> TW name as it actually appears in the game client.
AUGMENT_NAME_OVERRIDES: dict[int, str] = {
    # Internal: Kiwi_UltimateAwakening; icon ZeroHour_small.png.
    # CommunityDragon zh_tw: 「大絕覺醒」, Garena TW client ships 「最終型態」
    # (型 not 形 — Garena consistently picks 型態 over 形態 for "form" in
    # game context).  Verified against live client screenshot 2026-05-15.
    1349: "最終型態",
}

# Some tooltips contain spell-slot placeholders like "your @SpellName@ gains
# @Value@ ability haste".  Our generic cleaner intentionally collapses opaque
# numeric tokens to `[數值]`, but for Bread-and-* augments that also erases the
# Q/W/E slot and makes the tooltip misleading.  Override only the affected
# descriptions with the actual spell slot wording shown in-game.
AUGMENT_DESC_OVERRIDES: dict[int, str] = {
    1103: "你的第一個基礎技能（Q）獲得[數值]技能加速。",
    1150: "你的第二個基礎技能（W）獲得[數值]技能加速。",
    1151: "你的第三個基礎技能（E）獲得[數值]技能加速。",
}


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
    set_by_augment = _augment_set_lookup()
    name_overrides_applied: list[tuple[int, str, str]] = []
    for entry in rows:
        aug_id = entry.get("id")
        if aug_id is None:
            continue
        aug_id = int(aug_id)
        tw_entry = tw_by_id.get(aug_id, {})
        tw_name = tw_entry.get("nameTRA") or tw_entry.get("name")
        en_name = entry.get("nameTRA") or entry.get("name") or entry.get("simpleNameTRA")
        name_zh = tw_name if tw_name and tw_name.strip() else en_name
        name_en = en_name or tw_name
        name = name_zh
        # Apply manual TW translation override if we have one.
        if aug_id in AUGMENT_NAME_OVERRIDES:
            override = AUGMENT_NAME_OVERRIDES[aug_id]
            if name != override:
                name_overrides_applied.append((aug_id, name or "?", override))
                name = override
                name_zh = override
        icon_path = (
            entry.get("augmentSmallIconPath")
            or entry.get("augmentLargeIconPath")
        )
        en_lookup_name = entry.get("nameTRA") or entry.get("name") or entry.get("simpleNameTRA") or ""
        set_infos = set_by_augment.get(_normalize_augment_name(en_lookup_name), [])
        by_id[aug_id] = {
            "name": name or f"#{aug_id}",
            "name_zh": name_zh or name or f"#{aug_id}",
            "name_en": name_en or name or f"#{aug_id}",
            "icon": _icon_url(icon_path) if icon_path else "",
            "rarity": entry.get("rarity", ""),
            "desc": "",
            "desc_zh": "",
            "desc_en": "",
            "set": " / ".join(info["name"] for info in set_infos),
            "set_zh": " / ".join(info["name_zh"] for info in set_infos),
            "set_en": " / ".join(info["name_en"] for info in set_infos),
            "setSlug": " ".join(info["slug"] for info in set_infos),
            "sets": set_infos,
        }
    if name_overrides_applied:
        click.echo(
            f"[tierlist] applied {len(name_overrides_applied)} "
            "AUGMENT_NAME_OVERRIDES (CDragon zh_tw -> Garena TW):"
        )
        for aid, before, after in name_overrides_applied:
            click.echo(f"  {aid:5d}  {before}  ->  {after}")

    if cache_dir is not None:
        try:
            descs_zh = load_augment_descriptions(
                cache_dir,
                locale="zh_tw",
                cache_name="lol_stringtable_zh_tw.json",
            )
            for aid, txt in descs_zh.items():
                if aid in by_id:
                    by_id[aid]["desc"] = txt
                    by_id[aid]["desc_zh"] = txt
        except Exception as exc:
            click.echo(f"[tierlist] WARN: zh-TW augment description fetch failed: {exc}")
        try:
            descs_en = load_augment_descriptions(
                cache_dir,
                locale="en_us",
                cache_name="lol_stringtable_en_us.json",
            )
            for aid, txt in descs_en.items():
                if aid in by_id:
                    by_id[aid]["desc_en"] = txt
        except Exception as exc:
            click.echo(f"[tierlist] WARN: en-US augment description fetch failed: {exc}")

    for aid, txt in AUGMENT_DESC_OVERRIDES.items():
        if aid in by_id:
            by_id[aid]["desc"] = txt
            by_id[aid]["desc_zh"] = txt

    return by_id


def load_item_metadata(cache_dir: Path | None = None) -> dict[int, dict]:
    rows_default = _cached_get_json(
        f"{CDRAGON_BASE}/v1/items.json",
        (cache_dir or Path("data/cache")) / "cdragon_items_en_us.json",
    )
    rows_zh = _cached_get_json(
        f"{CDRAGON_BASE.replace('/default', '/zh_tw')}/v1/items.json",
        (cache_dir or Path("data/cache")) / "cdragon_items_zh_tw.json",
    )
    zh_by_id = {
        int(row["id"]): row
        for row in rows_zh
        if isinstance(row, dict) and row.get("id") is not None
    }
    out: dict[int, dict] = {}
    for row in rows_default:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        item_id = int(row["id"])
        zh_row = zh_by_id.get(item_id, {})
        icon_path = row.get("iconPath") or zh_row.get("iconPath") or ""
        price_raw = row.get("priceTotal")
        if isinstance(price_raw, dict):
            price_total = int(price_raw.get("amount") or 0)
        else:
            price_total = int(price_raw or 0)
        out[item_id] = {
            "id": item_id,
            "name": zh_row.get("name") or row.get("name") or f"#{item_id}",
            "name_zh": zh_row.get("name") or row.get("name") or f"#{item_id}",
            "name_en": row.get("name") or zh_row.get("name") or f"#{item_id}",
            "categories": list(row.get("categories") or zh_row.get("categories") or []),
            "price_total": price_total,
            "icon": _icon_url(icon_path) if icon_path else "",
        }
    return out


def compute_winrates(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    prior: float = 0.5,
    k: int = 200,
):
    """Compute champion winrates + per-(champion, augment) winrates.

    Returns: (champ_records, champ_aug_records, champ_pair_records)
      champ_records: list of dicts with champion_id, games, wins, raw_wr, bayes_wr
      champ_aug_records: list of dicts with champion_id, augment_id, games, wins,
                        raw_wr, smoothed_wr, lift (smoothed_wr - champ_baseline_wr)
      champ_pair_records: list of dicts with champion_id, teammate_id, games,
                        wins, expected_wr, lift, delta_vs_rest, z_score
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
    cp_games: Counter[tuple[int, int]] = Counter()
    cp_wins: Counter[tuple[int, int]] = Counter()

    for blue, red, bw, pj in rows:
        bw_bool = bool(bw)
        blue_team = json.loads(blue)
        red_team = json.loads(red)
        for team, team_won in ((blue_team, bw_bool), (red_team, not bw_bool)):
            for c in team:
                games[c] += 1
                if team_won:
                    wins[c] += 1
            # Ordered anchor -> teammate rows: recommendation is conditioned on
            # the already-picked champions, so we preserve "given anchor A,
            # how much does teammate B help?" rather than collapsing to an
            # undirected pair too early.
            for c in team:
                for teammate in team:
                    if teammate == c:
                        continue
                    cp_games[(c, teammate)] += 1
                    if team_won:
                        cp_wins[(c, teammate)] += 1
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

    # Same-team pair synergy is a residual over each champion's marginal
    # strength.  This avoids recommending "T2+ good-stuff piles" as synergy:
    # the pair has to beat the winrate expected from anchor + teammate strength.
    team_rows = sum(games.values())
    global_wr = (sum(wins.values()) / team_rows) if team_rows else 0.5
    eps = 1e-4

    def _logit(p: float) -> float:
        p = min(max(p, eps), 1.0 - eps)
        return math.log(p / (1.0 - p))

    def _sigmoid(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))

    champ_pair_records = []
    for (cid, teammate_id), g in cp_games.items():
        w = cp_wins[(cid, teammate_id)]
        raw = w / g if g else 0.0
        anchor_wr = raw_wr_by_champ.get(cid, global_wr)
        teammate_wr = raw_wr_by_champ.get(teammate_id, global_wr)
        expected_wr = _sigmoid(_logit(anchor_wr) + _logit(teammate_wr) - _logit(global_wr))

        rest_games = games[cid] - g
        rest_wins = wins[cid] - w
        delta_vs_expected = raw - expected_wr
        var_pair = raw * (1 - raw) / max(g, 1)
        var_anchor = anchor_wr * (1 - anchor_wr) / max(games[cid], 1)
        var_teammate = teammate_wr * (1 - teammate_wr) / max(games[teammate_id], 1)
        se = (var_pair + var_anchor + var_teammate) ** 0.5
        z_score = (delta_vs_expected / se) if se > 0 else 0.0

        if rest_games > 0:
            rest_wr = rest_wins / rest_games
            delta_vs_rest = raw - rest_wr
        else:
            rest_wr = anchor_wr
            delta_vs_rest = raw - rest_wr

        champ_pair_records.append({
            "champion_id": cid,
            "teammate_id": teammate_id,
            "games": g,
            "wins": w,
            "raw_wr": raw,
            "expected_wr": expected_wr,
            "baseline_wr": anchor_wr,
            "teammate_wr": teammate_wr,
            "rest_wr": rest_wr,
            "lift": delta_vs_expected,
            "delta_vs_rest": delta_vs_rest,
            "z_score": z_score,
        })

    return champ_records, champ_aug_records, champ_pair_records


RARITY_ORDER = ["kPrismatic", "kGold", "kSilver"]


def estimate_augment_prior_strength(champ_aug: list[dict]) -> float:
    """Estimate beta-binomial prior strength for champ x augment WRs.

    Each pair is centered on that champion's baseline winrate.  The fitted
    concentration controls how aggressively low-sample pairs shrink back to the
    champion baseline, and avoids a hand-picked `games / (games + k)` scale.
    """
    rows: list[tuple[int, int, float]] = []
    for row in champ_aug:
        games = int(row.get("games", 0))
        wins = int(row.get("wins", 0))
        baseline = float(row.get("baseline_wr", 0.5))
        if games <= 0:
            continue
        rows.append((wins, games, min(max(baseline, 1e-4), 1.0 - 1e-4)))

    if len(rows) < 20:
        return AUGMENT_PRIOR_DEFAULT

    if minimize_scalar is not None and betaln is not None:
        def nll(log_k: float) -> float:
            k = math.exp(log_k)
            loss = 0.0
            for wins, games, baseline in rows:
                alpha = baseline * k
                beta = (1.0 - baseline) * k
                # The combinatorial term is constant in k, so it is omitted.
                loss -= float(betaln(wins + alpha, games - wins + beta) - betaln(alpha, beta))
            return loss

        try:
            result = minimize_scalar(
                nll,
                bounds=(math.log(5.0), math.log(5000.0)),
                method="bounded",
                options={"xatol": 1e-3},
            )
            if result.success:
                return max(5.0, min(5000.0, math.exp(float(result.x))))
        except Exception:
            pass

    # Fallback: moment estimate from over-dispersion beyond binomial noise.
    rhos: list[float] = []
    for wins, games, baseline in rows:
        observed = wins / games
        denom = max(baseline * (1.0 - baseline), 1e-6)
        extra_var = max(0.0, (observed - baseline) ** 2 - denom / games)
        if extra_var > 0:
            rhos.append(extra_var / denom)
    if not rhos:
        return AUGMENT_PRIOR_DEFAULT
    rhos.sort()
    rho = rhos[len(rhos) // 2]
    if rho <= 0:
        return AUGMENT_PRIOR_DEFAULT
    return max(5.0, min(5000.0, (1.0 / rho) - 1.0))


def beta_posterior_quantile(q: float, alpha: float, beta: float) -> float:
    if betaincinv is not None:
        try:
            return float(betaincinv(alpha, beta, q))
        except Exception:
            pass
    mean = alpha / (alpha + beta)
    var = alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1.0))
    direction = -1.0 if q <= 0.5 else 1.0
    return min(max(mean + direction * AUGMENT_LCB_Z * math.sqrt(max(var, 0.0)), 0.0), 1.0)


def posterior_wr_summary(wins: int, games: int, baseline: float, prior_strength: float) -> tuple[float, float]:
    baseline = min(max(baseline, 1e-4), 1.0 - 1e-4)
    alpha = baseline * prior_strength + wins
    beta = (1.0 - baseline) * prior_strength + games - wins
    mean = alpha / (alpha + beta)
    lower = beta_posterior_quantile(AUGMENT_POSTERIOR_Q, alpha, beta)
    return mean, lower


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _damage_bucket(row: dict[str, str] | None, role: str) -> str:
    if row:
        physical = _safe_float(row.get("empirical_physical_damage_ratio"))
        magic = _safe_float(row.get("empirical_magic_damage_ratio"))
        if physical >= 0.55 and physical >= magic + 0.15:
            return "physical"
        if magic >= 0.55 and magic >= physical + 0.15:
            return "magic"
    if role in {"Marksman", "Fighter", "Assassin"}:
        return "physical"
    if role in {"Mage", "Support"}:
        return "magic"
    return "mixed"


def load_champion_pick_profiles(
    champ_meta: dict[int, dict],
    scores_path: Path = EMPIRICAL_CHAMPION_SCORES,
) -> dict[int, dict[str, str]]:
    score_rows: dict[int, dict[str, str]] = {}
    if scores_path.exists():
        with scores_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    score_rows[int(row["champion_id"])] = row
                except (KeyError, TypeError, ValueError):
                    continue

    profiles: dict[int, dict[str, str]] = {}
    for cid, meta in champ_meta.items():
        tags = list(meta.get("tags") or [])
        role = tags[0] if tags else "Unknown"
        profiles[int(cid)] = {
            "role": role,
            "damage": _damage_bucket(score_rows.get(int(cid)), role),
        }
    return profiles


_DAMAGE_PROFILE_KEYWORDS = (
    "ability power",
    "adaptive force",
    "attack damage",
    "attack speed",
    "basic attack",
    "basic attacks",
    "critical",
    "crit",
    "magic damage",
    "magic penetration",
    "on-hit",
    "physical damage",
    "armor penetration",
    "lethality",
    "spell damage",
    "true damage",
    "convert",
)


def augment_peer_scope(meta: dict | None) -> str:
    if not meta:
        return "role"
    text = " ".join(
        str(meta.get(key) or "")
        for key in ("name", "name_en", "desc", "desc_en", "set", "set_en")
    ).lower()
    if any(keyword in text for keyword in _DAMAGE_PROFILE_KEYWORDS):
        return "role_damage"
    return "role"


def _profile_group(cid: int, profiles: dict[int, dict[str, str]], scope: str) -> str:
    profile = profiles.get(cid, {})
    role = profile.get("role") or "Unknown"
    if scope == "role_damage":
        return f"{role}|{profile.get('damage') or 'mixed'}"
    return role


def build_pick_lift_index(
    champ_aug: list[dict],
    aug_meta: dict[int, dict],
    profiles: dict[int, dict[str, str]],
) -> dict[tuple[int, int], dict[str, float | str]]:
    champ_rarity_totals: Counter[tuple[int, str]] = Counter()
    global_totals: Counter[str] = Counter()
    global_counts: Counter[tuple[str, int]] = Counter()
    group_totals: Counter[tuple[str, str, str]] = Counter()
    group_counts: Counter[tuple[str, str, str, int]] = Counter()
    rarity_aug_ids: dict[str, set[int]] = defaultdict(set)

    for row in champ_aug:
        aid = int(row["augment_id"])
        cid = int(row["champion_id"])
        games = int(row["games"])
        meta = aug_meta.get(aid)
        rarity = str(meta.get("rarity") or "") if meta else ""
        if not rarity:
            continue
        rarity_aug_ids[rarity].add(aid)
        champ_rarity_totals[(cid, rarity)] += games
        global_totals[rarity] += games
        global_counts[(rarity, aid)] += games
        for scope in ("role", "role_damage"):
            group = _profile_group(cid, profiles, scope)
            group_totals[(scope, group, rarity)] += games
            group_counts[(scope, group, rarity, aid)] += games

    out: dict[tuple[int, int], dict[str, float | str]] = {}
    for row in champ_aug:
        aid = int(row["augment_id"])
        cid = int(row["champion_id"])
        games = int(row["games"])
        meta = aug_meta.get(aid)
        rarity = str(meta.get("rarity") or "") if meta else ""
        if not rarity or global_totals[rarity] <= 0:
            continue
        scope = augment_peer_scope(meta)
        group = _profile_group(cid, profiles, scope)
        group_key = (scope, group, rarity)
        champ_total = champ_rarity_totals[(cid, rarity)]
        peer_total = group_totals[group_key] - champ_total
        peer_count = group_counts[(scope, group, rarity, aid)] - games

        # If role+damage is too thin after leave-one-out, fall back to role-only
        # before falling all the way back to the same-rarity global baseline.
        min_peer_total = max(50.0, 2.0 * len(rarity_aug_ids[rarity]))
        if scope == "role_damage" and peer_total < min_peer_total:
            scope = "role"
            group = _profile_group(cid, profiles, scope)
            group_key = (scope, group, rarity)
            peer_total = group_totals[group_key] - champ_total
            peer_count = group_counts[(scope, group, rarity, aid)] - games

        m = max(1, len(rarity_aug_ids[rarity]))
        global_rate = (global_counts[(rarity, aid)] + 0.5) / (global_totals[rarity] + 0.5 * m)
        if peer_total > 0:
            peer_rate = (peer_count + 0.5 * m * global_rate) / (peer_total + 0.5 * m)
        else:
            peer_rate = global_rate
            group = "global"
        champ_rate = (games + 0.5 * m * peer_rate) / (champ_total + 0.5 * m) if champ_total > 0 else peer_rate
        pick_lift = math.log(max(champ_rate, 1e-9) / max(peer_rate, 1e-9))
        out[(cid, aid)] = {
            "pick_rate": champ_rate,
            "peer_pick_rate": peer_rate,
            "pick_lift": pick_lift,
            "peer_scope": scope,
            "peer_group": group,
        }
    return out


def _label_entry(labels: dict[str, dict[str, str]], slug: str) -> dict[str, str]:
    info = labels.get(slug, {})
    name_en = info.get("en") or slug
    name_zh = info.get("zh") or name_en
    return {
        "name": name_zh,
        "name_zh": name_zh,
        "name_en": name_en,
        "slug": slug,
    }


def item_style_infos(item: dict | None) -> list[dict[str, str]]:
    if not item:
        return []
    if int(item.get("price_total") or 0) < ITEM_MIN_TOTAL_GOLD:
        return []
    categories = set(str(c) for c in item.get("categories") or [])
    name = f"{item.get('name_en', '')} {item.get('name', '')}".lower()
    is_spell_item = "SpellDamage" in categories or "ability power" in name
    is_support = (
        "HealAndShieldPower" in categories
        or any(word in name for word in SUPPORT_ITEM_KEYWORDS)
    )
    # Use one primary style per completed item.  Multi-tag CDragon items such as
    # crit+AP Mayhem items otherwise make marksmen look like AP builders just
    # because their best crit item also carries spell-damage tags.
    if is_support:
        slug = "support"
    elif "CriticalStrike" in categories:
        slug = "crit"
    elif is_spell_item:
        if any(word in name for word in AP_ONHIT_ITEM_KEYWORDS) or (
            {"OnHit", "AttackSpeed"} & categories and "Damage" not in categories
        ):
            slug = "ap_onhit"
        elif any(word in name for word in AP_BURN_ITEM_KEYWORDS):
            slug = "ap_burn"
        elif any(word in name for word in AP_BRUISER_ITEM_KEYWORDS):
            slug = "ap_bruiser"
        elif any(word in name for word in AP_BURST_ITEM_KEYWORDS):
            slug = "ap_burst"
        elif {"Health", "Armor", "SpellBlock", "MagicResist"} & categories and "MagicPenetration" not in categories:
            slug = "ap_bruiser"
        else:
            slug = "ap_burst"
    elif {"Damage", "ArmorPenetration", "Lethality"} & categories and (
        "manamune" in name or "muramana" in name
    ):
        slug = "ad_poke"
    elif {"OnHit", "AttackSpeed"} & categories:
        slug = "onhit"
    elif {"Damage", "ArmorPenetration", "Lethality"} & categories:
        if any(word in name for word in AD_POKE_ITEM_KEYWORDS):
            slug = "ad_poke"
        elif any(word in name for word in AD_ASSASSIN_ITEM_KEYWORDS):
            slug = "ad_assassin"
        elif any(word in name for word in AD_BRUISER_ITEM_KEYWORDS):
            slug = "ad_bruiser"
        elif {"Health", "Armor", "SpellBlock", "MagicResist", "LifeSteal", "SpellVamp", "Tenacity"} & categories:
            slug = "ad_bruiser"
        elif {"ArmorPenetration", "Lethality"} & categories and {"Active", "NonbootsMovement", "Slow", "Stealth"} & categories:
            slug = "ad_assassin"
        elif {"ArmorPenetration", "Lethality"} & categories and {"AbilityHaste", "CooldownReduction", "Mana"} & categories:
            slug = "ad_poke"
        elif {"ArmorPenetration", "Lethality"} & categories:
            slug = "ad_assassin"
        else:
            slug = "ad_bruiser"
    elif {"Health", "Armor", "SpellBlock", "MagicResist"} & categories:
        slug = "tank"
    else:
        return []
    return [_label_entry(ITEM_STYLE_LABELS, slug)]


_AUGMENT_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "damage": (
        "damage", "burn", "missile", "fire", "lightning", "execute", "explosion",
        "goldrend", "boomerang", "blade", "laser", "bomb",
    ),
    "spell": (
        "ability power", "spell", "magic damage", "mana", "ultimate", "cooldown",
        "ability haste", "phenomenal evil", "mind to matter", "bread and",
    ),
    "attack": (
        "attack damage", "basic attack", "basic attacks", "attack speed", "on-hit",
        "physical damage", "fan the hammer", "light 'em up", "typhoon",
    ),
    "crit": ("critical", "crit", "jeweled", "infinity"),
    "tank": (
        "health", "armor", "magic resist", "damage reduction", "shield", "steel your heart",
        "immolate", "goliath", "perseverance",
    ),
    "sustain": (
        "heal", "healing", "shield", "omnivamp", "lifesteal", "first-aid",
        "windspeaker", "mikael", "all for you", "critical healing",
    ),
    "mobility": (
        "dash", "blink", "movement speed", "move speed", "speed", "haste",
        "transit", "dive bomber", "clown college",
    ),
    "snowball": ("snowball", "snowday", "pinball"),
    "economy": (
        "gold", "transmute", "pandora", "donation", "red envelope", "collector",
        "stats!", "make it rain",
    ),
    "stacking": (
        "stack", "quest", "infinite", "duality", "phenomenal", "hubris",
        "slap around", "soul eater", "tap dancer", "shrink engine",
    ),
    "utility": (
        "slow", "stun", "root", "crowd control", "ally", "allies", "intervention",
        "sonata", "polymorph", "buff buddies", "ocean soul",
    ),
    "auto": (
        "automated", "fully automated", "firefox", "frost wraith", "quantum",
        "self destruct", "prom queen", "ok boomerang",
    ),
}


_SET_TO_AUGMENT_TYPES = {
    "archmage": {"spell", "utility"},
    "dive-bomb": {"mobility", "damage"},
    "firecracker": {"damage", "attack", "crit"},
    "fully-automated": {"auto", "damage"},
    "high-roller": {"economy"},
    "make-it-rain": {"economy", "damage"},
    "snowday": {"snowball", "mobility"},
    "stackosaurus-rex": {"stacking", "tank"},
    "wee-woo-wee-woo": {"sustain", "utility"},
}


def augment_type_infos(meta: dict | None) -> list[dict[str, str]]:
    if not meta:
        return []
    text = " ".join(
        str(meta.get(key) or "")
        for key in ("name", "name_en", "desc", "desc_en", "set", "set_en", "setSlug")
    ).lower()
    slugs: set[str] = set()
    for info in meta.get("sets") or []:
        slugs.update(_SET_TO_AUGMENT_TYPES.get(str(info.get("slug") or ""), set()))
    for slug, keywords in _AUGMENT_TYPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            slugs.add(slug)
    return [_label_entry(AUGMENT_TYPE_LABELS, slug) for slug in sorted(slugs)]


def estimate_category_prior_strength(rows: list[dict]) -> float:
    usable = [
        (
            int(row["wins"]),
            int(row["games"]),
            min(max(float(row["prior_wr"]), 1e-4), 1.0 - 1e-4),
        )
        for row in rows
        if int(row.get("games", 0)) > 0
    ]
    if len(usable) < 8 or minimize_scalar is None or betaln is None:
        return CATEGORY_PRIOR_DEFAULT

    def nll(log_k: float) -> float:
        k = math.exp(log_k)
        loss = 0.0
        for wins, games, prior_wr in usable:
            alpha = prior_wr * k
            beta = (1.0 - prior_wr) * k
            loss -= float(betaln(wins + alpha, games - wins + beta) - betaln(alpha, beta))
        return loss

    try:
        result = minimize_scalar(
            nll,
            bounds=(math.log(5.0), math.log(5000.0)),
            method="bounded",
            options={"xatol": 1e-3},
        )
        if result.success:
            return max(5.0, min(5000.0, math.exp(float(result.x))))
    except Exception:
        pass
    return CATEGORY_PRIOR_DEFAULT


def _finalize_category_affinity(
    cs_games: Counter[tuple[int, str]],
    cs_wins: Counter[tuple[int, str]],
    cs_baseline_games: Counter[tuple[int, str]],
    category_games: Counter[str],
    category_wins: Counter[str],
    category_baseline_games: Counter[str],
    category_names: dict[str, dict[str, str]],
    *,
    min_games: int,
    fallback_min_games: int | None = None,
    top_n: int = 4,
    bot_n: int = 4,
) -> dict[int, dict]:
    category_avg_lift: dict[str, float] = {}
    for slug, games in category_games.items():
        if games > 0:
            category_avg_lift[slug] = (category_wins[slug] / games) - (category_baseline_games[slug] / games)

    raw_rows: list[dict] = []
    row_min_games = fallback_min_games or min_games
    for (cid, slug), games in cs_games.items():
        if games < row_min_games:
            continue
        wins = cs_wins[(cid, slug)]
        baseline = cs_baseline_games[(cid, slug)] / games
        avg_lift = category_avg_lift.get(slug, 0.0)
        prior_wr = min(max(baseline + avg_lift, 1e-4), 1.0 - 1e-4)
        raw_rows.append({
            "champion_id": cid,
            "slug": slug,
            "games": games,
            "wins": wins,
            "baseline_wr": baseline,
            "avg_lift": avg_lift,
            "prior_wr": prior_wr,
            "primary_sample": games >= min_games,
        })

    prior_strength = estimate_category_prior_strength(raw_rows)
    by_champ: dict[int, list[dict]] = {}
    for row in raw_rows:
        games = int(row["games"])
        wins = int(row["wins"])
        prior_wr = float(row["prior_wr"])
        alpha = wins + prior_wr * prior_strength
        beta = games - wins + (1.0 - prior_wr) * prior_strength
        mean_wr = alpha / (alpha + beta)
        lower_wr = beta_posterior_quantile(AUGMENT_POSTERIOR_Q, alpha, beta)
        upper_wr = beta_posterior_quantile(1.0 - AUGMENT_POSTERIOR_Q, alpha, beta)
        slug = str(row["slug"])
        name_info = category_names.get(slug, _label_entry({}, slug))
        lift = mean_wr - float(row["baseline_wr"])
        residual = mean_wr - prior_wr
        by_champ.setdefault(int(row["champion_id"]), []).append({
            "name": name_info["name"],
            "name_zh": name_info["name_zh"],
            "name_en": name_info["name_en"],
            "slug": slug,
            "games": games,
            "wins": wins,
            "raw_wr": wins / games if games else prior_wr,
            "smoothed_wr": mean_wr,
            "baseline_wr": float(row["baseline_wr"]),
            "avg_lift": float(row["avg_lift"]),
            "lift": lift,
            "residual": residual,
            "lcb_residual": lower_wr - prior_wr,
            "ucb_residual": upper_wr - prior_wr,
            "prior_strength": prior_strength,
            "primary_sample": bool(row.get("primary_sample")),
        })

    out: dict[int, dict] = {}
    for cid, rows in by_champ.items():
        rows.sort(key=lambda r: (-r["lcb_residual"], -r["residual"], -r["games"], r["name_en"]))
        eligible = [r for r in rows if r.get("primary_sample")]
        if not eligible:
            eligible = rows
        bot_rows = sorted(eligible, key=lambda r: (r["ucb_residual"], r["residual"], r["games"], r["name_en"]))
        out[cid] = {"top": eligible[:top_n], "bot": bot_rows[:bot_n], "prior_strength": prior_strength}
    return out


def compute_champ_category_affinities(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    aug_meta: dict[int, dict],
    item_meta: dict[int, dict],
    champ_records: list[dict],
    *,
    min_set_games: int,
    min_item_games: int,
    min_augtype_games: int,
) -> tuple[dict[int, dict], dict[int, dict], dict[int, dict]]:
    baseline_by_champ = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5))
        for row in champ_records
    }
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = list(
            con.execute(
                "SELECT blue_wins, participants_json FROM games "
                "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL",
                (queue_id, f"{patch_prefix}%"),
            )
        )
    else:
        rows = list(
            con.execute(
                "SELECT blue_wins, participants_json FROM games "
                "WHERE queue_id=? AND participants_json IS NOT NULL",
                (queue_id,),
            )
        )
    con.close()

    dims = ("sets", "items", "augtypes")
    cs_games = {dim: Counter() for dim in dims}
    cs_wins = {dim: Counter() for dim in dims}
    cs_baseline_games = {dim: Counter() for dim in dims}
    category_games = {dim: Counter() for dim in dims}
    category_wins = {dim: Counter() for dim in dims}
    category_baseline_games = {dim: Counter() for dim in dims}
    category_names: dict[str, dict[str, dict[str, str]]] = {dim: {} for dim in dims}

    def add(dim: str, cid: int, player_won: int, baseline: float, infos: list[dict[str, str]]) -> None:
        seen = {str(info.get("slug") or ""): info for info in infos if info.get("slug")}
        for slug, info in seen.items():
            key = (cid, slug)
            cs_games[dim][key] += 1
            cs_wins[dim][key] += player_won
            cs_baseline_games[dim][key] += baseline
            category_games[dim][slug] += 1
            category_wins[dim][slug] += player_won
            category_baseline_games[dim][slug] += baseline
            category_names[dim][slug] = {
                "name": str(info.get("name") or slug),
                "name_zh": str(info.get("name_zh") or info.get("name") or slug),
                "name_en": str(info.get("name_en") or info.get("name") or slug),
            }

    for blue_wins, participants_json in rows:
        if not participants_json:
            continue
        blue_won = bool(blue_wins)
        for participant in json.loads(participants_json):
            cid = int(participant.get("championId", 0) or 0)
            team_id = int(participant.get("teamId", 0) or 0)
            if cid <= 0 or team_id not in (100, 200):
                continue
            baseline = baseline_by_champ.get(cid, 0.5)
            player_won = 1 if (team_id == 100) == blue_won else 0
            set_infos: list[dict[str, str]] = []
            aug_type_infos: list[dict[str, str]] = []
            for augment_id in participant.get("augments") or []:
                meta = aug_meta.get(int(augment_id))
                if not meta:
                    continue
                set_infos.extend(meta.get("sets") or [])
                aug_type_infos.extend(augment_type_infos(meta))
            item_style_weights: Counter[str] = Counter()
            item_style_by_slug: dict[str, dict[str, str]] = {}
            for item_id in participant.get("items") or participant.get("itemSlots") or []:
                item = item_meta.get(int(item_id))
                for info in item_style_infos(item):
                    slug = str(info.get("slug") or "")
                    if not slug:
                        continue
                    item_style_weights[slug] += max(int((item or {}).get("price_total") or 0), 1)
                    item_style_by_slug[slug] = info
            item_infos: list[dict[str, str]] = []
            if item_style_weights:
                primary_slug = sorted(
                    item_style_weights,
                    key=lambda slug: (-item_style_weights[slug], slug),
                )[0]
                item_infos = [item_style_by_slug[primary_slug]]
            add("sets", cid, player_won, baseline, set_infos)
            add("items", cid, player_won, baseline, item_infos)
            add("augtypes", cid, player_won, baseline, aug_type_infos)

    return (
        _finalize_category_affinity(
            cs_games["sets"], cs_wins["sets"], cs_baseline_games["sets"],
            category_games["sets"], category_wins["sets"], category_baseline_games["sets"],
            category_names["sets"], min_games=min_set_games,
        ),
        _finalize_category_affinity(
            cs_games["items"], cs_wins["items"], cs_baseline_games["items"],
            category_games["items"], category_wins["items"], category_baseline_games["items"],
            category_names["items"], min_games=min_item_games, fallback_min_games=ITEM_STYLE_FALLBACK_MIN_GAMES,
        ),
        _finalize_category_affinity(
            cs_games["augtypes"], cs_wins["augtypes"], cs_baseline_games["augtypes"],
            category_games["augtypes"], category_wins["augtypes"], category_baseline_games["augtypes"],
            category_names["augtypes"], min_games=min_augtype_games,
        ),
    )


def build_champ_augment_picks(
    champ_aug: list[dict],
    aug_meta: dict[int, dict],
    profiles: dict[int, dict[str, str]],
    *,
    min_games_per_pair: int,
    top_n: int,
    bot_n: int,
    prior_strength: float,
) -> dict[int, dict]:
    """For each champion, pick top-N best and bot-N worst augments by fit score.

    Displayed WR remains the posterior mean.  Ranking uses a conservative
    posterior lower-bound lift.  Peer-relative pick-rate is packed for
    diagnostics and future labeling, but does not rewrite WR.
    """
    pick_lift_index = build_pick_lift_index(champ_aug, aug_meta, profiles)
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
        games = int(row["games"])
        wins = int(row["wins"])
        baseline = float(row.get("baseline_wr", 0.5))
        mean_wr, lower_wr = posterior_wr_summary(wins, games, baseline, prior_strength)
        pick_info = pick_lift_index.get((int(row["champion_id"]), int(row["augment_id"])), {})
        pick_lift = float(pick_info.get("pick_lift", 0.0))
        clamped_pick_lift = max(-AUGMENT_PICK_LIFT_CAP, min(AUGMENT_PICK_LIFT_CAP, pick_lift))
        ranked = {
            **row,
            "raw_wr": wins / games if games else baseline,
            "smoothed_wr": mean_wr,
            "lcb_wr": lower_wr,
            "baseline_wr": baseline,
            "lift": mean_wr - baseline,
            "lcb_lift": lower_wr - baseline,
            "rank_score": (lower_wr - baseline) + AUGMENT_PICK_LIFT_WEIGHT * clamped_pick_lift,
            "pick_rate": float(pick_info.get("pick_rate", 0.0)),
            "peer_pick_rate": float(pick_info.get("peer_pick_rate", 0.0)),
            "pick_lift": pick_lift,
            "peer_scope": str(pick_info.get("peer_scope", "")),
            "peer_group": str(pick_info.get("peer_group", "")),
        }
        bucket[rarity].append(ranked)

    out: dict[int, dict] = {}
    for cid, buckets in by_champ_rarity.items():
        top, bot = {}, {}
        for rarity, rows in buckets.items():
            rows.sort(key=lambda r: (-r["rank_score"], -r["lcb_lift"], -r["games"], r["augment_id"]))
            top[rarity] = rows[:top_n]
            bot[rarity] = sorted(
                rows,
                key=lambda r: (r["rank_score"], r["lcb_lift"], r["games"], r["augment_id"]),
            )[:bot_n]
        out[cid] = {"top": top, "bot": bot}
    return out


def build_champ_set_affinity(
    champ_aug: list[dict],
    aug_meta: dict[int, dict],
    *,
    min_games_per_set: int,
    top_n: int = 4,
    bot_n: int = 4,
) -> dict[int, dict]:
    """Aggregate per-augment rows into champion x augment-set affinity.

    `lift` asks whether a champion performs better with this set than their
    own baseline. `residual` then subtracts the global set lift, so generally
    strong sets do not automatically look like good champion-specific fits.
    """
    cs_games: Counter[tuple[int, str]] = Counter()
    cs_wins: Counter[tuple[int, str]] = Counter()
    cs_baseline_games: Counter[tuple[int, str]] = Counter()
    set_games: Counter[str] = Counter()
    set_wins: Counter[str] = Counter()
    set_baseline_games: Counter[str] = Counter()
    set_names: dict[str, dict[str, str]] = {}

    for row in champ_aug:
        meta = aug_meta.get(row["augment_id"])
        if not meta:
            continue
        memberships = meta.get("sets") or []
        if not memberships:
            continue
        games = int(row["games"])
        wins = int(row["wins"])
        baseline_games = float(row.get("baseline_wr", 0.5)) * games
        for info in memberships:
            slug = str(info.get("slug") or "")
            if not slug:
                continue
            name_info = {
                "name": str(info.get("name") or slug),
                "name_zh": str(info.get("name_zh") or info.get("name") or slug),
                "name_en": str(info.get("name_en") or info.get("name") or slug),
            }
            key = (int(row["champion_id"]), slug)
            cs_games[key] += games
            cs_wins[key] += wins
            cs_baseline_games[key] += baseline_games
            set_games[slug] += games
            set_wins[slug] += wins
            set_baseline_games[slug] += baseline_games
            set_names[slug] = name_info

    set_avg_lift: dict[str, float] = {}
    for slug, games in set_games.items():
        if games <= 0:
            continue
        set_wr = set_wins[slug] / games
        set_baseline = set_baseline_games[slug] / games
        set_avg_lift[slug] = set_wr - set_baseline

    pair_k = 30.0
    by_champ: dict[int, list[dict]] = {}
    for (cid, slug), games in cs_games.items():
        if games < min_games_per_set:
            continue
        wins = cs_wins[(cid, slug)]
        baseline = cs_baseline_games[(cid, slug)] / games
        raw = wins / games if games else baseline
        smoothed = (wins + baseline * pair_k) / (games + pair_k)
        lift = smoothed - baseline
        avg_lift = set_avg_lift.get(slug, 0.0)
        set_name_info = set_names.get(
            slug,
            {"name": slug, "name_zh": slug, "name_en": slug},
        )
        by_champ.setdefault(cid, []).append({
            "set": set_name_info["name"],
            "set_zh": set_name_info["name_zh"],
            "set_en": set_name_info["name_en"],
            "slug": slug,
            "games": games,
            "wins": wins,
            "raw_wr": raw,
            "smoothed_wr": smoothed,
            "baseline_wr": baseline,
            "lift": lift,
            "avg_lift": avg_lift,
            "residual": lift - avg_lift,
        })

    out: dict[int, dict] = {}
    for cid, rows in by_champ.items():
        rows.sort(key=lambda r: (-r["residual"], -abs(r["lift"]), -r["games"], r["set"]))
        out[cid] = {
            "top": rows[:top_n],
            "bot": sorted(rows, key=lambda r: (r["residual"], r["games"], r["set"]))[:bot_n],
        }
    return out


def compute_champ_set_affinity(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    aug_meta: dict[int, dict],
    champ_records: list[dict],
    *,
    min_games_per_set: int,
    top_n: int = 4,
    bot_n: int = 4,
) -> dict[int, dict]:
    """Compute champion x augment-set affinity from player-games.

    A player-game counts once for a set if that participant picked one or more
    augments from the set. This keeps the displayed `games` value literal while
    still capturing the performance of set-oriented builds.
    """
    baseline_by_champ = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5))
        for row in champ_records
    }
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = list(
            con.execute(
                "SELECT blue_wins, participants_json FROM games "
                "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL",
                (queue_id, f"{patch_prefix}%"),
            )
        )
    else:
        rows = list(
            con.execute(
                "SELECT blue_wins, participants_json FROM games "
                "WHERE queue_id=? AND participants_json IS NOT NULL",
                (queue_id,),
            )
        )
    con.close()

    cs_games: Counter[tuple[int, str]] = Counter()
    cs_wins: Counter[tuple[int, str]] = Counter()
    cs_baseline_games: Counter[tuple[int, str]] = Counter()
    set_games: Counter[str] = Counter()
    set_wins: Counter[str] = Counter()
    set_baseline_games: Counter[str] = Counter()
    set_names: dict[str, dict[str, str]] = {}

    for blue_wins, participants_json in rows:
        if not participants_json:
            continue
        blue_won = bool(blue_wins)
        for participant in json.loads(participants_json):
            cid = int(participant.get("championId", 0) or 0)
            team_id = int(participant.get("teamId", 0) or 0)
            if cid <= 0 or team_id not in (100, 200):
                continue
            seen_sets: dict[str, dict[str, str]] = {}
            for augment_id in participant.get("augments") or []:
                meta = aug_meta.get(int(augment_id))
                if not meta:
                    continue
                for info in meta.get("sets") or []:
                    slug = str(info.get("slug") or "")
                    if slug:
                        seen_sets[slug] = {
                            "name": str(info.get("name") or slug),
                            "name_zh": str(info.get("name_zh") or info.get("name") or slug),
                            "name_en": str(info.get("name_en") or info.get("name") or slug),
                        }
            if not seen_sets:
                continue
            player_won = 1 if (team_id == 100) == blue_won else 0
            baseline = baseline_by_champ.get(cid, 0.5)
            for slug, name_info in seen_sets.items():
                key = (cid, slug)
                cs_games[key] += 1
                cs_wins[key] += player_won
                cs_baseline_games[key] += baseline
                set_games[slug] += 1
                set_wins[slug] += player_won
                set_baseline_games[slug] += baseline
                set_names[slug] = name_info

    set_avg_lift: dict[str, float] = {}
    for slug, games in set_games.items():
        if games <= 0:
            continue
        set_avg_lift[slug] = (set_wins[slug] / games) - (set_baseline_games[slug] / games)

    pair_k = 30.0
    by_champ: dict[int, list[dict]] = {}
    for (cid, slug), games in cs_games.items():
        if games < min_games_per_set:
            continue
        wins = cs_wins[(cid, slug)]
        baseline = cs_baseline_games[(cid, slug)] / games
        raw = wins / games if games else baseline
        smoothed = (wins + baseline * pair_k) / (games + pair_k)
        lift = smoothed - baseline
        avg_lift = set_avg_lift.get(slug, 0.0)
        set_name_info = set_names.get(
            slug,
            {"name": slug, "name_zh": slug, "name_en": slug},
        )
        by_champ.setdefault(cid, []).append({
            "set": set_name_info["name"],
            "set_zh": set_name_info["name_zh"],
            "set_en": set_name_info["name_en"],
            "slug": slug,
            "games": games,
            "wins": wins,
            "raw_wr": raw,
            "smoothed_wr": smoothed,
            "baseline_wr": baseline,
            "lift": lift,
            "avg_lift": avg_lift,
            "residual": lift - avg_lift,
        })

    out: dict[int, dict] = {}
    for cid, rows in by_champ.items():
        rows.sort(key=lambda r: (-r["residual"], -abs(r["lift"]), -r["games"], r["set"]))
        out[cid] = {
            "top": rows[:top_n],
            "bot": sorted(rows, key=lambda r: (r["residual"], r["games"], r["set"]))[:bot_n],
        }
    return out


def build_champ_synergy_index(
    champ_pairs: list[dict],
    *,
    min_games: int,
) -> dict[int, list[dict]]:
    """Per champion, keep same-team teammate rows sorted by synergy lift.

    `lift` is pair WR minus the additive expectation from each champion's
    marginal winrate.  z-score is kept as a confidence tie-breaker, not the
    primary fit metric.
    """
    by_champ: dict[int, list[dict]] = {}
    for row in champ_pairs:
        if row["games"] < min_games:
            continue
        by_champ.setdefault(row["champion_id"], []).append(row)

    for cid, rows in by_champ.items():
        rows.sort(
            key=lambda r: (
                -r["lift"],
                -r["z_score"],
                -r["games"],
                r["teammate_id"],
            )
        )
    return by_champ


def render_html(
    records: list[dict],
    champ_meta: dict[int, dict],
    champ_picks: dict[int, dict],
    champ_sets: dict[int, dict],
    champ_item_styles: dict[int, dict],
    champ_augment_types: dict[int, dict],
    champ_synergy: dict[int, list[dict]],
    aug_meta: dict[int, dict],
    *,
    queue_id: int,
    patch_prefix: str | None,
    ddragon_version: str,
    total_games: int,
    min_games_per_pair: int,
    min_synergy_games: int,
    site_url: str = "",
    og_image: str = "",
    build_date: str = "",
    cloudflare_analytics_token: str = "",
    ga_measurement_id: str = "",
) -> str:
    # Group champions by tier
    by_tier: dict[str, list[dict]] = {t: [] for t in TIER_ORDER}
    for r in records:
        tier = assign_tier(r["bayes_wr"])
        meta = champ_meta.get(r["champion_id"])
        if meta is None:
            continue
        by_tier[tier].append({**r, **meta})

    header_title, queue_label = _queue_copy(queue_id)
    header_title_en = "ARAM Mayhem Database" if queue_id == 2400 else queue_label
    patch_label = f"patch {patch_prefix}" if patch_prefix else "all patches"

    # Build the JS data payload. Keep it slim: only champs we render + their
    # picked augments / teammate synergy rows + the augment metadata for ids
    # that actually appear.
    used_aug_ids: set[int] = set()
    js_champs: dict[str, dict] = {}

    def _pack(r: dict) -> dict:
        return {
            "id": r["augment_id"],
            "g": r["games"],
            "wr": round(r["smoothed_wr"], 4),
            "lift": round(r["lift"], 4),
            "score": round(r.get("rank_score", r["lift"]), 4),
            "lcb": round(r.get("lcb_lift", r["lift"]), 4),
            "pick": round(r.get("pick_rate", 0.0), 4),
            "peerPick": round(r.get("peer_pick_rate", 0.0), 4),
            "pickLift": round(r.get("pick_lift", 0.0), 3),
        }

    def _pack_set(r: dict) -> dict:
        return {
            "name": r.get("set", r.get("name", r["slug"])),
            "name_zh": r.get("set_zh", r.get("name_zh", r.get("set", r.get("name", r["slug"])))),
            "name_en": r.get("set_en", r.get("name_en", r.get("set", r.get("name", r["slug"])))),
            "slug": r["slug"],
            "g": r["games"],
            "wr": round(r["smoothed_wr"], 4),
            "lift": round(r["lift"], 4),
            "avg": round(r["avg_lift"], 4),
            "res": round(r["residual"], 4),
            "score": round(r.get("lcb_residual", r["residual"]), 4),
            "badScore": round(r.get("ucb_residual", r["residual"]), 4),
        }

    visible_cids = [int(r["champion_id"]) for r in records]
    visible_cid_set = set(visible_cids)
    for cid in visible_cids:
        meta = champ_meta.get(cid)
        if meta is None:
            continue
        picks = champ_picks.get(cid, {"top": {}, "bot": {}})
        top_buckets = {}
        bot_buckets = {}
        for rarity in RARITY_ORDER:
            top_rows = picks["top"].get(rarity, [])
            bot_rows = picks["bot"].get(rarity, [])
            for r in top_rows + bot_rows:
                used_aug_ids.add(r["augment_id"])
            top_buckets[rarity] = [_pack(r) for r in top_rows]
            bot_buckets[rarity] = [_pack(r) for r in bot_rows]
        pairs = [
            {
                "id": row["teammate_id"],
                "g": row["games"],
                "wr": round(row["raw_wr"], 4),
                "expected": round(row["expected_wr"], 4),
                "lift": round(row["lift"], 4),
                "z": round(row["z_score"], 3),
            }
            for row in champ_synergy.get(cid, [])
            if row["teammate_id"] in visible_cid_set
        ]
        js_champs[str(cid)] = {
            "name": meta["name"],
            "name_zh": meta.get("name_zh", meta["name"]),
            "name_en": meta.get("name_en", meta.get("alias", meta["name"])),
            "alias": meta.get("alias", ""),
            "image": meta.get("image", ""),
            "tags": meta.get("tags") or [],
            "top": top_buckets,
            "bot": bot_buckets,
            "sets": {
                "top": [_pack_set(r) for r in champ_sets.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_sets.get(cid, {}).get("bot", [])],
            },
            "items": {
                "top": [_pack_set(r) for r in champ_item_styles.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_item_styles.get(cid, {}).get("bot", [])],
            },
            "augTypes": {
                "top": [_pack_set(r) for r in champ_augment_types.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_augment_types.get(cid, {}).get("bot", [])],
            },
            "pairs": pairs,
        }
    js_augs = {
        str(aid): {
            "name": aug_meta[aid]["name"],
            "name_zh": aug_meta[aid].get("name_zh", aug_meta[aid]["name"]),
            "name_en": aug_meta[aid].get("name_en", aug_meta[aid]["name"]),
            "icon": aug_meta[aid]["icon"],
            "rarity": aug_meta[aid].get("rarity", ""),
            "desc": aug_meta[aid].get("desc", ""),
            "desc_zh": aug_meta[aid].get("desc_zh", aug_meta[aid].get("desc", "")),
            "desc_en": aug_meta[aid].get("desc_en", ""),
            "set": aug_meta[aid].get("set", ""),
            "set_zh": aug_meta[aid].get("set_zh", aug_meta[aid].get("set", "")),
            "set_en": aug_meta[aid].get("set_en", aug_meta[aid].get("set", "")),
            "setSlug": aug_meta[aid].get("setSlug", ""),
            "sets": aug_meta[aid].get("sets", []),
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
        /* Body = Noto Sans TC (modern sans, readable in dense UI).  Serif
           is reserved for small captions — see `.subtitle`,
           `.aug .alift`. */
        font-family: "Noto Sans TC", -apple-system, "Segoe UI",
                     "Microsoft JhengHei", "PingFang TC", sans-serif;
        padding: 32px 24px 64px;
    }
    h1 { margin: 0 0 4px; font-weight: 600; font-size: 22px; }
    /* Mincho-only captions — opt-in serif for the three small metadata
       lines the user picked out: page subtitle, detail-panel sub-heading,
       and augment card's lift/games row. */
    .subtitle,
    .aug .alift {
        font-family: "Noto Serif TC", "Source Han Serif TC",
                     "PingFang TC", "PMingLiU", "Songti TC", serif;
    }
    .subtitle { color: #9aa0a6; font-size: 13px; }
    /* Top header row — title on the left, GitHub star CTA on the right. */
    .page-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        margin-bottom: 16px;
    }
    .page-actions {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
    }
    .app-shell {
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        gap: 24px;
        align-items: start;
    }
    .app-shell.with-side-panel {
        grid-template-columns: minmax(0, 1fr) 320px;
    }
    .main-col { min-width: 0; }
    .icon-btn,
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
    .icon-btn {
        cursor: pointer;
        font: inherit;
    }
    .icon-btn:hover,
    .gh-star:hover { background: #30363d; border-color: #58606b; }
    .icon-btn svg,
    .gh-star svg { flex-shrink: 0; }
    .gh-star-mobile-label { display: none; }
    .lang-toggle { min-width: 56px; justify-content: center; }
    .lang-toggle span { font-size: 12px; letter-spacing: 0; }
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
    .filter-tools {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-left: auto;
        flex: 1;
        justify-content: flex-end;
    }
    .tool-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 34px;
        padding: 6px 12px;
        background: #21262d;
        color: #e6e8eb;
        border: 1px solid #30363d;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
        font-family: inherit;
        cursor: pointer;
        transition: background 0.12s, border-color 0.12s, color 0.12s;
    }
    .tool-btn:hover { background: #2a3142; border-color: #58606b; }
    .tool-btn.active {
        background: #f5d780;
        border-color: #f5d780;
        color: #231802;
    }
    .tool-btn.ghost {
        background: transparent;
        color: #c5cad3;
    }
    .tool-btn.ghost:hover {
        background: rgba(255,255,255,0.04);
    }
    .search-wrap {
        position: relative;
        flex: 1;
        max-width: 300px;
        min-width: 160px;
    }
    .search-wrap svg {
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        color: #6b7280;
        pointer-events: none;
    }
    .search-wrap:focus-within svg { color: #9aa0a6; }
    .search {
        width: 100%;
        padding: 7px 12px 7px 30px;
        background: #0b0e13;
        color: #e6e8eb;
        border: 1px solid #30363d;
        border-radius: 6px;
        font-size: 13px;
        font-family: inherit;
        outline: none;
        transition: border-color .12s, box-shadow .12s;
    }
    .search:focus {
        border-color: #58606b;
        box-shadow: 0 0 0 3px rgba(88,96,107,0.18);
    }
    .shown-count { color: #6b7280; font-size: 12px; white-space: nowrap; }
    .shown-count #shown-n {
        color: #e6e8eb;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
    }
    .side-panel {
        position: sticky;
        top: 24px;
        max-height: calc(100vh - 48px);
        overflow-y: auto;
        overscroll-behavior: contain;
        scrollbar-gutter: stable;
        background: #11151d;
        border: 1px solid #1f2530;
        border-radius: 12px;
        padding: 14px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.22);
    }
    .side-panel.is-modal-open {
        display: block;
    }
    .side-panel.is-hidden {
        display: none;
    }
    .side-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
    }
    .side-head h2 {
        margin: 0 0 4px;
        font-size: 16px;
        font-weight: 600;
    }
    .side-close,
    .detail-close,
    .rec-fab {
        border: 1px solid #30363d;
        background: #1b2030;
        color: #e6e8eb;
        font-family: inherit;
        font-weight: 700;
        cursor: pointer;
    }
    .side-close,
    .detail-close {
        display: none;
        width: 34px;
        height: 34px;
        border-radius: 999px;
        align-items: center;
        justify-content: center;
        font-size: 18px;
        line-height: 1;
        flex-shrink: 0;
    }
    .rec-fab {
        display: none;
        position: fixed;
        right: 14px;
        bottom: 14px;
        z-index: 40;
        min-height: 46px;
        padding: 0 16px;
        border-radius: 999px;
        box-shadow: 0 12px 30px rgba(0,0,0,0.36);
    }
    .rec-fab:not(.is-hidden) {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
    }
    .side-sub {
        color: #9aa0a6;
        font-size: 12px;
        line-height: 1.55;
    }
    .pick-slots {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin: 14px 0 10px;
    }
    .pick-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        min-height: 36px;
        padding: 6px 10px;
        border-radius: 999px;
        border: 1px solid #30363d;
        background: #1b2030;
        color: #e6e8eb;
        font-size: 12px;
        font-weight: 600;
        font-family: inherit;
        cursor: pointer;
    }
    .pick-chip img {
        width: 22px;
        height: 22px;
        border-radius: 999px;
        display: block;
        object-fit: cover;
        background: #2a3142;
        border: 1px solid rgba(255,255,255,0.08);
        flex-shrink: 0;
    }
    .pick-chip.empty {
        border-style: dashed;
        color: #6b7280;
        background: transparent;
        cursor: default;
    }
    .pick-chip .ord {
        width: 18px;
        height: 18px;
        border-radius: 999px;
        background: #f5d780;
        color: #231802;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 11px;
        font-weight: 700;
        flex-shrink: 0;
    }
    .pick-note {
        min-height: 18px;
        color: #9aa0a6;
        font-size: 11px;
        margin-bottom: 10px;
    }
    .rec-list {
        display: grid;
        gap: 8px;
    }
    .panel-empty {
        color: #6b7280;
        font-size: 12px;
        line-height: 1.6;
        padding: 8px 0 4px;
    }
    .rec-row {
        display: grid;
        grid-template-columns: 22px 40px 1fr;
        gap: 8px;
        align-items: center;
        padding: 8px;
        border-radius: 10px;
        background: #1b2030;
        border: 1px solid rgba(255,255,255,0.05);
        cursor: pointer;
        transition: background 0.12s, border-color 0.12s, transform 0.08s;
    }
    .rec-row:hover {
        background: #20263a;
        border-color: rgba(245,215,128,0.28);
        transform: translateY(-1px);
    }
    .rec-rank {
        color: #9aa0a6;
        font-size: 11px;
        font-weight: 700;
        text-align: center;
        font-variant-numeric: tabular-nums;
    }
    .rec-row img {
        width: 40px;
        height: 40px;
        border-radius: 8px;
        display: block;
        background: #2a3142;
    }
    .rec-main {
        min-width: 0;
    }
    .rec-name {
        display: block;
        color: #e6e8eb;
        font-size: 13px;
        font-weight: 600;
        line-height: 1.25;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .rec-meta {
        display: block;
        margin-top: 2px;
        color: #9aa0a6;
        font-size: 11px;
        line-height: 1.35;
        font-variant-numeric: tabular-nums;
    }
    .rec-meta .z {
        color: #6bd16b;
        font-weight: 700;
    }
    /* Empty filter state — surfaces when role × search yields zero champs.
       Mincho italic to match the caption typography elsewhere, deliberately
       gentle (not an error) since nothing actually broke. */
    .empty-state {
        display: none;
        margin: 32px auto;
        max-width: 480px;
        padding: 24px;
        text-align: center;
        color: #9aa0a6;
        font-family: "Noto Serif TC", "Source Han Serif TC", serif;
        font-size: 14px;
        font-style: italic;
        line-height: 1.6;
    }
    .empty-state.visible { display: block; }
    .empty-state strong {
        display: block;
        margin-bottom: 4px;
        color: #c5cad3;
        font-style: normal;
        font-weight: 600;
        font-size: 16px;
    }
    .tier-block { margin-bottom: 22px; position: relative; }
    .tier-block.hidden { display: none; }
    /* Tier name on its own line above the grid (replaces the old left-side
       full-height ornament bar).  A hairline rule tinted with the tier's
       colour trails the heading, visually anchoring the grid to the pill. */
    .tier-heading {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 16px 0 10px;
        padding-bottom: 8px;
        font-size: 14px;
        font-weight: 600;
        border-bottom: 1px solid color-mix(in oklab, var(--tier-color, #555) 30%, transparent);
    }
    /* OP block: faint radial wash behind the grid to elevate the apex tier
       without resorting to a full coloured backdrop.  Same trick on T1 with
       warmer hue and lower alpha. */
    .tier-block[data-tier="OP"] {
        background:
            radial-gradient(ellipse 70% 60% at 50% 60%,
                rgba(216,184,255,0.045) 0%, transparent 75%);
        border-radius: 12px;
        padding: 2px 6px 8px;
    }
    .tier-block[data-tier="T1"] {
        background:
            radial-gradient(ellipse 70% 60% at 50% 60%,
                rgba(255,120,80,0.028) 0%, transparent 75%);
        border-radius: 12px;
        padding: 2px 6px 8px;
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
        /* Champion thumbnail wears its tier's colour as a 2px frame.
           Non-OP tiers use a solid border; OP gets a prismatic gradient
           via the .tier-block[data-tier="OP"] .champ rule below. */
        border: 2px solid var(--tier-color, #555);
        cursor: pointer;
        transition: transform .08s, box-shadow .08s, filter .08s;
    }
    .champ:hover { transform: translateY(-1px); }
    .champ.detail-selected {
        transform: translateY(-2px);
        filter: brightness(1.08);
        box-shadow: 0 0 0 1px #fff, 0 6px 16px rgba(0,0,0,0.6);
    }
    .champ.pick-selected {
        box-shadow:
            inset 0 0 0 2px rgba(245,215,128,0.95),
            0 0 0 1px rgba(245,215,128,0.35),
            0 6px 16px rgba(0,0,0,0.38);
    }
    .champ.pick-selected::before {
        content: attr(data-pick-rank);
        position: absolute;
        top: 4px;
        left: 4px;
        width: 18px;
        height: 18px;
        border-radius: 999px;
        background: #f5d780;
        color: #231802;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 11px;
        font-weight: 800;
        z-index: 4;
        box-shadow: 0 1px 6px rgba(0,0,0,0.35);
    }
    /* OP-tier champions get the "棱彩飾框" — Prismatic decorative frame —
       so they're as visually distinct from T1 as Prismatic augments are
       from Gold ones.  Double-background trick: inner dark colour clips to
       padding-box, iridescent gradient renders on border-box, the transparent
       2px border lets the gradient show.  prismShift animates the hue. */
    .tier-block[data-tier="OP"] .champ {
        border-color: transparent;
        background:
            linear-gradient(#1f2530, #1f2530) padding-box,
            linear-gradient(135deg,
                #ffffff 0%, #e7d5ff 18%, #bcd6ff 36%,
                #ffd5ec 58%, #fff1c8 78%, #ffffff 100%) border-box;
        background-size: auto, 220% 220%;
        animation: prismShift 6s ease-in-out infinite;
        box-shadow: 0 0 8px rgba(220,180,255,0.45);
    }
    /* T1 = "premium red" — solid red would just look like a flat tier band,
       so promote it with a hot-coal gradient (orange-red → deep crimson →
       warm highlight), a slow shimmer (slower than OP so the hierarchy is
       legible), and a subtle red halo.  Reads as "valuable but not OP". */
    .tier-block[data-tier="T1"] .champ {
        border-color: transparent;
        background:
            linear-gradient(#1f2530, #1f2530) padding-box,
            linear-gradient(135deg,
                #ffb380 0%,   /* hot orange highlight */
                #ff5a3c 32%,  /* main red-orange */
                #c8262c 62%,  /* deep crimson */
                #ff8050 100%  /* warm trailing highlight */
            ) border-box;
        background-size: auto, 220% 220%;
        animation: prismShift 9s ease-in-out infinite;
        box-shadow: 0 0 6px rgba(255,90,60,0.42);
    }
    .champ img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
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
    /* Visually hidden but kept in the DOM as text — so browser Find on Page
       (Ctrl+F / Cmd+F) can still match English aliases like "Aatrox" while
       only the localized zh-TW name is visually drawn. */
    .sr-only {
        position: absolute;
        width: 1px; height: 1px;
        padding: 0; margin: -1px;
        overflow: hidden;
        clip: rect(0,0,0,0);
        white-space: nowrap;
        border: 0;
    }
    .detail {
        margin: 6px 0 4px;
        background: #1b2030;
        border-radius: 10px;
        padding: 14px 16px 16px;
        position: relative;
        animation: slideDown .18s ease-out;
    }
    .detail-close {
        position: absolute;
        top: 10px;
        right: 10px;
        z-index: 1;
        font-size: 18px;
        line-height: 1;
    }
    @keyframes slideDown {
        from { opacity: 0; transform: translateY(-4px); }
        to { opacity: 1; transform: translateY(0); }
    }
    .detail-head {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 12px;
    }
    .detail-avatar {
        width: 34px;
        height: 34px;
        border-radius: 8px;
        object-fit: cover;
        border: 1px solid rgba(255,255,255,0.12);
        box-shadow: 0 4px 10px rgba(0,0,0,0.24);
        flex: 0 0 auto;
    }
    .detail-head .cname { font-size: 16px; font-weight: 600; }
    .detail-section + .detail-section {
        margin-top: 18px;
        padding-top: 14px;
        border-top: 1px solid rgba(255,255,255,0.06);
    }
    .detail-section-head {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 10px;
    }
    .detail-section-head h3 {
        margin: 0;
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }
    .section-meta {
        color: #9aa0a6;
        font-size: 11px;
        font-family: "Noto Serif TC", "Source Han Serif TC", serif;
    }
    .aug-set-summary {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        max-width: 100%;
        padding: 3px 8px;
        border-radius: 999px;
        background: rgba(143, 216, 244, 0.10);
        border: 1px solid rgba(143, 216, 244, 0.24);
        color: #c9eefa;
        font-size: 10px;
        line-height: 1.35;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        cursor: help;
    }
    .aug-set-summary.bad {
        background: rgba(255, 125, 125, 0.09);
        border-color: rgba(255, 125, 125, 0.24);
        color: #ffd1d1;
    }
    .aug-set-summary .sum-item {
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .fit-list {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(108px, 1fr));
        gap: 8px;
    }
    .fit-card {
        background: #11151d;
        border: 1px solid rgba(255,255,255,0.05);
        border-radius: 8px;
        padding: 8px;
        min-width: 0;
    }
    .fit-card.good {
        border-color: rgba(107, 209, 107, 0.22);
        background: linear-gradient(180deg, rgba(107, 209, 107, 0.08), #11151d 42%);
    }
    .fit-card.bad {
        border-color: rgba(255, 107, 107, 0.22);
        background: linear-gradient(180deg, rgba(255, 107, 107, 0.07), #11151d 42%);
    }
    .fit-name {
        color: #e6e8eb;
        font-size: 12px;
        font-weight: 700;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .fit-score {
        margin-top: 4px;
        font-size: 12px;
        font-weight: 700;
    }
    .fit-card.good .fit-score { color: #6bd16b; }
    .fit-card.bad .fit-score { color: #ff8b8b; }
    .fit-meta {
        margin-top: 2px;
        color: #9aa0a6;
        font-size: 10px;
        line-height: 1.35;
    }
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
    .detail-col-heading {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 0 0 8px;
        min-width: 0;
        flex-wrap: wrap;
    }
    .detail-col-heading h3 { margin: 0; }
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
    /* When an augment sits near the top of the viewport, JS sets .flip-tip
       so the tooltip drops below the card instead of clipping above. */
    .aug.flip-tip .aug-tip {
        bottom: auto;
        top: calc(100% + 8px);
    }
    .aug.flip-tip .aug-tip::after {
        top: auto;
        bottom: 100%;
        border-top-color: transparent;
        border-bottom-color: #0b0e13;
    }
    .aug:hover .aug-tip,
    .aug:focus-visible .aug-tip { opacity: 1; }
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
    .aug-tip-score {
        color: #d4dae4;
        font-size: 10px;
        margin-top: 4px;
    }
    .aug-tip-set {
        color: #8fd8f4;
        font-size: 10px;
        margin-bottom: 4px;
    }
    .aug.rarity-kGold   { box-shadow: inset 0 0 0 2px #f5c518; }
    .aug.rarity-kSilver { box-shadow: inset 0 0 0 2px #c0c5cc; }
    .aug.rarity-kPrismatic { box-shadow: inset 0 0 0 2px #d36bff; }
    .mate-list {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
        gap: 10px;
    }
    .mate-list.empty-list { color: #6b7280; font-size: 11px; padding: 8px 0; }
    .mate-card {
        display: grid;
        grid-template-columns: 42px 1fr;
        gap: 8px;
        align-items: center;
        padding: 8px;
        background: #11151d;
        border-radius: 8px;
        border: 1px solid rgba(255,255,255,0.04);
    }
    .mate-card img {
        width: 42px;
        height: 42px;
        border-radius: 8px;
        display: block;
        background: #2a3142;
    }
    .mate-card .mname {
        font-size: 12px;
        font-weight: 600;
        color: #e6e8eb;
        line-height: 1.25;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .mate-card .mwr {
        margin-top: 2px;
        font-size: 11px;
        font-weight: 700;
    }
    .mate-card.good .mwr { color: #6bd16b; }
    .mate-card.bad .mwr { color: #ff6b6b; }
    .mate-card .mmeta {
        margin-top: 2px;
        font-size: 10px;
        color: #9aa0a6;
        font-family: "Noto Serif TC", "Source Han Serif TC", serif;
        font-variant-numeric: tabular-nums;
        line-height: 1.35;
    }
    .mate-card .mmeta .mmeta-z { white-space: nowrap; }
    .empty { color: #6b7280; font-size: 12px; }
    .footer {
        margin-top: 40px;
        padding-top: 24px;
        border-top: 1px solid #1f2530;
        color: #6b7280;
        font-size: 11px;
        text-align: center;
        line-height: 1.7;
    }
    .footer .cutoffs {
        font-variant-numeric: tabular-nums;
        letter-spacing: 0.02em;
    }
    .footer .cutoffs b {
        color: #c5cad3;
        font-weight: 600;
        margin-right: 2px;
    }
    .footer .freshness {
        margin-top: 6px;
        color: #555a63;
    }
    .footer .disclaimer {
        max-width: 760px;
        margin: 20px auto 0;
        padding-top: 14px;
        border-top: 1px solid #16191f;
        color: #555a63;
        font-size: 10px;
    }
    @media (max-width: 1080px) {
        .app-shell,
        .app-shell.with-side-panel { grid-template-columns: 1fr; }
        .side-panel {
            position: static;
            max-height: none;
            overflow: visible;
            order: -1;
        }
    }
    /* Mobile / narrow viewport: switch the detail panel from two columns
       (best / worst) to a single stack so prismatic / gold / silver rows
       stay readable, and shrink the tier-row label so champions get more
       space.  ~700px is around where the two-column layout starts looking
       cramped on most phones. */
    @media (max-width: 700px) {
        body { padding: 18px 10px 40px; }
        body.rec-modal-open,
        body.detail-modal-open { overflow: hidden; }
        h1 { font-size: 18px; }
        .subtitle { font-size: 12px; }
        /* Keep title/subtitle on the left and utility actions pinned to the
           top-right corner, so the header doesn't burn a full extra row. */
        .page-header {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            align-items: start;
            gap: 8px 10px;
            margin-bottom: 12px;
        }
        .page-header > div:first-child { min-width: 0; }
        .page-actions {
            justify-self: end;
            align-self: start;
            flex-wrap: nowrap;
        }
        /* Filter bar wraps tighter; search input becomes full-width on
           its own row. */
        .filter-bar { padding: 8px; gap: 8px; }
        .role-chips { gap: 4px; }
        .chip { padding: 4px 10px; font-size: 11px; }
        .filter-tools {
            margin-left: 0;
            width: 100%;
            justify-content: space-between;
            flex-wrap: wrap;
        }
        .tool-btn { min-height: 36px; }
        .side-panel {
            position: fixed;
            z-index: 60;
            left: 12px;
            right: 12px;
            top: 56px;
            bottom: 18px;
            max-height: none;
            overflow: auto;
            padding: 14px;
            border-radius: 14px;
            box-shadow: 0 22px 60px rgba(0,0,0,0.58);
        }
        body.rec-modal-open::before,
        body.detail-modal-open::before {
            content: "";
            position: fixed;
            inset: 0;
            z-index: 55;
            background: rgba(5, 8, 13, 0.72);
        }
        .side-close { display: inline-flex; }
        .detail-host {
            position: fixed;
            z-index: 70;
            inset: 0;
            overflow: auto;
            padding: 56px 12px 18px;
            -webkit-overflow-scrolling: touch;
        }
        .detail-host .detail {
            max-width: 680px;
            min-height: 100%;
            margin: 0 auto;
            padding: 14px;
            border: 1px solid #30363d;
            border-radius: 14px;
            box-shadow: 0 22px 60px rgba(0,0,0,0.58);
        }
        .detail-close { display: inline-flex; }
        .rec-fab { display: none; }
        .rec-fab:not(.is-hidden) { display: inline-flex; }
        .side-sub { font-size: 11px; }
        .pick-slots { gap: 6px; }
        .search { max-width: none; min-width: 0; }
        /* Tier heading slimmer; pill stays inline. */
        .tier-heading { margin: 6px 0; gap: 6px; }
        .tier-pill { padding: 3px 12px; font-size: 14px; }
        .tier-count { font-size: 11px; }
        /* Lock to 6 champions per row on mobile (instead of auto-fill which
           packs 7-8 in and makes icons tiny). */
        .tier-grid { grid-template-columns: repeat(6, 1fr); gap: 5px; }
        .detail-head {
            flex-direction: row;
            align-items: center;
            gap: 10px;
            padding-right: 42px;
        }
        .detail-avatar {
            display: block;
            width: 42px;
            height: 42px;
            border-radius: 9px;
        }
        .detail-section-head {
            flex-direction: column;
            align-items: flex-start;
            gap: 4px;
        }
        .aug-set-summary {
            max-width: 100%;
            white-space: normal;
        }
        .section-meta {
            font-size: 10px;
            line-height: 1.4;
        }
        .detail-cols { grid-template-columns: 1fr; gap: 14px; }
        .detail-cols.pair-cols { grid-template-columns: 1fr; gap: 14px; }
        .detail-cols.pair-cols .detail-col h3 { margin-bottom: 6px; font-size: 12px; }
        .detail-cols.pair-cols .detail-col-heading h3 { margin-bottom: 0; }
        /* Drop the rarity colored bar (label) on mobile to recover horizontal
           space.  Each augment card still has a rarity-coloured border, so
           which row is which is obvious. */
        .rarity-row { grid-template-columns: 1fr; gap: 4px; }
        .rlabel { display: none; }
        /* Each rarity row shows exactly the same 5 augments (top / bot),
           so force 5 columns and let each card shrink to fit. */
        .aug-list { grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 4px; }
        .mate-list { grid-template-columns: 1fr; gap: 6px; }
        .mate-card {
            grid-template-columns: 34px 1fr;
            gap: 5px;
            padding: 5px;
            min-width: 0;
        }
        .mate-card > div { min-width: 0; }
        .mate-card img {
            width: 34px;
            height: 34px;
            border-radius: 6px;
        }
        .mate-card .mname { font-size: 11px; }
        .mate-card .mwr { font-size: 10px; }
        .mate-card .mmeta { font-size: 9px; }
        .mate-card .mmeta .mmeta-label,
        .mate-card .mmeta .mmeta-z,
        .mate-card .mmeta .mmeta-games { display: none; }
        .aug { padding: 5px 3px; }
        .aug img { width: 36px; height: 36px; }
        .aug .aname { font-size: 9px; min-height: 22px; }
        .aug .awr { font-size: 10px; }
        /* Hide the lift% / games count on mobile - keep cards compact.
           Numbers still available on hover (tooltip) and via the title attr. */
        .aug .alift { display: none; }
        .aug-tip { display: none; }
        /* Touch-target floor (WCAG 2.5.5).  Chips were 4×10 padding on 11px
           font ≈ 32 px tall.  Bump to a real 44 px tap area without growing
           the visual pill, by adding transparent vertical padding. */
        .chip { padding: 8px 12px; font-size: 11px; min-height: 36px; }
        .icon-btn,
        .gh-star { padding: 8px 14px; min-height: 36px; }
        .gh-star svg,
        .gh-star-full-label { display: none; }
        .gh-star-mobile-label { display: inline; }
        .lang-toggle { min-width: 0; }
    }
    @media (min-width: 320px) and (max-width: 359px) {
        .mate-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (min-width: 360px) and (max-width: 700px) {
        .mate-list { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    /* Keyboard a11y: every interactive element gets a visible focus ring
       when focused via keyboard (not mouse click).  Uses the tier accent
       (or a neutral white when no tier is in scope) and stays well clear
       of the resting border colour. */
    .chip:focus-visible,
    .icon-btn:focus-visible,
    .gh-star:focus-visible,
    .tool-btn:focus-visible,
    .side-close:focus-visible,
    .detail-close:focus-visible,
    .rec-fab:focus-visible,
    .pick-chip:focus-visible,
    .rec-row:focus-visible,
    .search:focus-visible,
    .champ:focus-visible,
    .aug:focus-visible {
        outline: 2px solid #f5e8ff;
        outline-offset: 2px;
    }
    /* Reduced-motion override.  Disables prismShift / shineSweep /
       slideDown so vestibular-sensitive users don't get hue drift and
       sweep effects across the page. */
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {
            animation-duration: 0.001ms !important;
            animation-iteration-count: 1 !important;
            transition-duration: 0.001ms !important;
        }
    }
    """

    payload = {
        "champs": js_champs,
        "augs": js_augs,
        "min_games_per_pair": min_games_per_pair,
        "min_synergy_games": min_synergy_games,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    og_patch_label = f"patch {patch_prefix}" if patch_prefix else "all patches"
    og_title = f"{header_title}資料庫"
    og_desc = f"{og_patch_label}｜【英雄 x 海克斯勝率 · 組隊推薦】&#10;by 路燈"

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
        meta_lines.append("<meta property='og:image:width' content='512'>")
        meta_lines.append("<meta property='og:image:height' content='512'>")
        meta_lines.append("<meta property='og:image:alt' content='ARAM Mayhem Database preview'>")
        meta_lines.append("<meta name='twitter:card' content='summary'>")
        meta_lines.append(f"<meta name='twitter:image' content='{og_image}'>")
        meta_lines.append("<meta name='twitter:image:alt' content='ARAM Mayhem Database preview'>")
    else:
        meta_lines.append("<meta name='twitter:card' content='summary'>")
    meta_lines.append(f"<meta name='twitter:title' content=\"{og_title}\">")
    meta_lines.append(f"<meta name='twitter:description' content=\"{og_desc}\">")

    parts: list[str] = []
    parts.append("<!doctype html><html lang='zh-Hant'><head>")
    parts.extend(meta_lines)
    parts.extend(
        render_analytics_tags(
            cloudflare_token=cloudflare_analytics_token,
            ga_measurement_id=ga_measurement_id,
        )
    )
    # Webfonts: Noto Sans TC for everything by default; Noto Serif TC only
    # for a couple of small captions (subtitle, panel meta, augment lift)
    # where the mincho gives a "footnote" feel without hurting legibility.
    # `display=swap` lets system fallback paint immediately; weights pruned
    # to what each face actually uses on the page.
    parts.append(
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        "<link href='https://fonts.googleapis.com/css2"
        "?family=Noto+Sans+TC:wght@400;500;600;700"
        "&family=Noto+Serif+TC:wght@400;500"
        "&display=swap' rel='stylesheet'>"
    )
    parts.append(f"<style>{css}</style></head><body>")
    # Header: title + subtitle on the left, language toggle + GitHub star on the right.
    # The repo name is the canonical project URL; if the user later forks /
    # renames, update REPO_URL below.
    REPO_URL = "https://github.com/Lanternko/ARAM-Mayhem-Database"
    short_patch = f"patch {patch_prefix}" if patch_prefix else "全 patch"
    date_str = f"更新於 {build_date}" if build_date else "日期未標"
    globe_icon = (
        "<svg viewBox='0 0 24 24' width='16' height='16' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' aria-hidden='true'>"
        "<circle cx='12' cy='12' r='10'></circle>"
        "<path d='M2 12h20'></path>"
        "<path d='M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10Z'></path>"
        "</svg>"
    )
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
    parts.append(f"<h1 id='site-title'>{header_title}</h1>")
    parts.append(
        f"<div class='subtitle' id='site-subtitle'>"
        f"{short_patch}"
        f"</div>"
    )
    parts.append("</div>")
    parts.append("<div class='page-actions'>")
    parts.append(
        "<button class='icon-btn lang-toggle' id='lang-toggle' type='button' "
        "title='Switch to English' aria-label='切換語言'>"
        f"{globe_icon}<span id='lang-toggle-label'>EN</span>"
        "</button>"
    )
    parts.append(
        f"<a class='gh-star' href='{REPO_URL}' target='_blank' rel='noopener' "
        f"title='覺得有用請幫忙按 Star ⭐'>"
        f"{gh_icon}<span class='gh-star-full-label'>Star on GitHub</span>"
        "<span class='gh-star-mobile-label'>⭐ GitHub</span>"
        f"</a>"
    )
    parts.append("</div>")
    parts.append("</div>")  # /page-header
    parts.append("<div class='app-shell'>")
    parts.append("<div class='main-col'>")

    # Filter bar: role chips + free-text search + live "N shown" counter.
    parts.append("<div class='filter-bar'>")
    parts.append("<div class='role-chips'>")
    parts.append('<button class="chip active" data-role="" data-label-zh="★ All" data-label-en="★ All">★ All</button>')
    for role_en, role_zh in [
        ("Assassin", "刺客"),
        ("Fighter", "戰士"),
        ("Mage", "法師"),
        ("Marksman", "射手"),
        ("Support", "輔助"),
        ("Tank", "坦克"),
    ]:
        parts.append(
            f'<button class="chip" data-role="{role_en}" data-label-zh="{role_zh}" '
            f'data-label-en="{role_en}">{role_zh}</button>'
        )
    parts.append("</div>")  # /role-chips
    parts.append("<div class='filter-tools'>")
    parts.append(
        '<button class="tool-btn" id="recommend-mode" type="button" '
        'aria-pressed="false">選擇你的隊友：關</button>'
    )
    parts.append(
        '<button class="tool-btn ghost" id="clear-picks" type="button">清空選取</button>'
    )
    # Search input wrapped in a label with an inline magnifier SVG sitting
    # in the input's left padding (the wrapper is positioned, the input
    # has padding-left to clear the icon).
    search_icon = (
        "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' aria-hidden='true'>"
        "<circle cx='11' cy='11' r='7'></circle>"
        "<line x1='21' y1='21' x2='16.5' y2='16.5'></line></svg>"
    )
    parts.append(
        "<label class='search-wrap'>"
        f"{search_icon}"
        '<input class="search" id="champ-search" type="search" '
        'placeholder="搜尋英雄（中 / 英）" autocomplete="off" '
        'aria-label="搜尋英雄">'
        "</label>"
    )
    parts.append(
        f'<span class="shown-count"><span id="shown-n">{len(records)}</span> / {len(records)} '
        "<span id='shown-unit'>隻</span></span>"
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
            " <span class='tier-count-unit'>隻</span>"
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
            aria_label = f"{r['name']} {alias}，tier {tier}，勝率 {wr_pct}"
            parts.append(
                f"<div class='champ' data-cid='{r['champion_id']}' "
                f"data-name-zh=\"{html.escape(r['name'])}\" "
                f"data-name-en=\"{html.escape(meta.get('name_en', alias or r['name']))}\" "
                f"data-tags='{tag_str}' data-search=\"{search_blob}\" "
                f"data-tier='{tier}' data-wr='{wr_pct}' data-games='{r['games']}' "
                f"data-raw-wr='{r['raw_wr']*100:.1f}%' "
                f"role='button' tabindex='0' "
                f"aria-label=\"{aria_label}\" "
                f"title=\"{title}\">"
                f"<img loading='lazy' src='{r['image']}' alt=''>"
                # The English alias is rendered as screen-reader-only text so
                # Ctrl+F / Cmd+F can find e.g. "Aatrox" even though only the
                # zh-TW name is drawn.  (aria-label already announces it for
                # actual screen readers.)
                f"<span class='sr-only'>{alias}</span>"
                f"<span class='wr'>{wr_pct}</span>"
                f"<span class='name'>{r['name']}</span>"
                f"</div>"
            )
        # Detail host lives INSIDE .tier-grid so it can grid-span all columns
        # and be inserted right after the clicked champion's visual row.
        parts.append(f"<div class='detail-host' data-tier='{tier}'></div>")
        parts.append("</div>")  # /tier-grid
        parts.append("</div>")  # /tier-block

    # Empty state — toggled by JS when all tiers are filtered out.
    parts.append(
        "<div class='empty-state' id='empty-state'>"
        "<strong id='empty-title'>沒有符合條件的英雄</strong>"
        "<span id='empty-copy'>換個角色篩選，或試試英雄中／英文名。</span>"
        "</div>"
    )

    parts.append("<div class='footer'>")
    parts.append(
        "<div class='cutoffs'>"
        "Tier (Bayes WR): "
        "<b>OP</b>≥55% · "
        "<b>T1</b>≥52% · "
        "<b>T2</b>≥50% · "
        "<b>T3</b>≥48% · "
        "<b>T4</b>≥46% · "
        "<b>T5</b>&lt;46%"
        "</div>"
    )
    if build_date:
        parts.append(
            f"<div class='freshness' id='freshness-copy'>{date_str}（{total_games:,} 場） · {patch_label}</div>"
        )
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
    parts.append("</div>")  # /main-col
    parts.append(
        "<aside class='side-panel' id='side-panel'>"
        "<div class='side-head'>"
        "<div>"
        "<h2 id='side-title'>推薦組合排行</h2>"
        "<div class='side-sub' id='side-sub'>"
        "Residual：兩隻英雄同隊的實際勝率 - 預期勝率。<br>"
        "z：residual 除以標準誤，數值越高代表訊號越不像樣本雜訊。<br>"
        "排行依排序分排列：平均 residual × 覆蓋率。"
        "</div>"
        "</div>"
        "<button class='side-close' id='side-close' type='button' aria-label='關閉推薦組合'>×</button>"
        "</div>"
        "<div class='pick-slots' id='pick-slots'></div>"
        "<div class='pick-note' id='pick-note'></div>"
        "<div class='rec-list' id='rec-list'></div>"
        "</aside>"
        "<button class='rec-fab is-hidden' id='rec-fab' type='button'>看推薦組合</button>"
    )
    parts.append("</div>")  # /app-shell

    js = """
    const DATA = __PAYLOAD__;
    const pct = x => (x * 100).toFixed(1) + '%';
    const signed = x => (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%';
    const escHtml = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const HEADER_TITLE_ZH = __HEADER_TITLE_ZH__;
    const HEADER_TITLE_EN = __HEADER_TITLE_EN__;
    const SHORT_PATCH_ZH = __SHORT_PATCH_ZH__;
    const DATE_STR_ZH = __DATE_STR_ZH__;
    const BUILD_DATE = __BUILD_DATE__;
    const PATCH_LABEL = __PATCH_LABEL__;
    const TOTAL_GAMES = __TOTAL_GAMES__;
    const LANG_KEY = 'aram-mayhem-site-lang';
    const SET_RESIDUAL_THRESHOLD = 0.02;
    function trackEvent(name, params = {}) {
        if (typeof gtag === 'function') {
            gtag('event', name, params);
        }
    }
    const COPY = {
        zh: {
            htmlLang: 'zh-Hant',
            subtitle: () => `${SHORT_PATCH_ZH}`,
            searchPlaceholderDesktop: '搜尋英雄（中 / 英）   Ctrl+F',
            searchPlaceholderMobile: '搜尋英雄（中 / 英）',
            searchAria: '搜尋英雄',
            shownUnit: '隻',
            tierUnit: '隻',
            recModeOn: '選擇你的隊友：開',
            recModeOff: '選擇你的隊友：關',
            clearPicks: '清空選取',
            emptyTitle: '沒有符合條件的英雄',
            emptyCopy: '換個角色篩選，或試試英雄中／英文名。',
            freshness: () => `${DATE_STR_ZH}（${TOTAL_GAMES} 場） · ${PATCH_LABEL}`,
            sideTitle: '推薦組合排行',
            sideSub: 'Residual：兩隻英雄同隊的實際勝率 - 預期勝率。<br>z：residual 除以標準誤，數值越高代表訊號越不像樣本雜訊。<br>排行依排序分排列：平均 residual × 覆蓋率。',
            closeRecs: '關閉推薦組合',
            openRecs: n => `看推薦組合 (${n})`,
            langToggleLabel: 'EN',
            langToggleTitle: 'Switch to English',
            langToggleAria: '切換語言',
            removePick: name => `移除 ${name}`,
            pickEmpty: '尚未選擇',
            maxOnly: n => `最多只能選 ${n} 隻英雄。`,
            pickNoteEmpty: n => `最多選 ${n} 隻；排序分 = 平均 residual × 覆蓋率，未覆蓋的 pair 視為 0。`,
            pickNotePartial: want => `目前沒有 ${want}/${want} 全覆蓋候選，以下改用部分 pair 資料排序。`,
            pickNoteReady: (want, minGames) => `已選 ${want}/${MAX_TEAM_PICKS} 隻；pair 門檻 >= ${minGames} 場。`,
            panelEmpty: '先開啟「選擇你的隊友」，再從英雄列表點 1~4 隻英雄。系統會排出最適合補進來的英雄。',
            panelNoData: '這組英雄目前沒有足夠的 pair 資料。',
            detailEmpty: '這個英雄目前沒有可顯示的資料。',
            detailClose: '關閉詳細資訊',
            pairSectionTitle: '搭檔組合',
            pairSectionMeta: minGames => `依照搭檔的適配度排名，不是單純看勝率，至少 ${minGames} 場`,
            setSectionTitle: 'Augment 系列相性',
            setSectionMeta: '保守分數；負值代表相對較好，但未達正訊號',
            itemSectionTitle: '裝備取向',
            itemSectionMeta: '每場只算主出裝；負值仍可能是相對最佳',
            augTypeSectionTitle: 'Augment 類型取向',
            augTypeSectionMeta: '語意分組，同一套保守 residual 分數',
            relativeBest: '相對最佳',
            best: '最佳',
            worst: '最差',
            weak: '偏弱',
            insufficient: '資料不足',
            rarityLabels: { kPrismatic: '彩色', kGold: '金色', kSilver: '銀色' },
            augSetLabel: '系列',
            augTitle: (name, setName, wr, games, desc) => `${name}${setName ? ' · 系列：' + setName : ''} · WR ${wr} · ${games}場${desc ? ' — ' + desc : ''}`,
            augAria: (name, wr, lift, games, desc) => `${name}，勝率 ${wr}，相對基準 ${lift}，樣本 ${games} 場${desc ? '，' + desc : ''}`,
            augTipStat: (wr, lift, games) => `WR ${wr} · ${lift} · ${games}場`,
            augScoreNote: (score, pick, peerPick) => `強度分數 ${score}：勝率提升的保守估計；選取率 ${pick}（同類 ${peerPick}）只作參考。`,
            mateTitle: (name, wr, expectedText, lift, zText, games) => `${name} · WR ${wr}${expectedText} · residual ${lift} · z ${zText} · ${games}場`,
            mateMetaHtml: (lift, zText, games) => `${lift}<span class="mmeta-label"> residual</span><span class="mmeta-z"> · z ${zText}</span><span class="mmeta-games"> · ${games}場</span>`,
            setTitle: (name, res, lift, avg, wr, games) => `${name} · residual ${res} · 英雄 lift ${lift} · 全體平均 ${avg} · WR ${wr} · ${games}場`,
            setMeta: (lift, avg, wr, games) => `英雄 ${lift} · 全體 ${avg} · WR ${wr} · ${games}場`,
            expected: value => ` · 預期 ${value}`,
            detailSectionTitle: 'Augment',
            recRowTitle: (name, fit, liftAvg) => `${name} · 排序分 ${fit} · 平均 residual ${liftAvg}`,
            recRowMeta: (fit, liftAvg, zAvg, minGames, coverage) => `排序 ${fit} · ${liftAvg} residual · z <span class="z">${zAvg}</span> · min ${minGames}場（${coverage}）`,
            champCardTitle: (name, wr, games, raw) => `${name} · WR ${wr} · games ${games} · raw ${raw}`,
            champCardAria: (name, alias, tier, wr) => `${name} ${alias}，tier ${tier}，勝率 ${wr}`,
        },
        en: {
            htmlLang: 'en',
            subtitle: () => `${PATCH_LABEL}`,
            searchPlaceholderDesktop: 'Search champions (ZH / EN)   Ctrl+F',
            searchPlaceholderMobile: 'Search champions (ZH / EN)',
            searchAria: 'Search champions',
            shownUnit: 'shown',
            tierUnit: 'shown',
            recModeOn: 'Teammate mode: On',
            recModeOff: 'Teammate mode: Off',
            clearPicks: 'Clear picks',
            emptyTitle: 'No champions match the current filters',
            emptyCopy: 'Try a different role, or search by Chinese / English champion name.',
            freshness: () => `Updated ${BUILD_DATE} (${TOTAL_GAMES} games) · ${PATCH_LABEL}`,
            sideTitle: 'Recommended teammates',
            sideSub: 'Residual = actual same-team win rate minus expected win rate.<br>z = residual divided by standard error; higher means the signal is less likely to be sample noise.<br>Rows are ranked by average residual × coverage.',
            closeRecs: 'Close recommendations',
            openRecs: n => `Open recommendations (${n})`,
            langToggleLabel: '中',
            langToggleTitle: '切換成中文',
            langToggleAria: 'Switch language',
            removePick: name => `Remove ${name}`,
            pickEmpty: 'Empty',
            maxOnly: n => `You can only pick up to ${n} champions.`,
            pickNoteEmpty: n => `Pick up to ${n}; score = average residual × coverage, with missing pairs treated as 0.`,
            pickNotePartial: want => `No fully covered ${want}/${want} candidates yet, so the list falls back to partial pair coverage.`,
            pickNoteReady: (want, minGames) => `${want}/${MAX_TEAM_PICKS} picked; pair threshold >= ${minGames} games.`,
            panelEmpty: 'Turn on teammate mode, then click 1-4 champions in the grid. The site will rank the best additions.',
            panelNoData: 'This combination does not have enough pair data yet.',
            detailEmpty: 'No detail data is available for this champion yet.',
            detailClose: 'Close details',
            pairSectionTitle: 'Pairings',
            pairSectionMeta: minGames => `Ranked by teammate fit, not raw win rate, at least ${minGames} games`,
            setSectionTitle: 'Augment Sets',
            setSectionMeta: 'Conservative score; negative can still be relative-best',
            itemSectionTitle: 'Item Styles',
            itemSectionMeta: 'One main build per game; negative can still be relative-best',
            augTypeSectionTitle: 'Augment Types',
            augTypeSectionMeta: 'Semantic groups with the same conservative residual score',
            relativeBest: 'Relative Best',
            best: 'Best',
            worst: 'Worst',
            weak: 'Weak',
            insufficient: 'Not enough data',
            rarityLabels: { kPrismatic: 'Prismatic', kGold: 'Gold', kSilver: 'Silver' },
            augSetLabel: 'Set',
            augTitle: (name, setName, wr, games, desc) => `${name}${setName ? ' · Set: ' + setName : ''} · WR ${wr} · ${games} games${desc ? ' — ' + desc : ''}`,
            augAria: (name, wr, lift, games, desc) => `${name}, win rate ${wr}, versus baseline ${lift}, sample ${games} games${desc ? ', ' + desc : ''}`,
            augTipStat: (wr, lift, games) => `WR ${wr} · ${lift} · ${games} games`,
            augScoreNote: (score, pick, peerPick) => `Strength score ${score}: conservative win-rate lift; pick rate ${pick} (peer ${peerPick}) is context, not a penalty.`,
            mateTitle: (name, wr, expectedText, lift, zText, games) => `${name} · WR ${wr}${expectedText} · residual ${lift} · z ${zText} · ${games} games`,
            mateMetaHtml: (lift, zText, games) => `${lift}<span class="mmeta-label"> residual</span><span class="mmeta-z"> · z ${zText}</span><span class="mmeta-games"> · ${games} games</span>`,
            setTitle: (name, res, lift, avg, wr, games) => `${name} · residual ${res} · champion lift ${lift} · global average ${avg} · WR ${wr} · ${games} games`,
            setMeta: (lift, avg, wr, games) => `champ ${lift} · global ${avg} · WR ${wr} · ${games} games`,
            expected: value => ` · expected ${value}`,
            detailSectionTitle: 'Augments',
            recRowTitle: (name, fit, liftAvg) => `${name} · fit score ${fit} · average residual ${liftAvg}`,
            recRowMeta: (fit, liftAvg, zAvg, minGames, coverage) => `fit ${fit} · ${liftAvg} residual · z <span class="z">${zAvg}</span> · min ${minGames} games (${coverage})`,
            champCardTitle: (name, wr, games, raw) => `${name} · WR ${wr} · games ${games} · raw ${raw}`,
            champCardAria: (name, alias, tier, wr) => `${name} ${alias}, tier ${tier}, win rate ${wr}`,
        }
    };
    let currentLang = 'zh';

    function tr() {
        return COPY[currentLang] || COPY.zh;
    }

    function isMobileViewport() {
        return window.matchMedia('(max-width: 700px)').matches;
    }

    function searchPlaceholderFor(copy) {
        return isMobileViewport()
            ? copy.searchPlaceholderMobile
            : copy.searchPlaceholderDesktop;
    }

    function updateSearchPlaceholder() {
        const searchEl = document.getElementById('champ-search');
        if (!searchEl) return;
        const copy = tr();
        searchEl.placeholder = searchPlaceholderFor(copy);
        searchEl.setAttribute('aria-label', copy.searchAria);
    }

    function champName(info) {
        if (!info) return '';
        return currentLang === 'en' ? (info.name_en || info.alias || info.name || '') : (info.name_zh || info.name || info.alias || '');
    }

    function augName(aug) {
        if (!aug) return '';
        return currentLang === 'en' ? (aug.name_en || aug.name || '') : (aug.name_zh || aug.name || '');
    }

    function augDesc(aug) {
        if (!aug) return '';
        if (currentLang === 'en') return aug.desc_en || aug.desc || '';
        return aug.desc_zh || aug.desc || '';
    }

    function augSetName(aug) {
        if (!aug) return '';
        if (currentLang === 'en') return aug.set_en || aug.set || '';
        return aug.set_zh || aug.set || aug.set_en || '';
    }

    function setEntryName(entry) {
        if (!entry) return '';
        if (currentLang === 'en') return entry.name_en || entry.name || '';
        return entry.name_zh || entry.name || entry.name_en || '';
    }

    function buildAugCard(entry, kind) {
        const aug = DATA.augs[entry.id];
        const name = aug ? augName(aug) : '#' + entry.id;
        const icon = aug && aug.icon ? aug.icon : '';
        const rarity = aug ? (aug.rarity || '') : '';
        const desc = augDesc(aug);
        const setName = augSetName(aug);
        const copy = tr();
        const titleAttr = copy.augTitle(name, setName, pct(entry.wr), entry.g, desc);
        const scoreValue = entry.score !== undefined ? entry.score : entry.lift;
        const scoreNote = copy.augScoreNote(
            signed(scoreValue),
            pct(entry.pick || 0),
            pct(entry.peerPick || 0)
        );
        const tooltip = `
            <div class="aug-tip">
                <div class="aug-tip-name">${escHtml(name)}</div>
                ${setName ? `<div class="aug-tip-set">${copy.augSetLabel}: ${escHtml(setName)}</div>` : ''}
                ${desc ? `<div class="aug-tip-desc">${escHtml(desc)}</div>` : ''}
                <div class="aug-tip-stat">${copy.augTipStat(pct(entry.wr), signed(entry.lift), entry.g)}</div>
                <div class="aug-tip-score">${escHtml(scoreNote)}</div>
            </div>
        `;
        // Augment card carries its own ARIA semantics so screen readers and
        // keyboard users get the same info hover tooltip shows.
        const ariaLabel = copy.augAria(name, pct(entry.wr), signed(entry.lift), entry.g, desc);
        return `
            <div class="aug ${kind} rarity-${rarity}"
                 tabindex="0"
                 aria-label="${escHtml(ariaLabel)}"
                 title="${escHtml(titleAttr)}">
                ${icon ? `<img loading="lazy" src="${icon}" alt="">` : '<div style="width:48px;height:48px;margin:0 auto 4px;background:#2a3142;border-radius:6px"></div>'}
                <div class="aname">${escHtml(name)}</div>
                <div class="awr">${pct(entry.wr)}</div>
                <div class="alift">${signed(entry.lift)} · ${entry.g}場</div>
                ${tooltip}
            </div>
        `;
    }

    const RARITIES = [
        { key: 'kPrismatic', css: 'prismatic' },
        { key: 'kGold',      css: 'gold' },
        { key: 'kSilver',    css: 'silver' },
    ];
    const MATE_LIST_LIMIT_DESKTOP = 8;
    const MATE_LIST_LIMIT_MOBILE = 6;

    function buildRarityRow(items, kind, r) {
        const copy = tr();
        const cards = (items || []).map(e => buildAugCard(e, kind)).join('');
        const body = cards
            ? `<div class="aug-list">${cards}</div>`
            : `<div class="aug-list empty-list">${copy.insufficient}</div>`;
        return `
            <div class="rarity-row">
                <div class="rlabel ${r.css}">${copy.rarityLabels[r.key]}</div>
                ${body}
            </div>
        `;
    }

    function renderDetail(cid) {
        const info = DATA.champs[cid];
        if (!info) {
            return `<div class="empty">${tr().detailEmpty}</div>`;
        }
        const copy = tr();
        const top = info.top || {};
        const bot = info.bot || {};
        const setInfo = info.sets || {};
        const setTop = setInfo.top || [];
        const setBot = setInfo.bot || [];
        const itemInfo = info.items || {};
        const augTypeInfo = info.augTypes || {};
        const topRows = RARITIES.map(r => buildRarityRow(top[r.key], 'good', r)).join('');
        const botRows = RARITIES.map(r => buildRarityRow(bot[r.key], 'bad', r)).join('');
        const pairs = info.pairs || [];
        const mateLimit = isMobileViewport() ? MATE_LIST_LIMIT_MOBILE : MATE_LIST_LIMIT_DESKTOP;
        const mateTop = pairs.slice(0, mateLimit);
        const mateBot = [...pairs].slice(-mateLimit).reverse();
        const buildMateCard = (entry, kind) => {
            const mate = DATA.champs[String(entry.id)];
            const name = mate ? champName(mate) : ('#' + entry.id);
            const image = mate && mate.image ? mate.image : '';
            const zText = `${entry.z >= 0 ? '+' : ''}${entry.z.toFixed(2)}`;
            const expectedText = entry.expected !== undefined ? copy.expected(pct(entry.expected)) : '';
            const titleAttr = copy.mateTitle(name, pct(entry.wr), expectedText, signed(entry.lift), zText, entry.g);
            return `
                <div class="mate-card ${kind}" title="${escHtml(titleAttr)}">
                    ${image ? `<img loading="lazy" src="${image}" alt="">` : '<div style="width:42px;height:42px;border-radius:8px;background:#2a3142"></div>'}
                    <div>
                        <div class="mname">${escHtml(name)}</div>
                        <div class="mwr">${pct(entry.wr)}</div>
                        <div class="mmeta">${copy.mateMetaHtml(signed(entry.lift), zText, entry.g)}</div>
                    </div>
                </div>
            `;
        };
        const buildMateList = (items, kind) => {
            if (!items.length) return `<div class="mate-list empty-list">${copy.insufficient}</div>`;
            return `<div class="mate-list">${items.map(entry => buildMateCard(entry, kind)).join('')}</div>`;
        };
        const buildSetSummary = (rows, bad = false) => {
            const visibleSets = rows
                .filter(entry => {
                    const metric = bad ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
                    return bad ? metric <= -SET_RESIDUAL_THRESHOLD : metric >= SET_RESIDUAL_THRESHOLD;
                })
                .slice(0, 3);
            if (!visibleSets.length) return '';
            const titleAttr = visibleSets.map(entry => {
                const name = setEntryName(entry);
                const metric = bad ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
                return `${name} score ${signed(metric)}, residual ${signed(entry.res)}, lift ${signed(entry.lift)}, set avg ${signed(entry.avg)}, WR ${pct(entry.wr)}, ${entry.g} games`;
            }).join('\\n');
            return `
                <div class="aug-set-summary ${bad ? 'bad' : ''}" title="${escHtml(titleAttr)}">
                    ${visibleSets.map(entry => `<span class="sum-item">${escHtml(setEntryName(entry))}</span>`).join('')}
                </div>
            `;
        };
        const buildFitCard = (entry, kind) => {
            const name = setEntryName(entry);
            const score = kind === 'bad' ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
            const titleAttr = copy.setTitle(name, signed(entry.res), signed(entry.lift), signed(entry.avg), pct(entry.wr), entry.g);
            return `
                <div class="fit-card ${kind}" title="${escHtml(titleAttr)}">
                    <div class="fit-name">${escHtml(name)}</div>
                    <div class="fit-score">${signed(score)}</div>
                    <div class="fit-meta">${copy.setMeta(signed(entry.lift), signed(entry.avg), pct(entry.wr), entry.g)}</div>
                </div>
            `;
        };
        const buildFitList = (rows, kind) => {
            if (!rows || !rows.length) return `<div class="mate-list empty-list">${copy.insufficient}</div>`;
            return `<div class="fit-list">${rows.slice(0, 4).map(entry => buildFitCard(entry, kind)).join('')}</div>`;
        };
        const buildAffinitySection = (title, meta, payload) => {
            const bestRows = (payload && payload.top) || [];
            const weakRows = (payload && payload.bot) || [];
            if (!bestRows.length && !weakRows.length) return '';
            return `
                <div class="detail-section">
                    <div class="detail-section-head">
                        <h3>${title}</h3>
                        <span class="section-meta">${meta}</span>
                    </div>
                    <div class="detail-cols">
                        <div class="detail-col best">
                            <h3>${copy.relativeBest}</h3>
                            ${buildFitList(bestRows, 'good')}
                        </div>
                        <div class="detail-col worst">
                            <h3>${copy.weak}</h3>
                            ${buildFitList(weakRows, 'bad')}
                        </div>
                    </div>
                </div>
            `;
        };
        return `
            <button class="detail-close" type="button" title="${escHtml(copy.detailClose)}" aria-label="${escHtml(copy.detailClose)}">&times;</button>
            <div class="detail-head">
                ${info.image ? `<img class="detail-avatar" loading="lazy" src="${info.image}" alt="">` : ''}
                <span class="cname" id="detail-title-${cid}">${escHtml(champName(info))}</span>
            </div>
            <div class="detail-section">
                <div class="detail-section-head">
                    <h3>${copy.detailSectionTitle}</h3>
                </div>
                <div class="detail-cols pair-cols">
                    <div class="detail-col best">
                        <div class="detail-col-heading">
                            <h3>${copy.best}</h3>
                            ${buildSetSummary(setTop)}
                        </div>
                        ${topRows}
                    </div>
                    <div class="detail-col worst">
                        <div class="detail-col-heading">
                            <h3>${copy.worst}</h3>
                            ${buildSetSummary(setBot, true)}
                        </div>
                        ${botRows}
                    </div>
                </div>
            </div>
            ${buildAffinitySection(copy.itemSectionTitle, copy.itemSectionMeta, itemInfo)}
            ${buildAffinitySection(copy.augTypeSectionTitle, copy.augTypeSectionMeta, augTypeInfo)}
            <div class="detail-section">
                <div class="detail-section-head">
                    <h3>${copy.pairSectionTitle}</h3>
                    <span class="section-meta">${copy.pairSectionMeta(DATA.min_synergy_games)}</span>
                </div>
                <div class="detail-cols">
                    <div class="detail-col best">
                        <h3>${copy.best}</h3>
                        ${buildMateList(mateTop, 'good')}
                    </div>
                    <div class="detail-col worst">
                        <h3>${copy.worst}</h3>
                        ${buildMateList(mateBot, 'bad')}
                    </div>
                </div>
            </div>
        `;
    }

    const REC_LIST_LIMIT = 12;
    const MAX_TEAM_PICKS = 4;
    let detailSelected = null;
    let recommendMode = false;
    let recModalOpen = false;
    let teamPicks = [];
    let pickNotice = '';

    function zFmt(x) {
        return `${x >= 0 ? '+' : ''}${x.toFixed(2)}`;
    }

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

    function syncPickDecorations() {
        document.querySelectorAll('.champ').forEach(champ => {
            const cid = champ.getAttribute('data-cid');
            const idx = teamPicks.indexOf(cid);
            champ.classList.toggle('pick-selected', idx !== -1);
            if (idx !== -1) {
                champ.setAttribute('data-pick-rank', String(idx + 1));
            } else {
                champ.removeAttribute('data-pick-rank');
            }
        });
    }

    function aggregateRecommendations() {
        if (!teamPicks.length) return [];
        const pickedSet = new Set(teamPicks);
        const want = teamPicks.length;
        const byCandidate = new Map();
        teamPicks.forEach(anchorId => {
            const info = DATA.champs[anchorId];
            if (!info) return;
            (info.pairs || []).forEach(entry => {
                const candidateId = String(entry.id);
                if (pickedSet.has(candidateId)) return;
                const row = byCandidate.get(candidateId) || {
                    id: candidateId,
                    coverage: 0,
                    zSum: 0,
                    liftSum: 0,
                    wrSum: 0,
                    minGames: Number.POSITIVE_INFINITY,
                };
                row.coverage += 1;
                row.zSum += entry.z;
                row.liftSum += entry.lift;
                row.wrSum += entry.wr;
                row.minGames = Math.min(row.minGames, entry.g);
                byCandidate.set(candidateId, row);
            });
        });
        return [...byCandidate.values()]
            .map(row => ({
                ...row,
                full: row.coverage === want,
                coverageRatio: row.coverage / want,
                fitScore: row.liftSum / want,
                zAvg: row.zSum / row.coverage,
                liftAvg: row.liftSum / row.coverage,
                wrAvg: row.wrSum / row.coverage,
            }))
            .sort((a, b) =>
                b.fitScore - a.fitScore ||
                b.liftAvg - a.liftAvg ||
                b.zAvg - a.zAvg ||
                Number(b.full) - Number(a.full) ||
                b.coverage - a.coverage ||
                b.minGames - a.minGames
            );
    }

    function renderSidePanel() {
        const copy = tr();
        const shell = document.querySelector('.app-shell');
        const panel = document.getElementById('side-panel');
        const fab = document.getElementById('rec-fab');
        const slots = document.getElementById('pick-slots');
        const note = document.getElementById('pick-note');
        const recList = document.getElementById('rec-list');
        if (!shell || !panel || !slots || !note || !recList) return;

        const showPanel = recommendMode && teamPicks.length > 0;
        const isMobile = window.matchMedia('(max-width: 700px)').matches;
        if (!showPanel || !isMobile) recModalOpen = false;
        shell.classList.toggle('with-side-panel', showPanel && !isMobile);
        document.body.classList.toggle('rec-modal-open', showPanel && isMobile && recModalOpen);
        panel.classList.toggle('is-modal-open', showPanel && isMobile && recModalOpen);
        panel.classList.toggle('is-hidden', !showPanel || (isMobile && !recModalOpen));
        if (fab) {
            fab.classList.toggle('is-hidden', !(showPanel && isMobile && !recModalOpen));
            fab.textContent = copy.openRecs(teamPicks.length);
        }
        if (!showPanel) return;

        const chips = [];
        teamPicks.forEach((cid, idx) => {
            const info = DATA.champs[cid];
            const name = info ? champName(info) : ('#' + cid);
            const image = info && info.image ? info.image : '';
            chips.push(
                `<button class="pick-chip" type="button" data-remove-cid="${cid}" title="${escHtml(copy.removePick(name))}">` +
                `<span class="ord">${idx + 1}</span>` +
                (image ? `<img loading="lazy" src="${image}" alt="">` : '') +
                `<span>${escHtml(name)}</span></button>`
            );
        });
        for (let i = teamPicks.length; i < MAX_TEAM_PICKS; i += 1) {
            chips.push(`<div class="pick-chip empty"><span class="ord">${i + 1}</span>${copy.pickEmpty}</div>`);
        }
        slots.innerHTML = chips.join('');

        const recs = aggregateRecommendations();
        const want = teamPicks.length;
        const hasFull = recs.some(row => row.full);
        if (pickNotice) {
            note.textContent = pickNotice;
        } else if (!teamPicks.length) {
            note.textContent = copy.pickNoteEmpty(MAX_TEAM_PICKS);
        } else if (want > 1 && !hasFull) {
            note.textContent = copy.pickNotePartial(want);
        } else {
            note.textContent = copy.pickNoteReady(want, DATA.min_synergy_games);
        }

        if (!teamPicks.length) {
            recList.innerHTML = `<div class="panel-empty">${copy.panelEmpty}</div>`;
            return;
        }
        if (!recs.length) {
            recList.innerHTML = `<div class="panel-empty">${copy.panelNoData}</div>`;
            return;
        }

        recList.innerHTML = recs.slice(0, REC_LIST_LIMIT).map((row, idx) => {
            const info = DATA.champs[row.id];
            const name = info ? champName(info) : ('#' + row.id);
            const image = info && info.image ? info.image : '';
            const coverage = `${row.coverage}/${want}`;
            const meta = copy.recRowMeta(
                signed(row.fitScore),
                signed(row.liftAvg),
                zFmt(row.zAvg),
                row.minGames,
                coverage,
            );
            return `
                <button class="rec-row" type="button" data-cid="${row.id}" title="${escHtml(copy.recRowTitle(name, signed(row.fitScore), signed(row.liftAvg)))}">
                    <span class="rec-rank">${idx + 1}</span>
                    ${image ? `<img loading="lazy" src="${image}" alt="">` : '<div style="width:40px;height:40px;border-radius:8px;background:#2a3142"></div>'}
                    <span class="rec-main">
                        <span class="rec-name">${escHtml(name)}</span>
                        <span class="rec-meta">${meta}</span>
                    </span>
                </button>
            `;
        }).join('');
    }

    function updateChampCardCopy() {
        document.querySelectorAll('.champ').forEach(champ => {
            const cid = champ.getAttribute('data-cid');
            const info = DATA.champs[cid];
            if (!info) return;
            const name = champName(info);
            const alias = info.alias || '';
            const tier = champ.getAttribute('data-tier') || '';
            const wr = champ.getAttribute('data-wr') || '';
            const games = champ.getAttribute('data-games') || '';
            const raw = champ.getAttribute('data-raw-wr') || '';
            const nameEl = champ.querySelector('.name');
            if (nameEl) nameEl.textContent = name;
            champ.setAttribute('title', tr().champCardTitle(name, wr, games, raw));
            champ.setAttribute('aria-label', tr().champCardAria(name, alias, tier, wr));
        });
    }

    function applyLanguage(nextLang) {
        currentLang = nextLang === 'en' ? 'en' : 'zh';
        const copy = tr();
        document.documentElement.lang = copy.htmlLang;
        try { localStorage.setItem(LANG_KEY, currentLang); } catch {}

        const titleEl = document.getElementById('site-title');
        if (titleEl) titleEl.textContent = currentLang === 'en' ? HEADER_TITLE_EN : HEADER_TITLE_ZH;
        const subtitleEl = document.getElementById('site-subtitle');
        if (subtitleEl) subtitleEl.innerHTML = copy.subtitle();
        updateSearchPlaceholder();
        const shownUnit = document.getElementById('shown-unit');
        if (shownUnit) shownUnit.textContent = copy.shownUnit;
        document.querySelectorAll('.tier-count-unit').forEach(el => {
            el.textContent = copy.tierUnit;
        });
        document.querySelectorAll('.chip').forEach(chip => {
            chip.textContent = currentLang === 'en'
                ? (chip.getAttribute('data-label-en') || chip.textContent || '')
                : (chip.getAttribute('data-label-zh') || chip.textContent || '');
        });
        const clearBtn = document.getElementById('clear-picks');
        if (clearBtn) clearBtn.textContent = copy.clearPicks;
        const emptyTitle = document.getElementById('empty-title');
        if (emptyTitle) emptyTitle.textContent = copy.emptyTitle;
        const emptyCopy = document.getElementById('empty-copy');
        if (emptyCopy) emptyCopy.textContent = copy.emptyCopy;
        const freshness = document.getElementById('freshness-copy');
        if (freshness) freshness.textContent = copy.freshness();
        const sideTitle = document.getElementById('side-title');
        if (sideTitle) sideTitle.textContent = copy.sideTitle;
        const sideSub = document.getElementById('side-sub');
        if (sideSub) sideSub.innerHTML = copy.sideSub;
        const sideClose = document.getElementById('side-close');
        if (sideClose) sideClose.setAttribute('aria-label', copy.closeRecs);
        const toggle = document.getElementById('lang-toggle');
        const toggleLabel = document.getElementById('lang-toggle-label');
        if (toggle) {
            toggle.title = copy.langToggleTitle;
            toggle.setAttribute('aria-label', copy.langToggleAria);
        }
        if (toggleLabel) toggleLabel.textContent = copy.langToggleLabel;

        updateChampCardCopy();
        setRecommendMode(recommendMode);
        renderSidePanel();
        if (detailSelected) {
            const champ = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (champ) openDetailForChamp(champ, true);
        }
    }

    function setRecommendMode(next) {
        recommendMode = Boolean(next);
        if (!recommendMode) recModalOpen = false;
        const btn = document.getElementById('recommend-mode');
        if (!btn) return;
        btn.classList.toggle('active', recommendMode);
        btn.setAttribute('aria-pressed', recommendMode ? 'true' : 'false');
        btn.textContent = recommendMode ? tr().recModeOn : tr().recModeOff;
    }

    function syncDetailModalState() {
        document.body.classList.toggle('detail-modal-open', Boolean(detailSelected) && isMobileViewport());
    }

    function closeDetail() {
        document.querySelectorAll('.detail-host').forEach(h => h.innerHTML = '');
        document.querySelectorAll('.champ.detail-selected').forEach(el => el.classList.remove('detail-selected'));
        detailSelected = null;
        syncDetailModalState();
    }

    function openDetailForChamp(champ, force = false) {
        const cid = champ.getAttribute('data-cid');
        const block = champ.closest('.tier-block');
        const host  = block.querySelector('.detail-host');

        // Clear any previously selected highlight + detail elsewhere.
        document.querySelectorAll('.champ.detail-selected').forEach(el => {
            if (el !== champ) el.classList.remove('detail-selected');
        });
        document.querySelectorAll('.detail-host').forEach(el => {
            if (el !== host) el.innerHTML = '';
        });

        if (!force && detailSelected === cid && host.firstChild) {
            closeDetail();
            return;
        }

        // Position the detail host right after the last champ in the clicked
        // row, so the panel always pops up directly under the champion you
        // tapped — never hidden far below by other champs.
        const anchor = lastChampInRow(champ);
        if (anchor.nextSibling !== host) {
            anchor.after(host);
        }

        const dialogAttrs = isMobileViewport()
            ? ` role="dialog" aria-modal="true" aria-labelledby="detail-title-${cid}"`
            : '';
        host.innerHTML = `<div class="detail"${dialogAttrs}>${renderDetail(cid)}</div>`;
        champ.classList.add('detail-selected');
        detailSelected = cid;
        syncDetailModalState();
        if (isMobileViewport()) {
            host.querySelector('.detail-close')?.focus({ preventScroll: true });
        }
        if (!force) {
            trackEvent('champion_detail_open', {
                champion_id: cid,
                champion_name: champ.getAttribute('data-name-en') || '',
                tier: champ.getAttribute('data-tier') || '',
            });
        }
    }

    function openDetailByCid(cid) {
        const champ = document.querySelector(`.champ[data-cid="${cid}"]:not(.hidden)`);
        if (!champ) return;
        openDetailForChamp(champ);
        champ.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }

    function toggleTeamPick(cid) {
        pickNotice = '';
        const idx = teamPicks.indexOf(cid);
        if (idx !== -1) {
            teamPicks.splice(idx, 1);
        } else if (teamPicks.length >= MAX_TEAM_PICKS) {
            pickNotice = tr().maxOnly(MAX_TEAM_PICKS);
        } else {
            teamPicks.push(cid);
        }
        syncPickDecorations();
        renderSidePanel();
    }

    document.addEventListener('click', (ev) => {
        const ghStar = ev.target.closest('.gh-star');
        if (ghStar) {
            trackEvent('github_star_click', { location: 'header' });
            return;
        }
        const langBtn = ev.target.closest('#lang-toggle');
        if (langBtn) {
            const nextLang = currentLang === 'en' ? 'zh' : 'en';
            applyLanguage(nextLang);
            trackEvent('language_toggle', { language: nextLang });
            return;
        }
        const fabBtn = ev.target.closest('#rec-fab');
        if (fabBtn) {
            recModalOpen = true;
            renderSidePanel();
            trackEvent('recommendations_open', { source: 'fab', picks: teamPicks.length });
            return;
        }
        const sideClose = ev.target.closest('#side-close');
        if (sideClose) {
            recModalOpen = false;
            renderSidePanel();
            trackEvent('recommendations_close', { source: 'panel', picks: teamPicks.length });
            return;
        }
        const detailClose = ev.target.closest('.detail-close');
        if (detailClose) {
            closeDetail();
            return;
        }
        if (isMobileViewport() && ev.target.classList && ev.target.classList.contains('detail-host')) {
            closeDetail();
            return;
        }
        const modeBtn = ev.target.closest('#recommend-mode');
        if (modeBtn) {
            const nextMode = !recommendMode;
            setRecommendMode(nextMode);
            pickNotice = '';
            renderSidePanel();
            trackEvent('recommend_mode_toggle', { enabled: nextMode });
            return;
        }
        const clearBtn = ev.target.closest('#clear-picks');
        if (clearBtn) {
            const previousCount = teamPicks.length;
            teamPicks = [];
            pickNotice = '';
            syncPickDecorations();
            renderSidePanel();
            trackEvent('team_picks_clear', { previous_count: previousCount });
            return;
        }
        const removeBtn = ev.target.closest('[data-remove-cid]');
        if (removeBtn) {
            const removedCid = removeBtn.getAttribute('data-remove-cid');
            teamPicks = teamPicks.filter(cid => cid !== removeBtn.getAttribute('data-remove-cid'));
            pickNotice = '';
            syncPickDecorations();
            renderSidePanel();
            trackEvent('team_pick_remove', { champion_id: removedCid, picks: teamPicks.length });
            return;
        }
        const recRow = ev.target.closest('.rec-row');
        if (recRow) {
            recModalOpen = false;
            renderSidePanel();
            const recCid = recRow.getAttribute('data-cid');
            trackEvent('recommendation_click', { champion_id: recCid, picks: teamPicks.length });
            openDetailByCid(recCid);
            return;
        }
        const champ = ev.target.closest('.champ');
        if (!champ) return;
        const cid = champ.getAttribute('data-cid');
        if (recommendMode) {
            toggleTeamPick(cid);
            trackEvent('team_pick_toggle', { champion_id: cid, picks: teamPicks.length });
            return;
        }
        openDetailForChamp(champ);
    });

    // When viewport width changes, the row containing the selected champ
    // shifts — re-anchor the detail host so it stays directly under that
    // champ on the new layout.
    let resizeT = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeT);
        resizeT = setTimeout(() => {
            updateSearchPlaceholder();
            renderSidePanel();
            if (!detailSelected) return;
            const champ = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (!champ) return;
            const host = champ.closest('.tier-block').querySelector('.detail-host');
            const anchor = lastChampInRow(champ);
            if (anchor.nextSibling !== host) anchor.after(host);
            syncDetailModalState();
        }, 120);
    });

    try {
        const savedLang = localStorage.getItem(LANG_KEY);
        if (savedLang === 'en' || savedLang === 'zh') currentLang = savedLang;
    } catch {}

    setRecommendMode(false);
    syncPickDecorations();
    renderSidePanel();
    applyLanguage(currentLang);

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
        const empty = document.getElementById('empty-state');
        if (empty) empty.classList.toggle('visible', shown === 0);

        // If the currently-selected champ got hidden, close its detail panel.
        if (detailSelected) {
            const sel = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (!sel || sel.classList.contains('hidden')) {
                closeDetail();
            }
        }
    }

    function setActiveChip(role) {
        document.querySelectorAll('.chip').forEach(chip => {
            chip.classList.toggle('active', chip.getAttribute('data-role') === role);
        });
    }

    // Role chip clicks (event delegation).  "All" chip (data-role="") already
    // unsets role filter — no dedicated reset button needed.
    document.addEventListener('click', (ev) => {
        const chip = ev.target.closest('.chip');
        if (!chip) return;
        filterState.role = chip.getAttribute('data-role') || '';
        setActiveChip(filterState.role);
        applyFilters();
        trackEvent('role_filter_click', { role: filterState.role || 'all' });
    });

    // Keyboard activation for cards.  Enter / Space on a `.champ` or `.aug`
    // triggers the same path a click would (they're role="button" /
    // tabindex="0").  Preventing default on Space stops the page from
    // scrolling.
    document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') {
            if (detailSelected && isMobileViewport()) {
                closeDetail();
                return;
            }
            if (recModalOpen) {
                recModalOpen = false;
                renderSidePanel();
                return;
            }
        }
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        const t = ev.target;
        if (!t || !t.classList) return;
        if (t.classList.contains('champ') || t.classList.contains('aug')) {
            ev.preventDefault();
            t.click();
        }
    });

    // Augment tooltip viewport-clip protection: tooltips default to "above"
    // the card.  When the card sits near the top of the viewport, the
    // tooltip would clip — flip it below instead by toggling a class
    // computed from `getBoundingClientRect`.
    document.addEventListener('mouseover', (ev) => {
        const aug = ev.target.closest && ev.target.closest('.aug');
        if (!aug) return;
        const rect = aug.getBoundingClientRect();
        // Tooltip is ~ 110-140 px tall; flip when there's less than 160 px
        // of headroom above the card.
        aug.classList.toggle('flip-tip', rect.top < 160);
    }, { passive: true });

    // Live search.
    const searchEl = document.getElementById('champ-search');
    if (searchEl) {
        searchEl.addEventListener('input', () => {
            filterState.q = searchEl.value || '';
            applyFilters();
        });
        // Esc inside the search clears the filter and unfocuses, so the
        // typical "open, search, escape back to grid" flow works.
        searchEl.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') {
                searchEl.value = '';
                filterState.q = '';
                applyFilters();
                searchEl.blur();
            }
        });
    }

    // Ctrl+F / Cmd+F shortcut → focus our search input.
    //
    // Rationale: our search already understands zh-TW name + English alias +
    // role keywords (gua-Liang in one go).  Native browser find can also
    // discover champions thanks to the .sr-only English alias spans, but
    // the in-page search additionally filters out non-matches — usually
    // what the user wants.
    //
    // If the user is already inside the search box, fall through to the
    // browser's native find dialog (no preventDefault) so they retain that
    // escape hatch.
    document.addEventListener('keydown', (ev) => {
        const isFind = (ev.ctrlKey || ev.metaKey) && ev.key && ev.key.toLowerCase() === 'f';
        if (!isFind) return;
        const sEl = document.getElementById('champ-search');
        if (!sEl) return;
        if (document.activeElement === sEl) return;  // let browser take over on 2nd press
        ev.preventDefault();
        sEl.focus();
        sEl.select();
    });
    """
    js = js.replace("__PAYLOAD__", payload_json)
    js = js.replace("__HEADER_TITLE_ZH__", json.dumps(header_title, ensure_ascii=False))
    js = js.replace("__HEADER_TITLE_EN__", json.dumps(header_title_en, ensure_ascii=False))
    js = js.replace("__SHORT_PATCH_ZH__", json.dumps(short_patch, ensure_ascii=False))
    js = js.replace("__DATE_STR_ZH__", json.dumps(date_str, ensure_ascii=False))
    js = js.replace("__BUILD_DATE__", json.dumps(build_date, ensure_ascii=False))
    js = js.replace("__PATCH_LABEL__", json.dumps(patch_label, ensure_ascii=False))
    js = js.replace("__TOTAL_GAMES__", json.dumps(f"{total_games:,}", ensure_ascii=False))
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
@click.option("--min-synergy-games", type=int, default=40,
              help="Min games per same-team champion pair for synergy / recommendation ranking")
@click.option("--top-n", type=int, default=5)
@click.option("--bot-n", type=int, default=5)
@click.option("--site-url", default="",
              help="Canonical URL (used for OG og:url + <link rel=canonical>), e.g. https://user.github.io/repo/")
@click.option("--og-image", default="",
              help="Override the og:image URL (default: generated og-image.png under --site-url)")
@click.option("--build-date", default="",
              help="Date stamp shown in footer (default: today, YYYY-MM-DD)")
@click.option("--cloudflare-analytics-token", envvar="CLOUDFLARE_ANALYTICS_TOKEN", default="",
              help="Cloudflare Web Analytics token; can also be set via CLOUDFLARE_ANALYTICS_TOKEN")
@click.option("--ga-measurement-id", envvar="GA_MEASUREMENT_ID", default="",
              help="GA4 measurement id, e.g. G-XXXXXXXXXX; can also be set via GA_MEASUREMENT_ID")
def main(
    db: Path,
    queue_id: int,
    patch_prefix: str,
    ddragon_version: str | None,
    out_path: Path,
    min_games: int,
    min_pair_games: int,
    min_synergy_games: int,
    top_n: int,
    bot_n: int,
    site_url: str,
    og_image: str,
    build_date: str,
    cloudflare_analytics_token: str,
    ga_measurement_id: str,
) -> None:
    patch_prefix = patch_prefix or None
    click.echo(f"[tierlist] db={db}  queue={queue_id}  patch_prefix={patch_prefix}")

    version, champ_meta = load_champion_metadata(ddragon_version)
    click.echo(f"[tierlist] data dragon version: {version}")

    aug_meta = load_augment_metadata(cache_dir=Path("data/cache"))
    desc_n = sum(1 for v in aug_meta.values() if v.get("desc"))
    click.echo(
        f"[tierlist] augment catalogue: {len(aug_meta)} entries "
        f"({desc_n} with zh-TW description)"
    )
    item_meta = load_item_metadata(cache_dir=Path("data/cache"))
    click.echo(f"[tierlist] item catalogue: {len(item_meta)} entries")

    champ_records, champ_aug, champ_pairs = compute_winrates(db, queue_id, patch_prefix)
    total_games = sum(r["games"] for r in champ_records) // 10
    champ_records = [r for r in champ_records if r["games"] >= min_games]
    click.echo(f"[tierlist] {len(champ_records)} champions after min_games={min_games}")
    click.echo(f"[tierlist] {len(champ_aug):,} (champ, augment) pairs total")
    click.echo(f"[tierlist] {len(champ_pairs):,} ordered same-team champion pairs total")

    aug_prior_strength = estimate_augment_prior_strength(champ_aug)
    click.echo(
        f"[tierlist] augment EB prior strength k={aug_prior_strength:.1f} "
        f"(posterior q={AUGMENT_POSTERIOR_Q:.2f}, pick_weight={AUGMENT_PICK_LIFT_WEIGHT:g})"
    )
    champ_profiles = load_champion_pick_profiles(champ_meta)
    picks = build_champ_augment_picks(
        champ_aug,
        aug_meta,
        champ_profiles,
        min_games_per_pair=min_pair_games,
        top_n=top_n,
        bot_n=bot_n,
        prior_strength=aug_prior_strength,
    )
    click.echo(
        f"[tierlist] {len(picks)} champions have >= 1 rarity-bucketed pair "
        f"(games >= {min_pair_games})"
    )
    affinity_min_games = max(min_pair_games * 3, 45)
    item_style_min_games = max(affinity_min_games, ITEM_STYLE_MIN_GAMES)
    augment_type_min_games = max(affinity_min_games, AUGMENT_TYPE_MIN_GAMES)
    set_affinity, item_style_affinity, augment_type_affinity = compute_champ_category_affinities(
        db,
        queue_id,
        patch_prefix,
        aug_meta,
        item_meta,
        champ_records,
        min_set_games=affinity_min_games,
        min_item_games=item_style_min_games,
        min_augtype_games=augment_type_min_games,
    )
    click.echo(
        f"[tierlist] {len(set_affinity)} champions have >= 1 augment-set affinity row "
        f"(games >= {affinity_min_games})"
    )
    click.echo(
        f"[tierlist] {len(item_style_affinity)} champions have >= 1 item-style affinity row "
        f"(games >= {item_style_min_games})"
    )
    click.echo(
        f"[tierlist] {len(augment_type_affinity)} champions have >= 1 augment-type affinity row "
        f"(games >= {augment_type_min_games})"
    )
    synergy = build_champ_synergy_index(
        champ_pairs,
        min_games=min_synergy_games,
    )
    click.echo(
        f"[tierlist] {len(synergy)} champions have >= 1 teammate synergy row "
        f"(games >= {min_synergy_games})"
    )

    if not build_date:
        build_date = _dt.date.today().isoformat()

    if cloudflare_analytics_token:
        click.echo("[tierlist] Cloudflare Web Analytics enabled")
    if ga_measurement_id:
        click.echo(f"[tierlist] GA4 enabled: {ga_measurement_id}")

    if not og_image:
        og_asset_path = out_path.parent / "og-image.png"
        try:
            write_og_image(
                og_asset_path,
                champ_records,
                champ_meta,
                queue_id=queue_id,
                patch_prefix=patch_prefix,
                total_games=total_games,
            )
            click.echo(f"[tierlist] wrote {og_asset_path}  ({og_asset_path.stat().st_size:,} bytes)")
            if site_url:
                og_version = (build_date or _dt.date.today().isoformat()).replace("-", "")
                og_image = site_url.rstrip("/") + "/" + og_asset_path.name + f"?v={og_version}-thumb"
        except Exception as exc:
            click.echo(f"[tierlist] WARN: og image generation failed: {exc}")

    html = render_html(
        champ_records,
        champ_meta,
        picks,
        set_affinity,
        item_style_affinity,
        augment_type_affinity,
        synergy,
        aug_meta,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
        ddragon_version=version,
        total_games=total_games,
        min_games_per_pair=min_pair_games,
        min_synergy_games=min_synergy_games,
        site_url=site_url,
        og_image=og_image,
        build_date=build_date,
        cloudflare_analytics_token=cloudflare_analytics_token,
        ga_measurement_id=ga_measurement_id,
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
