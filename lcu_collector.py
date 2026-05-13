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
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import click
import polars as pl


DEFAULT_DB = Path("data/lcu/games.db")


@click.group()
def cli() -> None:
    """LCU local game-data collector for ARAM / Mayhem."""


def _write_csv_rows(path: Path, rows: list[dict], schema: dict[str, pl.DataType], sort_by: list[str]) -> None:
    if rows:
        pl.DataFrame(rows).sort(sort_by, descending=[True] * len(sort_by)).write_csv(path)
    else:
        pl.DataFrame(schema=schema).write_csv(path)


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
        "--max-depth",
        str(max_depth),
    ]

    for qid in queue:
        args.extend(["--queue", str(qid)])
    for queue_type in apex_queue:
        args.extend(["--apex-queue", str(queue_type)])
    for tier in apex_tier:
        args.extend(["--apex-tier", str(tier)])

    args.append("--seed-self" if seed_self else "--no-seed-self")
    args.append("--seed-friends" if seed_friends else "--no-seed-friends")
    args.append("--seed-ladder" if seed_ladder else "--no-seed-ladder")
    args.append("--seed-apex" if seed_apex else "--no-seed-apex")
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
    click.echo("  stop:    kill all python processes matching 'lcu_collector.py snowball' (Ctrl-C each terminal, or use your OS process manager)")


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
            label = "Mayhem" if qid == 2400 else ("ARAM" if qid == 450 else f"q{qid}")
            click.echo(f"    {gid}  {label:<6}  {patch}  {'win' if bw else 'loss'}  "
                       f"{dur}s  @{cap[:16]}")
    if crawl_status:
        click.echo("\n  crawl frontier:")
        for status_name, count in crawl_status:
            click.echo(f"    {status_name:<7} {count}")
        click.echo("  crawl sources:")
        for source, count in crawl_sources:
            click.echo(f"    {source:<7} {count}")


if __name__ == "__main__":
    cli()
