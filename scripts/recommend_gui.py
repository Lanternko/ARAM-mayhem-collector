"""Tk GUI for the ARAM champ-select recommender.

A standalone always-on-top window that shows bench-swap suggestions with
live updates as the LCU champ-select state changes.  Architecturally the
same as `lcu_collector.py recommend` but renders into a Tk window instead
of clearing the terminal.

Threading:
  - main thread: Tk event loop, owns all widgets.
  - poll thread: runs the LCU polling loop, never touches Tk; pushes
    updates onto a queue.Queue that the main thread drains via root.after.

Tkinter is not thread-safe — keep this separation strict.

Usage:
  python scripts/recommend_gui.py \
      --lr-model models/tier2_mayhem/lr_model.pkl \
      --vocab    models/tier2_mayhem/tier2_checkpoint.pt \
      --pair-stats models/pair_synergy_16_10.json
"""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path

import click

from aram_nn.icons import IconCache
from aram_nn.lcu.client import (
    LCUClient, get_champion_summary, get_champ_select_session, get_gameflow_phase,
)
from aram_nn.lcu.process import get_credentials
from aram_nn.recommend import (
    ParsedSession, load_lr, parse_session, session_state_hash, suggest_for_cell,
)
from aram_nn.pair_synergy import PairSynergyStats, load_pair_synergy


# ---------- Polling thread ----------

