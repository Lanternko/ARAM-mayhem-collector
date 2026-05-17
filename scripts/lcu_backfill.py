"""Backfill recent LCU-visible ARAM / Mayhem games into games.db.

Uses /lol-match-history/v1/games/{gameId}, which returns all 10 participants.
This script shares the production poller parser so participant payloads include
augments plus selected post-game stats such as items, gold, damage, tanking,
CC, healing, and shielding.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "src")
from aram_nn.lcu.client import LCUClient, get_current_summoner, get_game_detail, get_match_history
from aram_nn.lcu.poller import (
    _ensure_games_schema,
    _parse_game_detail,
    _participants_payload_has_postgame_stats,
)
from aram_nn.lcu.process import get_credentials

DB = Path("data/lcu/games.db")
TARGET_QUEUES = {450, 2400}
_MODE_TO_QUEUE = {"KIWI": 2400, "ARAM": 450}


def _save_or_backfill(
    con: sqlite3.Connection,
    record: dict,
    already: set[str],
    existing_payload: dict[str, str],
) -> str:
    participants_json = json.dumps(record.get("participants", []), separators=(",", ":"))
    if record["game_id"] in already:
        current_json = existing_payload.get(record["game_id"], "")
        if current_json:
            if _participants_payload_has_postgame_stats(current_json):
                return "skipped"
            if not _participants_payload_has_postgame_stats(participants_json):
                return "skipped"
        con.execute(
            """
            UPDATE games
            SET participants_json = ?
            WHERE game_id = ?
            """,
            (participants_json, record["game_id"]),
        )
        existing_payload[record["game_id"]] = participants_json
        return "backfilled"

    con.execute(
        """
        INSERT OR IGNORE INTO games (
            game_id, queue_id, patch, blue_champs, red_champs,
            blue_wins, duration_sec, created_ms, captured_at, participants_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            record["game_id"],
            record["queue_id"],
            record["patch"],
            json.dumps(record["blue_champs"]),
            json.dumps(record["red_champs"]),
            record["blue_wins"],
            record["duration_sec"],
            record["created_ms"],
            record["captured_at"],
            participants_json,
        ),
    )
    already.add(record["game_id"])
    existing_payload[record["game_id"]] = participants_json
    return "saved"


creds = get_credentials()
if not creds:
    print("[error] League client not found")
    sys.exit(1)

DB.parent.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(str(DB))
_ensure_games_schema(con)

existing_payload = {
    str(row[0]): str(row[1] or "")
    for row in con.execute("SELECT game_id, participants_json FROM games").fetchall()
}
already = set(existing_payload)
print(f"DB already has {len(already)} games")

with LCUClient(creds) as lcu:
    summoner = get_current_summoner(lcu)
    if not summoner or not summoner.get("puuid"):
        print("[error] could not resolve current summoner")
        sys.exit(1)

    puuid = summoner["puuid"]
    name = summoner.get("gameName") or summoner.get("displayName")
    print(f"Connected as {name}  puuid={puuid[:12]}")

    history = get_match_history(lcu, puuid, begin=0, end=20)
    target_ids = [
        str(game["gameId"])
        for game in history
        if game.get("queueId") in TARGET_QUEUES
        or _MODE_TO_QUEUE.get(game.get("gameMode", ""), -1) in TARGET_QUEUES
    ]
    print(f"Found {len(target_ids)} target-queue games in history (last 20)")

    saved = backfilled = skipped = failed = 0
    for game_id in target_ids:
        if game_id in already and _participants_payload_has_postgame_stats(
            existing_payload.get(game_id, "")
        ):
            skipped += 1
            continue

        detail = get_game_detail(lcu, game_id)
        if not detail:
            print(f"  [warn] could not fetch detail for {game_id}")
            failed += 1
            continue

        record = _parse_game_detail(detail, TARGET_QUEUES)
        if not record:
            print(f"  [skip] {game_id} filtered (wrong queue / too short / parse fail)")
            skipped += 1
            continue

        action = _save_or_backfill(con, record, already, existing_payload)
        con.commit()
        if action == "saved":
            saved += 1
        elif action == "backfilled":
            backfilled += 1
        else:
            skipped += 1
            continue

        label = "Mayhem" if record["queue_id"] == 2400 else "ARAM"
        print(
            f"  [{action}] {label}  {game_id}  patch={record['patch']}  "
            f"blue={record['blue_champs']}  red={record['red_champs']}  "
            f"blue_wins={bool(record['blue_wins'])}  dur={record['duration_sec']}s"
        )

total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
con.close()
print(
    f"\nDone.  saved={saved}  backfilled={backfilled}  "
    f"skipped={skipped}  failed={failed}  total_in_db={total}"
)
