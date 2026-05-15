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
      --vocab    models/tier2_mayhem/tier2_checkpoint.pt
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


# ---------- Polling thread ----------

def poll_loop(stop_event: threading.Event, q: queue.Queue, model, creds, poll_interval: float) -> None:
    """Run in background thread.  Pushes messages onto `q`:
      ("static", id_to_name)         — once, after LCU static data loads
      ("idle", phase)                — when not in (or about to leave) champ select
      ("suggestions", parsed, sugs)  — when champ select state changes
      ("error", message)             — on unrecoverable failure
    """
    try:
        with LCUClient(creds) as lcu:
            id_to_name: dict[int, str] = {}
            for entry in get_champion_summary(lcu):
                cid = entry.get("id")
                name = entry.get("name") or entry.get("alias")
                if isinstance(cid, int) and isinstance(name, str) and cid > 0:
                    id_to_name[cid] = name
            q.put(("static", id_to_name))

            last_hash: tuple | None = None
            last_phase: str | None = None

            while not stop_event.is_set():
                session = get_champ_select_session(lcu)
                parsed = parse_session(session) if session else None

                if parsed is None:
                    phase = get_gameflow_phase(lcu)
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
                        parsed.my_team_ids, parsed.my_current_id, parsed.bench_ids, model,
                    )
                    q.put(("suggestions", parsed, suggestions))
                    last_hash = state

                stop_event.wait(poll_interval)
    except Exception as exc:  # pragma: no cover — surfaced to GUI
        q.put(("error", repr(exc)))


def fake_poll_loop(stop_event: threading.Event, q: queue.Queue, model, interval: float = 3.0) -> None:
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
        suggestions = suggest_for_cell(my_team, my_current, bench, model)
        q.put(("suggestions", parsed, suggestions))
        stop_event.wait(interval)


# ---------- GUI ----------

# Dark palette tuned to be readable next to League's own UI.
BG       = "#1a1a1a"
FG       = "#dddddd"
DIM      = "#888888"
MUTED    = "#666666"
GREEN    = "#4caf50"
RED      = "#e57373"
ACCENT   = "#ffd54f"


