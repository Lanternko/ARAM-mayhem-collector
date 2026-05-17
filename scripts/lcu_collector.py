"""Local LCU collector: captures ARAM / Mayhem comps from the running League client.

Sub-commands
------------
collect  Run the background collector (blocks until Ctrl-C).
export   Convert the SQLite database to a Parquet file for training.
status   Show what's in the database.

Examples
--------
# Start collecting (run this before you play; leave it open)
python scripts/lcu_collector.py collect

# Snowball crawl recent visible match history across self / friends / strangers
python scripts/lcu_collector.py snowball --target-games 500 --max-players 200

# Store different clients into separate SQLite DBs, then merge by game_id
python scripts/lcu_collector.py snowball --db data/lcu/games_account_a.db
python scripts/lcu_collector.py merge-db --out-db data/lcu/games_merged.db data/lcu/games_account_a.db data/lcu/games_account_b.db

# Export everything to parquet (same schema as snowball output)
python scripts/lcu_collector.py export --out data/raw/lcu_games.parquet

# Export Mayhem only
python scripts/lcu_collector.py export --queue 2400 --out data/raw/mayhem_games.parquet

# See how many games you've captured
python scripts/lcu_collector.py status
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import webbrowser
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

import click
import httpx
try:
    import psutil
except Exception:  # pragma: no cover - optional dependency in local runtime
    psutil = None


class _LazyPolars:
    """Defer polars import until first attribute access.

    polars is a heavy native library (~10-20 MB DLLs); on Windows post-reboot,
    Defender's first-load DLL scan can stall the import for minutes and freeze
    every subcommand that imports this module — including `status`, `--help`,
    etc. that don't actually use polars at all.

    Type annotations like `pl.DataType` keep working because `from __future__
    import annotations` (above) makes all annotations lazy strings, so they're
    never evaluated at class/function definition time.
    """

    _module = None

    def __getattr__(self, name: str):
        if self._module is None:
            import polars as _polars  # type: ignore[import-not-found]
            object.__setattr__(self, "_module", _polars)
        return getattr(self._module, name)


pl = _LazyPolars()


DEFAULT_DB = Path("data/lcu/games.db")
_OPGG_TIER_LEADERBOARD_URL = "https://op.gg/zh-tw/lol/leaderboards/tier"
_OPGG_LEVEL_LEADERBOARD_URL = "https://op.gg/zh-tw/lol/leaderboards/level"
DEFAULT_OPGG_STATE = Path("data/seeds/opgg_tw_state.json")
DEFAULT_OPGG_HISTORY = Path("data/seeds/opgg_tw_history.jsonl")
DEFAULT_METRICS_HISTORY = Path("data/monitor/crawl_metrics.jsonl")
_GAMES_CREATE_SQL = """
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
_GAMES_INSERT_SQL = """
INSERT OR IGNORE INTO games (
    game_id, queue_id, patch, blue_champs, red_champs,
    blue_wins, duration_sec, created_ms, captured_at, participants_json
) VALUES (?,?,?,?,?,?,?,?,?,?)
"""


@click.group()
def cli() -> None:
    """LCU local game-data collector for ARAM / Mayhem."""


