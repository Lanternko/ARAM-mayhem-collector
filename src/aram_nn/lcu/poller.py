"""LCU poller — EoG-first Mayhem capture.

Primary path: poll /lol-end-of-game/v1/eog-stats-block every 5 s.
The EoG block contains all 10 champion IDs (integers) + isWinningTeam,
so no champion name mapping or in-game port-2999 polling is needed.

Fallback: during InProgress, remember the game_id from gameflow session
in case the user dismisses the EoG screen before we catch it.

LCU WebSocket events are used as wake-up signals only.  The main loop still
does the REST reads and SQLite writes, so a stuck event stream falls back to
normal polling.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, SimpleQueue

from .client import (
    LCUClient,
    get_current_summoner,
    get_eog_stats,
    get_game_detail,
    get_gameflow_phase,
    get_gameflow_session,
    get_match_history,
)
from .events import LCUApiEvent, LCUEventListener
from .process import get_credentials

DEFAULT_QUEUES = {450, 2400}

_MODE_TO_QUEUE = {"KIWI": 2400, "ARAM": 450}

_CREATE_SQL = """
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

_GAMEFLOW_PHASE_URI = "/lol-gameflow/v1/gameflow-phase"
_GAMEFLOW_SESSION_URI = "/lol-gameflow/v1/session"
_EOG_EVENT_PREFIXES = ("/lol-end-of-game/", "/lol-pre-end-of-game/")

_ITEM_KEYS = tuple(f"item{idx}" for idx in range(7))

_PARTICIPANT_STAT_ALIASES: dict[str, tuple[str, ...]] = {
    "gold_earned": ("goldEarned",),
    "gold_spent": ("goldSpent",),
    "champ_level": ("champLevel",),
    "kills": ("kills",),
    "deaths": ("deaths",),
    "assists": ("assists",),
    "largest_killing_spree": ("largestKillingSpree",),
    "largest_multi_kill": ("largestMultiKill",),
    "first_blood_kill": ("firstBloodKill",),
    "first_blood_assist": ("firstBloodAssist",),
    "total_minions_killed": ("totalMinionsKilled",),
    "neutral_minions_killed": ("neutralMinionsKilled",),
    "total_damage_dealt_to_champions": ("totalDamageDealtToChampions",),
    "physical_damage_dealt_to_champions": ("physicalDamageDealtToChampions",),
    "magic_damage_dealt_to_champions": ("magicDamageDealtToChampions",),
    "true_damage_dealt_to_champions": ("trueDamageDealtToChampions",),
    "total_damage_dealt": ("totalDamageDealt",),
    "physical_damage_dealt": ("physicalDamageDealt",),
    "magic_damage_dealt": ("magicDamageDealt",),
    "true_damage_dealt": ("trueDamageDealt",),
    "largest_critical_strike": ("largestCriticalStrike",),
    "damage_dealt_to_turrets": ("damageDealtToTurrets",),
    "damage_dealt_to_objectives": ("damageDealtToObjectives",),
    "total_damage_taken": ("totalDamageTaken",),
    "physical_damage_taken": ("physicalDamageTaken",),
    "magic_damage_taken": ("magicDamageTaken", "magicalDamageTaken"),
    "true_damage_taken": ("trueDamageTaken",),
    "damage_self_mitigated": ("damageSelfMitigated",),
    "crowd_control_score": ("crowdControlScore",),
    "time_ccing_others": ("timeCCingOthers",),
    "total_time_cc_dealt": ("totalTimeCCDealt", "totalTimeCrowdControlDealt"),
    "total_heal": ("totalHeal",),
    "total_heals_on_teammates": ("totalHealsOnTeammates", "totalHealOnTeammates"),
    "total_units_healed": ("totalUnitsHealed",),
    "total_damage_shielded_on_teammates": (
        "totalDamageShieldedOnTeammates",
        "damageShieldedOnTeammates",
    ),
    "effective_heal_and_shielding": ("effectiveHealAndShielding",),
    "turret_kills": ("turretKills",),
    "inhibitor_kills": ("inhibitorKills", "inhibKills"),
}


