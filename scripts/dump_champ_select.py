"""Dump a single champ-select session payload to disk for schema inspection.

Run while the League client is in ARAM champ select.  Writes the raw JSON to
data/lcu/champ_select_sample.json so we can confirm field names / opponent
visibility before wiring up the recommender.

Usage:
  python scripts/dump_champ_select.py
  python scripts/dump_champ_select.py --out data/lcu/my_dump.json
  python scripts/dump_champ_select.py --wait 60   # poll until ChampSelect appears
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import click

from aram_nn.lcu.client import LCUClient, get_champ_select_session, get_gameflow_phase
from aram_nn.lcu.process import get_credentials


@click.command()
@click.option("--out", default=Path("data/lcu/champ_select_sample.json"),
              type=click.Path(path_type=Path), show_default=True)
@click.option("--wait", default=0, type=int,
              help="Seconds to poll waiting for ChampSelect phase. 0 = require it now.")
def main(out: Path, wait: int) -> None:
    creds = get_credentials()
    if not creds:
        click.echo("[error] League client not running (no LCU credentials found).")
        raise SystemExit(1)

    deadline = time.monotonic() + wait
    session = None
    with LCUClient(creds) as lcu:
        while True:
            phase = get_gameflow_phase(lcu)
            if phase == "ChampSelect":
                session = get_champ_select_session(lcu)
                if session:
                    break
            if time.monotonic() >= deadline:
                click.echo(f"[error] not in ChampSelect (current phase: {phase})")
                raise SystemExit(2)
            click.echo(f"  phase={phase}  waiting...")
            time.sleep(2)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(session, indent=2, ensure_ascii=False))

    # Print the key fields we care about so the user sees what got dumped.
    my_team = session.get("myTeam") or []
    their_team = session.get("theirTeam") or []
    bench = session.get("benchChampions") or []
    my_cell = session.get("localPlayerCellId")
    their_visible = sum(1 for t in their_team if (t.get("championId") or 0) > 0)

    click.echo(f"\n[dumped] {out}  ({out.stat().st_size} bytes)")
    click.echo(f"  localPlayerCellId : {my_cell}")
    click.echo(f"  myTeam            : {len(my_team)} cells, "
               f"champions = {[t.get('championId') for t in my_team]}")
    click.echo(f"  theirTeam         : {len(their_team)} cells, "
               f"visible championIds = {their_visible}/{len(their_team)} "
               f"({[t.get('championId') for t in their_team]})")
    click.echo(f"  benchEnabled      : {session.get('benchEnabled')}")
    click.echo(f"  benchChampions    : {[b.get('championId') for b in bench]}")


if __name__ == "__main__":
    main()
