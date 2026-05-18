"""Mayhem in-game overlay: press F8 during augment-offer screen, get an
instant rating of the 3 augments you were offered, scored against the
champion you're actually playing.

Install (one-off):
    pip install easyocr mss keyboard PyQt6 rapidfuzz

Run (Windows):
    python scripts/mayhem_overlay.py

First-run flow
--------------
1. A semi-transparent full-screen overlay appears: drag a rectangle around
   the area where the 3 augment cards normally show up.
2. The rectangle is saved to `data/cache/overlay_config.json` so this only
   happens once.
3. The overlay collapses to a small dock in the corner.
4. In-game, when the augment-offer screen shows, press F8.  The overlay
   captures that rectangle, OCRs the augment names, looks up your current
   champion's (champ, augment) win-rate + lift for each one, and shows
   them ranked best→worst.

Anti-cheat note
---------------
This tool never reads game memory.  It only:
  * Screenshots a region of YOUR own screen
  * Hits Riot's public Live Client Data API (https://127.0.0.1:2999/...)
  * Reads its own local SQLite (data/lcu/games.db) for ratings
That's the same approach Mobalytics / Blitz / Porofessor use, and it's
explicitly outside Vanguard's scope (no process injection, no DLL hook).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Optional

# Lazy imports keep startup snappy when the user just wants `--help`.

# All paths resolved relative to the script's own location so the overlay
# works no matter where PowerShell happens to be CD'd to (worktree, parent
# repo, anywhere).
SCRIPT_DIR = Path(__file__).resolve().parent     # scripts/
REPO_ROOT = SCRIPT_DIR.parent                    # repo root
CONFIG_PATH = REPO_ROOT / "data" / "cache" / "overlay_config.json"
GAMES_DB = REPO_ROOT / "data" / "lcu" / "games.db"
CACHE_DIR = REPO_ROOT / "data" / "cache"
DEFAULT_HOTKEY = "f8"

# How long to keep the result overlay visible before auto-hiding.
RESULT_DWELL_MS = 12_000


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Data: champion + augment catalogue + per-(champ, augment) scores
# ---------------------------------------------------------------------------

def _import_build_tier_list():
    """Reuse the existing builder's compute / pick logic.  No CLI side effects;
    Click decorators don't fire unless `main()` is called."""
    here = Path(__file__).parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import build_tier_list  # type: ignore
    return build_tier_list