@dataclass
class _CollectorSignals:
    phase: str | None = None
    game_id: str | None = None
    should_fetch_eog: bool = False
    should_fetch_session: bool = False


# ---------- Parsing ----------

def _extract_augments(stats: dict) -> list[int]:
    augments: list[int] = []
    for idx in range(1, 7):
        value = _to_int(stats.get(f"playerAugment{idx}", 0)) or 0
        if value > 0:
            augments.append(value)
    return augments


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _lookup_raw_value(sources: list[dict], aliases: tuple[str, ...]) -> object | None:
    normalized_aliases = {_norm_key(alias) for alias in aliases}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for alias in aliases:
            if alias in source and source[alias] is not None:
                return source[alias]
        normalized_source = {_norm_key(str(key)): value for key, value in source.items()}
        for alias in normalized_aliases:
            if alias in normalized_source and normalized_source[alias] is not None:
                return normalized_source[alias]
    return None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _extract_item_slots(participant: dict, stats: dict) -> list[int]:
    slots: list[int] = []
    for key in _ITEM_KEYS:
        item_id = _to_int(_lookup_raw_value([stats, participant], (key,)))
        slots.append(item_id if item_id is not None and item_id > 0 else 0)
    return slots


def _extract_selected_stats(participant: dict, stats: dict, challenges: dict) -> dict[str, int]:
    selected: dict[str, int] = {}
    for out_key, aliases in _PARTICIPANT_STAT_ALIASES.items():
        value = _to_int(_lookup_raw_value([stats, challenges, participant], aliases))
        if value is not None:
            selected[out_key] = value
    return selected


def _build_participant_record(team_id: int, champion_id: int, raw: dict) -> dict:
    stats_raw = raw.get("stats") or {}
    stats = stats_raw if isinstance(stats_raw, dict) else {}
    challenges_raw = raw.get("challenges") or {}
    challenges = challenges_raw if isinstance(challenges_raw, dict) else {}
    item_slots = _extract_item_slots(raw, stats)
    record = {
        "teamId": int(team_id),
        "championId": int(champion_id),
        "augments": _extract_augments(stats),
    }

    items = [item_id for item_id in item_slots if item_id > 0]
    if items:
        record["items"] = items
        record["itemSlots"] = item_slots

    selected_stats = _extract_selected_stats(raw, stats, challenges)
    if selected_stats:
        record["stats"] = selected_stats

    return record


def _participants_payload_has_postgame_stats(payload: object) -> bool:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "[]")
        except Exception:
            return False
    if not isinstance(payload, list):
        return False
    for participant in payload:
        if not isinstance(participant, dict):
            continue
        if participant.get("stats"):
            return True
    return False


def _build_participant_payload(participants: list[dict]) -> list[dict]:
    payload: list[dict] = []
    for participant in participants:
        team_id = participant.get("teamId")
        champion_id = participant.get("championId")
        if team_id not in (100, 200) or champion_id is None:
            continue
        payload.append(_build_participant_record(int(team_id), int(champion_id), participant))
    payload.sort(key=lambda row: (row["teamId"], row["championId"]))
    return payload


def _ensure_games_schema(con: sqlite3.Connection) -> None:
    con.execute(_CREATE_SQL)
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(games)").fetchall()}
    if "participants_json" not in columns:
        con.execute("ALTER TABLE games ADD COLUMN participants_json TEXT")
    con.commit()


