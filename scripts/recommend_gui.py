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

from aram_nn.lcu.client import (
    LCUClient, get_champion_summary, get_champ_select_session, get_gameflow_phase,
)
from aram_nn.lcu.process import get_credentials
from aram_nn.recommend import (
    load_lr, parse_session, session_state_hash, suggest_for_cell,
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
    def __init__(self, root: tk.Tk, q: queue.Queue) -> None:
        self.root = root
        self.q = q
        self.id_to_name: dict[int, str] = {}

        root.title("ARAM Recommender")
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.93)
        root.geometry("360x300+40+40")
        root.configure(bg=BG)
        root.minsize(320, 200)

        self.header = tk.Label(
            root, text="Loading model & LCU...",
            bg=BG, fg=FG, font=("Consolas", 12, "bold"),
            anchor="w", padx=12, pady=(10, 2),
        )
        self.header.pack(fill="x")

        self.subheader = tk.Label(
            root, text="",
            bg=BG, fg=DIM, font=("Consolas", 9),
            anchor="w", padx=12, pady=(0, 8),
        )
        self.subheader.pack(fill="x")

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
        self.subheader.config(text="P(win) assumes average opponent")

        self._clear_body()

        # Column headers
        hdr = tk.Frame(self.body, bg=BG)
        hdr.pack(fill="x", pady=(0, 4))
        for col, text, width in [(0, "Δ%", 8), (1, "P(win)", 8), (2, "champion", 18)]:
            tk.Label(
                hdr, text=text, bg=BG, fg=DIM,
                font=("Consolas", 9, "bold"), width=width, anchor="w",
            ).grid(row=0, column=col, sticky="w")

        for i, s in enumerate(suggestions):
            name = self.id_to_name.get(s.champion_id, f"#{s.champion_id}")
            row = tk.Frame(self.body, bg=BG)
            row.pack(fill="x", pady=1)

            if not s.is_known:
                self._cell(row, 0, " n/a", MUTED, 8)
                self._cell(row, 1, " n/a", MUTED, 8)
                self._cell(row, 2, f"{name}  (not in vocab)", MUTED, 18)
                continue

            if s.source == "keep":
                delta_text = "  ——"
                delta_color = DIM
                marker = "⊙"
                name_color = FG
            else:
                delta_pp = s.delta * 100
                delta_text = f"{delta_pp:+5.1f}%"
                delta_color = GREEN if delta_pp > 0 else (RED if delta_pp < 0 else DIM)
                # Best non-keep suggestion gets the star.
                first_non_keep = next(
                    (idx for idx, sg in enumerate(suggestions)
                     if sg.source != "keep" and sg.is_known), None,
                )
                marker = "★" if i == first_non_keep else " "
                name_color = ACCENT if marker == "★" else FG

            self._cell(row, 0, delta_text, delta_color, 8)
            self._cell(row, 1, f"{s.win_prob * 100:5.1f}%", FG, 8)
            self._cell(row, 2, f"{marker} {name}", name_color, 18)

    @staticmethod
    def _cell(parent: tk.Frame, col: int, text: str, fg: str, width: int) -> None:
        tk.Label(
            parent, text=text, bg=BG, fg=fg,
            font=("Consolas", 10), width=width, anchor="w",
        ).grid(row=0, column=col, sticky="w")


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
def main(lr_model: Path, vocab: Path, poll_interval: float) -> None:
    """Tk GUI for the ARAM champ-select recommender."""
    creds = get_credentials()
    if not creds:
        # Show the error in a window — easier to notice than a stderr message
        # that scrolls off when the user double-clicks the script.
        root = tk.Tk()
        root.title("ARAM Recommender — error")
        root.configure(bg=BG)
        tk.Label(
            root, text="League client not running.\n(No LCU credentials found.)",
            bg=BG, fg=RED, font=("Consolas", 11), padx=20, pady=20,
        ).pack()
        root.mainloop()
        sys.exit(1)

    print(f"[gui] loading model from {lr_model}")
    model = load_lr(lr_model, vocab)
    print(f"[gui] vocab covers {model.n_champs} champions")

    q: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=poll_loop,
        args=(stop_event, q, model, creds, poll_interval),
        daemon=True,
    )
    thread.start()

    root = tk.Tk()
    RecommenderApp(root, q)
    try:
        root.mainloop()
    finally:
        # Signal the poll thread to exit cleanly so the httpx client closes.
        stop_event.set()


if __name__ == "__main__":
    main()