class Catalogue:
    """Everything the hotkey path needs, prebuilt once."""

    def __init__(self) -> None:
        btl = _import_build_tier_list()

        print("[overlay] loading champion metadata (Data Dragon)…", flush=True)
        self.ddragon_version, self.champ_meta = btl.load_champion_metadata(None)
        # Two lookups: Live Client Data on Garena TW returns the localized
        # name (e.g. 「斯溫」, 「莉莉亞」) instead of the English alias.
        # Merge both keyspaces into one dict so callers don't care which the
        # client gave us.
        self.alias_to_id: dict[str, int] = {}
        for cid, m in self.champ_meta.items():
            if m.get("alias"):
                self.alias_to_id[m["alias"]] = cid          # "Swain"
            if m.get("name"):
                self.alias_to_id[m["name"]] = cid           # "斯溫"

        print("[overlay] loading augment catalogue (CommunityDragon)…", flush=True)
        self.aug_meta = btl.load_augment_metadata(cache_dir=CACHE_DIR)

        print("[overlay] computing per-champion augment scores…", flush=True)
        records, champ_aug = btl.compute_winrates(
            GAMES_DB, queue_id=2400, patch_prefix="16.10"
        )
        # Map (champion_id, augment_id) -> row dict
        self.pair: dict[tuple[int, int], dict] = {
            (r["champion_id"], r["augment_id"]): r for r in champ_aug
        }

        # Lookup table for fuzzy match: augment name (zh-TW) -> aug_id
        self.name_to_aid: dict[str, int] = {
            v["name"]: aid for aid, v in self.aug_meta.items() if v.get("name")
        }
        print(
            f"[overlay] ready  "
            f"champs={len(self.champ_meta)}  "
            f"augs={len(self.aug_meta)}  "
            f"pairs={len(self.pair):,}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# LCU Live Client Data
# ---------------------------------------------------------------------------

def get_current_champion_alias() -> Optional[str]:
    """Returns the English alias (e.g. "Lillia") of the active player's
    champion, or None if the game isn't in a state we can read."""
    import httpx
    try:
        r1 = httpx.get(
            "https://127.0.0.1:2999/liveclientdata/activeplayername",
            verify=False, timeout=1.5,
        )
        if r1.status_code != 200:
            return None
        active = r1.json()  # full Riot ID, e.g. "Henry#TW2"
        r2 = httpx.get(
            "https://127.0.0.1:2999/liveclientdata/playerlist",
            verify=False, timeout=1.5,
        )
        if r2.status_code != 200:
            return None
        # Live Client lists players with `championName` (alias) +
        # `riotId` or `summonerName`.
        for p in r2.json():
            rid = p.get("riotId") or p.get("summonerName") or ""
            if rid == active or active.startswith(rid):
                return p.get("championName")  # e.g. "Lillia"
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

def capture(region: dict):
    """Snapshot the saved rect.  Returns numpy array (H, W, 4) BGRA."""
    import mss
    import numpy as np
    # mss 10.x deprecated the lowercase `mss.mss()` alias; use `mss.MSS()`.
    with mss.MSS() as sct:
        raw = sct.grab(region)
        return np.array(raw)


# ---------------------------------------------------------------------------
# OCR + fuzzy match
# ---------------------------------------------------------------------------

_OCR_READER = None
_OCR_LOCK = threading.Lock()


def get_ocr_reader():
    global _OCR_READER
    with _OCR_LOCK:
        if _OCR_READER is None:
            import easyocr
            print("[overlay] EasyOCR initialising (one-off)…", flush=True)
            _OCR_READER = easyocr.Reader(["ch_tra", "en"], gpu=False, verbose=False)
            print("[overlay] EasyOCR ready", flush=True)
    return _OCR_READER


def ocr_strings(img) -> list[str]:
    reader = get_ocr_reader()
    return reader.readtext(img, detail=0)


_DESC_PARTICLES = ("的", "會", "造成", "增加", "減少", "對", "若", "可以", "%",
                   "回復", "額外", "命中", "效果", "提升", "使", "讓", "獲得",
                   "並", "或", "並且", "且", "之", "於", "為", "與")


def _looks_like_aug_name(s: str) -> bool:
    """Augment names on the offer screen are 2-7 chars, no descriptive
    particles, and no Latin/digit clutter (鄉村 / numerical descriptions)."""
    s = s.strip()
    if not (2 <= len(s) <= 8):
        return False
    if any(p in s for p in _DESC_PARTICLES):
        return False
    # Must be mostly CJK; descriptions often leak ASCII / digits.
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    return cjk >= len(s) - 1   # tolerate 1 stray char (punctuation)


def fuzzy_pick_augments(
    strings: list[str], catalogue: Catalogue, top_k: int = 3
) -> list[tuple[int, str, int]]:
    """For each OCR'd line, fuzzy-match against the augment-name catalogue.
    Returns up to `top_k` unique (aug_id, name, score) tuples sorted by score."""
    from rapidfuzz import process, fuzz
    names = list(catalogue.name_to_aid.keys())
    # Pre-filter: drop description-shaped strings BEFORE fuzzy matching so
    # particles like 「最大生命」 from "...最大生命的真實傷害" don't fool
    # us into picking the wrong augment.
    candidates = [s.strip() for s in strings if _looks_like_aug_name(s)]
    hits: list[tuple[int, str, int]] = []
    seen: set[int] = set()
    for s in candidates:
        # Score >= 80 only — augment names are short so even one wrong
        # char drops WRatio significantly; below 80 is almost always a
        # description fragment overlap.
        best = process.extractOne(s, names, scorer=fuzz.WRatio, score_cutoff=80)
        if not best:
            continue
        name, score, _ = best
        aid = catalogue.name_to_aid[name]
        if aid in seen:
            continue
        seen.add(aid)
        hits.append((aid, name, int(score)))
    hits.sort(key=lambda t: -t[2])
    return hits[:top_k]


# ---------------------------------------------------------------------------
# Scoring: (champion_id, augment_id) -> WR + lift
# ---------------------------------------------------------------------------

def score_picks(
    champion_id: int, augment_ids: list[int], catalogue: Catalogue
) -> list[dict]:
    out = []
    for aid in augment_ids:
        row = catalogue.pair.get((champion_id, aid))
        meta = catalogue.aug_meta.get(aid, {})
        if row is None:
            out.append({
                "aug_id": aid,
                "name": meta.get("name", f"#{aid}"),
                "rarity": meta.get("rarity", ""),
                "wr": None, "lift": None, "games": 0,
                "note": "資料不足",
            })
            continue
        out.append({
            "aug_id": aid,
            "name": meta.get("name", f"#{aid}"),
            "rarity": meta.get("rarity", ""),
            "wr": row["smoothed_wr"],
            "lift": row["lift"],
            "games": row["games"],
            "note": "" if row["games"] >= 15 else "樣本少",
        })
    # Rank within this batch.
    scored = [r for r in out if r["wr"] is not None]
    scored.sort(key=lambda r: -r["wr"])
    for rank, r in enumerate(scored, start=1):
        r["rank"] = rank
    return out


# ---------------------------------------------------------------------------
# PyQt UI
# ---------------------------------------------------------------------------

def make_app():
    from PyQt6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    return app


# Picker --------------------------------------------------------------------

class RegionPicker:
    """Full-screen translucent overlay; user click-drags a rectangle, saved."""

    def __init__(self) -> None:
        from PyQt6 import QtCore, QtGui, QtWidgets

        screen = QtWidgets.QApplication.primaryScreen()
        self.geom = screen.geometry()

        self.window = QtWidgets.QWidget()
        self.window.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.window.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.window.setGeometry(self.geom)
        self.window.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.CrossCursor))

        self._start: Optional[QtCore.QPoint] = None
        self._end: Optional[QtCore.QPoint] = None
        self.result: Optional[dict] = None

        self.window.paintEvent = self._paint   # type: ignore[assignment]
        self.window.mousePressEvent = self._press  # type: ignore[assignment]
        self.window.mouseMoveEvent = self._move    # type: ignore[assignment]
        self.window.mouseReleaseEvent = self._release  # type: ignore[assignment]

    def run(self) -> Optional[dict]:
        self.window.show()
        # Block until user releases.
        loop = make_app()
        loop.exec()
        return self.result

    def _paint(self, _event):
        from PyQt6 import QtCore, QtGui
        p = QtGui.QPainter(self.window)
        p.fillRect(self.window.rect(), QtGui.QColor(14, 17, 22, 140))  # dim
        if self._start and self._end:
            rect = QtCore.QRect(self._start, self._end).normalized()
            # Clear the chosen area
            p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(rect, QtCore.Qt.GlobalColor.transparent)
            p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            pen = QtGui.QPen(QtGui.QColor(245, 197, 24), 2)
            p.setPen(pen)
            p.drawRect(rect)
            # Label
            p.setPen(QtGui.QColor(245, 232, 255))
            f = p.font()
            f.setPointSize(11)
            p.setFont(f)
            p.drawText(rect.bottomLeft() + QtCore.QPoint(0, 18),
                       f"{rect.width()}×{rect.height()}  鬆開滑鼠確認")

        # Hint text
        p.setPen(QtGui.QColor(245, 232, 255))
        f = p.font()
        f.setPointSize(13)
        p.setFont(f)
        p.drawText(40, 60,
                   "拖一個方框框住 augment offer 對話框（3 張卡都要包到）。Esc 取消。")
        p.end()

    def _press(self, ev):
        self._start = ev.position().toPoint()
        self._end = self._start
        self.window.update()

    def _move(self, ev):
        if self._start:
            self._end = ev.position().toPoint()
            self.window.update()

    def _release(self, _ev):
        from PyQt6 import QtCore
        if self._start and self._end:
            rect = QtCore.QRect(self._start, self._end).normalized()
            if rect.width() > 50 and rect.height() > 50:
                self.result = {
                    "left": rect.left(),
                    "top": rect.top(),
                    "width": rect.width(),
                    "height": rect.height(),
                }
        self.window.close()
        QtCore.QCoreApplication.quit()