def _extract_session_game_id(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    game_data = data.get("gameData")
    if not isinstance(game_data, dict):
        return None
    game_id = game_data.get("gameId")
    if game_id is None:
        return None
    value = str(game_id)
    return value if value else None


def _drain_lcu_events(event_queue: SimpleQueue[LCUApiEvent]) -> _CollectorSignals:
    signals = _CollectorSignals()
    while True:
        try:
            event = event_queue.get_nowait()
        except Empty:
            break

        if event.uri == _GAMEFLOW_PHASE_URI and isinstance(event.data, str):
            signals.phase = event.data
            if event.data == "InProgress":
                signals.should_fetch_session = True
        elif event.uri == _GAMEFLOW_SESSION_URI:
            signals.should_fetch_session = True
            game_id = _extract_session_game_id(event.data)
            if game_id:
                signals.game_id = game_id
            if isinstance(event.data, dict):
                phase = event.data.get("phase")
                if isinstance(phase, str):
                    signals.phase = phase
        elif event.uri.startswith(_EOG_EVENT_PREFIXES):
            signals.should_fetch_eog = True

    return signals


def _parse_eog_block(eog: dict, target_queues: set[int], patch: str) -> dict | None:
    """Parse the EoG stats block into a saveable record.

    EoG gives us integer championIds directly — no name mapping needed.
    """
    game_id = str(eog.get("gameId", ""))
    if not game_id:
        return None

    mode = eog.get("gameMode", "")
    queue_id = _MODE_TO_QUEUE.get(mode, -1)
    if queue_id not in target_queues:
        return None

    duration = int(eog.get("gameLength", 0))
    if duration < 300:
        return None

    teams = eog.get("teams") or []
    if len(teams) != 2:
        return None

    blue_champs: list[int] = []
    red_champs:  list[int] = []
    blue_wins: int | None = None
    payload: list[dict] = []

    for team in teams:
        tid     = team.get("teamId")
        winning = bool(team.get("isWinningTeam", False))
        players = team.get("players") or []
        if len(players) != 5 or tid not in (100, 200):
            return None
        champs = sorted(int(p["championId"]) for p in players if p.get("championId") is not None)
        if len(champs) != 5:
            return None
        for player in players:
            champion_id = player.get("championId")
            if champion_id is None:
                continue
            payload.append(_build_participant_record(int(tid), int(champion_id), player))
        if tid == 100:
            blue_champs = champs
            blue_wins   = 1 if winning else 0
        else:
            red_champs = champs

    if not blue_champs or not red_champs or blue_wins is None:
        return None

    created_ms = int(eog.get("endOfGameTimestamp", 0)) - duration * 1000

    return {
        "game_id":     game_id,
        "queue_id":    queue_id,
        "patch":       patch,
        "blue_champs": blue_champs,
        "red_champs":  red_champs,
        "blue_wins":   blue_wins,
        "duration_sec": duration,
        "created_ms":  created_ms,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "participants": sorted(payload, key=lambda row: (row["teamId"], row["championId"])),
    }


def _parse_game_detail(game: dict, target_queues: set[int]) -> dict | None:
    """Parse a /lol-match-history/v1/games/{id} response (all 10 participants)."""
    game_id = str(game.get("gameId", ""))
    if not game_id:
        return None

    queue_id = game.get("queueId", -1)
    if queue_id not in target_queues:
        queue_id = _MODE_TO_QUEUE.get(game.get("gameMode", ""), -1)
    if queue_id not in target_queues:
        return None

    duration = int(game.get("gameDuration", 0))
    if duration < 300:
        return None

    participants = game.get("participants") or []
    if len(participants) != 10:
        return None

    blue_champs = sorted(int(p["championId"]) for p in participants if p.get("teamId") == 100)
    red_champs  = sorted(int(p["championId"]) for p in participants if p.get("teamId") == 200)
    if len(blue_champs) != 5 or len(red_champs) != 5:
        return None

    blue_wins: int | None = None
    for team in (game.get("teams") or []):
        if team.get("teamId") == 100:
            w = team.get("win")
            if isinstance(w, bool):
                blue_wins = 1 if w else 0
            elif isinstance(w, str):
                blue_wins = 1 if w.lower() == "win" else 0
            break
    if blue_wins is None:
        for p in participants:
            if p.get("teamId") == 100:
                w = (p.get("stats") or {}).get("win")
                if w is not None:
                    blue_wins = 1 if w else 0
                    break
    if blue_wins is None:
        return None

    ver = game.get("gameVersion", "")
    vparts = ver.split(".")
    patch = ".".join(vparts[:3]) if len(vparts) >= 3 else (ver or "unknown")

    return {
        "game_id":      game_id,
        "queue_id":     queue_id,
        "patch":        patch,
        "blue_champs":  blue_champs,
        "red_champs":   red_champs,
        "blue_wins":    blue_wins,
        "duration_sec": duration,
        "created_ms":   int(game.get("gameCreation", 0)),
        "captured_at":  datetime.now(timezone.utc).isoformat(),
        "participants": _build_participant_payload(participants),
    }


def _get_patch(lcu: LCUClient, puuid: str, game_id: str) -> str:
    """Look up patch string from match history for the given gameId."""
    for g in get_match_history(lcu, puuid, begin=0, end=5):
        ver = g.get("gameVersion", "")
        if ver:
            parts = ver.split(".")
            patch = ".".join(parts[:3]) if len(parts) >= 3 else ver
            if str(g.get("gameId", "")) == game_id:
                return patch  # exact match
            # keep this as fallback; loop may find exact match later
    # Return whatever we found as fallback
    for g in get_match_history(lcu, puuid, begin=0, end=1):
        ver = g.get("gameVersion", "")
        if ver:
            parts = ver.split(".")
            return ".".join(parts[:3]) if len(parts) >= 3 else ver
    return "unknown"


def _save(con: sqlite3.Connection, record: dict, seen_ids: set[str]) -> bool:
    """INSERT record into DB. Returns True on success. Only updates seen_ids on success."""
    try:
        con.execute(
            """
            INSERT OR IGNORE INTO games (
                game_id, queue_id, patch, blue_champs, red_champs,
                blue_wins, duration_sec, created_ms, captured_at, participants_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record["game_id"], record["queue_id"], record["patch"],
                json.dumps(record["blue_champs"]), json.dumps(record["red_champs"]),
                record["blue_wins"], record["duration_sec"],
                record["created_ms"], record["captured_at"],
                json.dumps(record.get("participants", []), separators=(",", ":")),
            ),
        )
        con.commit()
        seen_ids.add(record["game_id"])
        return True
    except sqlite3.Error as e:
        print(f"[lcu] db error (will retry): {e}")
        return False


# ---------- Main loop ----------

def run_collector(
    db_path: Path,
    poll_interval: int = 30,
    target_queues: set[int] | None = None,
) -> None:
    if target_queues is None:
        target_queues = DEFAULT_QUEUES

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    _ensure_games_schema(con)

    seen_ids: set[str] = {
        row[0] for row in con.execute("SELECT game_id FROM games").fetchall()
    }
    print(f"[lcu] db={db_path}  already_saved={len(seen_ids)}  queues={sorted(target_queues)}")
    print("[lcu] waiting for League client …  (Ctrl-C to stop)")
    print("[lcu] TIP: keep this running — it captures at the post-game screen")

    puuid: str | None = None
    summoner_fail_streak = 0
    # Fallback: if user dismisses EoG before poller catches it, we know the game_id
    # from InProgress and can fetch full detail afterwards via get_game_detail.
    pending_game_id: str | None = None
    last_in_progress_at: float = 0.0   # time.time() of last InProgress poll
    event_queue: SimpleQueue[LCUApiEvent] = SimpleQueue()
    wake_event = threading.Event()
    event_listener: LCUEventListener | None = None
    event_listener_key: tuple[int, str] | None = None

    def _on_lcu_event(event: LCUApiEvent) -> None:
        event_queue.put(event)
        wake_event.set()

    def _stop_event_listener() -> None:
        nonlocal event_listener, event_listener_key
        if event_listener is not None:
            event_listener.stop()
        event_listener = None
        event_listener_key = None

    def _ensure_event_listener(creds) -> None:
        nonlocal event_listener, event_listener_key
        key = (creds.port, creds.token)
        if event_listener_key == key and event_listener is not None and event_listener.is_alive():
            return
        _stop_event_listener()
        event_listener = LCUEventListener(
            creds,
            on_event=_on_lcu_event,
            on_status=lambda status: print(f"[lcu:ws] {status}"),
        )
        event_listener_key = key
        event_listener.start()

    def _sleep(seconds: float) -> None:
        wake_event.wait(max(0.0, seconds))
        wake_event.clear()

    try:
        while True:
            creds = get_credentials()
            if creds is None:
                if puuid is not None:
                    print("[lcu] League client not found — waiting …")
                puuid = None
                summoner_fail_streak = 0
                _stop_event_listener()
                _sleep(poll_interval)
                continue

            _ensure_event_listener(creds)

            try:
                with LCUClient(creds) as lcu:
                    if puuid is None:
                        summoner = get_current_summoner(lcu)
                        if summoner:
                            puuid = summoner.get("puuid")
                            summoner_fail_streak = 0
                            name = summoner.get("gameName") or summoner.get("displayName", "?")
                            print(f"[lcu] connected as {name}  puuid {(puuid or '')[:12]}…")
                        else:
                            summoner_fail_streak += 1
                            if summoner_fail_streak >= 3:
                                print("[lcu] WARNING: cannot resolve summoner — credentials may be stale")
                            _sleep(poll_interval)
                            continue

                    if not puuid:
                        _sleep(poll_interval)
                        continue

                    # ── Primary: EoG stats block ─────────────────────────────
                    signals = _drain_lcu_events(event_queue)
                    if signals.phase == "InProgress" or signals.should_fetch_eog:
                        last_in_progress_at = time.time()
                    if signals.game_id and signals.game_id not in seen_ids:
                        if pending_game_id != signals.game_id:
                            pending_game_id = signals.game_id
                            print(f"[lcu] event: tracking game {pending_game_id}")

                    eog = get_eog_stats(lcu)
                    if eog:
                        game_id = str(eog.get("gameId", ""))
                        if game_id and game_id not in seen_ids:
                            patch = _get_patch(lcu, puuid, game_id)
                            record = _parse_eog_block(eog, target_queues, patch)
                            if record:
                                if _save(con, record, seen_ids):
                                    total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
                                    q_tag = "Mayhem" if record["queue_id"] == 2400 else "ARAM"
                                    print(
                                        f"[lcu] SAVED {q_tag}  game_id={game_id}  "
                                        f"patch={patch}  blue_wins={bool(record['blue_wins'])}  "
                                        f"total={total}"
                                    )
                                    pending_game_id = None  # EoG succeeded
                            else:
                                # Wrong queue or too short — mark seen to avoid re-checking
                                seen_ids.add(game_id)
                        _sleep(5)
                        continue

                    # ── Record game_id during InProgress for fallback ─────────
                    phase = signals.phase or get_gameflow_phase(lcu)
                    if phase == "InProgress" or signals.should_fetch_session:
                        last_in_progress_at = time.time()
                        if pending_game_id is None:
                            session = get_gameflow_session(lcu)
                            gid = str(
                                ((session or {}).get("gameData") or {}).get("gameId", "")
                            )
                            if gid and gid not in seen_ids:
                                pending_game_id = gid
                                print(f"[lcu] fallback: tracking game {gid}")
                        if phase == "InProgress":
                            _sleep(5)
                            continue

                    # ── Fallback: fetch full detail after game ends ───────────
                    if pending_game_id and pending_game_id not in seen_ids:
                        detail = get_game_detail(lcu, pending_game_id)
                        if detail:
                            record = _parse_game_detail(detail, target_queues)
                            if record:
                                if _save(con, record, seen_ids):
                                    total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
                                    q_tag = "Mayhem" if record["queue_id"] == 2400 else "ARAM"
                                    print(f"[lcu] SAVED (fallback)  {q_tag}  "
                                          f"game_id={pending_game_id}  total={total}")
                            else:
                                seen_ids.add(pending_game_id)
                            pending_game_id = None

            except Exception as exc:
                print(f"[lcu] error: {exc}")
                puuid = None

            # Poll fast for 2 min after a game ends (so we catch the EoG screen).
            since_game = time.time() - last_in_progress_at
            _sleep(5 if since_game < 120 else poll_interval)

    except KeyboardInterrupt:
        print("\n[lcu] stopped by user")
    finally:
        _stop_event_listener()
        con.close()
