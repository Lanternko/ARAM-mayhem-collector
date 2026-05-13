"""Snowball crawl recent LCU-visible match history across discovered players.

The LCU match-history list endpoint usually exposes only the last ~20 games for a puuid.
This crawler persists two separate crawl structures in SQLite:

1. `crawl_seen`: de-dup set of discovered puuids plus crawl metadata
2. `crawl_queue`: persistent priority queue of pending / in-progress / done nodes

That means we can pause at any time, then resume from the saved queue state.
Newer discovered matches get higher priority because they are more likely to be
current-patch and from active players. Exact match de-duplication still uses game_id,
not champion composition.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .client import (
    LCUClient,
    get_apex_league,
    get_current_summoner,
    get_friends,
    get_game_detail,
    get_league_ladders,
    get_match_history,
)
from .poller import DEFAULT_QUEUES, _parse_game_detail
from .process import get_credentials

_CREATE_GAMES_SQL = """
CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT PRIMARY KEY,
    queue_id     INTEGER NOT NULL,
    patch        TEXT NOT NULL,
    blue_champs  TEXT NOT NULL,
    red_champs   TEXT NOT NULL,
    blue_wins    INTEGER NOT NULL,
    duration_sec INTEGER NOT NULL,
    created_ms   INTEGER NOT NULL,
    captured_at  TEXT NOT NULL,
    participants_json TEXT
);
"""

_CREATE_CRAWL_SEEN_SQL = """
CREATE TABLE IF NOT EXISTS crawl_seen (
    puuid                         TEXT PRIMARY KEY,
    source                        TEXT NOT NULL,
    priority                      INTEGER NOT NULL,
    min_depth                     INTEGER NOT NULL,
    discovered_from_game_id       TEXT,
    first_seen_at                 TEXT NOT NULL,
    last_crawled_at               TEXT,
    process_count                 INTEGER NOT NULL DEFAULT 0,
    new_games_found               INTEGER NOT NULL DEFAULT 0,
    latest_seen_match_created_ms  INTEGER NOT NULL DEFAULT 0,
    last_crawled_match_created_ms INTEGER NOT NULL DEFAULT 0,
    processed                     INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_CRAWL_QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS crawl_queue (
    queue_idx                   INTEGER PRIMARY KEY AUTOINCREMENT,
    puuid                       TEXT NOT NULL UNIQUE,
    depth                       INTEGER NOT NULL,
    source                      TEXT NOT NULL,
    priority                    INTEGER NOT NULL,
    discovered_from_game_id     TEXT,
    discovered_match_created_ms INTEGER NOT NULL DEFAULT 0,
    enqueued_at                 TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    claimed_by                  TEXT,
    claimed_at_ms               INTEGER NOT NULL DEFAULT 0,
    eligible_at_ms              INTEGER NOT NULL DEFAULT 0,
    status                      TEXT NOT NULL DEFAULT 'pending'
);
"""

_CREATE_CRAWL_QUEUE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_queue_status_priority
ON crawl_queue(
    status,
    eligible_at_ms,
    discovered_match_created_ms DESC,
    priority ASC,
    depth ASC,
    updated_at ASC,
    queue_idx ASC
);
"""

_CREATE_CRAWL_GAME_CLAIMS_SQL = """
CREATE TABLE IF NOT EXISTS crawl_game_claims (
    game_id        TEXT PRIMARY KEY,
    claimed_by     TEXT,
    claimed_at_ms  INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
);
"""

_CREATE_CRAWL_GAME_CLAIMS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_game_claims_status
ON crawl_game_claims(status, claimed_at_ms, updated_at, game_id);
"""

_MODE_TO_QUEUE = {"KIWI": 2400, "ARAM": 450}
_SOURCE_PRIORITY = {
    "self": 0,
    "match": 10,
    "apex": 20,
    "ladder": 30,
    "friend": 40,
}


@dataclass
class CrawlStats:
    seeded_players: int = 0
    processed_players: int = 0
    expanded_games: int = 0
    saved_games: int = 0
    existing_games: int = 0
    filtered_games: int = 0
    failed_games: int = 0
    requeued_players: int = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _connect_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(con: sqlite3.Connection, table_name: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_column(con: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
    if column_name not in _table_columns(con, table_name):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def _lookup_game_created_ms(con: sqlite3.Connection, game_id: str | None) -> int:
    if not game_id:
        return 0
    row = con.execute(
        "SELECT created_ms FROM games WHERE game_id = ?",
        (str(game_id),),
    ).fetchone()
    return int(row[0]) if row else 0


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(_CREATE_GAMES_SQL)
    con.execute(_CREATE_CRAWL_SEEN_SQL)
    con.execute(_CREATE_CRAWL_QUEUE_SQL)
    con.execute(_CREATE_CRAWL_GAME_CLAIMS_SQL)

    _ensure_column(
        con,
        "games",
        "participants_json",
        "participants_json TEXT",
    )

    _ensure_column(
        con,
        "crawl_seen",
        "latest_seen_match_created_ms",
        "latest_seen_match_created_ms INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        con,
        "crawl_seen",
        "last_crawled_match_created_ms",
        "last_crawled_match_created_ms INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "discovered_match_created_ms",
        "discovered_match_created_ms INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "updated_at",
        "updated_at TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "claimed_by",
        "claimed_by TEXT",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "claimed_at_ms",
        "claimed_at_ms INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "eligible_at_ms",
        "eligible_at_ms INTEGER NOT NULL DEFAULT 0",
    )

    con.execute(_CREATE_CRAWL_QUEUE_INDEX_SQL)
    con.execute(_CREATE_CRAWL_GAME_CLAIMS_INDEX_SQL)

    if _table_exists(con, "crawl_queue"):
        con.execute(
            """
            UPDATE crawl_queue
            SET updated_at = CASE
                WHEN updated_at = '' THEN enqueued_at
                ELSE updated_at
            END
            """
        )
        con.execute(
            """
            UPDATE crawl_queue
            SET discovered_match_created_ms = COALESCE(
                (
                    SELECT games.created_ms
                    FROM games
                    WHERE games.game_id = crawl_queue.discovered_from_game_id
                ),
                discovered_match_created_ms,
                0
            )
            WHERE discovered_match_created_ms = 0
              AND discovered_from_game_id IS NOT NULL
            """
        )
    if _table_exists(con, "crawl_seen"):
        con.execute(
            """
            UPDATE crawl_seen
            SET latest_seen_match_created_ms = COALESCE(
                (
                    SELECT games.created_ms
                    FROM games
                    WHERE games.game_id = crawl_seen.discovered_from_game_id
                ),
                latest_seen_match_created_ms,
                0
            )
            WHERE latest_seen_match_created_ms = 0
              AND discovered_from_game_id IS NOT NULL
            """
        )
        con.execute(
            """
            UPDATE crawl_seen
            SET last_crawled_match_created_ms = latest_seen_match_created_ms
            WHERE processed = 1 AND last_crawled_match_created_ms = 0
            """
        )
    con.commit()


def _migrate_legacy_crawl_players(con: sqlite3.Connection) -> int:
    """One-time migration from the older crawl_players frontier schema."""
    if not _table_exists(con, "crawl_players"):
        return 0
    if con.execute("SELECT COUNT(*) FROM crawl_seen").fetchone()[0] > 0:
        return 0
    if con.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0] > 0:
        return 0

    rows = con.execute(
        """
        SELECT puuid, source, priority, depth, discovered_from_game_id, status,
               first_seen_at, last_crawled_at, process_count, new_games_found
        FROM crawl_players
        ORDER BY priority ASC, depth ASC, first_seen_at ASC
        """
    ).fetchall()
    for (
        puuid,
        source,
        priority,
        depth,
        discovered_from_game_id,
        status,
        first_seen_at,
        last_crawled_at,
        process_count,
        new_games_found,
    ) in rows:
        discovered_ms = _lookup_game_created_ms(con, discovered_from_game_id)
        processed = 1 if status == "done" else 0
        queue_status = "done" if processed else "pending"
        last_crawled_ms = discovered_ms if processed else 0

        con.execute(
            """
            INSERT OR IGNORE INTO crawl_seen (
                puuid, source, priority, min_depth, discovered_from_game_id,
                first_seen_at, last_crawled_at, process_count, new_games_found,
                latest_seen_match_created_ms, last_crawled_match_created_ms, processed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                puuid,
                source,
                priority,
                depth,
                discovered_from_game_id,
                first_seen_at,
                last_crawled_at,
                process_count,
                new_games_found,
                discovered_ms,
                last_crawled_ms,
                processed,
            ),
        )
        con.execute(
            """
            INSERT OR IGNORE INTO crawl_queue (
                puuid, depth, source, priority, discovered_from_game_id,
                discovered_match_created_ms, enqueued_at, updated_at,
                claimed_by, claimed_at_ms, eligible_at_ms, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?)
            """,
            (
                puuid,
                depth,
                source,
                priority,
                discovered_from_game_id,
                discovered_ms,
                first_seen_at,
                first_seen_at,
                queue_status,
            ),
        )
    con.commit()
    return len(rows)