# Result overlay ------------------------------------------------------------

class ResultOverlay:
    """Small frameless always-on-top window with the three rated augments."""

    def __init__(self) -> None:
        from PyQt6 import QtCore, QtWidgets, QtGui
        self.QtCore = QtCore
        self.QtWidgets = QtWidgets
        self.QtGui = QtGui

        self.window = QtWidgets.QWidget()
        self.window.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.window.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.window.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.window.setStyleSheet("""
            QWidget#card {
                background: rgba(14, 17, 22, 230);
                border: 1px solid rgba(245, 232, 255, 60);
                border-radius: 10px;
            }
            QLabel { color: #e6e8eb; font-family: "Noto Sans TC", "Microsoft JhengHei"; }
            QLabel#title { font-size: 13px; font-weight: 600; color: #f5e8ff; }
            QLabel#meta  { font-size: 10px; color: #9aa0a6; }
            QLabel.aug-name { font-size: 12px; font-weight: 600; }
            QLabel.aug-wr   { font-size: 16px; font-weight: 700; font-family: "Noto Sans TC", monospace; }
            QLabel.aug-lift { font-size: 10px; color: #9aa0a6; }
            QLabel.aug-tag  { font-size: 9px; color: #6b7280; }
            QLabel.rank-1   { color: #6bd16b; }
            QLabel.rank-2   { color: #f5c518; }
            QLabel.rank-3   { color: #ff8a4a; }
            QLabel.rank-na  { color: #58606b; }
        """)
        self.window.setObjectName("root")

        self.card = QtWidgets.QWidget(self.window)
        self.card.setObjectName("card")

        self._build_layout()

        # Auto-hide timer
        self._hide_timer = QtCore.QTimer(self.window)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.window.hide)

        # Position: top-right of primary screen by default
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        w, h = 340, 240
        self.window.resize(w, h)
        self.window.move(screen.right() - w - 20, screen.top() + 20)

    def _build_layout(self):
        QtWidgets = self.QtWidgets
        outer = QtWidgets.QVBoxLayout(self.window)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.card)

        v = QtWidgets.QVBoxLayout(self.card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        self.title_l = QtWidgets.QLabel()
        self.title_l.setObjectName("title")
        v.addWidget(self.title_l)

        self.meta_l = QtWidgets.QLabel()
        self.meta_l.setObjectName("meta")
        v.addWidget(self.meta_l)

        self.rows_box = QtWidgets.QVBoxLayout()
        self.rows_box.setSpacing(6)
        v.addLayout(self.rows_box)

        v.addStretch(1)

    def _clear_rows(self):
        while self.rows_box.count():
            item = self.rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def show_result(
        self, champ_name: str, picks: list[dict], dwell_ms: int = RESULT_DWELL_MS
    ) -> None:
        from PyQt6 import QtCore, QtWidgets
        self._clear_rows()
        self.title_l.setText(f"{champ_name} · Augment 評分")
        if not picks:
            self.meta_l.setText("OCR 沒抓到 augment 名稱。試著重抓 / 重定位區域。")
        else:
            self.meta_l.setText(f"按 smoothed WR 排序 · 樣本 ≥ 15 場才入排名")

        for r in picks:
            row = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(10)

            rank_text = "—" if r.get("wr") is None else str(r.get("rank", "?"))
            rank_l = QtWidgets.QLabel(rank_text)
            rank_l.setFixedWidth(18)
            rank_l.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            cls = (
                f"rank-{r['rank']}" if r.get("wr") is not None and r.get("rank") in (1, 2, 3)
                else "rank-na"
            )
            rank_l.setProperty("class", cls)
            rank_l.setStyleSheet(f"font-size: 18px; font-weight: 700;")
            h.addWidget(rank_l)

            # Name + tag
            name_box = QtWidgets.QVBoxLayout()
            name_box.setSpacing(1)
            name_l = QtWidgets.QLabel(r["name"])
            name_l.setProperty("class", "aug-name")
            name_box.addWidget(name_l)
            rarity_short = {
                "kPrismatic": "彩色", "kGold": "金色", "kSilver": "銀色",
            }.get(r.get("rarity"), "")
            tag_text = rarity_short
            if r.get("note"):
                tag_text = f"{rarity_short} · {r['note']}" if rarity_short else r["note"]
            tag_l = QtWidgets.QLabel(tag_text)
            tag_l.setProperty("class", "aug-tag")
            name_box.addWidget(tag_l)
            h.addLayout(name_box, 1)

            if r.get("wr") is not None:
                wr_l = QtWidgets.QLabel(f"{r['wr']*100:.1f}%")
                wr_l.setProperty("class", "aug-wr")
                wr_l.setStyleSheet("font-size: 16px; font-weight: 700;")
                h.addWidget(wr_l)

                lift_text = (f"+{r['lift']*100:.1f}pp" if r["lift"] >= 0
                             else f"{r['lift']*100:.1f}pp")
                lift_l = QtWidgets.QLabel(f"{lift_text} · {r['games']}場")
                lift_l.setProperty("class", "aug-lift")
                h.addWidget(lift_l)
            else:
                na_l = QtWidgets.QLabel("資料不足")
                na_l.setProperty("class", "aug-tag")
                h.addWidget(na_l)

            self.rows_box.addWidget(row)

        # Auto-fit height
        self.window.adjustSize()
        self.window.show()
        self._hide_timer.start(dwell_ms)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

class HotkeyBridge:
    """Cross-thread bridge.  `keyboard` calls our callback on its own worker
    thread; Qt UI work has to happen on the main thread.  A Qt signal emitted
    from a non-Qt thread is auto-dispatched via `QueuedConnection`, which is
    the documented thread-safe way to do this.  `QTimer.singleShot(0, fn)`
    from a worker thread is NOT — that was the previous bug; F8 logged but
    `handle_capture` never ran because the timer went nowhere."""

    def __init__(self, slot):
        from PyQt6 import QtCore

        class _Bridge(QtCore.QObject):
            triggered = QtCore.pyqtSignal()

        self._obj = _Bridge()
        self._obj.triggered.connect(slot)

    def fire(self):
        self._obj.triggered.emit()


def run_overlay():
    import keyboard
    from PyQt6 import QtCore, QtWidgets

    cfg = load_config()
    region = cfg.get("region")

    app = make_app()

    if not region:
        print("[overlay] no saved region — launching picker", flush=True)
        picker = RegionPicker()
        rect = picker.run()
        if not rect:
            print("[overlay] picker cancelled", flush=True)
            return
        cfg["region"] = rect
        save_config(cfg)
        region = rect
        print(f"[overlay] saved region {rect}", flush=True)

    catalogue = Catalogue()

    # Build the result window once; reuse on each hotkey.
    overlay = ResultOverlay()

    # Debounce: only one capture-OCR cycle at a time.  First F8 takes 10-60s
    # the very first run (EasyOCR model init); subsequent F8 spam during that
    # window used to pile 10+ jobs on the queue.  Now we drop them.
    state = {"busy": False}

    def run_capture():
        if state["busy"]:
            print("[overlay] busy — skipped (still processing previous F8)", flush=True)
            return
        state["busy"] = True
        try:
            handle_capture(region, catalogue, overlay)
        finally:
            state["busy"] = False

    # Cross-thread bridge: keyboard thread emits → Qt main thread handles.
    bridge = HotkeyBridge(run_capture)

    def on_hotkey():
        print(f"[overlay] hotkey {DEFAULT_HOTKEY.upper()} — capturing", flush=True)
        bridge.fire()

    keyboard.add_hotkey(DEFAULT_HOTKEY, on_hotkey)
    print(
        f"[overlay] listening — press {DEFAULT_HOTKEY.upper()} during the "
        "augment-offer screen.  Ctrl+C in this terminal to quit.",
        flush=True,
    )

    sys.exit(app.exec())


def handle_capture(region: dict, catalogue: Catalogue, overlay: ResultOverlay):
    t0 = time.perf_counter()
    img = capture(region)
    t1 = time.perf_counter()

    strings = ocr_strings(img)
    t2 = time.perf_counter()
    print(f"[overlay] OCR: {strings}", flush=True)

    hits = fuzzy_pick_augments(strings, catalogue, top_k=3)
    t3 = time.perf_counter()
    print(f"[overlay] matched: {[(n, s) for _, n, s in hits]}", flush=True)

    alias = get_current_champion_alias()
    champ_id = catalogue.alias_to_id.get(alias) if alias else None
    champ_name_display = (
        catalogue.champ_meta.get(champ_id, {}).get("name", alias or "?")
        if champ_id else "(無法讀取目前英雄)"
    )
    print(
        f"[overlay] champ alias={alias} id={champ_id} "
        f"timings: cap={1000*(t1-t0):.0f}ms ocr={1000*(t2-t1):.0f}ms "
        f"match={1000*(t3-t2):.0f}ms",
        flush=True,
    )

    if champ_id is None:
        # Show OCR results + helpful note
        picks_no_champ = [
            {"aug_id": aid, "name": name, "rarity":
                catalogue.aug_meta.get(aid, {}).get("rarity", ""),
             "wr": None, "lift": None, "games": 0,
             "note": "未連到 Live Client"}
            for aid, name, _ in hits
        ]
        overlay.show_result("(未偵測到遊戲)", picks_no_champ)
        return

    picks = score_picks(champ_id, [aid for aid, _, _ in hits], catalogue)
    overlay.show_result(champ_name_display, picks)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None):
    argv = argv or sys.argv[1:]
    if "--reset-region" in argv:
        cfg = load_config()
        cfg.pop("region", None)
        save_config(cfg)
        print("[overlay] cleared saved region; will re-prompt on next run")
        return
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return
    run_overlay()


if __name__ == "__main__":
    main()