class RecommenderApp:
    def __init__(self, root: tk.Tk, q: queue.Queue, icon_cache: IconCache | None = None) -> None:
        self.root = root
        self.q = q
        self.id_to_name: dict[int, str] = {}
        self.icon_cache = icon_cache

        root.title("ARAM Recommender")
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.93)
        # Wider + taller than v1: icons add ~40px width per row, and bench
        # can now have up to 10 champions (11 rows incl. keep).
        root.geometry("420x560+40+40")
        root.configure(bg=BG)
        root.minsize(380, 240)

        # Tk widget constructors only accept a single int for padx/pady
        # (internal padding).  Asymmetric padding goes on the geometry
        # manager call (.pack / .grid).
        self.header = tk.Label(
            root, text="Loading model & LCU...",
            bg=BG, fg=FG, font=("Consolas", 12, "bold"),
            anchor="w", padx=12,
        )
        self.header.pack(fill="x", pady=(10, 2))

        self.subheader = tk.Label(
            root, text="",
            bg=BG, fg=DIM, font=("Consolas", 9),
            anchor="w", padx=12,
        )
        self.subheader.pack(fill="x", pady=(0, 8))

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

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
            self.header.config(text="Waiting for ARAM champ select...")
            self.subheader.config(text=f"{len(self.id_to_name)} champion names loaded")
            self._clear_body()
        elif kind == "idle":
            phase = msg[1]
            self.header.config(text=f"Idle  ({phase})")
            self.subheader.config(text="Open League and queue for ARAM.")
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
        self.header.config(text=f"Cell {parsed.my_cell_id}   Current: {cur_name}", fg=FG)
        # z = (coef - mean) / std over all champions in this model.  At |z|=1
        # you're roughly top/bottom 16%; |z|=2 is top/bottom 2.5%.
        self.subheader.config(text="z = champion strength (σ from meta mean)")

        self._clear_body()

        # Column headers — columns: [icon] [Δ%] [z] [name].
        hdr = tk.Frame(self.body, bg=BG)
        hdr.pack(fill="x", pady=(0, 4))
        # Empty placeholder cell for the icon column so the header aligns.
        tk.Label(hdr, text="", bg=BG, width=4).grid(row=0, column=0)
        for col, text, width in [(1, "Δ%", 7), (2, "z", 6), (3, "champion", 16)]:
            tk.Label(
                hdr, text=text, bg=BG, fg=DIM,
                font=("Consolas", 9, "bold"), width=width, anchor="w",
            ).grid(row=0, column=col, sticky="w", padx=(2, 0))

        # Best non-keep suggestion gets the ★.  Computed once outside the loop
        # so we don't re-scan the list for every row.
        first_non_keep = next(
            (idx for idx, sg in enumerate(suggestions)
             if sg.source != "keep" and sg.is_known), None,
        )

        for i, s in enumerate(suggestions):
            name = self.id_to_name.get(s.champion_id, f"#{s.champion_id}")
            row = tk.Frame(self.body, bg=BG)
            row.pack(fill="x", pady=2)

            # Column 0: icon (or blank square if unavailable).
            self._icon_cell(row, s.champion_id)

            if not s.is_known:
                self._cell(row, 1, " n/a", MUTED, 7)
                self._cell(row, 2, " n/a", MUTED, 6)
                self._cell(row, 3, f"{name}  (not in vocab)", MUTED, 18)
                continue

            # Column 1: Δ% (change in P(win) from keeping current).
            if s.source == "keep":
                delta_text = "  ——"
                delta_color = DIM
                marker = "⊙"
                name_color = FG
            else:
                delta_pp = s.delta * 100
                delta_text = f"{delta_pp:+5.1f}%"
                delta_color = GREEN if delta_pp > 0 else (RED if delta_pp < 0 else DIM)
                marker = "★" if i == first_non_keep else " "
                name_color = ACCENT if marker == "★" else FG

            # Column 2: absolute z-score of this champion in the meta.
            z = s.z_score
            z_text = f"{z:+.2f}"
            z_color = GREEN if z > 0.5 else (RED if z < -0.5 else FG)

            self._cell(row, 1, delta_text, delta_color, 7)
            self._cell(row, 2, z_text, z_color, 6)
            self._cell(row, 3, f"{marker} {name}", name_color, 18)

    def _icon_cell(self, parent: tk.Frame, champion_id: int) -> None:
        """Place the champion icon in column 0 of `parent`.

        Falls back to a hollow placeholder Label of the same width if the
        IconCache can't produce a PhotoImage — keeps row alignment stable
        whether icons resolve or not.
        """
        photo = self.icon_cache.get(champion_id) if self.icon_cache else None
        if photo is not None:
            lbl = tk.Label(parent, image=photo, bg=BG, bd=0)
            # Hold the reference on the widget too — Tk doesn't keep it, and
            # if the only reference is in IconCache._photos we're still safe,
            # but the redundancy is cheap and removes a class of GC bugs.
            lbl.image = photo  # type: ignore[attr-defined]
            lbl.grid(row=0, column=0, padx=(0, 6))
        else:
            tk.Label(
                parent, text="", bg=BG, width=4, height=2,
            ).grid(row=0, column=0, padx=(0, 6))

    @staticmethod
    def _cell(parent: tk.Frame, col: int, text: str, fg: str, width: int) -> None:
        # padx=(2,0) matches the header row so columns line up across frames.
        tk.Label(
            parent, text=text, bg=BG, fg=fg,
            font=("Consolas", 10), width=width, anchor="w",
        ).grid(row=0, column=col, sticky="w", padx=(2, 0))


# ---------- Entry point ----------

@click.command()
@click.option("--lr-model", required=True,
              type=click.Path(exists=True, path_type=Path, dir_okay=False),
              help="Path to lr_model.pkl (sklearn LR pickle, loaded without sklearn) or lr_weights.json.")
@click.option("--vocab", required=True,
              type=click.Path(exists=True, path_type=Path, dir_okay=False),
              help="Path to tier2_checkpoint.pt or champ_to_idx.json — used for champion vocab.")
@click.option("--poll-interval", default=1.0, show_default=True, type=float,
              help="Seconds between LCU polls while in ChampSelect.")
@click.option("--fake", is_flag=True, default=False,
              help="Demo mode: skip LCU, generate random champ-select states every 3s. "
                   "Useful to verify the GUI works without launching League.")
def main(lr_model: Path, vocab: Path, poll_interval: float, fake: bool) -> None:
    """Tk GUI for the ARAM champ-select recommender."""
    print(f"[gui] loading model from {lr_model}")
    model = load_lr(lr_model, vocab)
    print(f"[gui] vocab covers {model.n_champs} champions")

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
            target=fake_poll_loop, args=(stop_event, q, model), daemon=True,
        )
    else:
        creds = creds_for_icons  # reuse — same credentials work for both
        if not creds:
            # Show the error in a window — easier to notice than a stderr message
            # that scrolls off when the user double-clicks the script.
            root = tk.Tk()
            root.title("ARAM Recommender — error")
            root.configure(bg=BG)
            tk.Label(
                root, text="League client not running.\n(No LCU credentials found.)\n\n"
                           "Tip: pass --fake to demo the GUI without League.",
                bg=BG, fg=RED, font=("Consolas", 11), padx=20, pady=20,
            ).pack()
            root.mainloop()
            sys.exit(1)
        thread = threading.Thread(
            target=poll_loop,
            args=(stop_event, q, model, creds, poll_interval),
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