def _queue_id_from_meta(game: dict) -> int:
    queue_id = int(game.get("queueId", -1))
    if queue_id != -1:
        return queue_id
    return _MODE_TO_QUEUE.get(str(game.get("gameMode", "")), -1)


def _extract_target_game_ids(history: list[dict], target_queues: set[int]) -> list[str]:
    game_ids: list[str] = []
    for game in history:
        queue_id = _queue_id_from_meta(game)
        game_id = game.get("gameId")
        if queue_id in target_queues and game_id is not None:
            game_ids.append(str(game_id))
    return game_ids


def _extract_participant_puuids(detail: dict) -> list[str]:
    puuids: list[str] = []
    for ident in detail.get("participantIdentities") or []:
        player = ident.get("player") or {}
        puuid = player.get("puuid")
        if puuid:
            puuids.append(str(puuid))
    return puuids


def _claim_game_id(
    con: sqlite3.Connection,
    game_id: str,
    worker_id: str,
    claim_timeout_ms: int,
) -> bool:
    now_text = _utc_now()
    now_ms = _now_ms()
    cutoff_ms = now_ms - claim_timeout_ms

    con.execute("BEGIN IMMEDIATE")
    if con.execute("SELECT 1 FROM games WHERE game_id = ?", (game_id,)).fetchone():
        con.commit()
        return False

    con.execute(
        """
        UPDATE crawl_game_claims
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE status = 'in_progress'
          AND claimed_at_ms > 0
          AND claimed_at_ms < ?
        """,
        (now_text, cutoff_ms),
    )
    row = con.execute(
        """
        SELECT status, claimed_at_ms
        FROM crawl_game_claims
        WHERE game_id = ?
        """,
        (game_id,),
    ).fetchone()
    if row is None:
        con.execute(
            """
            INSERT INTO crawl_game_claims (
                game_id, claimed_by, claimed_at_ms, updated_at, status
            ) VALUES (?, ?, ?, ?, 'in_progress')
            """,
            (game_id, worker_id, now_ms, now_text),
        )
        con.commit()
        return True

    status, claimed_at_ms = row
    if str(status) == "done":
        con.commit()
        return False
    if str(status) == "pending" or int(claimed_at_ms) < cutoff_ms:
        con.execute(
            """
            UPDATE crawl_game_claims
            SET status = 'in_progress',
                claimed_by = ?,
                claimed_at_ms = ?,
                updated_at = ?
            WHERE game_id = ?
            """,
            (worker_id, now_ms, now_text, game_id),
        )
        con.commit()
        return True

    con.commit()
    return False