def _write_csv_rows(path: Path, rows: list[dict], schema: dict[str, pl.DataType], sort_by: list[str]) -> None:
    if rows:
        pl.DataFrame(rows).sort(sort_by, descending=[True] * len(sort_by)).write_csv(path)
    else:
        pl.DataFrame(schema=schema).write_csv(path)


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(con: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_games_schema(con: sqlite3.Connection) -> None:
    con.execute(_GAMES_CREATE_SQL)
    columns = _table_columns(con, "games")
    if "participants_json" not in columns:
        con.execute("ALTER TABLE games ADD COLUMN participants_json TEXT")
    con.commit()


def _iter_game_rows(con: sqlite3.Connection, chunk_size: int = 2000):
    columns = _table_columns(con, "games")
    has_participants = "participants_json" in columns
    cursor = con.execute(
        """
        SELECT game_id, queue_id, patch, blue_champs, red_champs,
               blue_wins, duration_sec, created_ms, captured_at, participants_json
        FROM games
        ORDER BY created_ms, game_id
        """
        if has_participants
        else
        """
        SELECT game_id, queue_id, patch, blue_champs, red_champs,
               blue_wins, duration_sec, created_ms, captured_at, NULL as participants_json
        FROM games
        ORDER BY created_ms, game_id
        """
    )
    while True:
        rows = cursor.fetchmany(chunk_size)
        if not rows:
            break
        yield rows


def _normalize_opgg_profile_to_riot_id(url_or_slug: str) -> str | None:
    value = url_or_slug.strip().strip("/")
    if not value:
        return None

    if "/lol/summoners/" in value:
        value = value.rsplit("/", 1)[-1]

    value = unquote(value)
    if "#" in value:
        game_name, tag_line = value.split("#", 1)
        game_name = game_name.strip()
        tag_line = tag_line.strip()
        if game_name and tag_line:
            return f"{game_name}#{tag_line}"
        return None

    if "-" not in value:
        return None
    game_name, tag_line = value.rsplit("-", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    if not game_name or not tag_line:
        return None
    return f"{game_name}#{tag_line}"


def _fetch_opgg_leaderboard_riot_ids(
    *,
    region: str,
    tier: str,
    pages: int,
    topn: int,
) -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0"}
    riot_ids: list[str] = []
    seen: set[str] = set()

    with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
        for page in range(1, max(1, pages) + 1):
            matches = _fetch_opgg_leaderboard_page_slugs(
                client=client,
                region=region,
                tier=tier,
                page=page,
            )

            if not matches:
                break

            added_this_page = 0
            for match in sorted(matches):
                riot_id = _normalize_opgg_profile_to_riot_id(match)
                if not riot_id or riot_id in seen:
                    continue
                seen.add(riot_id)
                riot_ids.append(riot_id)
                added_this_page += 1
                if topn > 0 and len(riot_ids) >= topn:
                    return riot_ids

            if added_this_page == 0:
                break

    return riot_ids


def _fetch_opgg_leaderboard_page_slugs(
    *,
    client: httpx.Client,
    region: str,
    source: str = "tier",
    tier: str | None = None,
    page: int,
) -> set[str]:
    normalized_source = source.strip().lower()
    if normalized_source == "tier":
        if not tier:
            raise ValueError("tier leaderboard source requires a tier")
        params = {"tier": tier.lower(), "region": region.lower(), "page": page}
        url = _OPGG_TIER_LEADERBOARD_URL
    elif normalized_source == "level":
        params = {"region": region.lower(), "page": page}
        url = _OPGG_LEVEL_LEADERBOARD_URL
    else:
        raise ValueError(f"unsupported OPGG leaderboard source: {source}")

    resp = client.get(url, params=params)
    resp.raise_for_status()
    html = resp.content.decode("utf-8", errors="replace")
    patterns = [
        rf'/zh-tw/lol/summoners/{re.escape(region.lower())}/([^"<\\]+)',
        rf'/lol/summoners/{re.escape(region.lower())}/([^"<\\]+)',
    ]
    matches: set[str] = set()
    for pattern in patterns:
        matches.update(re.findall(pattern, html))
    return matches


def _write_seed_file(path: Path, riot_ids: list[str], append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8", newline="\n") as fh:
        if append and path.stat().st_size > 0:
            fh.write("\n")
        for riot_id in riot_ids:
            fh.write(f"{riot_id}\n")


def _console_safe(text: str) -> str:
    return text.encode("cp950", errors="backslashreplace").decode("cp950")


def _normalize_opgg_sources(sources: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for source in sources:
        value = str(source).strip().lower()
        if not value:
            continue
        if value not in {"tier", "level"}:
            raise click.ClickException(f"unsupported OPGG source: {source}")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized or ["tier"])


def _default_opgg_state(
    region: str,
    tiers: tuple[str, ...],
    start_page: int,
    sources: tuple[str, ...] = ("tier",),
) -> dict:
    normalized_sources = _normalize_opgg_sources(sources)
    normalized_tiers = [str(t).strip().lower() for t in tiers if str(t).strip()]
    state = {
        "region": str(region).strip().lower(),
    }
    if "tier" in normalized_sources:
        state["tiers"] = {
            tier: {"next_page": max(1, int(start_page)), "exhausted": False}
            for tier in normalized_tiers
        }
    if "level" in normalized_sources:
        state["level"] = {"next_page": max(1, int(start_page)), "exhausted": False}
    return state


def _load_opgg_state(
    path: Path,
    region: str,
    tiers: tuple[str, ...],
    start_page: int,
    sources: tuple[str, ...] = ("tier",),
) -> dict:
    if not path.exists():
        return _default_opgg_state(region=region, tiers=tiers, start_page=start_page, sources=sources)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_opgg_state(region=region, tiers=tiers, start_page=start_page, sources=sources)

    state = _default_opgg_state(region=region, tiers=tiers, start_page=start_page, sources=sources)
    if not isinstance(payload, dict):
        return state

    state["region"] = str(payload.get("region") or state["region"]).strip().lower()
    tiers_payload = payload.get("tiers")
    if "tiers" in state and isinstance(tiers_payload, dict):
        for tier in list(state["tiers"].keys()):
            raw = tiers_payload.get(tier)
            if not isinstance(raw, dict):
                continue
            next_page = raw.get("next_page", state["tiers"][tier]["next_page"])
            exhausted = raw.get("exhausted", state["tiers"][tier]["exhausted"])
            try:
                next_page = max(1, int(next_page))
            except Exception:
                next_page = state["tiers"][tier]["next_page"]
            state["tiers"][tier] = {
                "next_page": next_page,
                "exhausted": bool(exhausted),
            }
    level_payload = payload.get("level")
    if "level" in state and isinstance(level_payload, dict):
        next_page = level_payload.get("next_page", state["level"]["next_page"])
        exhausted = level_payload.get("exhausted", state["level"]["exhausted"])
        try:
            next_page = max(1, int(next_page))
        except Exception:
            next_page = state["level"]["next_page"]
        state["level"] = {"next_page": next_page, "exhausted": bool(exhausted)}
    return state


def _save_opgg_state(path: Path, state: dict) -> None:
    payload = dict(state)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_opgg_history(path: Path, event: dict) -> None:
    payload = dict(event)
    payload["logged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _walk_opgg_pages(
    *,
    client: httpx.Client,
    region: str,
    source: str,
    bucket: str,
    cursor_state: dict,
    start_page: int,
    page_window: int,
    riot_ids: list[str],
    seen: set[str],
    page_hits: list[tuple[str, str, int, int]],
    topn_total: int,
) -> tuple[int, bool]:
    page_start = max(1, int(cursor_state.get("next_page", max(1, start_page))))
    page_stop = page_start + max(1, page_window)
    total_added = 0
    exhausted = False

    for page in range(page_start, page_stop):
        slugs = _fetch_opgg_leaderboard_page_slugs(
            client=client,
            region=region,
            source=source,
            tier=bucket if source == "tier" else None,
            page=page,
        )
        if not slugs:
            exhausted = True
            break

        added_this_page = 0
        for slug in sorted(slugs):
            riot_id = _normalize_opgg_profile_to_riot_id(slug)
            if not riot_id or riot_id in seen:
                continue
            seen.add(riot_id)
            riot_ids.append(riot_id)
            added_this_page += 1
            total_added += 1
            if topn_total > 0 and len(riot_ids) >= topn_total:
                break

        page_hits.append((source, bucket, page, added_this_page))
        if added_this_page == 0:
            exhausted = True
            break
        cursor_state["next_page"] = page + 1
        if topn_total > 0 and len(riot_ids) >= topn_total:
            break

    return total_added, exhausted


def _load_jsonl_tail(path: Path, limit: int = 5) -> list[dict]:
    if not path.exists():
        return []
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    payloads: list[dict] = []
    for raw in lines[-limit:]:
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads


def _append_metrics_history(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def _find_active_snowball_workers() -> list[dict]:
    workers: list[dict] = []
    if psutil is not None:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                name = str(proc.info.get("name") or "")
                if "python" not in name.lower():
                    continue
                cmdline = [str(part) for part in (proc.info.get("cmdline") or [])]
                if not cmdline:
                    continue
                joined = " ".join(cmdline)
                lower = joined.lower()
                if "lcu_collector.py" not in lower or "snowball" not in lower:
                    continue
                workers.append(
                    {
                        "pid": int(proc.info["pid"]),
                        "create_time": float(proc.info.get("create_time") or 0.0),
                        "cmdline": joined,
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    else:
        probe = """
$rows = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -like '*lcu_collector.py*' -and $_.CommandLine -like '*snowball*'
} | Select-Object ProcessId, CommandLine, CreationDate
$rows | ConvertTo-Json -Compress
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", probe],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            raw = (result.stdout or "").strip()
            if raw:
                payload = json.loads(raw)
                rows = payload if isinstance(payload, list) else [payload]
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    workers.append(
                        {
                            "pid": int(row.get("ProcessId") or 0),
                            "create_time": 0.0,
                            "cmdline": str(row.get("CommandLine") or ""),
                        }
                    )
        except Exception:
            workers = []
    workers.sort(key=lambda row: row["pid"])
    return workers


def _stop_active_snowball_workers(timeout_sec: float = 10.0) -> int:
    workers = _find_active_snowball_workers()
    if not workers:
        return 0

    pids = [int(worker["pid"]) for worker in workers if int(worker.get("pid") or 0) > 0]
    stopped = 0
    if psutil is not None:
        procs = []
        for pid in pids:
            try:
                proc = psutil.Process(pid)
                proc.terminate()
                procs.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        gone, alive = psutil.wait_procs(procs, timeout=timeout_sec)
        stopped += len(gone)
        for proc in alive:
            try:
                proc.kill()
                stopped += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        return stopped

    if pids:
        pid_list = ",".join(str(pid) for pid in pids)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"{pid_list}.Split(',') | ForEach-Object {{ Stop-Process -Id ([int]$_) -Force }}",
            ],
            check=False,
            timeout=max(5.0, timeout_sec),
        )
        stopped = len(pids)
    return stopped


def _collect_status_snapshot(
    db: Path,
    *,
    seed_state_file: Path = DEFAULT_OPGG_STATE,
    seed_history_file: Path = DEFAULT_OPGG_HISTORY,
) -> dict:
    snapshot = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "captured_at_unix": time.time(),
        "db": str(db),
        "db_exists": db.exists(),
        "total": 0,
        "queues": {},
        "mayhem": {
            "total": 0,
            "latest_patch": None,
            "latest_patch_games": 0,
            "latest_game_created_ms": None,
            "latest_game_captured_at": None,
        },
        "recent": [],
        "crawl_frontier": {},
        "crawl_sources": {},
        "active_workers": [],
        "seed_state_file": str(seed_state_file),
        "seed_history_file": str(seed_history_file),
        "seed_state": None,
        "latest_seed_refresh": None,
    }

    if not db.exists():
        snapshot["active_workers"] = _find_active_snowball_workers()
        if seed_state_file.exists():
            try:
                snapshot["seed_state"] = json.loads(seed_state_file.read_text(encoding="utf-8"))
            except Exception:
                snapshot["seed_state"] = None
        history_tail = _load_jsonl_tail(seed_history_file, limit=1)
        if history_tail:
            snapshot["latest_seed_refresh"] = history_tail[-1]
        return snapshot

    con = sqlite3.connect(str(db))
    try:
        snapshot["total"] = int(con.execute("SELECT COUNT(*) FROM games").fetchone()[0])
        queue_rows = con.execute(
            "SELECT queue_id, COUNT(*), AVG(blue_wins), MIN(patch), MAX(patch), MAX(created_ms) "
            "FROM games GROUP BY queue_id"
        ).fetchall()
        snapshot["queues"] = {
            str(queue_id): {
                "games": int(count),
                "blue_wr": float(wr or 0.0),
                "min_patch": min_patch,
                "max_patch": max_patch,
                "latest_created_ms": int(latest_created_ms or 0),
            }
            for queue_id, count, wr, min_patch, max_patch, latest_created_ms in queue_rows
        }
        recent_rows = con.execute(
            "SELECT game_id, queue_id, patch, blue_wins, duration_sec, created_ms, captured_at "
            "FROM games ORDER BY created_ms DESC LIMIT 5"
        ).fetchall()
        snapshot["recent"] = [
            {
                "game_id": str(game_id),
                "queue_id": int(queue_id),
                "patch": patch,
                "blue_wins": bool(blue_wins),
                "duration_sec": int(duration_sec),
                "created_ms": int(created_ms),
                "captured_at": captured_at,
            }
            for game_id, queue_id, patch, blue_wins, duration_sec, created_ms, captured_at in recent_rows
        ]
        latest_mayhem = con.execute(
            """
            SELECT patch, COUNT(*), MAX(created_ms), MAX(captured_at)
            FROM games
            WHERE queue_id = 2400
            GROUP BY patch
            ORDER BY MAX(created_ms) DESC
            LIMIT 1
            """
        ).fetchone()
        mayhem_total = int(snapshot["queues"].get("2400", {}).get("games", 0))
        if latest_mayhem:
            latest_patch, latest_patch_games, latest_created_ms, latest_captured_at = latest_mayhem
            snapshot["mayhem"] = {
                "total": mayhem_total,
                "latest_patch": latest_patch,
                "latest_patch_games": int(latest_patch_games or 0),
                "latest_game_created_ms": int(latest_created_ms or 0),
                "latest_game_captured_at": latest_captured_at,
            }
        else:
            snapshot["mayhem"]["total"] = mayhem_total

        has_crawl_queue = _table_exists(con, "crawl_queue")
        has_crawl_seen = _table_exists(con, "crawl_seen")
        has_crawl_players = _table_exists(con, "crawl_players")
        if has_crawl_queue and has_crawl_seen:
            snapshot["crawl_frontier"] = {
                str(status): int(count)
                for status, count in con.execute(
                    "SELECT status, COUNT(*) FROM crawl_queue GROUP BY status"
                ).fetchall()
            }
            snapshot["crawl_sources"] = {
                str(source): int(count)
                for source, count in con.execute(
                    "SELECT source, COUNT(*) FROM crawl_seen GROUP BY source"
                ).fetchall()
            }
        elif has_crawl_players:
            snapshot["crawl_frontier"] = {
                str(status): int(count)
                for status, count in con.execute(
                    "SELECT status, COUNT(*) FROM crawl_players GROUP BY status"
                ).fetchall()
            }
            snapshot["crawl_sources"] = {
                str(source): int(count)
                for source, count in con.execute(
                    "SELECT source, COUNT(*) FROM crawl_players GROUP BY source"
                ).fetchall()
            }
    finally:
        con.close()

    snapshot["active_workers"] = _find_active_snowball_workers()
    if seed_state_file.exists():
        try:
            snapshot["seed_state"] = json.loads(seed_state_file.read_text(encoding="utf-8"))
        except Exception:
            snapshot["seed_state"] = None
    history_tail = _load_jsonl_tail(seed_history_file, limit=1)
    if history_tail:
        snapshot["latest_seed_refresh"] = history_tail[-1]
    return snapshot


def _format_rate(delta: int, elapsed_sec: float) -> str:
    if elapsed_sec <= 0:
        return "n/a"
    per_hour = delta * 3600.0 / elapsed_sec
    per_min = delta * 60.0 / elapsed_sec
    return f"{per_hour:.1f}/h ({per_min:.2f}/min)"


def _merge_games_from_source(
    dst_con: sqlite3.Connection,
    src_db: Path,
    *,
    chunk_size: int = 2000,
) -> dict[str, int]:
    if not src_db.exists():
        raise click.ClickException(f"source database not found: {src_db}")

    src_con = sqlite3.connect(str(src_db), timeout=30.0)
    try:
        if not _table_exists(src_con, "games"):
            raise click.ClickException(f"source database has no games table: {src_db}")

        rows_read = 0
        inserted = 0
        participants_backfilled = 0
        for rows in _iter_game_rows(src_con, chunk_size=chunk_size):
            rows_read += len(rows)

            before_insert = dst_con.total_changes
            dst_con.executemany(_GAMES_INSERT_SQL, rows)
            inserted += dst_con.total_changes - before_insert

            backfill_rows = [(row[9], row[0]) for row in rows if row[9]]
            if backfill_rows:
                before_backfill = dst_con.total_changes
                dst_con.executemany(
                    """
                    UPDATE games
                    SET participants_json = ?
                    WHERE game_id = ?
                      AND COALESCE(participants_json, '') = ''
                    """,
                    backfill_rows,
                )
                participants_backfilled += dst_con.total_changes - before_backfill

        dst_con.commit()
        return {
            "rows_read": rows_read,
            "inserted": inserted,
            "participants_backfilled": participants_backfilled,
        }
    finally:
        src_con.close()


def _load_game_rows(db: Path) -> tuple[list[tuple], bool]:
    con = sqlite3.connect(str(db))
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(games)").fetchall()}
    has_participants = "participants_json" in columns
    rows = con.execute(
        """
        SELECT game_id, queue_id, patch, blue_champs, red_champs, blue_wins, participants_json
        FROM games
        ORDER BY created_ms
        """
        if has_participants
        else
        """
        SELECT game_id, queue_id, patch, blue_champs, red_champs, blue_wins, NULL as participants_json
        FROM games
        ORDER BY created_ms
        """
    ).fetchall()
    con.close()
    return rows, has_participants


def _aggregate_stats(
    rows: list[tuple],
    queue: tuple[int, ...],
    patch_prefix: tuple[str, ...],
) -> tuple[int, int, list[dict], list[dict], list[dict]]:
    queue_filter = set(queue)
    hero_games: Counter[int] = Counter()
    hero_wins: Counter[int] = Counter()
    augment_games: Counter[int] = Counter()
    augment_wins: Counter[int] = Counter()
    hero_augment_games: Counter[tuple[int, int]] = Counter()
    hero_augment_wins: Counter[tuple[int, int]] = Counter()

    kept_games = 0
    participant_games = 0
    for _, queue_id, patch, blue_json, red_json, blue_wins, participants_json in rows:
        if queue_filter and queue_id not in queue_filter:
            continue
        if patch_prefix and not any(str(patch).startswith(prefix) for prefix in patch_prefix):
            continue

        kept_games += 1
        blue_ids = json.loads(blue_json)
        red_ids = json.loads(red_json)
        blue_win_int = int(bool(blue_wins))

        for champion_id in blue_ids:
            hero_games[int(champion_id)] += 1
            hero_wins[int(champion_id)] += blue_win_int
        for champion_id in red_ids:
            hero_games[int(champion_id)] += 1
            hero_wins[int(champion_id)] += 1 - blue_win_int

        payload = json.loads(participants_json or "[]")
        if not payload:
            continue
        participant_games += 1
        for participant in payload:
            champion_id = int(participant.get("championId", 0) or 0)
            team_id = int(participant.get("teamId", 0) or 0)
            if champion_id <= 0 or team_id not in (100, 200):
                continue
            player_win = blue_win_int if team_id == 100 else (1 - blue_win_int)
            for augment_id in participant.get("augments") or []:
                augment_id = int(augment_id)
                if augment_id <= 0:
                    continue
                augment_games[augment_id] += 1
                augment_wins[augment_id] += player_win
                hero_augment_games[(champion_id, augment_id)] += 1
                hero_augment_wins[(champion_id, augment_id)] += player_win

    total_player_games = sum(hero_games.values())
    total_player_wins = sum(hero_wins.values())
    global_wr = (total_player_wins / total_player_games) if total_player_games > 0 else 0.5
    prior_strength = 50.0

    hero_rows_raw = []
    for champion_id, games_played in hero_games.items():
        wins = hero_wins[champion_id]
        hero_rows_raw.append(
            {
                "champion_id": champion_id,
                "games": games_played,
                "wins": wins,
            }
        )
    hero_rows = _decorate_rate_rows(
        hero_rows_raw, key_field="champion_id", global_wr=global_wr, prior_strength=prior_strength
    )

    augment_rows_raw = []
    for augment_id, games_played in augment_games.items():
        wins = augment_wins[augment_id]
        augment_rows_raw.append(
            {
                "augment_id": augment_id,
                "games": games_played,
                "wins": wins,
            }
        )
    augment_rows = _decorate_rate_rows(
        augment_rows_raw, key_field="augment_id", global_wr=global_wr, prior_strength=prior_strength
    )

    hero_augment_rows_raw = []
    for (champion_id, augment_id), games_played in hero_augment_games.items():
        wins = hero_augment_wins[(champion_id, augment_id)]
        hero_augment_rows_raw.append(
            {
                "champion_id": champion_id,
                "augment_id": augment_id,
                "games": games_played,
                "wins": wins,
            }
        )
    hero_augment_rows = []
    for row in hero_augment_rows_raw:
        wins = int(row["wins"])
        games = int(row["games"])
        hero_augment_rows.append(
            {
                "champion_id": row["champion_id"],
                "augment_id": row["augment_id"],
                "games": games,
                "wins": wins,
                "win_rate": wins / games if games > 0 else global_wr,
                "bayes_win_rate": _bayes_win_rate(wins, games, global_wr, prior_strength),
                "wilson_lb": _wilson_lower_bound(wins, games),
            }
        )

    return kept_games, participant_games, hero_rows, augment_rows, hero_augment_rows


def _load_champion_name_map() -> dict[int, str]:
    try:
        from aram_nn.lcu.client import LCUClient, get_champion_summary
        from aram_nn.lcu.process import get_credentials
    except Exception:
        return {}

    creds = get_credentials()
    if creds is None:
        return {}

    try:
        with LCUClient(creds) as lcu:
            summary = get_champion_summary(lcu)
    except Exception:
        return {}

    mapping: dict[int, str] = {}
    for row in summary:
        champion_id = row.get("id")
        name = row.get("name") or row.get("alias")
        if champion_id is None or not name:
            continue
        mapping[int(champion_id)] = str(name)
    return mapping


def _load_augment_name_map() -> dict[int, str]:
    try:
        from aram_nn.lcu.client import LCUClient
        from aram_nn.lcu.process import get_credentials
    except Exception:
        return {}

    creds = get_credentials()
    if creds is None:
        return {}

    try:
        with LCUClient(creds) as lcu:
            data = lcu.get("/lol-game-data/assets/v1/cherry-augments.json") or []
    except Exception:
        return {}

    mapping: dict[int, str] = {}
    for row in data:
        augment_id = row.get("id")
        name = row.get("nameTRA") or row.get("simpleNameTRA") or row.get("name")
        if augment_id is None or not name:
            continue
        mapping[int(augment_id)] = str(name)
    return mapping


def _bayes_win_rate(wins: int, games: int, global_wr: float, prior_strength: float) -> float:
    if games <= 0:
        return global_wr
    return (wins + prior_strength * global_wr) / (games + prior_strength)


def _wilson_lower_bound(wins: int, games: int, z: float = 1.96) -> float:
    if games <= 0:
        return 0.0
    phat = wins / games
    denom = 1.0 + (z * z) / games
    centre = phat + (z * z) / (2.0 * games)
    margin = z * math.sqrt((phat * (1.0 - phat) + (z * z) / (4.0 * games)) / games)
    return (centre - margin) / denom


def _decorate_rate_rows(
    rows: list[dict],
    *,
    key_field: str,
    global_wr: float,
    prior_strength: float,
) -> list[dict]:
    decorated: list[dict] = []
    for row in rows:
        wins = int(row["wins"])
        games = int(row["games"])
        decorated.append(
            {
                key_field: row[key_field],
                "games": games,
                "wins": wins,
                "win_rate": wins / games if games > 0 else global_wr,
                "bayes_win_rate": _bayes_win_rate(wins, games, global_wr, prior_strength),
                "wilson_lb": _wilson_lower_bound(wins, games),
            }
        )
    return decorated


def _build_snowball_subprocess_args(
    *,
    db: Path,
    target_games: int,
    max_players: int,
    history_window: int,
    games_per_player: int,
    worker_id: str,
    claim_timeout_sec: int,
    player_requeue_cooldown_sec: int,
    queue: tuple[int, ...],
    seed_self: bool,
    seed_friends: bool,
    seed_ladder: bool,
    ladder_cap: int,
    seed_apex: bool,
    apex_queue: tuple[str, ...],
    apex_tier: tuple[str, ...],
    apex_cap: int,
    seed_riot_tier: bool,
    riot_region: str,
    riot_queue: tuple[str, ...],
    riot_tier: tuple[str, ...],
    riot_division: tuple[str, ...],
    riot_page_limit: int,
    riot_cap: int,
    seed_riot_ids: tuple[str, ...],
    seed_riot_id_files: tuple[Path, ...],
    manual_seed_pending_cap: int,
    max_depth: int,
) -> list[str]:
    args = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "snowball",
        "--db",
        str(db),
        "--target-games",
        str(target_games),
        "--max-players",
        str(max_players),
        "--history-window",
        str(history_window),
        "--games-per-player",
        str(games_per_player),
        "--worker-id",
        worker_id,
        "--claim-timeout-sec",
        str(claim_timeout_sec),
        "--player-requeue-cooldown-sec",
        str(player_requeue_cooldown_sec),
        "--ladder-cap",
        str(ladder_cap),
        "--apex-cap",
        str(apex_cap),
        "--riot-region",
        str(riot_region),
        "--riot-page-limit",
        str(riot_page_limit),
        "--riot-cap",
        str(riot_cap),
        "--manual-seed-pending-cap",
        str(manual_seed_pending_cap),
        "--max-depth",
        str(max_depth),
    ]

    for qid in queue:
        args.extend(["--queue", str(qid)])
    for queue_type in apex_queue:
        args.extend(["--apex-queue", str(queue_type)])
    for tier in apex_tier:
        args.extend(["--apex-tier", str(tier)])
    for queue_type in riot_queue:
        args.extend(["--riot-queue", str(queue_type)])
    for tier in riot_tier:
        args.extend(["--riot-tier", str(tier)])
    for division in riot_division:
        args.extend(["--riot-division", str(division)])
    for riot_id in seed_riot_ids:
        args.extend(["--seed-riot-id", str(riot_id)])
    for riot_id_file in seed_riot_id_files:
        args.extend(["--seed-riot-id-file", str(riot_id_file)])

    args.append("--seed-self" if seed_self else "--no-seed-self")
    args.append("--seed-friends" if seed_friends else "--no-seed-friends")
    args.append("--seed-ladder" if seed_ladder else "--no-seed-ladder")
    args.append("--seed-apex" if seed_apex else "--no-seed-apex")
    args.append("--seed-riot-tier" if seed_riot_tier else "--no-seed-riot-tier")
    return args


# ------------------------------------------------------------------ collect --

@cli.command()
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True, help="SQLite database path")
@click.option("--interval", default=30, show_default=True, type=int,
              help="Poll interval in seconds")
@click.option("--queue", multiple=True, type=int, default=(450, 2400),
              help="Queue IDs to capture (repeatable).  Default: 450 and 2400.")
def collect(db: Path, interval: int, queue: tuple[int, ...]) -> None:
    """Run the collector — blocks until Ctrl-C.

    Polls the League Client every INTERVAL seconds and saves any new ARAM or
    Mayhem games to the SQLite database.  Safe to restart; already-saved games
    are skipped automatically.
    """
    try:
        from aram_nn.lcu.poller import run_collector
    except ImportError as exc:
        click.echo(f"[error] import failed: {exc}\n  Run: pip install -e .", err=True)
        sys.exit(1)
    run_collector(db, poll_interval=interval, target_queues=set(queue))


# ---------------------------------------------------------------- snowball --

@cli.command()
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True, help="SQLite database path")
@click.option("--target-games", default=500, show_default=True, type=int,
              help="Stop after saving this many new games")
@click.option("--max-players", default=250, show_default=True, type=int,
              help="Stop after processing this many distinct player nodes")
@click.option("--history-window", default=20, show_default=True, type=int,
              help="How many recent games to inspect per player")
@click.option("--games-per-player", default=8, show_default=True, type=int,
              help="Cap how many target-queue games to expand per player for wider diffusion")
@click.option("--worker-id", default="", show_default=False,
              help="Optional logical worker id for parallel crawlers (default: pid-<process>)")
@click.option("--claim-timeout-sec", default=300, show_default=True, type=int,
              help="Reclaim an in-progress queue item if a worker disappears for this long")
@click.option("--player-requeue-cooldown-sec", default=45, show_default=True, type=int,
              help="Cooldown before a newer rediscovery can requeue the same processed player")
@click.option("--queue", multiple=True, type=int, default=(450, 2400),
              help="Queue IDs to capture (repeatable).  Default: 450 and 2400.")
@click.option("--seed-self/--no-seed-self", default=True, show_default=True,
              help="Seed the crawl with the current summoner")
@click.option("--seed-friends/--no-seed-friends", default=True, show_default=True,
              help="Seed the crawl with friend-list puuids")
@click.option("--seed-ladder/--no-seed-ladder", default=False, show_default=True,
              help="Seed the crawl with current ranked ladder neighbors")
@click.option("--ladder-cap", default=100, show_default=True, type=int,
              help="Maximum ladder players to enqueue when --seed-ladder is on")
@click.option("--seed-apex/--no-seed-apex", default=False, show_default=True,
              help="Seed the crawl with TW apex ladders (Challenger / GM / Master)")
@click.option("--apex-queue", multiple=True, default=("RANKED_SOLO_5x5", "RANKED_FLEX_SR"),
              show_default=True, help="Apex ladder queue types to seed from")
@click.option("--apex-tier", multiple=True, default=("CHALLENGER", "GRANDMASTER", "MASTER"),
              show_default=True, help="Apex ladder tiers to seed from")
@click.option("--apex-cap", default=300, show_default=True, type=int,
              help="Maximum apex-ladder players to enqueue when --seed-apex is on")
@click.option("--seed-riot-tier/--no-seed-riot-tier", default=False, show_default=True,
              help="Seed the crawl with Riot league-exp tiers such as GOLD / PLATINUM / DIAMOND")
@click.option("--riot-region", default="tw", show_default=True,
              help="Riot API region for league-exp tier seeds")
@click.option("--riot-queue", multiple=True, default=("RANKED_SOLO_5x5",),
              show_default=True, help="Riot league-exp queue types to seed from")
@click.option("--riot-tier", multiple=True, default=("GOLD",),
              show_default=True, help="Riot league-exp tiers to seed from")
@click.option("--riot-division", multiple=True, default=("I", "II", "III", "IV"),
              show_default=True, help="Riot league-exp divisions to seed from")
@click.option("--riot-page-limit", default=2, show_default=True, type=int,
              help="Maximum Riot league-exp pages to scan per tier/division")
@click.option("--riot-cap", default=400, show_default=True, type=int,
              help="Maximum Riot league-exp players to enqueue when --seed-riot-tier is on")
@click.option("--seed-riot-id", "seed_riot_ids", multiple=True, default=(),
              show_default=False, help="Manual Riot IDs to enqueue, e.g. Name#TAG")
@click.option("--seed-riot-id-file", "seed_riot_id_files", multiple=True,
              type=click.Path(path_type=Path, exists=True, dir_okay=False),
              show_default=False, help="Text file with one Riot ID or OPGG summoner URL per line")
@click.option("--manual-seed-pending-cap", default=40, show_default=True, type=int,
              help="Maximum pending/in-progress manual_riot_id queue items to keep open at once (0 = unlimited)")
@click.option("--max-depth", default=3, show_default=True, type=int,
              help="Maximum BFS depth for discovered participant puuids")
def snowball(
    db: Path,
    target_games: int,
    max_players: int,
    history_window: int,
    games_per_player: int,
    worker_id: str,
    claim_timeout_sec: int,
    player_requeue_cooldown_sec: int,
    queue: tuple[int, ...],
    seed_self: bool,
    seed_friends: bool,
    seed_ladder: bool,
    ladder_cap: int,
    seed_apex: bool,
    apex_queue: tuple[str, ...],
    apex_tier: tuple[str, ...],
    apex_cap: int,
    seed_riot_tier: bool,
    riot_region: str,
    riot_queue: tuple[str, ...],
    riot_tier: tuple[str, ...],
    riot_division: tuple[str, ...],
    riot_page_limit: int,
    riot_cap: int,
    seed_riot_ids: tuple[str, ...],
    seed_riot_id_files: tuple[Path, ...],
    manual_seed_pending_cap: int,
    max_depth: int,
) -> None:
    """Expand recent LCU-visible match history through discovered player IDs."""
    try:
        from aram_nn.lcu.snowball import run_snowball
    except ImportError as exc:
        click.echo(f"[error] import failed: {exc}\n  Run: pip install -e .", err=True)
        sys.exit(1)

    try:
        run_snowball(
            db_path=db,
            target_games=target_games,
            max_players=max_players,
            history_window=history_window,
            games_per_player=games_per_player,
            worker_id=(worker_id or None),
            claim_timeout_sec=claim_timeout_sec,
            player_requeue_cooldown_sec=player_requeue_cooldown_sec,
            target_queues=set(queue),
            include_self=seed_self,
            include_friends=seed_friends,
            include_ladder=seed_ladder,
            ladder_cap=ladder_cap,
            include_apex=seed_apex,
            apex_queues=apex_queue,
            apex_tiers=apex_tier,
            apex_cap=apex_cap,
            include_riot_tier=seed_riot_tier,
            riot_region=riot_region,
            riot_queues=riot_queue,
            riot_tiers=riot_tier,
            riot_divisions=riot_division,
            riot_page_limit=riot_page_limit,
            riot_cap=riot_cap,
            seed_riot_ids=seed_riot_ids,
            seed_riot_id_files=seed_riot_id_files,
            manual_seed_pending_cap=manual_seed_pending_cap,
            max_depth=max_depth,
        )
    except RuntimeError as exc:
        click.echo(f"[error] {exc}", err=True)
        sys.exit(1)


@cli.command("snowball-workers")
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True, help="SQLite database path")
@click.option("--workers", default=2, show_default=True, type=int,
              help="How many parallel snowball worker processes to launch")
@click.option("--log-dir", default=Path(".codex/logs"), type=click.Path(path_type=Path),
              show_default=True, help="Directory for per-worker stdout/stderr logs")
@click.option("--worker-prefix", default="W", show_default=True,
              help="Worker id prefix; workers become W01 / W02 / ...")
@click.option("--stagger-sec", default=0.75, show_default=True, type=float,
              help="Delay between worker launches to reduce startup contention")
@click.option("--seed-on-first-only/--seed-on-all", default=True, show_default=True,
              help="Only the first worker seeds self/friends/ladders; later workers consume the saved queue")
@click.option("--target-games", default=500, show_default=True, type=int,
              help="Per-worker stop condition: stop after saving this many new games")
@click.option("--max-players", default=250, show_default=True, type=int,
              help="Per-worker stop condition: stop after processing this many player nodes")
@click.option("--history-window", default=20, show_default=True, type=int,
              help="How many recent games to inspect per player")
@click.option("--games-per-player", default=8, show_default=True, type=int,
              help="Cap how many target-queue games to expand per player for wider diffusion")
@click.option("--claim-timeout-sec", default=300, show_default=True, type=int,
              help="Reclaim an in-progress queue item if a worker disappears for this long")
@click.option("--player-requeue-cooldown-sec", default=45, show_default=True, type=int,
              help="Cooldown before a newer rediscovery can requeue the same processed player")
@click.option("--queue", multiple=True, type=int, default=(450, 2400),
              help="Queue IDs to capture (repeatable).  Default: 450 and 2400.")
@click.option("--seed-self/--no-seed-self", default=True, show_default=True,
              help="Seed the crawl with the current summoner")
@click.option("--seed-friends/--no-seed-friends", default=True, show_default=True,
              help="Seed the crawl with friend-list puuids")
@click.option("--seed-ladder/--no-seed-ladder", default=False, show_default=True,
              help="Seed the crawl with current ranked ladder neighbors")
@click.option("--ladder-cap", default=100, show_default=True, type=int,
              help="Maximum ladder players to enqueue when --seed-ladder is on")
@click.option("--seed-apex/--no-seed-apex", default=False, show_default=True,
              help="Seed the crawl with TW apex ladders (Challenger / GM / Master)")
@click.option("--apex-queue", multiple=True, default=("RANKED_SOLO_5x5", "RANKED_FLEX_SR"),
              show_default=True, help="Apex ladder queue types to seed from")
@click.option("--apex-tier", multiple=True, default=("CHALLENGER", "GRANDMASTER", "MASTER"),
              show_default=True, help="Apex ladder tiers to seed from")
@click.option("--apex-cap", default=300, show_default=True, type=int,
              help="Maximum apex-ladder players to enqueue when --seed-apex is on")
@click.option("--seed-riot-tier/--no-seed-riot-tier", default=False, show_default=True,
              help="Seed the crawl with Riot league-exp tiers such as GOLD / PLATINUM / DIAMOND")
@click.option("--riot-region", default="tw", show_default=True,
              help="Riot API region for league-exp tier seeds")
@click.option("--riot-queue", multiple=True, default=("RANKED_SOLO_5x5",),
              show_default=True, help="Riot league-exp queue types to seed from")
@click.option("--riot-tier", multiple=True, default=("GOLD",),
              show_default=True, help="Riot league-exp tiers to seed from")
@click.option("--riot-division", multiple=True, default=("I", "II", "III", "IV"),
              show_default=True, help="Riot league-exp divisions to seed from")
@click.option("--riot-page-limit", default=2, show_default=True, type=int,
              help="Maximum Riot league-exp pages to scan per tier/division")
@click.option("--riot-cap", default=400, show_default=True, type=int,
              help="Maximum Riot league-exp players to enqueue when --seed-riot-tier is on")
@click.option("--seed-riot-id", "seed_riot_ids", multiple=True, default=(),
              show_default=False, help="Manual Riot IDs to enqueue, e.g. Name#TAG")
@click.option("--seed-riot-id-file", "seed_riot_id_files", multiple=True,
              type=click.Path(path_type=Path, exists=True, dir_okay=False),
              show_default=False, help="Text file with one Riot ID or OPGG summoner URL per line")
@click.option("--manual-seed-pending-cap", default=40, show_default=True, type=int,
              help="Maximum pending/in-progress manual_riot_id queue items to keep open at once (0 = unlimited)")
@click.option("--max-depth", default=3, show_default=True, type=int,
              help="Maximum BFS depth for discovered participant puuids")
def snowball_workers(
    db: Path,
    workers: int,
    log_dir: Path,
    worker_prefix: str,
    stagger_sec: float,
    seed_on_first_only: bool,
    target_games: int,
    max_players: int,
    history_window: int,
    games_per_player: int,
    claim_timeout_sec: int,
    player_requeue_cooldown_sec: int,
    queue: tuple[int, ...],
    seed_self: bool,
    seed_friends: bool,
    seed_ladder: bool,
    ladder_cap: int,
    seed_apex: bool,
    apex_queue: tuple[str, ...],
    apex_tier: tuple[str, ...],
    apex_cap: int,
    seed_riot_tier: bool,
    riot_region: str,
    riot_queue: tuple[str, ...],
    riot_tier: tuple[str, ...],
    riot_division: tuple[str, ...],
    riot_page_limit: int,
    riot_cap: int,
    seed_riot_ids: tuple[str, ...],
    seed_riot_id_files: tuple[Path, ...],
    manual_seed_pending_cap: int,
    max_depth: int,
) -> None:
    """Launch multiple background snowball workers against the same SQLite frontier."""
    if workers < 1:
        click.echo("[error] --workers must be >= 1", err=True)
        sys.exit(1)

    db.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    launched: list[tuple[str, int, Path, Path, bool]] = []

    for idx in range(workers):
        worker_id = f"{worker_prefix}{idx + 1:02d}"
        should_seed = (idx == 0) or (not seed_on_first_only)
        cmd = _build_snowball_subprocess_args(
            db=db,
            target_games=target_games,
            max_players=max_players,
            history_window=history_window,
            games_per_player=games_per_player,
            worker_id=worker_id,
            claim_timeout_sec=claim_timeout_sec,
            player_requeue_cooldown_sec=player_requeue_cooldown_sec,
            queue=queue,
            seed_self=(seed_self and should_seed),
            seed_friends=(seed_friends and should_seed),
            seed_ladder=(seed_ladder and should_seed),
            ladder_cap=ladder_cap,
            seed_apex=(seed_apex and should_seed),
            apex_queue=apex_queue,
            apex_tier=apex_tier,
            apex_cap=apex_cap,
            seed_riot_tier=(seed_riot_tier and should_seed),
            riot_region=riot_region,
            riot_queue=riot_queue,
            riot_tier=riot_tier,
            riot_division=riot_division,
            riot_page_limit=riot_page_limit,
            riot_cap=riot_cap,
            seed_riot_ids=(seed_riot_ids if should_seed else ()),
            seed_riot_id_files=(seed_riot_id_files if should_seed else ()),
            manual_seed_pending_cap=manual_seed_pending_cap,
            max_depth=max_depth,
        )

        stdout_path = log_dir / f"snowball_{worker_id}.log"
        stderr_path = log_dir / f"snowball_{worker_id}.err"
        with stdout_path.open("ab") as stdout_file, stderr_path.open("ab") as stderr_file:
            proc = subprocess.Popen(
                cmd,
                cwd=str(Path.cwd()),
                stdout=stdout_file,
                stderr=stderr_file,
                creationflags=creationflags,
            )
        launched.append((worker_id, proc.pid, stdout_path, stderr_path, should_seed))
        if stagger_sec > 0 and idx + 1 < workers:
            time.sleep(stagger_sec)

    click.echo(
        f"[workers] launched {len(launched)} snowball workers against {db}  "
        f"pid={os.getpid()}"
    )
    for worker_id, pid, stdout_path, stderr_path, should_seed in launched:
        seed_mode = "seed" if should_seed else "consume"
        click.echo(
            f"  {worker_id}: child_pid={pid}  mode={seed_mode}  "
            f"log={stdout_path}  err={stderr_path}"
        )
    click.echo("  monitor: python scripts/lcu_collector.py status")
    click.echo("  stop:    Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*lcu_collector.py*snowball*' } | ForEach-Object { Stop-Process -Id $_.ProcessId }")


# ------------------------------------------------------------------ export ---

@cli.command()
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--out", required=True, type=click.Path(path_type=Path),
              help="Output .parquet path")
@click.option("--queue", multiple=True, type=int, default=(),
              help="Filter to these queue IDs (omit for all queues)")
@click.option("--platform", default="TW2", show_default=True,
              help="Platform tag written to the parquet 'platform' column "
                   "(e.g. TW2, KR, EUW1).  Metadata only — not used by train.py.")
def export(db: Path, out: Path, queue: tuple[int, ...], platform: str) -> None:
    """Export captured games to Parquet (same schema as snowball output).

    The parquet file can be passed directly to `python -m aram_nn.train --data`.
    Champion IDs from LCU are integers, same as Riot match-v5.
    """
    if not db.exists():
        click.echo(f"[error] database not found: {db}", err=True)
        sys.exit(1)

    con = sqlite3.connect(str(db))
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(games)").fetchall()}
    has_participants = "participants_json" in columns
    rows = con.execute(
        """
        SELECT game_id, queue_id, patch, blue_champs, red_champs,
               blue_wins, duration_sec, created_ms, captured_at,
               participants_json
        FROM games
        ORDER BY created_ms
        """
        if has_participants
        else
        """
        SELECT game_id, queue_id, patch, blue_champs, red_champs,
               blue_wins, duration_sec, created_ms, captured_at,
               NULL as participants_json
        FROM games
        ORDER BY created_ms
        """
    ).fetchall()
    con.close()

    if not rows:
        click.echo("[export] no games in database")
        return

    records = []
    skipped = 0
    for game_id, queue_id, patch, blue_json, red_json, blue_wins, duration_sec, created_ms, _, participants_json in rows:
        if queue and queue_id not in set(queue):
            skipped += 1
            continue
        records.append({
            "match_id":          f"LCU_{game_id}",
            "patch":             patch,
            "queue_id":          queue_id,
            "platform":          platform,
            "duration_sec":      duration_sec,
            "blue_champions":    sorted(json.loads(blue_json)),
            "red_champions":     sorted(json.loads(red_json)),
            "blue_wins":         bool(blue_wins),
            "game_creation_ms":  created_ms,
            "game_end_ms":       created_ms + duration_sec * 1000,
            "max_leaver_gap_sec": 0,  # LCU doesn't expose this
            "participants_json": participants_json or "[]",
        })

    if not records:
        click.echo(f"[export] 0 records match the queue filter (skipped {skipped})")
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(records)
    df.write_parquet(out, compression="zstd")

    click.echo(f"[export] {len(records)} games → {out}")
    by_q = df.group_by("queue_id").agg(pl.len().alias("count")).sort("queue_id")
    for row in by_q.iter_rows():
        label = "Mayhem" if row[0] == 2400 else ("ARAM" if row[0] == 450 else f"q{row[0]}")
        click.echo(f"  {label} ({row[0]}): {row[1]}")
    click.echo(f"  blue_win_rate: {df['blue_wins'].mean():.3f}")
    if skipped:
        click.echo(f"  (skipped {skipped} games not matching queue filter)")


@cli.command("merge-db")
@click.option("--out-db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True, help="Destination merged SQLite database")
@click.option("--glob", "globs", multiple=True,
              help="Optional glob(s) for source DBs, e.g. data/lcu/games_*.db")
@click.option("--vacuum/--no-vacuum", default=False, show_default=True,
              help="Run VACUUM on the merged DB after importing")
@click.argument("sources", nargs=-1, type=click.Path(path_type=Path))
def merge_db(out_db: Path, globs: tuple[str, ...], vacuum: bool, sources: tuple[Path, ...]) -> None:
    """Merge per-client SQLite DBs into one games DB using exact game_id de-dup."""
    source_paths: list[Path] = []
    seen_paths: set[Path] = set()

    def add_source(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen_paths:
            return
        seen_paths.add(resolved)
        source_paths.append(path)

    for path in sources:
        add_source(path)
    for pattern in globs:
        for path in sorted(Path().glob(pattern)):
            add_source(path)

    if not source_paths:
        raise click.ClickException("provide at least one source DB path or --glob pattern")

    out_db.parent.mkdir(parents=True, exist_ok=True)
    out_resolved = out_db.resolve()
    filtered_sources = [path for path in source_paths if path.resolve() != out_resolved]
    skipped_self = len(source_paths) - len(filtered_sources)
    if not filtered_sources:
        raise click.ClickException("all sources resolve to the destination DB; nothing to merge")

    dst_con = sqlite3.connect(str(out_db), timeout=30.0)
    try:
        _ensure_games_schema(dst_con)
        before_total = dst_con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        click.echo(
            f"[merge-db] destination={out_db}  existing_games={before_total}  "
            f"sources={len(filtered_sources)}"
        )
        if skipped_self:
            click.echo(f"[merge-db] skipped destination path from sources x{skipped_self}")

        read_total = 0
        inserted_total = 0
        backfilled_total = 0
        for src_db in filtered_sources:
            stats = _merge_games_from_source(dst_con, src_db)
            read_total += stats["rows_read"]
            inserted_total += stats["inserted"]
            backfilled_total += stats["participants_backfilled"]
            click.echo(
                f"  [source] {src_db}  rows={stats['rows_read']}  "
                f"inserted={stats['inserted']}  participants_backfilled={stats['participants_backfilled']}"
            )

        if vacuum:
            dst_con.execute("VACUUM")

        after_total = dst_con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        click.echo(
            f"[merge-db] done  total={after_total}  added={after_total - before_total}  "
            f"rows_read={read_total}  participants_backfilled={backfilled_total}"
        )
    finally:
        dst_con.close()


_CONTRIB_REPO_DEFAULT = "Lanternko/ARAM-Mayhem-Database"


def _patch_sort_key(patch: str) -> tuple:
    """Version-aware sort key so 16.10.x > 16.9.x (lex would put 16.10 < 16.9)."""
    try:
        return tuple(int(part) for part in str(patch).split("."))
    except (ValueError, AttributeError):
        return (-1,)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.2f}MB"


@cli.command("export-share")
@click.option("--db", "src_db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True, help="Source SQLite database (your local games.db)")
@click.option("--out", "out_path", default=None, type=click.Path(path_type=Path),
              help="Output path. Defaults to data/share/share_<utc>.db")
@click.option("--queue", multiple=True, type=int, default=(2400,),
              show_default=True, help="Queue IDs to include (repeatable). Default is Mayhem only.")
@click.option("--patch-prefix", multiple=True, default=(), show_default=True,
              help="Optional patch prefix filters such as 16.10 (repeatable). Empty = all patches.")
@click.option("--auto-issue/--no-auto-issue", default=False, show_default=True,
              help="After export, open a pre-filled GitHub Issue in your browser "
                   "(you still need to drag-drop the .db into the comment).")
@click.option("--print-issue-url/--no-print-issue-url", default=False, show_default=True,
              help="Print the GitHub Issue URL instead of opening it. Implies --auto-issue logic.")
@click.option("--issue-repo", default=_CONTRIB_REPO_DEFAULT, show_default=True,
              help="GitHub repo (owner/name) where the Issue is filed.")
def export_share(
    src_db: Path,
    out_path: Path | None,
    queue: tuple[int, ...],
    patch_prefix: tuple[str, ...],
    auto_issue: bool,
    print_issue_url: bool,
    issue_repo: str,
) -> None:
    """Export a PUUID-free SQLite snapshot suitable for sharing publicly.

    Output contains ONLY the `games` table (no crawl_seen / crawl_queue / riot_id_bridge).
    The games table itself stores no PUUIDs - just game_id, queue, patch, the 10 champion
    IDs by side, augments per champion, and win flag. Attach the output file to a GitHub
    Issue using the "Contribute Match Data" template.
    """
    if not src_db.exists():
        cwd_abs = Path.cwd().resolve()
        db_abs = src_db.resolve() if src_db.is_absolute() else (cwd_abs / src_db).resolve()
        raise click.ClickException(
            "no games database to export from.\n\n"
            f"  looked for : {db_abs}\n"
            f"  current dir: {cwd_abs}\n\n"
            "Most likely cause: you haven't collected any games yet.\n"
            "Run this first (with League client open in the background):\n\n"
            "  python scripts/lcu_collector.py auto-collect --rounds 50 --target-games 500 "
            "--max-players 1000 --opgg-tier platinum --opgg-tier gold\n\n"
            "Then re-run export-share. Or, if your DB lives elsewhere, pass --db <path-to-games.db>."
        )

    if out_path is None:
        timestamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
        out_path = Path("data/share") / f"share_{timestamp}.db"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    queue_filter = set(queue)
    patch_filters = tuple(patch_prefix)

    src_con = sqlite3.connect(str(src_db), timeout=30.0)
    dst_con = sqlite3.connect(str(out_path), timeout=30.0)
    try:
        if not _table_exists(src_con, "games"):
            raise click.ClickException(f"source DB has no games table: {src_db}")
        _ensure_games_schema(dst_con)

        total_in = 0
        total_out = 0
        blue_wins_total = 0
        patches_seen: Counter[str] = Counter()
        queues_seen: Counter[int] = Counter()
        for rows in _iter_game_rows(src_con, chunk_size=2000):
            total_in += len(rows)
            batch: list[tuple] = []
            for row in rows:
                queue_id = row[1]
                patch_str = row[2]
                if queue_filter and queue_id not in queue_filter:
                    continue
                if patch_filters and not any(str(patch_str).startswith(p) for p in patch_filters):
                    continue
                batch.append(row)
                patches_seen[str(patch_str)] += 1
                queues_seen[int(queue_id)] += 1
                blue_wins_total += int(row[5] or 0)
            if batch:
                dst_con.executemany(_GAMES_INSERT_SQL, batch)
                total_out += len(batch)
        dst_con.commit()
    finally:
        src_con.close()
        dst_con.close()

    if total_out == 0:
        click.echo(
            f"[export-share] no games matched filters "
            f"(queue={sorted(queue_filter)} patch_prefix={list(patch_filters)})  "
            f"scanned={total_in}"
        )
        if out_path.exists():
            out_path.unlink()
        sys.exit(1)

    blue_wr = blue_wins_total / total_out
    db_size = out_path.stat().st_size
    db_size_str = _format_size(db_size)

    # Zip the .db because GitHub Issue attachments reject .db (file type not on
    # the allowlist). The .zip is also smaller, which helps the 25 MB cap.
    zip_path = out_path.with_suffix(out_path.suffix + ".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(out_path, arcname=out_path.name)
    zip_size = zip_path.stat().st_size
    zip_size_str = _format_size(zip_size)

    abs_out_path = out_path.resolve()
    abs_zip_path = zip_path.resolve()
    folder_uri = abs_zip_path.parent.as_uri()

    sorted_patches = sorted(patches_seen.items(), key=lambda kv: _patch_sort_key(kv[0]))
    if not patches_seen:
        patch_display = "unknown"
    elif len(sorted_patches) <= 4:
        patch_display = ", ".join(f"{p}={n}" for p, n in sorted_patches)
    else:
        patch_display = f"{sorted_patches[0][0]}...{sorted_patches[-1][0]} ({len(sorted_patches)} patches)"

    # Title patch logic: prefer single patch, then dominant patch (>=60%), else version range.
    if patches_seen:
        top_patch, top_count = max(patches_seen.items(), key=lambda kv: kv[1])
        if len(patches_seen) == 1:
            title_patch = ".".join(str(top_patch).split(".")[:2])
        elif top_count / total_out >= 0.6:
            title_patch = ".".join(str(top_patch).split(".")[:2]) + "+"
        else:
            lo = ".".join(str(sorted_patches[0][0]).split(".")[:2])
            hi = ".".join(str(sorted_patches[-1][0]).split(".")[:2])
            title_patch = f"{lo}-{hi}" if lo != hi else lo
    else:
        title_patch = "unknown"

    click.echo(f"[export-share] wrote {abs_zip_path}")
    click.echo(f"  games    : {total_out} (filtered from {total_in})")
    queue_summary = ", ".join(f"{q}={n}" for q, n in queues_seen.most_common())
    click.echo(f"  queues   : {queue_summary}")
    click.echo(f"  blue_wr  : {blue_wr:.3f}")
    if patches_seen:
        click.echo(f"  patches  : {patch_display}")
    click.echo(f"  file size: {zip_size_str} (zipped from {db_size_str})")
    click.echo("  contents : games table only - no PUUIDs, no crawl frontier")
    click.echo(f"  open dir : {folder_uri}   (Ctrl+click in most terminals)")
    click.echo("")
    if zip_size > 24 * 1024 * 1024:
        click.echo(f"WARNING: zip is {zip_size_str}; GitHub Issue attachments cap at 25MB.")
        click.echo("Consider exporting per-patch (e.g. --patch-prefix 16.10) and submitting multiple files.")
        click.echo("")
    if len(patches_seen) >= 5:
        click.echo(f"NOTE: this export spans {len(patches_seen)} patches. The tier list is patch-sensitive -")
        click.echo("consider re-running with --patch-prefix 16.10 (or current patch) to get a cleaner signal.")
        click.echo("")

    if auto_issue or print_issue_url:
        issue_title = f"[data] {title_patch} - {total_out} games"
        body_lines = [
            "<!-- Auto-generated by `export-share --auto-issue` -->",
            "",
            "## Export summary",
            "",
            "```",
            f"games    : {total_out}",
            f"queues   : {queue_summary}",
            f"blue_wr  : {blue_wr:.3f}",
        ]
        if patches_seen:
            body_lines.append(f"patches  : {patch_display}")
        body_lines += [
            f"file size: {zip_size_str} (zipped from {db_size_str})",
            "```",
            "",
            "## Attach your file",
            "",
            f"**Drag-and-drop `{abs_zip_path.name}` into this comment box** before submitting.",
            "(GitHub rejects `.db` directly, so the file was zipped automatically.)",
            "",
            f"File location on your machine: `{abs_zip_path}`",
            "",
            "## Collection notes (optional)",
            "",
            "- **Region / server**: ",
            "- **Approximate collection period**: ",
            "- **Anything unusual**: ",
            "",
            "## Privacy checklist",
            "",
            "- [x] Generated by `export-share` - file contains no PUUIDs",
            "- [ ] I understand the data will be merged into the public tier-list dataset",
        ]
        issue_body = "\n".join(body_lines)
        params = {
            "template": "contribute-data.md",
            "title": issue_title,
            "body": issue_body,
            "labels": "data-contribution",
        }
        issue_url = f"https://github.com/{issue_repo}/issues/new?" + urlencode(
            params, quote_via=quote
        )

        click.echo("Pre-filled GitHub Issue ready:")
        click.echo(f"  title : {issue_title}")
        click.echo(f"  repo  : {issue_repo}")
        click.echo(f"  URL   : {issue_url}")
        click.echo("")
        if print_issue_url:
            return
        click.echo("Opening in your default browser...")
        try:
            opened = webbrowser.open(issue_url)
        except Exception as exc:
            click.echo(f"  (browser open failed: {exc})")
            opened = False
        if not opened:
            click.echo("  (no browser opened - copy the URL above manually)")
        click.echo("")
        click.echo(f"In the browser tab: drag `{abs_zip_path.name}` into the comment box, then click Submit new issue.")
        click.echo(f"(GitHub rejects `.db` directly - that's why the zip was created.)")
    else:
        click.echo("Next step: open a GitHub Issue using the 'Contribute Match Data' template")
        click.echo(f"and attach `{abs_zip_path.name}` (NOT the raw .db - GitHub rejects .db).")
        click.echo("The summary above is fine to paste into the issue body.")
        click.echo("(Tip: re-run with --auto-issue to skip the manual navigation.)")


@cli.command("verify-share")
@click.argument("sources", nargs=-1, type=click.Path(path_type=Path))
@click.option("--strict/--no-strict", default=False, show_default=True,
              help="Exit non-zero if any file has warnings (useful in CI)")
def verify_share(sources: tuple[Path, ...], strict: bool) -> None:
    """Sanity-check share-export files before merging into your local DB.

    Checks per file: schema, absence of PUUID-bearing auxiliary tables, no PUUID-like
    fields inside participants_json, valid row contents (5+5 champ IDs, win flag, duration,
    10 participants), blue_wr in plausible range, no duplicate game_ids, no unexpected queues.

    Prints one OK / WARN line per file, with warnings listed beneath. Use --strict to make
    this exit non-zero on any warning (suitable for scripting batched ingest).
    """
    if not sources:
        raise click.ClickException("provide at least one share .db path")

    any_warning = False
    import tempfile

    for src in sources:
        warnings: list[str] = []
        if not src.exists():
            click.echo(f"  MISS  {src}  (file not found)")
            any_warning = True
            continue

        # Auto-extract .zip (since export-share now produces .db.zip for GitHub upload)
        extracted_temp: Path | None = None
        db_path = src
        if src.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(src) as zf:
                    db_members = [n for n in zf.namelist() if n.lower().endswith(".db")]
                    if not db_members:
                        click.echo(f"  FAIL  {src}  (zip has no .db inside)")
                        any_warning = True
                        continue
                    if len(db_members) > 1:
                        warnings.append(f"zip_has_multiple_dbs={db_members}")
                    member = db_members[0]
                    tmp_dir = Path(tempfile.mkdtemp(prefix="verify_share_"))
                    extracted_temp = tmp_dir / Path(member).name
                    with zf.open(member) as src_f, extracted_temp.open("wb") as dst_f:
                        dst_f.write(src_f.read())
                    db_path = extracted_temp
            except zipfile.BadZipFile:
                click.echo(f"  FAIL  {src}  (corrupt zip)")
                any_warning = True
                continue

        try:
            con = sqlite3.connect(str(db_path), timeout=30.0)
            con.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            click.echo(f"  FAIL  {src}  (open failed: {exc})")
            if extracted_temp and extracted_temp.exists():
                extracted_temp.unlink()
                extracted_temp.parent.rmdir()
            any_warning = True
            continue

        try:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "games" not in tables:
                click.echo(f"  FAIL  {src}  (no games table)")
                any_warning = True
                continue

            puuid_tables = tables & {"crawl_seen", "crawl_queue", "riot_id_bridge", "crawl_game_claims"}
            if puuid_tables:
                warnings.append(f"contains_puuid_tables={sorted(puuid_tables)}")

            cols = _table_columns(con, "games")
            has_participants = "participants_json" in cols

            # Spot-check first participants_json row for PUUID-like fields
            if has_participants:
                sample = con.execute(
                    "SELECT participants_json FROM games "
                    "WHERE participants_json IS NOT NULL AND participants_json != '' LIMIT 1"
                ).fetchone()
                if sample and sample[0]:
                    try:
                        payload = json.loads(sample[0])
                        forbidden = {"puuid", "summonerId", "summonerName", "riotIdGameName"}
                        if isinstance(payload, list):
                            for p in payload:
                                if isinstance(p, dict) and forbidden & set(p.keys()):
                                    warnings.append("participants_contain_puuid_field")
                                    break
                    except Exception:
                        warnings.append("participants_json_unparseable_sample")

            total = 0
            blue_wins_total = 0
            queues_seen: Counter[int] = Counter()
            bad_rows = 0
            seen_ids: set[str] = set()
            dupe_ids = 0
            cursor = con.execute(
                "SELECT game_id, queue_id, blue_champs, red_champs, blue_wins, duration_sec, "
                + ("participants_json" if has_participants else "NULL AS participants_json")
                + " FROM games"
            )
            for row in cursor:
                total += 1
                game_id = row["game_id"]
                if game_id in seen_ids:
                    dupe_ids += 1
                else:
                    seen_ids.add(game_id)

                queues_seen[int(row["queue_id"])] += 1

                blue_wins = row["blue_wins"]
                if blue_wins not in (0, 1):
                    bad_rows += 1
                    continue
                blue_wins_total += int(blue_wins)

                try:
                    blue = json.loads(row["blue_champs"])
                    red = json.loads(row["red_champs"])
                except Exception:
                    bad_rows += 1
                    continue
                if not (isinstance(blue, list) and isinstance(red, list)
                        and len(blue) == 5 and len(red) == 5
                        and all(isinstance(x, int) and 1 <= x <= 9999 for x in blue + red)):
                    bad_rows += 1
                    continue

                duration = row["duration_sec"]
                if not isinstance(duration, int) or duration < 300:
                    bad_rows += 1
                    continue

                if has_participants and row["participants_json"]:
                    try:
                        payload = json.loads(row["participants_json"])
                    except Exception:
                        bad_rows += 1
                        continue
                    if not (isinstance(payload, list) and len(payload) == 10
                            and all(isinstance(p, dict)
                                    and p.get("teamId") in (100, 200)
                                    and isinstance(p.get("championId"), int)
                                    for p in payload)):
                        bad_rows += 1
                        continue

            if total == 0:
                warnings.append("zero_games")
                blue_wr = 0.0
            else:
                blue_wr = blue_wins_total / total
                if not (0.40 <= blue_wr <= 0.60):
                    warnings.append(f"blue_wr_out_of_range={blue_wr:.3f}")
                if bad_rows:
                    warnings.append(f"bad_rows={bad_rows}")
                if dupe_ids:
                    warnings.append(f"duplicate_game_ids={dupe_ids}")
                unknown_queues = {q for q in queues_seen if q not in (450, 2400)}
                if unknown_queues:
                    warnings.append(f"unexpected_queues={sorted(unknown_queues)}")

            tag = "OK  " if not warnings else "WARN"
            queue_str = ",".join(f"{q}={n}" for q, n in queues_seen.most_common()) or "-"
            click.echo(
                f"  {tag}  {src}  games={total}  blue_wr={blue_wr:.3f}  queues={queue_str}"
            )
            if warnings:
                any_warning = True
                for w in warnings:
                    click.echo(f"        - {w}")
        finally:
            con.close()
            if extracted_temp and extracted_temp.exists():
                extracted_temp.unlink()
                try:
                    extracted_temp.parent.rmdir()
                except OSError:
                    pass

    if strict and any_warning:
        sys.exit(1)


@cli.command("seed-opgg")
@click.option("--tier", default="diamond", show_default=True,
              help="Leaderboard tier to scrape from OPGG, e.g. gold / platinum / diamond")
@click.option("--region", default="tw", show_default=True,
              help="OPGG region slug, e.g. tw / kr / na")
@click.option("--pages", default=1, show_default=True, type=int,
              help="How many leaderboard pages to scrape")
@click.option("--topn", default=100, show_default=True, type=int,
              help="Stop after collecting this many Riot IDs (0 = no explicit cap)")
@click.option("--out", default=Path("data/seeds/opgg_tw.txt"), type=click.Path(path_type=Path),
              show_default=True, help="Output seed file path")
@click.option("--append/--overwrite", default=False, show_default=True,
              help="Append to the existing seed file instead of replacing it")
def seed_opgg(tier: str, region: str, pages: int, topn: int, out: Path, append: bool) -> None:
    """Scrape OPGG leaderboard profile links into a Riot-ID seed file."""
    try:
        riot_ids = _fetch_opgg_leaderboard_riot_ids(
            region=region,
            tier=tier,
            pages=pages,
            topn=topn,
        )
    except Exception as exc:
        raise click.ClickException(f"OPGG scrape failed: {exc}") from exc

    if not riot_ids:
        raise click.ClickException("OPGG returned no summoner profile links for the requested leaderboard")

    _write_seed_file(out, riot_ids, append=append)
    click.echo(
        f"[seed-opgg] wrote {len(riot_ids)} Riot IDs to {out}  "
        f"region={region}  tier={tier}  pages={pages}"
    )
    for riot_id in riot_ids[:10]:
        click.echo(f"  {_console_safe(riot_id)}")


@cli.command("seed-opgg-plan")
@click.option("--region", default="tw", show_default=True,
              help="OPGG region slug, e.g. tw / kr / na")
@click.option("--source", "sources", multiple=True,
              default=("tier",),
              show_default=True,
              help="Leaderboard sources to walk: tier and/or level")
@click.option("--tier", "tiers", multiple=True,
              default=("diamond", "emerald", "platinum", "gold"),
              show_default=True,
              help="Tier order to walk when refreshing seeds")
@click.option("--pages-per-tier", default=80, show_default=True, type=int,
              help="Walk sequential pages 1..N for each tier before moving to the next tier")
@click.option("--pages-per-source", default=None, type=int,
              help="Pages to walk per selected source; defaults to --pages-per-tier")
@click.option("--topn-total", default=400, show_default=True, type=int,
              help="Stop after collecting this many total Riot IDs across all tiers; use 0 for exhaustive paging")
@click.option("--state-file", default=DEFAULT_OPGG_STATE, type=click.Path(path_type=Path),
              show_default=True, help="JSON cursor file used to resume from the next leaderboard pages")
@click.option("--history-file", default=DEFAULT_OPGG_HISTORY, type=click.Path(path_type=Path),
              show_default=True, help="Append-only JSONL audit log for OPGG seed refresh runs")
@click.option("--resume/--restart", default=False, show_default=True,
              help="Resume from saved per-tier next-page cursors instead of starting from --start-page")
@click.option("--start-page", default=1, show_default=True, type=int,
              help="Starting page for each tier when not using --resume")
@click.option("--out", default=Path("data/seeds/opgg_tw.txt"), type=click.Path(path_type=Path),
              show_default=True, help="Output seed file path")
@click.option("--append/--overwrite", default=False, show_default=True,
              help="Append to the existing seed file instead of replacing it")
def seed_opgg_plan(
    region: str,
    sources: tuple[str, ...],
    tiers: tuple[str, ...],
    pages_per_tier: int,
    pages_per_source: int | None,
    topn_total: int,
    state_file: Path,
    history_file: Path,
    resume: bool,
    start_page: int,
    out: Path,
    append: bool,
) -> None:
    """Refresh seeds by walking OPGG leaderboard pages in priority order."""
    normalized_sources = _normalize_opgg_sources(sources)
    page_window = max(1, pages_per_source if pages_per_source is not None else pages_per_tier)
    headers = {"User-Agent": "Mozilla/5.0"}
    riot_ids: list[str] = []
    seen: set[str] = set()
    page_hits: list[tuple[str, str, int, int]] = []
    state = _load_opgg_state(
        path=state_file,
        region=region,
        tiers=tiers,
        start_page=max(1, start_page),
        sources=normalized_sources,
    ) if resume else _default_opgg_state(
        region=region,
        tiers=tiers,
        start_page=max(1, start_page),
        sources=normalized_sources,
    )

    try:
        with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
            if "tier" in normalized_sources:
                for tier in tiers:
                    normalized_tier = str(tier).strip().lower()
                    if not normalized_tier:
                        continue
                    tier_state = state["tiers"].setdefault(
                        normalized_tier,
                        {"next_page": max(1, start_page), "exhausted": False},
                    )
                    if tier_state.get("exhausted"):
                        continue

                    added, exhausted = _walk_opgg_pages(
                        client=client,
                        region=region,
                        source="tier",
                        bucket=normalized_tier,
                        cursor_state=tier_state,
                        start_page=start_page,
                        page_window=page_window,
                        riot_ids=riot_ids,
                        seen=seen,
                        page_hits=page_hits,
                        topn_total=topn_total,
                    )
                    tier_state["exhausted"] = exhausted
                    if topn_total > 0 and len(riot_ids) >= topn_total:
                        break

            if "level" in normalized_sources and (topn_total <= 0 or len(riot_ids) < topn_total):
                level_state = state.setdefault(
                    "level",
                    {"next_page": max(1, start_page), "exhausted": False},
                )
                if not level_state.get("exhausted"):
                    _, exhausted = _walk_opgg_pages(
                        client=client,
                        region=region,
                        source="level",
                        bucket="level",
                        cursor_state=level_state,
                        start_page=start_page,
                        page_window=page_window,
                        riot_ids=riot_ids,
                        seen=seen,
                        page_hits=page_hits,
                        topn_total=topn_total,
                    )
                    level_state["exhausted"] = exhausted
    except Exception as exc:
        raise click.ClickException(f"OPGG plan scrape failed: {exc}") from exc

    if not riot_ids:
        _save_opgg_state(state_file, state)
        _append_opgg_history(history_file, {
            "region": region,
            "sources": list(normalized_sources),
            "tiers": list(tiers),
            "pages_per_source": page_window,
            "topn_total": topn_total,
            "resume": resume,
            "state_file": str(state_file),
            "out": str(out),
            "written": 0,
            "page_hits": page_hits,
            "note": "no_riot_ids",
        })
        raise click.ClickException("OPGG plan returned no summoner profile links for the requested tiers/pages")

    _write_seed_file(out, riot_ids, append=append)
    _save_opgg_state(state_file, state)
    _append_opgg_history(history_file, {
        "region": region,
        "sources": list(normalized_sources),
        "tiers": list(tiers),
        "pages_per_source": page_window,
        "topn_total": topn_total,
        "resume": resume,
        "state_file": str(state_file),
        "out": str(out),
        "written": len(riot_ids),
        "page_hits": page_hits,
        "cursor_after": {
            "tiers": state.get("tiers", {}),
            "level": state.get("level"),
        },
    })
    click.echo(
        f"[seed-opgg-plan] wrote {len(riot_ids)} Riot IDs to {out}  "
        f"region={region}  sources={list(normalized_sources)}  pages_per_source={page_window}  "
        f"resume={resume}  state_file={state_file}  history_file={history_file}"
    )
    for page_source, page_bucket, page_no, page_added in page_hits:
        click.echo(f"  [{page_source}:{page_bucket} page {page_no}] added={page_added}")
    for riot_id in riot_ids[:10]:
        click.echo(f"  {_console_safe(riot_id)}")


@cli.command("seed-opgg-state")
@click.option("--state-file", default=DEFAULT_OPGG_STATE, type=click.Path(path_type=Path),
              show_default=True, help="JSON cursor file used by seed-opgg-plan --resume")
@click.option("--history-file", default=DEFAULT_OPGG_HISTORY, type=click.Path(path_type=Path),
              show_default=True, help="Append-only JSONL audit log for prior refresh runs")
@click.option("--tail", default=5, show_default=True, type=int,
              help="How many recent history entries to print")
def seed_opgg_state(state_file: Path, history_file: Path, tail: int) -> None:
    """Show the current OPGG page cursors and recent refresh history."""
    if state_file.exists():
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"failed to read state file {state_file}: {exc}") from exc
        click.echo(f"[seed-opgg-state] state_file={state_file}")
        click.echo(f"  region={payload.get('region')}")
        tiers = payload.get("tiers") or {}
        for tier, tier_state in tiers.items():
            if not isinstance(tier_state, dict):
                continue
            click.echo(
                f"  {tier}: next_page={tier_state.get('next_page')}  exhausted={bool(tier_state.get('exhausted'))}"
            )
        level_state = payload.get("level")
        if isinstance(level_state, dict):
            click.echo(
                f"  level: next_page={level_state.get('next_page')}  exhausted={bool(level_state.get('exhausted'))}"
            )
        if payload.get("updated_at"):
            click.echo(f"  updated_at={payload.get('updated_at')}")
    else:
        click.echo(f"[seed-opgg-state] no state file at {state_file}")

    if history_file.exists():
        lines = history_file.read_text(encoding="utf-8").splitlines()
        recent = lines[-max(0, tail):] if tail > 0 else []
        click.echo(f"[seed-opgg-state] history_file={history_file}  entries={len(lines)}")
        for line in recent:
            try:
                item = json.loads(line)
            except Exception:
                click.echo(f"  raw={line}")
                continue
            click.echo(
                f"  {item.get('logged_at')}  written={item.get('written')}  "
                f"resume={item.get('resume')}  "
                f"sources={item.get('sources', ['tier'])}  "
                f"pages={item.get('pages_per_source', item.get('pages_per_tier'))}"
            )
    else:
        click.echo(f"[seed-opgg-state] no history file at {history_file}")


# ----------------------------------------------------------------- metrics ---

@cli.command("metrics")
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--seed-state-file", default=DEFAULT_OPGG_STATE, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--seed-history-file", default=DEFAULT_OPGG_HISTORY, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--history-file", default=DEFAULT_METRICS_HISTORY, type=click.Path(path_type=Path),
              show_default=True, help="Append-only JSONL history of crawler metrics snapshots")
@click.option("--record/--no-record", default=True, show_default=True,
              help="Append the current snapshot to --history-file")
@click.option("--tail", default=3, show_default=True, type=int,
              help="How many previous snapshots to summarize")
def metrics(
    db: Path,
    seed_state_file: Path,
    seed_history_file: Path,
    history_file: Path,
    record: bool,
    tail: int,
) -> None:
    """Show crawler growth, speed, and seed-efficiency metrics."""
    snapshot = _collect_status_snapshot(
        db,
        seed_state_file=seed_state_file,
        seed_history_file=seed_history_file,
    )
    previous_entries = _load_jsonl_tail(history_file, limit=max(1, tail))
    previous = previous_entries[-1] if previous_entries else None

    if record:
        _append_metrics_history(history_file, snapshot)

    click.echo(
        f"[metrics] {db}  total={snapshot['total']}  mayhem={snapshot['mayhem']['total']}  "
        f"latest_patch={snapshot['mayhem']['latest_patch'] or '-'}  "
        f"latest_patch_games={snapshot['mayhem']['latest_patch_games']}  "
        f"active_workers={len(snapshot['active_workers'])}"
    )

    frontier = snapshot.get("crawl_frontier") or {}
    if frontier:
        ordered = [
            f"{name}={frontier.get(name, 0)}"
            for name in ("pending", "in_progress", "done")
            if name in frontier
        ]
        click.echo("  frontier  " + "  ".join(ordered))

    seed_state = snapshot.get("seed_state")
    if isinstance(seed_state, dict) and isinstance(seed_state.get("tiers"), dict):
        tier_bits = []
        for tier, payload in seed_state["tiers"].items():
            if not isinstance(payload, dict):
                continue
            tier_bits.append(
                f"{tier}:{payload.get('next_page', '?')}"
                f"{'x' if payload.get('exhausted') else ''}"
            )
        if tier_bits:
            click.echo("  seed_cursor  " + "  ".join(tier_bits))
    if isinstance(seed_state, dict) and isinstance(seed_state.get("level"), dict):
        level_state = seed_state["level"]
        click.echo(
            "  level_cursor  "
            f"page={level_state.get('next_page', '?')}"
            f"{'x' if level_state.get('exhausted') else ''}"
        )

    latest_seed_refresh = snapshot.get("latest_seed_refresh")
    if isinstance(latest_seed_refresh, dict):
        click.echo(
            f"  latest_seed_refresh  @{latest_seed_refresh.get('logged_at', '?')}  "
            f"written={latest_seed_refresh.get('written', 0)}  "
            f"{'resume' if latest_seed_refresh.get('resume') else 'restart'}"
        )

    if previous:
        elapsed_sec = max(
            0.0,
            float(snapshot["captured_at_unix"]) - float(previous.get("captured_at_unix", 0.0)),
        )
        total_delta = int(snapshot["total"]) - int(previous.get("total", 0))
        mayhem_delta = int(snapshot["mayhem"]["total"]) - int(previous.get("mayhem", {}).get("total", 0))
        previous_patch = previous.get("mayhem", {}).get("latest_patch")
        current_patch = snapshot["mayhem"]["latest_patch"]
        if previous_patch == current_patch and current_patch:
            current_patch_delta = (
                int(snapshot["mayhem"]["latest_patch_games"])
                - int(previous.get("mayhem", {}).get("latest_patch_games", 0))
            )
        else:
            current_patch_delta = None
        done_delta = (
            int(frontier.get("done", 0))
            - int((previous.get("crawl_frontier") or {}).get("done", 0))
        )
        click.echo(
            f"  since_prev  dt={elapsed_sec / 60.0:.1f}m  "
            f"total={total_delta:+d} [{_format_rate(total_delta, elapsed_sec)}]  "
            f"mayhem={mayhem_delta:+d} [{_format_rate(mayhem_delta, elapsed_sec)}]"
        )
        if current_patch_delta is None:
            click.echo("  current_patch_delta  n/a (patch rolled or no prior comparable snapshot)")
        else:
            click.echo(
                f"  current_patch_delta  {current_patch} {current_patch_delta:+d}  "
                f"[{_format_rate(current_patch_delta, elapsed_sec)}]"
            )
        if done_delta > 0:
            mayhem_per_100_done = 100.0 * mayhem_delta / done_delta
            click.echo(
                f"  frontier_efficiency  done_delta={done_delta:+d}  "
                f"mayhem_per_100_done={mayhem_per_100_done:.2f}"
            )
        else:
            click.echo("  frontier_efficiency  n/a (done count did not advance)")
    else:
        click.echo("  since_prev  no previous metrics snapshot yet; baseline recorded now")

    if previous_entries:
        click.echo(
            f"  recent_snapshots  showing last {min(len(previous_entries), tail)} before this run"
        )
        for entry in previous_entries[-tail:]:
            mayhem = entry.get("mayhem") or {}
            click.echo(
                f"    @{entry.get('captured_at', '?')}  total={entry.get('total', 0)}  "
                f"mayhem={mayhem.get('total', 0)}  patch={mayhem.get('latest_patch') or '-'}  "
                f"patch_games={mayhem.get('latest_patch_games', 0)}"
            )


def _run_seed_opgg_plan_subprocess(
    *,
    sources: tuple[str, ...],
    region: str,
    tiers: tuple[str, ...],
    pages_per_source: int,
    topn_total: int,
    seed_state_file: Path,
    seed_history_file: Path,
    seed_file: Path,
) -> int:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "seed-opgg-plan",
        "--region",
        region,
        "--pages-per-source",
        str(max(1, pages_per_source)),
        "--topn-total",
        str(max(0, topn_total)),
        "--resume",
        "--state-file",
        str(seed_state_file),
        "--history-file",
        str(seed_history_file),
        "--out",
        str(seed_file),
    ]
    for source in sources:
        cmd.extend(["--source", source])
    for tier in tiers:
        cmd.extend(["--tier", tier])

    result = subprocess.run(cmd, cwd=str(Path.cwd()), text=True, check=False)
    return int(result.returncode)


def _launch_snowball_workers_subprocess(
    *,
    db: Path,
    workers: int,
    seed_file: Path,
    target_games: int,
    max_players: int,
    games_per_player: int,
    manual_seed_pending_cap: int,
) -> int:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "snowball-workers",
        "--db",
        str(db),
        "--workers",
        str(max(1, workers)),
        "--seed-riot-id-file",
        str(seed_file),
        "--target-games",
        str(target_games),
        "--max-players",
        str(max_players),
        "--games-per-player",
        str(max(1, games_per_player)),
        "--manual-seed-pending-cap",
        str(max(0, manual_seed_pending_cap)),
    ]
    result = subprocess.run(cmd, cwd=str(Path.cwd()), text=True, check=False)
    return int(result.returncode)


@cli.command("opgg-autorefresh")
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--seed-file", default=Path("data/seeds/opgg_tw.txt"), type=click.Path(path_type=Path),
              show_default=True)
@click.option("--seed-state-file", default=DEFAULT_OPGG_STATE, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--seed-history-file", default=DEFAULT_OPGG_HISTORY, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--region", default="tw", show_default=True)
@click.option("--source", "sources", multiple=True, default=("tier", "level"),
              show_default=True, help="OPGG sources to refresh when crawler is low-yield")
@click.option("--tier", "tiers", multiple=True,
              default=("diamond", "emerald", "platinum", "gold"),
              show_default=True)
@click.option("--workers", default=3, show_default=True, type=int,
              help="Snowball worker count to keep after each refresh")
@click.option("--interval-sec", default=180, show_default=True, type=int,
              help="Seconds between crawler health checks")
@click.option("--cooldown-sec", default=300, show_default=True, type=int,
              help="Minimum seconds between OPGG refresh attempts")
@click.option("--pending-threshold", default=20, show_default=True, type=int,
              help="Refresh when pending+in_progress frontier is at or below this")
@click.option("--done-window", default=50, show_default=True, type=int,
              help="Refresh when this many players were processed without Mayhem growth")
@click.option("--pages-per-source", default=1, show_default=True, type=int,
              help="Small OPGG page window per source per refresh")
@click.option("--topn-total", default=0, show_default=True, type=int,
              help="Max Riot IDs per refresh; 0 means no explicit cap")
@click.option("--target-games", default=50000, show_default=True, type=int)
@click.option("--max-players", default=50000, show_default=True, type=int)
@click.option("--games-per-player", default=4, show_default=True, type=int)
@click.option("--manual-seed-pending-cap", default=40, show_default=True, type=int)
@click.option("--once", is_flag=True, default=False,
              help="Run one health check and refresh/restart if needed")
def opgg_autorefresh(
    db: Path,
    seed_file: Path,
    seed_state_file: Path,
    seed_history_file: Path,
    region: str,
    sources: tuple[str, ...],
    tiers: tuple[str, ...],
    workers: int,
    interval_sec: int,
    cooldown_sec: int,
    pending_threshold: int,
    done_window: int,
    pages_per_source: int,
    topn_total: int,
    target_games: int,
    max_players: int,
    games_per_player: int,
    manual_seed_pending_cap: int,
    once: bool,
) -> None:
    """Watch crawler health and refresh OPGG seeds when the frontier goes low-yield."""
    normalized_sources = _normalize_opgg_sources(sources)
    previous = _collect_status_snapshot(
        db,
        seed_state_file=seed_state_file,
        seed_history_file=seed_history_file,
    )
    last_refresh_at = 0.0

    click.echo(
        f"[opgg-autorefresh] db={db}  workers={workers}  "
        f"sources={list(normalized_sources)}  interval={interval_sec}s"
    )
    try:
        while True:
            snapshot = _collect_status_snapshot(
                db,
                seed_state_file=seed_state_file,
                seed_history_file=seed_history_file,
            )
            frontier = snapshot.get("crawl_frontier") or {}
            pending = int(frontier.get("pending", 0))
            in_progress = int(frontier.get("in_progress", 0))
            open_frontier = pending + in_progress
            done = int(frontier.get("done", 0))
            mayhem = int((snapshot.get("mayhem") or {}).get("total", 0))
            previous_frontier = previous.get("crawl_frontier") or {}
            done_delta = done - int(previous_frontier.get("done", 0))
            mayhem_delta = mayhem - int((previous.get("mayhem") or {}).get("total", 0))
            active_workers = len(snapshot.get("active_workers") or [])

            reasons: list[str] = []
            if active_workers == 0:
                reasons.append("workers=0")
            if open_frontier <= pending_threshold:
                reasons.append(f"frontier={open_frontier}<={pending_threshold}")
            if done_delta >= done_window and mayhem_delta <= 0:
                reasons.append(f"low_yield done+{done_delta} mayhem+{mayhem_delta}")

            now = time.time()
            click.echo(
                f"[opgg-autorefresh] workers={active_workers}  frontier={open_frontier}  "
                f"done_delta={done_delta:+d}  mayhem_delta={mayhem_delta:+d}  "
                f"{'trigger=' + ','.join(reasons) if reasons else 'ok'}"
            )

            if reasons and now - last_refresh_at >= max(0, cooldown_sec):
                seed_file.parent.mkdir(parents=True, exist_ok=True)
                click.echo("[opgg-autorefresh] refreshing OPGG seed file")
                rc = _run_seed_opgg_plan_subprocess(
                    sources=normalized_sources,
                    region=region,
                    tiers=tiers,
                    pages_per_source=pages_per_source,
                    topn_total=topn_total,
                    seed_state_file=seed_state_file,
                    seed_history_file=seed_history_file,
                    seed_file=seed_file,
                )
                if rc == 0:
                    stopped = _stop_active_snowball_workers()
                    click.echo(f"[opgg-autorefresh] stopped snowball workers={stopped}")
                    launch_rc = _launch_snowball_workers_subprocess(
                        db=db,
                        workers=workers,
                        seed_file=seed_file,
                        target_games=target_games,
                        max_players=max_players,
                        games_per_player=games_per_player,
                        manual_seed_pending_cap=manual_seed_pending_cap,
                    )
                    if launch_rc != 0:
                        click.echo(f"[opgg-autorefresh] worker launch failed rc={launch_rc}", err=True)
                    last_refresh_at = now
                else:
                    click.echo(f"[opgg-autorefresh] seed refresh failed rc={rc}", err=True)
                    last_refresh_at = now
            elif reasons:
                remain = max(0, int(cooldown_sec - (now - last_refresh_at)))
                click.echo(f"[opgg-autorefresh] refresh suppressed by cooldown  remaining={remain}s")

            previous = snapshot
            if once:
                break
            time.sleep(max(10, interval_sec))
    except KeyboardInterrupt:
        click.echo("[opgg-autorefresh] stopped")


# ------------------------------------------------------------------- stats ---

@cli.command()
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--out-dir", default=Path("data/stats"), type=click.Path(path_type=Path),
              show_default=True, help="Directory for generated CSV summaries")
@click.option("--queue", multiple=True, type=int, default=(2400,),
              show_default=True, help="Queue IDs to include (repeatable)")
@click.option("--patch-prefix", multiple=True, default=(), show_default=True,
              help="Optional patch prefix filters such as 16.9 or 16.9.772")
def stats(db: Path, out_dir: Path, queue: tuple[int, ...], patch_prefix: tuple[str, ...]) -> None:
    """Generate useful summary tables: hero winrate, augment winrate, and hero x augment."""
    if not db.exists():
        click.echo(f"[error] database not found: {db}", err=True)
        sys.exit(1)

    con = sqlite3.connect(str(db))
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(games)").fetchall()}
    has_participants = "participants_json" in columns
    rows = con.execute(
        """
        SELECT game_id, queue_id, patch, blue_champs, red_champs, blue_wins, participants_json
        FROM games
        ORDER BY created_ms
        """
        if has_participants
        else
        """
        SELECT game_id, queue_id, patch, blue_champs, red_champs, blue_wins, NULL as participants_json
        FROM games
        ORDER BY created_ms
        """
    ).fetchall()
    con.close()

    queue_filter = set(queue)
    hero_games: Counter[int] = Counter()
    hero_wins: Counter[int] = Counter()
    augment_games: Counter[int] = Counter()
    augment_wins: Counter[int] = Counter()
    hero_augment_games: Counter[tuple[int, int]] = Counter()
    hero_augment_wins: Counter[tuple[int, int]] = Counter()

    kept_games = 0
    participant_games = 0
    for _, queue_id, patch, blue_json, red_json, blue_wins, participants_json in rows:
        if queue_filter and queue_id not in queue_filter:
            continue
        if patch_prefix and not any(str(patch).startswith(prefix) for prefix in patch_prefix):
            continue

        kept_games += 1
        blue_ids = json.loads(blue_json)
        red_ids = json.loads(red_json)
        blue_win_int = int(bool(blue_wins))

        for champion_id in blue_ids:
            hero_games[int(champion_id)] += 1
            hero_wins[int(champion_id)] += blue_win_int
        for champion_id in red_ids:
            hero_games[int(champion_id)] += 1
            hero_wins[int(champion_id)] += 1 - blue_win_int

        payload = json.loads(participants_json or "[]")
        if not payload:
            continue
        participant_games += 1
        for participant in payload:
            champion_id = int(participant.get("championId", 0) or 0)
            team_id = int(participant.get("teamId", 0) or 0)
            if champion_id <= 0 or team_id not in (100, 200):
                continue
            player_win = blue_win_int if team_id == 100 else (1 - blue_win_int)
            for augment_id in participant.get("augments") or []:
                augment_id = int(augment_id)
                if augment_id <= 0:
                    continue
                augment_games[augment_id] += 1
                augment_wins[augment_id] += player_win
                hero_augment_games[(champion_id, augment_id)] += 1
                hero_augment_wins[(champion_id, augment_id)] += player_win

    out_dir.mkdir(parents=True, exist_ok=True)

    hero_rows = []
    for champion_id, games_played in hero_games.items():
        wins = hero_wins[champion_id]
        hero_rows.append(
            {
                "champion_id": champion_id,
                "games": games_played,
                "wins": wins,
                "win_rate": wins / games_played,
                "smoothed_win_rate": (wins + 1.0) / (games_played + 2.0),
            }
        )
    _write_csv_rows(
        out_dir / "hero_winrates.csv",
        hero_rows,
        {
            "champion_id": pl.Int64,
            "games": pl.Int64,
            "wins": pl.Int64,
            "win_rate": pl.Float64,
            "bayes_win_rate": pl.Float64,
            "wilson_lb": pl.Float64,
        },
        ["games", "bayes_win_rate"],
    )

    augment_rows = []
    for augment_id, games_played in augment_games.items():
        wins = augment_wins[augment_id]
        augment_rows.append(
            {
                "augment_id": augment_id,
                "games": games_played,
                "wins": wins,
                "win_rate": wins / games_played,
                "smoothed_win_rate": (wins + 1.0) / (games_played + 2.0),
            }
        )
    _write_csv_rows(
        out_dir / "augment_winrates.csv",
        augment_rows,
        {
            "augment_id": pl.Int64,
            "games": pl.Int64,
            "wins": pl.Int64,
            "win_rate": pl.Float64,
            "bayes_win_rate": pl.Float64,
            "wilson_lb": pl.Float64,
        },
        ["games", "bayes_win_rate"],
    )

    hero_augment_rows = []
    for (champion_id, augment_id), games_played in hero_augment_games.items():
        wins = hero_augment_wins[(champion_id, augment_id)]
        hero_augment_rows.append(
            {
                "champion_id": champion_id,
                "augment_id": augment_id,
                "games": games_played,
                "wins": wins,
                "win_rate": wins / games_played,
                "smoothed_win_rate": (wins + 1.0) / (games_played + 2.0),
            }
        )
    _write_csv_rows(
        out_dir / "hero_augment_winrates.csv",
        hero_augment_rows,
        {
            "champion_id": pl.Int64,
            "augment_id": pl.Int64,
            "games": pl.Int64,
            "wins": pl.Int64,
            "win_rate": pl.Float64,
            "bayes_win_rate": pl.Float64,
            "wilson_lb": pl.Float64,
        },
        ["games", "bayes_win_rate"],
    )

    click.echo(
        f"[stats] wrote {out_dir}  games={kept_games}  "
        f"games_with_participants={participant_games}  "
        f"heroes={len(hero_rows)}  augments={len(augment_rows)}  "
        f"hero_x_augment={len(hero_augment_rows)}"
    )


@cli.command()
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--queue", multiple=True, type=int, default=(2400,),
              show_default=True, help="Queue IDs to include (repeatable)")
@click.option("--patch-prefix", multiple=True, default=(), show_default=True,
              help="Optional patch prefix filters such as 16.9 or 16.9.772")
@click.option("--topn", default=20, show_default=True, type=int,
              help="How many ranking rows to print")
@click.option("--bottomn", default=10, show_default=True, type=int,
              help="How many bottom-ranking rows to print")
@click.option("--min-games", default=30, show_default=True, type=int,
              help="Minimum games threshold for hero ranking")
@click.option("--prior-strength", default=50.0, show_default=True, type=float,
              help="Empirical-Bayes prior strength k for Bayes win-rate shrinkage")
@click.option("--show-augments/--hide-augments", default=False, show_default=True,
              help="Also print top augment rankings when participants_json is available")
@click.option("--show-hero-augments/--hide-hero-augments", default=False, show_default=True,
              help="Also print top hero x augment pair rankings when participants_json is available")
@click.option("--show-hero-augment-bottom/--hide-hero-augment-bottom", default=False, show_default=True,
              help="Also print bottom hero x augment pair rankings when participants_json is available")
@click.option("--pair-min-games", default=10, show_default=True, type=int,
              help="Minimum games threshold for hero x augment pair ranking")
def dataset(
    db: Path,
    queue: tuple[int, ...],
    patch_prefix: tuple[str, ...],
    topn: int,
    bottomn: int,
    min_games: int,
    prior_strength: float,
    show_augments: bool,
    show_hero_augments: bool,
    show_hero_augment_bottom: bool,
    pair_min_games: int,
) -> None:
    """Print dataset summary and current hero winrate rankings."""
    if not db.exists():
        click.echo(f"[error] database not found: {db}", err=True)
        sys.exit(1)

    rows, _ = _load_game_rows(db)
    kept_games, participant_games, hero_rows, augment_rows, hero_augment_rows = _aggregate_stats(
        rows, queue=queue, patch_prefix=patch_prefix
    )
    champion_names = _load_champion_name_map()
    augment_names = _load_augment_name_map()

    total_player_games = sum(int(row["games"]) for row in hero_rows)
    total_player_wins = sum(int(row["wins"]) for row in hero_rows)
    global_wr = (total_player_wins / total_player_games) if total_player_games > 0 else 0.5
    hero_rows = _decorate_rate_rows(
        [{"champion_id": row["champion_id"], "games": row["games"], "wins": row["wins"]} for row in hero_rows],
        key_field="champion_id",
        global_wr=global_wr,
        prior_strength=prior_strength,
    )
    augment_rows = _decorate_rate_rows(
        [{"augment_id": row["augment_id"], "games": row["games"], "wins": row["wins"]} for row in augment_rows],
        key_field="augment_id",
        global_wr=global_wr,
        prior_strength=prior_strength,
    )

    hero_df = pl.DataFrame(hero_rows) if hero_rows else pl.DataFrame(
        schema={
            "champion_id": pl.Int64,
            "games": pl.Int64,
            "wins": pl.Int64,
            "win_rate": pl.Float64,
            "bayes_win_rate": pl.Float64,
            "wilson_lb": pl.Float64,
        }
    )
    ranked_df = (
        hero_df
        .filter(pl.col("games") >= min_games)
        .sort(["bayes_win_rate", "games"], descending=[True, True])
        .head(topn)
    )
    bottom_df = (
        hero_df
        .filter(pl.col("games") >= min_games)
        .sort(["bayes_win_rate", "games"], descending=[False, True])
        .head(bottomn)
    )

    click.echo(
        f"[dataset] games={kept_games}  games_with_participants={participant_games}  "
        f"heroes={len(hero_rows)}  augments={len(augment_rows)}  "
        f"hero_x_augment={len(hero_augment_rows)}  queues={list(queue)}  "
        f"patches={list(patch_prefix) if patch_prefix else ['all']}  "
        f"global_wr={global_wr:.4f}  prior_k={prior_strength:.1f}"
    )
    click.echo(f"[dataset] hero ranking  top={topn}  min_games={min_games}")
    if ranked_df.height == 0:
        click.echo("  no heroes pass the min-games threshold")
    else:
        click.echo("  rank  champion_id  champion_name         games  wins  win_rate  bayes_wr  wilson_lb")
        for idx, row in enumerate(ranked_df.iter_rows(named=True), start=1):
            champion_name = champion_names.get(int(row["champion_id"]), "?")
            click.echo(
                f"  {idx:>4}  {row['champion_id']:>11}  {champion_name:<20.20}  "
                f"{row['games']:>5}  {row['wins']:>4}  {row['win_rate']:.4f}  "
                f"{row['bayes_win_rate']:.4f}  {row['wilson_lb']:.4f}"
            )
    click.echo(f"[dataset] hero bottom  bottom={bottomn}  min_games={min_games}")
    if bottom_df.height == 0:
        click.echo("  no heroes pass the min-games threshold")
    else:
        click.echo("  rank  champion_id  champion_name         games  wins  win_rate  bayes_wr  wilson_lb")
        for idx, row in enumerate(bottom_df.iter_rows(named=True), start=1):
            champion_name = champion_names.get(int(row["champion_id"]), "?")
            click.echo(
                f"  {idx:>4}  {row['champion_id']:>11}  {champion_name:<20.20}  "
                f"{row['games']:>5}  {row['wins']:>4}  {row['win_rate']:.4f}  "
                f"{row['bayes_win_rate']:.4f}  {row['wilson_lb']:.4f}"
            )

    if show_augments:
        augment_df = pl.DataFrame(augment_rows) if augment_rows else pl.DataFrame(
            schema={
                "augment_id": pl.Int64,
                "games": pl.Int64,
                "wins": pl.Int64,
                "win_rate": pl.Float64,
                "bayes_win_rate": pl.Float64,
                "wilson_lb": pl.Float64,
            }
        )
        augment_ranked = (
            augment_df
            .filter(pl.col("games") >= min_games)
            .sort(["bayes_win_rate", "games"], descending=[True, True])
            .head(topn)
        )
        augment_bottom = (
            augment_df
            .filter(pl.col("games") >= min_games)
            .sort(["bayes_win_rate", "games"], descending=[False, True])
            .head(bottomn)
        )
        click.echo(f"[dataset] augment top  top={topn}  min_games={min_games}")
        if augment_ranked.height == 0:
            click.echo("  no augments pass the min-games threshold")
        else:
            click.echo("  rank  augment_id  augment_name           games  wins  win_rate  bayes_wr  wilson_lb")
            for idx, row in enumerate(augment_ranked.iter_rows(named=True), start=1):
                augment_name = augment_names.get(int(row["augment_id"]), "?")
                click.echo(
                    f"  {idx:>4}  {row['augment_id']:>10}  {augment_name:<20.20}  {row['games']:>5}  "
                    f"{row['wins']:>4}  {row['win_rate']:.4f}  "
                    f"{row['bayes_win_rate']:.4f}  {row['wilson_lb']:.4f}"
                )
        click.echo(f"[dataset] augment bottom  bottom={bottomn}  min_games={min_games}")
        if augment_bottom.height == 0:
            click.echo("  no augments pass the min-games threshold")
        else:
            click.echo("  rank  augment_id  augment_name           games  wins  win_rate  bayes_wr  wilson_lb")
            for idx, row in enumerate(augment_bottom.iter_rows(named=True), start=1):
                augment_name = augment_names.get(int(row["augment_id"]), "?")
                click.echo(
                    f"  {idx:>4}  {row['augment_id']:>10}  {augment_name:<20.20}  {row['games']:>5}  "
                    f"{row['wins']:>4}  {row['win_rate']:.4f}  "
                    f"{row['bayes_win_rate']:.4f}  {row['wilson_lb']:.4f}"
                )

    if show_hero_augments:
        pair_df = pl.DataFrame(hero_augment_rows) if hero_augment_rows else pl.DataFrame(
            schema={
                "champion_id": pl.Int64,
                "augment_id": pl.Int64,
                "games": pl.Int64,
                "wins": pl.Int64,
                "win_rate": pl.Float64,
                "bayes_win_rate": pl.Float64,
                "wilson_lb": pl.Float64,
            }
        )
        pair_ranked = (
            pair_df
            .filter(pl.col("games") >= pair_min_games)
            .sort(["bayes_win_rate", "games"], descending=[True, True])
            .head(topn)
        )
        pair_bottom = (
            pair_df
            .filter(pl.col("games") >= pair_min_games)
            .sort(["bayes_win_rate", "games"], descending=[False, True])
            .head(bottomn)
        )
        click.echo(f"[dataset] hero x augment top  top={topn}  min_games={pair_min_games}")
        if pair_ranked.height == 0:
            click.echo("  no hero x augment pairs pass the min-games threshold")
        else:
            click.echo("  rank  champion_name         augment_name           games  wins  win_rate  bayes_wr  wilson_lb")
            for idx, row in enumerate(pair_ranked.iter_rows(named=True), start=1):
                champion_name = champion_names.get(int(row["champion_id"]), "?")
                augment_name = augment_names.get(int(row["augment_id"]), "?")
                click.echo(
                    f"  {idx:>4}  {champion_name:<20.20}  {augment_name:<20.20}  {row['games']:>5}  "
                    f"{row['wins']:>4}  {row['win_rate']:.4f}  {row['bayes_win_rate']:.4f}  {row['wilson_lb']:.4f}"
                )
        if show_hero_augment_bottom:
            click.echo(f"[dataset] hero x augment bottom  bottom={bottomn}  min_games={pair_min_games}")
            if pair_bottom.height == 0:
                click.echo("  no hero x augment pairs pass the min-games threshold")
            else:
                click.echo("  rank  champion_name         augment_name           games  wins  win_rate  bayes_wr  wilson_lb")
                for idx, row in enumerate(pair_bottom.iter_rows(named=True), start=1):
                    champion_name = champion_names.get(int(row["champion_id"]), "?")
                    augment_name = augment_names.get(int(row["augment_id"]), "?")
                    click.echo(
                        f"  {idx:>4}  {champion_name:<20.20}  {augment_name:<20.20}  {row['games']:>5}  "
                        f"{row['wins']:>4}  {row['win_rate']:.4f}  {row['bayes_win_rate']:.4f}  {row['wilson_lb']:.4f}"
                    )


# ------------------------------------------------------------------ status ---

@cli.command()
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True)
def status(db: Path) -> None:
    """Show a summary of what's been captured so far."""
    if not db.exists():
        click.echo(f"[status] no database at {db}")
        return

    con = sqlite3.connect(str(db))
    total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    by_q = con.execute(
        "SELECT queue_id, COUNT(*), AVG(blue_wins), MIN(patch), MAX(patch) "
        "FROM games GROUP BY queue_id"
    ).fetchall()
    recent = con.execute(
        "SELECT game_id, queue_id, patch, blue_wins, duration_sec, captured_at "
        "FROM games ORDER BY created_ms DESC LIMIT 5"
    ).fetchall()
    has_crawl_queue = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='crawl_queue'"
    ).fetchone()[0] > 0
    has_crawl_seen = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='crawl_seen'"
    ).fetchone()[0] > 0
    has_crawl_players = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='crawl_players'"
    ).fetchone()[0] > 0
    crawl_status = []
    crawl_sources = []
    if has_crawl_queue and has_crawl_seen:
        crawl_status = con.execute(
            "SELECT status, COUNT(*) FROM crawl_queue GROUP BY status ORDER BY status"
        ).fetchall()
        crawl_sources = con.execute(
            "SELECT source, COUNT(*) FROM crawl_seen GROUP BY source ORDER BY MIN(priority), source"
        ).fetchall()
    elif has_crawl_players:
        crawl_status = con.execute(
            "SELECT status, COUNT(*) FROM crawl_players GROUP BY status ORDER BY status"
        ).fetchall()
        crawl_sources = con.execute(
            "SELECT source, COUNT(*) FROM crawl_players GROUP BY source ORDER BY MIN(priority), source"
        ).fetchall()
    con.close()

    click.echo(f"[status] {db}  total={total}")
    for queue_id, count, wr, min_p, max_p in by_q:
        label = "Mayhem" if queue_id == 2400 else ("ARAM" if queue_id == 450 else f"q{queue_id}")
        click.echo(f"  {label:8s} ({queue_id}): {count:4d} games  "
                   f"blue_wr={wr:.3f}  patches {min_p}…{max_p}")
    if recent:
        click.echo("\n  5 most recent:")
        for gid, qid, patch, bw, dur, cap in recent:
            label = "Mayhem" if qid == 2400 else "ARAM"
            click.echo(f"    {gid}  {label:<6}  {patch}  {'win' if bw else 'loss'}  "
                       f"{dur}s  @{cap[:16]}")
    if crawl_status:
        click.echo("\n  crawl frontier:")
        for status_name, count in crawl_status:
            click.echo(f"    {status_name:<7} {count}")
        click.echo("  crawl sources:")
        for source, count in crawl_sources:
            click.echo(f"    {source:<7} {count}")


@cli.command("family-stats")
@click.option("--db", default=DEFAULT_DB, type=click.Path(path_type=Path),
              show_default=True)
@click.option("--queue", multiple=True, type=int, default=(),
              help="Restrict captured-game tally to these queueIds (default: all)")
def family_stats(db: Path, queue: tuple[int, ...]) -> None:
    """Per-seed_family ROI: transitive frontier footprint vs captured-game yield.

    Compares two complementary lenses: (1) crawl_seen — how many puuids the
    family pulled in and how much downstream new_games_found credit those
    puuids accumulated; (2) games — how many distinct captured games the
    family is responsible for, with blue_wr.

    'legacy_match' rows are pre-attribution captures (no parent linkage); they
    are reported but excluded from comparison decisions about live seeds.
    """
    if not db.exists():
        click.echo(f"[family-stats] no database at {db}")
        return

    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(crawl_seen)").fetchall()}
    if "seed_family" not in cols:
        click.echo(
            "[family-stats] crawl_seen.seed_family missing — run snowball once "
            "to trigger the migration."
        )
        con.close()
        return

    seen_rows = con.execute(
        """
        SELECT seed_family,
               COUNT(*) AS puuids,
               SUM(CASE WHEN processed=1 THEN 1 ELSE 0 END) AS done_puuids,
               SUM(CASE WHEN new_games_found > 0 THEN 1 ELSE 0 END) AS productive,
               COALESCE(SUM(new_games_found), 0) AS total_new_games
        FROM crawl_seen
        GROUP BY seed_family
        """
    ).fetchall()

    queue_filter_sql = ""
    queue_params: tuple = ()
    if queue:
        placeholders = ",".join("?" for _ in queue)
        queue_filter_sql = f"WHERE queue_id IN ({placeholders})"
        queue_params = tuple(int(q) for q in queue)
    games_rows = con.execute(
        f"""
        SELECT seed_family,
               COUNT(*) AS games,
               ROUND(AVG(blue_wins), 3) AS blue_wr
        FROM games
        {queue_filter_sql}
        GROUP BY seed_family
        """,
        queue_params,
    ).fetchall()
    games_by_q = con.execute(
        f"""
        SELECT seed_family, queue_id, COUNT(*) AS games,
               ROUND(AVG(blue_wins), 3) AS blue_wr
        FROM games
        {queue_filter_sql}
        GROUP BY seed_family, queue_id
        """,
        queue_params,
    ).fetchall()
    con.close()

    seen_by_fam = {row[0]: row for row in seen_rows}
    games_by_fam = {row[0]: row for row in games_rows}
    families = sorted(
        set(seen_by_fam) | set(games_by_fam),
        key=lambda f: -((games_by_fam.get(f) or (None, 0, None))[1]),
    )
    queue_label = (
        f"queue={','.join(str(q) for q in queue)}" if queue else "queue=all"
    )
    click.echo(f"[family-stats] {db}  {queue_label}")
    click.echo(
        f"  {'family':<18s} {'puuids':>7s} {'done':>6s} {'productive':>10s} "
        f"{'transitive_yield':>17s} {'captured':>8s} {'blue_wr':>8s}"
    )
    for fam in families:
        s = seen_by_fam.get(fam, (fam, 0, 0, 0, 0))
        g = games_by_fam.get(fam, (fam, 0, None))
        _, puuids, done_puuids, productive, total_new = s
        _, captured, blue_wr = g
        wr_text = f"{blue_wr:.3f}" if blue_wr is not None else "-"
        click.echo(
            f"  {fam:<18s} {puuids:>7d} {done_puuids or 0:>6d} {productive or 0:>10d} "
            f"{total_new or 0:>17d} {captured or 0:>8d} {wr_text:>8s}"
        )

    if games_by_q:
        click.echo("\n  per-queue captured-game breakdown:")
        last_fam = ""
        for fam, qid, games, wr in sorted(games_by_q, key=lambda r: (-r[2], r[0])):
            label = "Mayhem" if qid == 2400 else ("ARAM" if qid == 450 else f"q{qid}")
            wr_text = f"{wr:.3f}" if wr is not None else "-"
            prefix = fam if fam != last_fam else ""
            click.echo(f"    {prefix:<18s} {label:<6s} ({qid}): {games:>5d} games  blue_wr={wr_text}")
            last_fam = fam


@cli.command()
@click.option("--lr-model", required=True,
              type=click.Path(exists=True, path_type=Path, dir_okay=False),
              help="Path to lr_model.pkl (sklearn LogisticRegression).")
@click.option("--vocab", required=True,
              type=click.Path(exists=True, path_type=Path, dir_okay=False),
              help="Path to tier2_checkpoint.pt or champ_to_idx.json — used for champion vocab.")
@click.option("--pair-stats", default=Path("models/pair_synergy_16_10.json"),
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to pair synergy JSON from scripts/build_pair_stats.py.")
@click.option("--poll-interval", default=1.0, show_default=True, type=float,
              help="Seconds between LCU polls while in ChampSelect.")
@click.option("--verbose", is_flag=True, default=False,
              help="Print per-poll diagnostic info (phase + session presence).")
def recommend(
    lr_model: Path,
    vocab: Path,
    pair_stats: Path,
    poll_interval: float,
    verbose: bool,
) -> None:
    """Real-time bench-swap recommendations during ARAM champ select.

    Uses anchor-conditional pair synergy as the primary signal, blended with
    the LR baseline as a universal champion-strength prior.  Absolute win
    probabilities are LR point estimates assuming an "average" opponent.

    Run BEFORE you queue:
        python scripts/lcu_collector.py recommend \\
            --lr-model models/tier2_mayhem/lr_model.pkl \\
            --vocab    models/tier2_mayhem/tier2_checkpoint.pt \\
            --pair-stats models/pair_synergy_16_10.json
    """
    # Lazy imports keep the CLI startup fast for other subcommands.
    from aram_nn.lcu.client import (
        LCUClient, get_champion_summary, get_champ_select_session, get_gameflow_phase,
    )
    from aram_nn.lcu.process import get_credentials
    from aram_nn.recommend import (
        load_lr, parse_session, session_state_hash, suggest_for_cell,
    )
    from aram_nn.pair_synergy import load_pair_synergy

    creds = get_credentials()
    if not creds:
        click.echo("[error] League client not running (no LCU credentials).")
        raise SystemExit(1)

    click.echo(f"[recommend] loading model from {lr_model}")
    model = load_lr(lr_model, vocab)
    click.echo(f"[recommend] vocab covers {model.n_champs} champions")
    pair_model = None
    if pair_stats.exists():
        pair_model = load_pair_synergy(pair_stats)
        click.echo(
            f"[recommend] pair synergy rows={len(pair_model.rows):,} "
            f"patch={pair_model.patch_prefix} min_pair={pair_model.min_pair}"
        )
    else:
        click.echo(
            f"[recommend] WARN: pair stats not found at {pair_stats}; "
            "falling back to 30% LR score only"
        )

    last_hash: tuple | None = None
    last_phase: str | None = None
    id_to_name: dict[int, str] = {}

    try:
        with LCUClient(creds) as lcu:
            # Build the championId → name lookup once.  LCU static data is stable
            # across the session; no need to re-fetch on every poll.
            for entry in get_champion_summary(lcu):
                cid = entry.get("id")
                name = entry.get("name") or entry.get("alias")
                if isinstance(cid, int) and isinstance(name, str) and cid > 0:
                    id_to_name[cid] = name

            # Gate directly on the champ-select session endpoint rather than
            # /lol-gameflow/v1/phase.  Some League client versions don't report
            # "ChampSelect" for ARAM via the gameflow phase string, but the
            # session endpoint always returns a payload during champ select
            # and 404s outside it — making it the more reliable signal.
            while True:
                session = get_champ_select_session(lcu)
                parsed = parse_session(session) if session else None

                if parsed is None:
                    phase = get_gameflow_phase(lcu)
                    if verbose or phase != last_phase:
                        click.echo(
                            f"[recommend] idle  phase={phase}  "
                            f"session={'yes(incomplete)' if session else 'no'}"
                        )
                        last_phase = phase
                        last_hash = None
                    time.sleep(max(poll_interval, 2.0))
                    continue
                last_phase = "ChampSelect"

                state = session_state_hash(parsed)
                if state == last_hash:
                    time.sleep(poll_interval)
                    continue
                last_hash = state

                suggestions = suggest_for_cell(
                    parsed.my_team_ids,
                    parsed.my_current_id,
                    parsed.bench_ids,
                    model,
                    pair_model,
                )

                # Clear screen + home cursor for in-place refresh.
                click.echo("\033[2J\033[H", nl=False)
                cur_name = id_to_name.get(parsed.my_current_id, f"#{parsed.my_current_id}")
                click.echo(f"[champ select] cell {parsed.my_cell_id}   current: {cur_name}")
                click.echo("               (score = 70% synergy + 30% LR; P(win) assumes average opponent)\n")
                click.echo(f"  {'score':>7}  {'syn':>7}  {'LR':>7}  {'P(win)':>7}  candidate")
                click.echo("  " + "-" * 62)
                for s in suggestions:
                    name = id_to_name.get(s.champion_id, f"#{s.champion_id}")
                    tag = "keep" if s.source == "keep" else "bench"
                    if not s.is_known:
                        click.echo(
                            f"  {'  n/a':>7}  {'  n/a':>7}  {'  n/a':>7}  "
                            f"{'  n/a':>7}  {name} ({tag}, not in vocab)"
                        )
                        continue
                    score_pp = s.score * 100.0
                    syn_pp = s.synergy_delta * 100.0
                    lr_pp = s.prob_delta_lr * 100.0
                    prob_pp = s.win_prob * 100.0
                    score_str = f"{score_pp:+.1f}%" if s.source != "keep" else "  —  "
                    coverage = f"{s.anchors_covered}/4"
                    click.echo(
                        f"  {score_str:>7}  {syn_pp:+6.1f}%  {lr_pp:+6.1f}%  "
                        f"{prob_pp:5.1f}%   {name} ({tag}, pairs {coverage})"
                    )

                time.sleep(poll_interval)
    except KeyboardInterrupt:
        click.echo("\n[recommend] stopped.")


if __name__ == "__main__":
    cli()