def poll_loop(
    stop_event: threading.Event, q: queue.Queue, model, pair_stats, creds,
    poll_interval: float, verbose: bool = False,
) -> None:
    """Run in background thread.  Pushes messages onto `q`:
      ("static", id_to_name)         — once, after LCU static data loads
      ("idle", phase)                — when not in (or about to leave) champ select
      ("suggestions", parsed, sugs)  — when champ select state changes
      ("error", message)             — on unrecoverable failure

    When verbose, also prints a status line to stdout on every poll so the
    user can see what the LCU is returning (phase + session presence)
    while watching the terminal during a real game.
    """
    def log(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    try:
        with LCUClient(creds) as lcu:
            id_to_name: dict[int, str] = {}
            for entry in get_champion_summary(lcu):
                cid = entry.get("id")
                name = entry.get("name") or entry.get("alias")
                if isinstance(cid, int) and isinstance(name, str) and cid > 0:
                    id_to_name[cid] = name
            q.put(("static", id_to_name))
            log(f"[poll] loaded {len(id_to_name)} champion names from LCU")

            last_hash: tuple | None = None
            last_phase: str | None = None

            while not stop_event.is_set():
                session = get_champ_select_session(lcu)
                parsed = parse_session(session) if session else None

                if parsed is None:
                    phase = get_gameflow_phase(lcu)
                    if verbose or phase != last_phase:
                        log(f"[poll] idle  phase={phase}  "
                            f"session={'yes(incomplete)' if session else 'no'}")
                    if phase != last_phase:
                        q.put(("idle", phase))
                        last_phase = phase
                        last_hash = None
                    stop_event.wait(max(poll_interval, 2.0))
                    continue
                last_phase = "ChampSelect"

                state = session_state_hash(parsed)
                if state != last_hash:
                    suggestions = suggest_for_cell(
                        parsed.my_team_ids,
                        parsed.my_current_id,
                        parsed.bench_ids,
                        model,
                        pair_stats,
                    )
                    q.put(("suggestions", parsed, suggestions))
                    last_hash = state
                    log(f"[poll] champ-select update  cell={parsed.my_cell_id}  "
                        f"current={parsed.my_current_id}  bench={len(parsed.bench_ids)}")

                stop_event.wait(poll_interval)
    except Exception as exc:  # pragma: no cover — surfaced to GUI
        q.put(("error", repr(exc)))
        log(f"[poll] error: {exc!r}")


def fake_poll_loop(
    stop_event: threading.Event,
    q: queue.Queue,
    model,
    pair_stats: PairSynergyStats | None,
    interval: float = 3.0,
) -> None:
    """Synthetic poll loop for --fake mode.

    Emits randomly-generated champ-select states every `interval` seconds so
    the GUI can be validated without an LCU connection.  Predictions use the
    real LR model on the random teams, so delta magnitudes match what real
    play would produce — only the champion picks are synthetic.

    Bench size is randomized between 5 and 10 each tick to exercise the
    GUI's vertical scrolling and to match the bench sizes a real ARAM
    queue produces once teammates start rerolling.
    """
    import random

    q.put(("static", {}))  # empty name map — GUI falls back to "#<id>"
    all_ids = sorted(model.champ_to_idx.keys())
    cell_id = 2

    while not stop_event.is_set():
        bench_size = random.randint(5, 10)
        sample = random.sample(all_ids, 5 + bench_size)
        my_team = sample[:5]
        bench = sample[5:]
        my_current = my_team[cell_id]

        parsed = ParsedSession(
            my_team_ids=my_team,
            my_current_id=my_current,
            my_cell_id=cell_id,
            bench_ids=bench,
            bench_enabled=True,
        )
        suggestions = suggest_for_cell(my_team, my_current, bench, model, pair_stats)
        q.put(("suggestions", parsed, suggestions))
        stop_event.wait(interval)


# ---------- GUI ----------

# Palette — warm tinted dark neutrals + a single muted gold accent.
#
# Picked against the scene this UI actually shows up in: an ARAM player
# glancing at a secondary monitor while League's cool, saturated dark UI
# dominates the main screen.  The warmth (low-chroma amber tint) reads as
# a "different surface" rather than competing with League's blues, and
# the muted sage/terracotta deltas avoid the neon-tool-dashboard reflex.
BG        = "#181612"   # warm dark, very low chroma
SURFACE   = "#1f1c17"   # one notch lighter, used sparingly
BEST_BG   = "#2b251a"   # full-row tint for the best-pick row (no side stripes)
FG        = "#e5e0d4"   # warm off-white
DIM       = "#7e7869"   # secondary text, divider hints
MUTED     = "#4a4639"   # tertiary text, unknown / n.a.
DIVIDER   = "#2a261e"   # barely-visible separators
GOLD      = "#d4a73e"   # accent: you, best pick — ≤10% of pixels
GREEN     = "#7eb05e"   # sage — positive delta
RED       = "#c87560"   # terracotta — negative delta

# Fonts — Segoe UI for prose (Windows default sans, ships with the OS and
# pairs well next to League's own Latin UI), Consolas for tabular numbers
# so Δ% and z columns stay aligned across rows.
FONT_HEAD    = ("Segoe UI", 14, "bold")
FONT_SUB     = ("Segoe UI", 9)
FONT_SECTION = ("Segoe UI", 8, "bold")
FONT_NAME    = ("Segoe UI", 11)
FONT_NAME_B  = ("Segoe UI", 11, "bold")
FONT_NUM      = ("Consolas", 11)
FONT_NUM_B    = ("Consolas", 11, "bold")
FONT_NUM_BEST = ("Consolas", 13, "bold")   # one notch up; only best-pick Δ.

# U+2212 MINUS SIGN — proper typographic minus instead of HYPHEN-MINUS.
# Same width as "+" in Consolas so the columns still align.
MINUS = "−"


def _fmt_signed_pct(value_pp: float) -> str:
    """Format a percentage-point delta with a typographic minus for negatives."""
    if value_pp > 0:
        return f"+{value_pp:.1f}%"
    if value_pp < 0:
        return f"{MINUS}{abs(value_pp):.1f}%"
    return f" {value_pp:.1f}%"


def _fmt_signed_z(z: float) -> str:
    """Format a z-score with a typographic minus for negatives."""
    if z > 0:
        return f"+{z:.2f}"
    if z < 0:
        return f"{MINUS}{abs(z):.2f}"
    return f" {z:.2f}"


class RecommenderApp:
    def __init__(self, root: tk.Tk, q: queue.Queue, icon_cache: IconCache | None = None) -> None:
        self.root = root
        self.q = q
        self.id_to_name: dict[int, str] = {}
        self.icon_cache = icon_cache

        root.title("ARAM Recommender")
        root.attributes("-topmost", True)
        # 0.98 keeps a hair of see-through so a window underneath isn't a
        # hard rectangle behind the UI, but stops terminal text reading
        # through as ghosted noise the way 0.93 did.
        root.attributes("-alpha", 0.98)
        # Two-column layout — wider + shorter than the vertical v3.  The
        # 10-row bench dictates height; the team column sits beside it
        # rather than stacked above, which suits a secondary monitor better
        # than a tall column did.
        root.geometry("680x520+40+40")
        root.configure(bg=BG)
        root.minsize(600, 420)

        # Pixel-perfect column geometry, applied identically to every row
        # frame so cells line up regardless of font.  Tk widget `width=N`
        # is in font-average chars; mixing FONT_SECTION (Segoe UI
        # proportional) with FONT_NUM (Consolas mono) at the same `width=N`
        # produced visibly different pixel widths in earlier versions.
        # Pinning to minsize fixes it regardless of font.
        self.COL_ICON   = 44   # 40px icon + 4px gutter
        self.COL_DELTA  = 64
        self.COL_Z      = 56

        # Tk widget constructors only accept a single int for padx/pady
        # (internal padding).  Asymmetric padding goes on the geometry
        # manager call (.pack / .grid).  We use that distinction to set
        # generous outer rhythm without bloating the labels themselves.
        self.header = tk.Label(
            root, text="Loading…",
            bg=BG, fg=FG, font=FONT_HEAD,
            anchor="w", padx=16,
        )
        self.header.pack(fill="x", pady=(14, 0))

        self.subheader = tk.Label(
            root, text="",
            bg=BG, fg=DIM, font=FONT_SUB,
            anchor="w", padx=16,
        )
        self.subheader.pack(fill="x", pady=(2, 12))

        # Thin divider between header and the dynamic body — replaces what
        # a bottom border on the header would do, without violating the
        # absolute ban on accent borders.
        tk.Frame(root, bg=DIVIDER, height=1).pack(fill="x", padx=16)

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(12, 14))

        # Begin draining the queue.
        self.root.after(100, self._drain)

    # ----- Queue handling -----

    def _drain(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.root.after(150, self._drain)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "static":
            self.id_to_name = msg[1]
            self.header.config(text="Waiting for champ select", fg=FG)
            self.subheader.config(text=f"{len(self.id_to_name)} champions loaded")
            self._clear_body()
        elif kind == "idle":
            phase = msg[1]
            self.header.config(text=f"Idle · {phase}", fg=DIM)
            self.subheader.config(text="Queue for ARAM to see swap suggestions.")
            self._clear_body()
        elif kind == "error":
            self.header.config(text="LCU error", fg=RED)
            self.subheader.config(text=msg[1])
            self._clear_body()
        elif kind == "suggestions":
            _, parsed, suggestions = msg
            self._render(parsed, suggestions)

    # ----- Rendering -----

    def _clear_body(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()

    def _render(self, parsed, suggestions) -> None:
        cur_name = self.id_to_name.get(parsed.my_current_id, f"#{parsed.my_current_id}")
        self.header.config(text=f"Cell {parsed.my_cell_id}  ·  {cur_name}", fg=FG)
        # Compressed legend.  v1 split this across "opponent unknown" and
        # the column meanings; one line reads faster during a 30s timer.
        self.subheader.config(
            text="Δ  win-rate change if you swap     z  champion meta strength"
        )

        self._clear_body()

        # Two-column body: team (static context) on the left, bench (the
        # action zone) on the right.  Bench gets more weight because it's
        # what the user is actually scanning during a 30-second timer.
        self.body.grid_columnconfigure(0, weight=2, minsize=260)
        self.body.grid_columnconfigure(1, weight=3, minsize=320)
        self.body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(self.body, bg=BG)
        left.grid(row=0, column=0, sticky="new", padx=(0, 24))
        right = tk.Frame(self.body, bg=BG)
        right.grid(row=0, column=1, sticky="new")

        self._render_team_section(left, parsed, suggestions)
        self._render_bench_section(right, suggestions)

    def _configure_team_row(self, row: tk.Frame) -> None:
        """Team rows only need icon + name (no Δ / no z column for teammates)."""
        row.grid_columnconfigure(0, minsize=self.COL_ICON)
        row.grid_columnconfigure(1, weight=1)

    def _configure_bench_row(self, row: tk.Frame) -> None:
        """Bench rows: icon + Δ + z + name."""
        row.grid_columnconfigure(0, minsize=self.COL_ICON)
        row.grid_columnconfigure(1, minsize=self.COL_DELTA)
        row.grid_columnconfigure(2, minsize=self.COL_Z)
        row.grid_columnconfigure(3, weight=1)

    def _render_team_section(self, parent, parsed, suggestions) -> None:
        """Show all 5 blue-team champions in the left column.

        Teammates are dimmed (you can't swap them, they're context).  Your
        own row gets a gold name + ⊙ marker and the z-score inline so the
        user always knows their current meta strength as an anchor for
        comparing the bench candidates on the right.
        """
        own_z = next(
            (s.z_score for s in suggestions if s.source == "keep" and s.is_known),
            None,
        )

        tk.Label(
            parent, text="YOUR TEAM",
            bg=BG, fg=DIM, anchor="w", font=FONT_SECTION,
        ).pack(fill="x", pady=(0, 10))

        for cid in parsed.my_team_ids:
            is_me = (cid == parsed.my_current_id)
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", pady=2)
            self._configure_team_row(row)

            self._icon_cell(row, cid, bg=BG)

            name = self.id_to_name.get(cid, f"#{cid}")
            if is_me:
                z_str = f"   {_fmt_signed_z(own_z)}" if own_z is not None else ""
                tk.Label(
                    row, text=f"⊙ {name}{z_str}",
                    bg=BG, fg=GOLD, font=FONT_NAME_B, anchor="w",
                ).grid(row=0, column=1, sticky="w")
            else:
                tk.Label(
                    row, text=name, bg=BG, fg=DIM,
                    font=FONT_NAME, anchor="w",
                ).grid(row=0, column=1, sticky="w")

    def _render_bench_section(self, parent, suggestions) -> None:
        """Show bench swap candidates in the right column.

        The keep entry from `suggestions` is excluded — it's already shown
        in the team section.  Best pick gets a full-row warm-tint
        background (no side stripe — that's a hard ban), a slightly larger
        bold gold Δ, and a gold ★ marker; remaining rows fall back to BG.

        Column-header row is intentionally absent: the subheader at the
        top of the window explains Δ + z once, and a per-section header
        in different font metrics from the data rows is what caused the
        v2 alignment bug.
        """
        bench = [s for s in suggestions if s.source == "bench"]

        tk.Label(
            parent, text=f"BENCH   ·   {len(bench)} OPTIONS",
            bg=BG, fg=DIM, anchor="w", font=FONT_SECTION,
        ).pack(fill="x", pady=(0, 10))

        # First known bench entry is the best swap (suggestions sorted desc by Δ).
        best_idx = next((i for i, s in enumerate(bench) if s.is_known), None)

        for i, s in enumerate(bench):
            is_best = (i == best_idx)
            row_bg = BEST_BG if is_best else BG

            name = self.id_to_name.get(s.champion_id, f"#{s.champion_id}")
            row = tk.Frame(parent, bg=row_bg)
            row.pack(fill="x", pady=1, ipady=3)
            self._configure_bench_row(row)
            self._icon_cell(row, s.champion_id, bg=row_bg)

            if not s.is_known:
                self._cell(row, 1, "n/a", MUTED, bg=row_bg, font=FONT_NUM)
                self._cell(row, 2, "n/a", MUTED, bg=row_bg, font=FONT_NUM)
                self._cell(row, 3, f"{name}   (not in vocab)",
                           MUTED, bg=row_bg, font=FONT_NAME)
                continue

            delta_pp = s.delta * 100
            delta_text = _fmt_signed_pct(delta_pp)
            delta_color = GREEN if delta_pp > 0 else (RED if delta_pp < 0 else DIM)
            # The best row's Δ gets the extra visual weight: bigger + bold,
            # in gold so it ties to the row's name color.  All others use
            # standard FONT_NUM and the green/red signal carries the
            # status.  Heightened Δ on the best row is the single primary
            # affordance — the eye lands there first.
            if is_best:
                delta_font = FONT_NUM_BEST
                delta_color = GOLD if delta_pp >= 0 else RED
            else:
                delta_font = FONT_NUM

            z_text = _fmt_signed_z(s.z_score)
            z_color = GREEN if s.z_score > 0.5 else (RED if s.z_score < -0.5 else FG)

            name_color = GOLD if is_best else FG
            name_font = FONT_NAME_B if is_best else FONT_NAME
            marker = "★  " if is_best else "    "

            self._cell(row, 1, delta_text, delta_color, bg=row_bg, font=delta_font)
            self._cell(row, 2, z_text, z_color, bg=row_bg, font=FONT_NUM)
            self._cell(row, 3, f"{marker}{name}", name_color, bg=row_bg, font=name_font)

    def _icon_cell(self, parent: tk.Frame, champion_id: int, bg: str = BG) -> None:
        """Place the champion icon in column 0 of `parent`.

        bg matches the parent row's background so the icon's surrounding
        pixels blend on tinted (best-pick) rows.  Falls back to a hollow
        placeholder Label of the same width if the IconCache can't produce
        a PhotoImage, so row alignment stays stable.
        """
        photo = self.icon_cache.get(champion_id) if self.icon_cache else None
        if photo is not None:
            lbl = tk.Label(parent, image=photo, bg=bg, bd=0)
            # Hold the reference on the widget too — Tk doesn't keep it, and
            # the redundancy is cheap and removes a class of GC bugs.
            lbl.image = photo  # type: ignore[attr-defined]
            lbl.grid(row=0, column=0, padx=(0, 6))
        else:
            tk.Label(
                parent, text="", bg=bg, width=4, height=2,
            ).grid(row=0, column=0, padx=(0, 6))

    @staticmethod
    def _cell(
        parent: tk.Frame, col: int, text: str, fg: str,
        bg: str = BG, font: tuple = FONT_NUM,
    ) -> None:
        """Place a left-aligned label at `col` in the row's shared grid.

        Width is no longer passed explicitly: column widths come from
        the row's grid_columnconfigure(minsize=...) so every row pins
        to the same x positions regardless of which font the content
        is set in.
        """
        tk.Label(
            parent, text=text, bg=bg, fg=fg,
            font=font, anchor="w",
        ).grid(row=0, column=col, sticky="w")


# ---------- Entry point ----------

@click.command()
@click.option("--lr-model", required=True,
              type=click.Path(exists=True, path_type=Path, dir_okay=False),
              help="Path to lr_model.pkl (sklearn LR pickle, loaded without sklearn) or lr_weights.json.")
@click.option("--vocab", required=True,
              type=click.Path(exists=True, path_type=Path, dir_okay=False),
              help="Path to tier2_checkpoint.pt or champ_to_idx.json — used for champion vocab.")
@click.option("--pair-stats", default=Path("models/pair_synergy_16_10.json"),
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to pair synergy JSON from scripts/build_pair_stats.py.")
@click.option("--poll-interval", default=1.0, show_default=True, type=float,
              help="Seconds between LCU polls while in ChampSelect.")
@click.option("--fake", is_flag=True, default=False,
              help="Demo mode: skip LCU, generate random champ-select states every 3s. "
                   "Useful to verify the GUI works without launching League.")
@click.option("--verbose", is_flag=True, default=False,
              help="Print per-poll status (phase + session presence) to stdout. "
                   "Useful for diagnosing why a champ-select isn't being detected.")
def main(
    lr_model: Path,
    vocab: Path,
    pair_stats: Path,
    poll_interval: float,
    fake: bool,
    verbose: bool,
) -> None:
    """Tk GUI for the ARAM champ-select recommender."""
    print(f"[gui] loading model from {lr_model}")
    model = load_lr(lr_model, vocab)
    print(f"[gui] vocab covers {model.n_champs} champions")
    pair_model = None
    if pair_stats.exists():
        pair_model = load_pair_synergy(pair_stats)
        print(
            f"[gui] pair synergy rows={len(pair_model.rows):,} "
            f"patch={pair_model.patch_prefix} min_pair={pair_model.min_pair}"
        )
    else:
        print(f"[gui] WARN: pair stats not found at {pair_stats}; falling back to 30% LR score")

    q: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    # IconCache works in both modes: prefers LCU (local, fast) when creds
    # are present, otherwise falls back to Riot's Data Dragon CDN.  In
    # --fake without League running, only the CDN path is used; that needs
    # internet but caches to disk so future runs are instant offline.
    creds_for_icons = get_credentials()  # may be None, that's fine
    icon_cache = IconCache(Path("data/icons"), lcu_creds=creds_for_icons)
    threading.Thread(target=icon_cache.prefetch_all, daemon=True).start()

    if fake:
        print("[gui] --fake: synthesizing champ-select states every 3s, no LCU needed")
        thread = threading.Thread(
            target=fake_poll_loop, args=(stop_event, q, model, pair_model), daemon=True,
        )
    else:
        creds = creds_for_icons  # reuse — same credentials work for both
        if not creds:
            # Show the error in a window — easier to notice than a stderr message
            # that scrolls off when the user double-clicks the script.
            root = tk.Tk()
            root.title("ARAM Recommender")
            root.configure(bg=BG)
            tk.Label(
                root, text="League client not running",
                bg=BG, fg=RED, font=FONT_HEAD, padx=24, pady=(20, 4), anchor="w",
            ).pack(fill="x")
            tk.Label(
                root, text="No LCU credentials found.\n\nTip: pass --fake to demo the GUI without League.",
                bg=BG, fg=DIM, font=FONT_NAME, padx=24, pady=(0, 24),
                anchor="w", justify="left",
            ).pack(fill="x")
            root.mainloop()
            sys.exit(1)
        thread = threading.Thread(
            target=poll_loop,
            args=(stop_event, q, model, pair_model, creds, poll_interval, verbose),
            daemon=True,
        )

    thread.start()  # crucial — without this, the poll loop never runs and
                    # the GUI stays on its placeholder "Loading..." header forever.

    root = tk.Tk()
    RecommenderApp(root, q, icon_cache=icon_cache)
    try:
        root.mainloop()
    finally:
        # Signal the poll thread to exit cleanly so the httpx client closes.
        stop_event.set()


if __name__ == "__main__":
    main()