def _mark_game_done(con: sqlite3.Connection, game_id: str) -> None:
    now_text = _utc_now()
    con.execute(
        """
        INSERT INTO crawl_game_claims (
            game_id, claimed_by, claimed_at_ms, updated_at, status
        ) VALUES (?, NULL, 0, ?, 'done')
        ON CONFLICT(game_id) DO UPDATE SET
            status = 'done',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = excluded.updated_at
        """,
        (game_id, now_text),
    )
    con.commit()


def _release_game_claim(con: sqlite3.Connection, game_id: str) -> None:
    con.execute(
        """
        UPDATE crawl_game_claims
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE game_id = ?
        """,
        (_utc_now(), game_id),
    )
    con.commit()


def _insert_game(con: sqlite3.Connection, record: dict) -> bool:
    before = con.total_changes
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
            json.dumps(record.get("participants", []), separators=(",", ":")),
        ),
    )
    con.commit()
    return con.total_changes > before


def _backfill_participants_json(con: sqlite3.Connection, record: dict) -> bool:
    before = con.total_changes
    con.execute(
        """
        UPDATE games
        SET participants_json = ?
        WHERE game_id = ?
          AND (participants_json IS NULL OR participants_json = '')
        """,
        (
            json.dumps(record.get("participants", []), separators=(",", ":")),
            record["game_id"],
        ),
    )
    con.commit()
    return con.total_changes > before


def _load_existing_game_ids(con: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in con.execute("SELECT game_id FROM games").fetchall()}


def _pick_best_metadata(
    old_source: str,
    old_priority: int,
    old_depth: int,
    new_source: str,
    new_priority: int,
    new_depth: int,
) -> tuple[str, int, int]:
    best_source = old_source
    best_priority = old_priority
    best_depth = old_depth
    if new_priority < old_priority or (new_priority == old_priority and new_depth < old_depth):
        best_source = new_source
        best_priority = new_priority
    if new_depth < old_depth:
        best_depth = new_depth
    return best_source, best_priority, best_depth


def _upsert_queue_row(
    con: sqlite3.Connection,
    puuid: str,
    depth: int,
    source: str,
    priority: int,
    discovered_from_game_id: str | None,
    discovered_match_created_ms: int,
    requeue: bool,
    eligible_at_ms: int = 0,
) -> bool:
    """Insert or refresh a queue row. Returns True if it became pending now."""
    now = _utc_now()
    row = con.execute(
        """
        SELECT status, priority, depth, discovered_match_created_ms
        FROM crawl_queue
        WHERE puuid = ?
        """,
        (puuid,),
    ).fetchone()

    if row is None:
        con.execute(
            """
            INSERT INTO crawl_queue (
                puuid, depth, source, priority, discovered_from_game_id,
                discovered_match_created_ms, enqueued_at, updated_at,
                claimed_by, claimed_at_ms, eligible_at_ms, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, 'pending')
            """,
            (
                puuid,
                depth,
                source,
                priority,
                discovered_from_game_id,
                discovered_match_created_ms,
                now,
                now,
                eligible_at_ms,
            ),
        )
        con.commit()
        return True

    queue_status, queue_priority, queue_depth, queue_match_ms = row
    became_pending = False
    if str(queue_status) != "pending" and requeue:
        con.execute(
            """
            UPDATE crawl_queue
            SET depth = ?, source = ?, priority = ?, discovered_from_game_id = ?,
                discovered_match_created_ms = ?, updated_at = ?, eligible_at_ms = ?,
                claimed_by = NULL, claimed_at_ms = 0, status = 'pending'
            WHERE puuid = ?
            """,
            (
                depth,
                source,
                priority,
                discovered_from_game_id,
                discovered_match_created_ms,
                now,
                eligible_at_ms,
                puuid,
            ),
        )
        became_pending = True
    elif str(queue_status) in ("pending", "in_progress"):
        should_update = (
            discovered_match_created_ms > int(queue_match_ms)
            or priority < int(queue_priority)
            or depth < int(queue_depth)
        )
        if should_update:
            con.execute(
                f"""
                UPDATE crawl_queue
                SET depth = ?, source = ?, priority = ?, discovered_from_game_id = ?,
                    discovered_match_created_ms = ?, updated_at = ?
                    {", claimed_by = NULL, claimed_at_ms = 0" if str(queue_status) == "pending" else ""}
                WHERE puuid = ?
                """,
                (
                    depth,
                    source,
                    priority,
                    discovered_from_game_id,
                    discovered_match_created_ms,
                    now,
                    puuid,
                ),
            )
    con.commit()
    return became_pending


def _enqueue_player(
    con: sqlite3.Connection,
    puuid: str,
    depth: int,
    source: str,
    discovered_from_game_id: str | None = None,
    discovered_match_created_ms: int = 0,
    requeue_cooldown_ms: int = 0,
) -> str:
    """Add puuid to seen-set and queue when needed.

    Returns:
      - 'new' if the puuid was unseen and newly queued
      - 'requeued' if it had been processed before and a newer match reactivated it
      - 'updated' if metadata / priority changed but it was already queued or in progress
      - 'noop' otherwise
    """
    if not puuid:
        return "noop"

    now = _utc_now()
    priority = _SOURCE_PRIORITY.get(source, 99)
    row = con.execute(
        """
        SELECT source, priority, min_depth, discovered_from_game_id,
               latest_seen_match_created_ms, last_crawled_match_created_ms, processed
        FROM crawl_seen
        WHERE puuid = ?
        """,
        (puuid,),
    ).fetchone()

    if row is None:
        con.execute(
            """
            INSERT INTO crawl_seen (
                puuid, source, priority, min_depth, discovered_from_game_id,
                first_seen_at, latest_seen_match_created_ms,
                last_crawled_match_created_ms, processed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                puuid,
                source,
                priority,
                depth,
                discovered_from_game_id,
                now,
                discovered_match_created_ms,
            ),
        )
        con.commit()
        _upsert_queue_row(
            con,
            puuid,
            depth,
            source,
            priority,
            discovered_from_game_id,
            discovered_match_created_ms,
            requeue=True,
            eligible_at_ms=0,
        )
        return "new"

    (
        old_source,
        old_priority,
        old_depth,
        old_discovered_game_id,
        old_latest_match_ms,
        last_crawled_match_ms,
        processed,
    ) = row
    best_source, best_priority, best_depth = _pick_best_metadata(
        str(old_source),
        int(old_priority),
        int(old_depth),
        source,
        priority,
        depth,
    )
    latest_match_ms = max(int(old_latest_match_ms), int(discovered_match_created_ms))
    best_game_id = old_discovered_game_id
    if discovered_match_created_ms >= int(old_latest_match_ms) and discovered_from_game_id:
        best_game_id = discovered_from_game_id

    con.execute(
        """
        UPDATE crawl_seen
        SET source = ?, priority = ?, min_depth = ?, discovered_from_game_id = ?,
            latest_seen_match_created_ms = ?
        WHERE puuid = ?
        """,
        (
            best_source,
            best_priority,
            best_depth,
            best_game_id,
            latest_match_ms,
            puuid,
        ),
    )
    con.commit()

    should_requeue = int(processed) == 1 and int(discovered_match_created_ms) > int(last_crawled_match_ms)
    became_pending = _upsert_queue_row(
        con,
        puuid,
        best_depth,
        best_source,
        best_priority,
        best_game_id,
        latest_match_ms,
        requeue=should_requeue,
        eligible_at_ms=_now_ms() + requeue_cooldown_ms if should_requeue else 0,
    )
    if should_requeue and became_pending:
        con.execute(
            "UPDATE crawl_seen SET processed = 0 WHERE puuid = ?",
            (puuid,),
        )
        con.commit()
        return "requeued"
    if int(processed) == 0:
        return "updated"
    return "noop"


def _requeue_stale_claims(con: sqlite3.Connection, claim_timeout_ms: int) -> int:
    cutoff_ms = _now_ms() - claim_timeout_ms
    before = con.total_changes
    con.execute(
        """
        UPDATE crawl_queue
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE status = 'in_progress'
          AND claimed_at_ms > 0
          AND claimed_at_ms < ?
        """,
        (_utc_now(), cutoff_ms),
    )
    con.commit()
    return con.total_changes - before


def _claim_next_player(
    con: sqlite3.Connection,
    worker_id: str,
    claim_timeout_ms: int,
) -> tuple[str, int, str, int] | None:
    """Atomically claim one pending queue item for this worker."""
    now_text = _utc_now()
    now_ms = _now_ms()
    cutoff_ms = now_ms - claim_timeout_ms

    con.execute("BEGIN IMMEDIATE")
    con.execute(
        """
        UPDATE crawl_queue
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE status = 'in_progress'
          AND claimed_at_ms > 0
          AND claimed_at_ms < ?
        """,
        (now_text, cutoff_ms),
    )
    row = con.execute(
        """
        SELECT queue_idx, puuid, depth, source, discovered_match_created_ms
        FROM crawl_queue
        WHERE status = 'pending'
          AND eligible_at_ms <= ?
        ORDER BY discovered_match_created_ms DESC,
                 priority ASC,
                 depth ASC,
                 updated_at ASC,
                 queue_idx ASC
        LIMIT 1
        """
    , (now_ms,)).fetchone()
    if row is None:
        con.commit()
        return None

    queue_idx, puuid, depth, source, claimed_match_ms = row
    before = con.total_changes
    con.execute(
        """
        UPDATE crawl_queue
        SET status = 'in_progress',
            claimed_by = ?,
            claimed_at_ms = ?,
            updated_at = ?
        WHERE queue_idx = ?
          AND status = 'pending'
        """,
        (worker_id, now_ms, now_text, queue_idx),
    )
    claimed = con.total_changes > before
    con.commit()
    if not claimed:
        return None
    return str(puuid), int(depth), str(source), int(claimed_match_ms)


def _pending_player_count(con: sqlite3.Connection) -> int:
    return int(
        con.execute(
            "SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'"
        ).fetchone()[0]
    )


def _mark_player_done(
    con: sqlite3.Connection,
    puuid: str,
    new_games_found: int,
    claimed_match_created_ms: int,
    requeue_cooldown_ms: int,
) -> bool:
    """Finalize a claimed player.

    Returns True if the player was re-queued immediately due to a newer discovery
    arriving while this worker was processing it.
    """
    now = _utc_now()
    row = con.execute(
        """
        SELECT latest_seen_match_created_ms, last_crawled_match_created_ms
        FROM crawl_seen
        WHERE puuid = ?
        """,
        (puuid,),
    ).fetchone()
    latest_seen_match_ms = int(row[0]) if row else 0
    last_crawled_match_ms = int(row[1]) if row else 0
    needs_requeue = latest_seen_match_ms > int(claimed_match_created_ms)

    if needs_requeue:
        eligible_at_ms = _now_ms() + max(0, requeue_cooldown_ms)
        con.execute(
            """
            UPDATE crawl_seen
            SET processed = 0,
                last_crawled_at = ?,
                process_count = process_count + 1,
                new_games_found = new_games_found + ?,
                last_crawled_match_created_ms = ?
            WHERE puuid = ?
            """,
            (now, new_games_found, max(last_crawled_match_ms, int(claimed_match_created_ms)), puuid),
        )
        con.execute(
            """
            UPDATE crawl_queue
            SET status = 'pending',
                claimed_by = NULL,
                claimed_at_ms = 0,
                eligible_at_ms = ?,
                updated_at = ?
            WHERE puuid = ?
            """,
            (eligible_at_ms, now, puuid),
        )
        con.commit()
        return True

    con.execute(
        """
        UPDATE crawl_seen
        SET processed = 1,
            last_crawled_at = ?,
            process_count = process_count + 1,
            new_games_found = new_games_found + ?,
            last_crawled_match_created_ms = ?
        WHERE puuid = ?
        """,
        (now, new_games_found, max(last_crawled_match_ms, int(claimed_match_created_ms)), puuid),
    )
    con.execute(
        """
        UPDATE crawl_queue
        SET status = 'done',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE puuid = ?
        """,
        (now, puuid),
    )
    con.commit()
    return False


def _seed_ladder_neighbors(
    con: sqlite3.Connection,
    lcu: LCUClient,
    puuid: str,
    ladder_cap: int,
) -> int:
    added = 0
    for ladder in get_league_ladders(lcu, puuid):
        for division in ladder.get("divisions") or []:
            for standing in division.get("standings") or []:
                standing_puuid = standing.get("puuid")
                if not standing_puuid:
                    continue
                result = _enqueue_player(con, str(standing_puuid), depth=0, source="ladder")
                if result == "new":
                    added += 1
                if added >= ladder_cap:
                    return added
    return added


def _seed_apex_players(
    con: sqlite3.Connection,
    lcu: LCUClient,
    apex_queues: tuple[str, ...],
    apex_tiers: tuple[str, ...],
    apex_cap: int,
) -> int:
    added = 0
    for queue_type in apex_queues:
        for tier in apex_tiers:
            payload = get_apex_league(lcu, queue_type, tier)
            if not payload:
                continue
            for division in payload.get("divisions") or []:
                for standing in division.get("standings") or []:
                    standing_puuid = standing.get("puuid")
                    if not standing_puuid:
                        continue
                    result = _enqueue_player(con, str(standing_puuid), depth=0, source="apex")
                    if result == "new":
                        added += 1
                    if added >= apex_cap:
                        return added
    return added


def run_snowball(
    db_path: Path,
    target_games: int = 500,
    max_players: int = 250,
    history_window: int = 20,
    games_per_player: int | None = None,
    worker_id: str | None = None,
    claim_timeout_sec: int = 300,
    player_requeue_cooldown_sec: int = 45,
    target_queues: set[int] | None = None,
    include_self: bool = True,
    include_friends: bool = True,
    include_ladder: bool = False,
    ladder_cap: int = 100,
    include_apex: bool = False,
    apex_queues: tuple[str, ...] = ("RANKED_SOLO_5x5", "RANKED_FLEX_SR"),
    apex_tiers: tuple[str, ...] = ("CHALLENGER", "GRANDMASTER", "MASTER"),
    apex_cap: int = 300,
    max_depth: int = 3,
) -> CrawlStats:
    """Expand the LCU-visible player graph and save unseen target-queue matches."""
    if target_queues is None:
        target_queues = DEFAULT_QUEUES

    creds = get_credentials()
    if creds is None:
        raise RuntimeError("League client not found")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect_db(db_path)
    _ensure_schema(con)
    migrated = _migrate_legacy_crawl_players(con)
    claim_timeout_ms = max(1, claim_timeout_sec) * 1000
    player_requeue_cooldown_ms = max(0, player_requeue_cooldown_sec) * 1000
    worker_id = worker_id or f"pid-{os.getpid()}"

    existing_game_ids = _load_existing_game_ids(con)
    expanded_game_ids: set[str] = set()
    local_puuid_latest_ms: dict[str, int] = {}
    stats = CrawlStats()

    with LCUClient(creds) as lcu:
        me = get_current_summoner(lcu)
        if not me or not me.get("puuid"):
            raise RuntimeError("Could not resolve current summoner")

        my_puuid = str(me["puuid"])
        my_name = "[connected]"

        if include_self:
            result = _enqueue_player(con, my_puuid, depth=0, source="self")
            if result == "new":
                stats.seeded_players += 1
            elif result == "requeued":
                stats.requeued_players += 1

        if include_friends:
            for friend in get_friends(lcu):
                friend_puuid = friend.get("puuid")
                if not friend_puuid:
                    continue
                result = _enqueue_player(con, str(friend_puuid), depth=0, source="friend")
                if result == "new":
                    stats.seeded_players += 1
                elif result == "requeued":
                    stats.requeued_players += 1

        if include_ladder:
            stats.seeded_players += _seed_ladder_neighbors(con, lcu, my_puuid, ladder_cap)

        if include_apex:
            stats.seeded_players += _seed_apex_players(
                con, lcu, apex_queues=apex_queues, apex_tiers=apex_tiers, apex_cap=apex_cap
            )

        pending = _pending_player_count(con)
        print(
            f"[snowball] connected as {my_name}  pending={pending}  "
            f"newly_seeded={stats.seeded_players}  requeued={stats.requeued_players}  "
            f"existing_games={len(existing_game_ids)}  queues={sorted(target_queues)}  worker={worker_id}"
        )
        if migrated:
            print(f"[snowball] migrated legacy crawl_players -> seen+priority-queue  rows={migrated}")
        reclaimed = _requeue_stale_claims(con, claim_timeout_ms)
        if reclaimed:
            print(f"[snowball] reclaimed stale claims={reclaimed}")

        while stats.saved_games < target_games and stats.processed_players < max_players:
            next_player = _claim_next_player(con, worker_id=worker_id, claim_timeout_ms=claim_timeout_ms)
            if next_player is None:
                break

            puuid, depth, source, claimed_match_created_ms = next_player
            stats.processed_players += 1

            history = get_match_history(lcu, puuid, begin=0, end=history_window)
            game_ids = _extract_target_game_ids(history, target_queues)
            if games_per_player is not None and games_per_player > 0:
                game_ids = game_ids[:games_per_player]
            print(
                f"[snowball] player {stats.processed_players}/{max_players}  "
                f"depth={depth}  source={source:<6}  "
                f"target_games={len(game_ids)}  pending={max(0, _pending_player_count(con) - 1)}  "
                f"worker={worker_id}"
            )

            new_games_for_player = 0
            for game_id in game_ids:
                if stats.saved_games >= target_games:
                    break
                if game_id in expanded_game_ids:
                    continue
                if not _claim_game_id(con, game_id, worker_id=worker_id, claim_timeout_ms=claim_timeout_ms):
                    continue

                detail = get_game_detail(lcu, game_id)
                if not detail:
                    _release_game_claim(con, game_id)
                    stats.failed_games += 1
                    continue

                expanded_game_ids.add(game_id)
                stats.expanded_games += 1

                record = _parse_game_detail(detail, target_queues)
                if record is None:
                    _mark_game_done(con, game_id)
                    stats.filtered_games += 1
                    continue

                if record["game_id"] in existing_game_ids:
                    _backfill_participants_json(con, record)
                    _mark_game_done(con, record["game_id"])
                    stats.existing_games += 1
                else:
                    record["captured_at"] = _utc_now()
                    if _insert_game(con, record):
                        existing_game_ids.add(record["game_id"])
                        _mark_game_done(con, record["game_id"])
                        stats.saved_games += 1
                        new_games_for_player += 1
                        label = "Mayhem" if record["queue_id"] == 2400 else "ARAM"
                        print(
                            f"  [saved] {label:<6}  game_id={record['game_id']}  "
                            f"patch={record['patch']}  total_saved={stats.saved_games}  "
                            f"worker={worker_id}"
                        )
                    else:
                        _release_game_claim(con, record["game_id"])
                        stats.failed_games += 1
                        continue

                if depth >= max_depth:
                    continue

                for participant_puuid in _extract_participant_puuids(detail):
                    cached_match_ms = local_puuid_latest_ms.get(participant_puuid)
                    if cached_match_ms is not None and cached_match_ms >= int(record["created_ms"]):
                        continue
                    local_puuid_latest_ms[participant_puuid] = int(record["created_ms"])
                    result = _enqueue_player(
                        con,
                        participant_puuid,
                        depth + 1,
                        source="match",
                        discovered_from_game_id=record["game_id"],
                        discovered_match_created_ms=int(record["created_ms"]),
                        requeue_cooldown_ms=player_requeue_cooldown_ms,
                    )
                    if result == "new":
                        stats.seeded_players += 1
                    elif result == "requeued":
                        stats.requeued_players += 1

            requeued_on_finish = _mark_player_done(
                con,
                puuid,
                new_games_found=new_games_for_player,
                claimed_match_created_ms=claimed_match_created_ms,
                requeue_cooldown_ms=player_requeue_cooldown_ms,
            )
            if requeued_on_finish:
                stats.requeued_players += 1

    pending_after = _pending_player_count(con)
    con.close()
    print(
        f"[snowball] done  processed_players={stats.processed_players}  "
        f"expanded_games={stats.expanded_games}  saved_games={stats.saved_games}  "
        f"existing_games={stats.existing_games}  filtered={stats.filtered_games}  "
        f"failed={stats.failed_games}  requeued={stats.requeued_players}  "
        f"pending={pending_after}  worker={worker_id}"
    )
    return stats
