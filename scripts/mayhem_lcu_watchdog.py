from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "lcu" / "games.db"
DEFAULT_SEED_FILE = ROOT / "data" / "seeds" / "opgg_tw.txt"
DEFAULT_LOG_DIR = ROOT / ".codex" / "logs" / "mayhem_lcu_watchdog"
DEFAULT_STATE_FILE = ROOT / "data" / "monitor" / "mayhem_lcu_watchdog.jsonl"
LEAGUE_LOCKFILES = (
    Path(r"C:\Riot Games\League of Legends\lockfile"),
    Path(r"D:\遊戲\Riot Games\League of Legends\lockfile"),
    Path(r"D:\Riot Games\League of Legends\lockfile"),
)
DEFAULT_RIOT_CLIENTS = (
    Path(r"D:\遊戲\Riot Games\Riot Client\RiotClientServices.exe"),
    Path(r"C:\Riot Games\Riot Client\RiotClientServices.exe"),
    Path(r"D:\Riot Games\Riot Client\RiotClientServices.exe"),
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def mb(rss: int | float) -> float:
    return round(float(rss) / 1024 / 1024, 1)


def iter_processes() -> list[psutil.Process]:
    procs: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "memory_info"]):
        try:
            # Touch info now so later access is less likely to race.
            _ = proc.info
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def is_snowball_worker(proc: psutil.Process) -> bool:
    try:
        name = (proc.info.get("name") or "").lower()
        cmdline = proc.info.get("cmdline") or []
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if name != "python.exe" or len(cmdline) < 4:
        return False
    script = str(cmdline[2]).replace("/", "\\")
    return (
        str(cmdline[1]) == "-u"
        and script.endswith(r"scripts\lcu_collector.py")
        and str(cmdline[3]) == "snowball"
    )


def snowball_workers() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proc in iter_processes():
        if not is_snowball_worker(proc):
            continue
        try:
            rss = proc.info.get("memory_info").rss if proc.info.get("memory_info") else 0
            rows.append(
                {
                    "pid": proc.info["pid"],
                    "rss_mb": mb(rss),
                    "cmdline": proc.info.get("cmdline") or [],
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def stop_snowball_workers(grace_sec: int = 5) -> list[int]:
    stopped: list[int] = []
    procs = [proc for proc in iter_processes() if is_snowball_worker(proc)]
    for proc in procs:
        try:
            proc.terminate()
            stopped.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(procs, timeout=grace_sec)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return stopped


def stop_extra_snowball_workers(keep: int, grace_sec: int = 5) -> list[int]:
    workers: list[tuple[psutil.Process, int]] = []
    for proc in iter_processes():
        if not is_snowball_worker(proc):
            continue
        worker_num = 999
        try:
            cmdline = proc.info.get("cmdline") or []
            if "--worker-id" in cmdline:
                worker_id = str(cmdline[cmdline.index("--worker-id") + 1])
                if len(worker_id) > 1 and worker_id[0].upper() == "W":
                    worker_num = int(worker_id[1:])
        except (ValueError, IndexError, TypeError):
            worker_num = 999
        workers.append((proc, worker_num))

    workers.sort(key=lambda item: item[1])
    to_stop = [proc for proc, _ in workers[keep:]]
    stopped: list[int] = []
    for proc in to_stop:
        try:
            proc.terminate()
            stopped.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(to_stop, timeout=grace_sec)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return stopped


def league_processes() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proc in iter_processes():
        try:
            name = proc.info.get("name") or ""
            if not name.lower().startswith("leagueclient"):
                continue
            rss = proc.info.get("memory_info").rss if proc.info.get("memory_info") else 0
            rows.append(
                {
                    "pid": proc.info["pid"],
                    "name": name,
                    "exe": proc.info.get("exe"),
                    "rss_mb": mb(rss),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def league_main_mb() -> float:
    vals = [row["rss_mb"] for row in league_processes() if row["name"].lower() == "leagueclient.exe"]
    return max(vals, default=0.0)


def read_lockfile() -> tuple[str, str] | None:
    for path in LEAGUE_LOCKFILES:
        try:
            if not path.exists():
                continue
            parts = path.read_text(encoding="utf-8").strip().split(":")
            if len(parts) >= 5:
                return parts[2], parts[3]
        except OSError:
            continue
    return None


def lcu_get(path: str, timeout_sec: float = 5.0) -> tuple[int | str, str]:
    lock = read_lockfile()
    if not lock:
        return "ERR", "lockfile missing"
    port, password = lock
    auth = base64.b64encode(("riot:" + password).encode("utf-8")).decode("ascii")
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        f"https://127.0.0.1:{port}{path}",
        headers={"Authorization": "Basic " + auth},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_sec) as resp:
            body = resp.read(512).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc)
    except Exception as exc:
        return "ERR", type(exc).__name__ + ": " + str(exc)


def lcu_health() -> dict[str, Any]:
    summoner_status, summoner_body = lcu_get("/lol-summoner/v1/current-summoner")
    phase_status, phase_body = lcu_get("/lol-gameflow/v1/gameflow-phase")
    phase = None
    if phase_status == 200:
        try:
            phase = json.loads(phase_body)
        except json.JSONDecodeError:
            phase = phase_body.strip('"')
    return {
        "ok": summoner_status == 200 and phase_status == 200,
        "current_summoner_status": summoner_status,
        "gameflow_status": phase_status,
        "phase": phase,
        "summoner_body_prefix": summoner_body[:120],
        "phase_body_prefix": phase_body[:120],
    }


def find_riot_client() -> Path | None:
    for proc in iter_processes():
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe")
            if name == "riotclientservices.exe" and exe:
                return Path(exe)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for path in DEFAULT_RIOT_CLIENTS:
        if path.exists():
            return path
    return None


def riot_remoting() -> tuple[str, str] | None:
    for proc in iter_processes():
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "--app-port=" not in cmd or "--remoting-auth-token=" not in cmd:
            continue
        port = re.search(r"--app-port=(\d+)", cmd)
        token = re.search(r"--remoting-auth-token=([^\s]+)", cmd)
        if port and token:
            return port.group(1), token.group(1)
    return None


def remoting_request(path: str, method: str = "GET", data: bytes | None = None) -> tuple[int | str, str]:
    remoting = riot_remoting()
    if not remoting:
        return "ERR", "Riot remoting port/token not found"
    port, token = remoting
    auth = base64.b64encode(("riot:" + token).encode("utf-8")).decode("ascii")
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        f"https://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            body = resp.read(512).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc)
    except Exception as exc:
        return "ERR", type(exc).__name__ + ": " + str(exc)


def wait_for_riot_remoting(timeout_sec: int) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if riot_remoting():
            return True
        time.sleep(2)
    return False


def close_league_client(grace_sec: int = 20) -> list[int]:
    targets = []
    for proc in iter_processes():
        try:
            name = (proc.info.get("name") or "").lower()
            if name.startswith("leagueclient"):
                targets.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    closed: list[int] = []
    for proc in targets:
        try:
            proc.terminate()
            closed.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(targets, timeout=grace_sec)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return closed


def start_league_client() -> dict[str, Any]:
    status, body = remoting_request(
        "/product-launcher/v1/products/league_of_legends/patchlines/live",
        method="POST",
        data=b"{}",
    )
    if status == 200:
        return {"started": True, "method": "riot_remoting", "response": body}

    riot = find_riot_client()
    if not riot:
        return {
            "started": False,
            "method": "riot_remoting",
            "error": f"launch failed: {status} {body}; RiotClientServices.exe not found",
        }
    args = [
        str(riot),
        "--app-command=riotclient://launch-product=league_of_legends&launch-patchline=live",
    ]
    subprocess.Popen(args, cwd=str(riot.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wait_for_riot_remoting(60):
        status, body = remoting_request(
            "/product-launcher/v1/products/league_of_legends/patchlines/live",
            method="POST",
            data=b"{}",
        )
        if status == 200:
            return {
                "started": True,
                "method": "riot_remoting_after_start",
                "exe": str(riot),
                "response": body,
            }
    return {
        "started": False,
        "method": "riot_remoting_after_start",
        "exe": str(riot),
        "error": f"launch failed: {status} {body}",
    }


def start_snowball_worker(args: argparse.Namespace, worker_number: int) -> dict[str, Any]:
    args.log_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    worker_id = f"W{worker_number:02d}"
    out_path = args.log_dir / f"snowball_{worker_id}_{stamp}.out.log"
    err_path = args.log_dir / f"snowball_{worker_id}_{stamp}.err.log"
    cmd = [
        sys.executable,
        "-u",
        str(ROOT / "scripts" / "lcu_collector.py"),
        "snowball",
        "--db",
        str(args.db),
        "--target-games",
        str(args.target_games),
        "--max-players",
        str(args.max_players),
        "--history-window",
        str(args.history_window),
        "--games-per-player",
        str(args.games_per_player),
        "--worker-id",
        worker_id,
        "--claim-timeout-sec",
        str(args.claim_timeout_sec),
        "--player-requeue-cooldown-sec",
        str(args.player_requeue_cooldown_sec),
        "--manual-seed-pending-cap",
        str(args.manual_seed_pending_cap),
        "--max-depth",
        str(args.max_depth),
        "--queue",
        "450",
        "--queue",
        "2400",
        "--seed-riot-id-file",
        str(args.seed_riot_id_file),
        "--seed-self",
        "--seed-friends",
        "--no-seed-ladder",
        "--no-seed-apex",
        "--no-seed-riot-tier",
    ]
    with out_path.open("ab") as out, err_path.open("ab") as err:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=err)
    return {
        "pid": proc.pid,
        "worker_id": worker_id,
        "cmd": cmd,
        "stdout": str(out_path),
        "stderr": str(err_path),
    }


def latest_capture_age_min(db: Path) -> float | None:
    try:
        import sqlite3

        con = sqlite3.connect(db)
        row = con.execute("select max(captured_at) from games where queue_id=2400").fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    try:
        ts = dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return round((utc_now() - ts).total_seconds() / 60, 2)
    except ValueError:
        return None


def append_state(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def should_restart_client(args: argparse.Namespace, health: dict[str, Any], main_mb: float) -> tuple[bool, str]:
    phase = health.get("phase")
    if phase not in args.safe_restart_phase:
        return False, f"phase {phase!r} is not safe to restart"
    if main_mb >= args.client_restart_mb:
        return True, f"LeagueClient memory {main_mb:.1f}MB >= {args.client_restart_mb:.1f}MB"
    if not health["ok"]:
        return True, "LCU health check failed"
    return False, "client healthy enough"


def action_context(args: argparse.Namespace, health: dict[str, Any], main_mb: float) -> dict[str, Any]:
    return {
        "league_main_mb_at_action": main_mb,
        "degrade_client_mb": args.degrade_client_mb,
        "client_restart_mb": args.client_restart_mb,
        "worker_start_max_client_mb": args.worker_start_max_client_mb,
        "lcu_ok_at_action": health.get("ok"),
        "gameflow_phase_at_action": health.get("phase"),
        "current_summoner_status_at_action": health.get("current_summoner_status"),
        "gameflow_status_at_action": health.get("gameflow_status"),
    }


def wait_for_lcu_ready(args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.client_ready_timeout_sec
    last_health: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_health = lcu_health()
        main_mb = league_main_mb()
        if last_health["ok"] and main_mb and main_mb <= args.worker_start_max_client_mb:
            return {"ready": True, "health": last_health, "league_main_mb": main_mb}
        time.sleep(args.check_interval_sec)
    return {"ready": False, "health": last_health, "league_main_mb": league_main_mb()}


def check_once(args: argparse.Namespace) -> dict[str, Any]:
    workers = snowball_workers()
    main_mb = league_main_mb()
    health = lcu_health()
    latest_age = latest_capture_age_min(args.db)
    actions: list[dict[str, Any]] = []

    if workers and not health["ok"]:
        stopped = stop_snowball_workers()
        workers = snowball_workers()
        actions.append(
            {
                "action": "stop_workers_lcu_unhealthy",
                "pids": stopped,
                **action_context(args, health, main_mb),
            }
        )
    elif (
        len(workers) > args.degraded_workers
        and main_mb >= args.degrade_client_mb
        and main_mb < args.client_restart_mb
        and health["ok"]
    ):
        stopped = stop_extra_snowball_workers(args.degraded_workers)
        workers = snowball_workers()
        actions.append(
            {
                "action": "degrade_workers",
                "pids": stopped,
                "reason": f"LeagueClient memory {main_mb:.1f}MB >= {args.degrade_client_mb:.1f}MB",
                "target_workers": args.degraded_workers,
                **action_context(args, health, main_mb),
            }
        )

    restart, restart_reason = should_restart_client(args, health, main_mb)
    if args.restart_client and restart:
        if workers:
            stopped = stop_snowball_workers()
            workers = snowball_workers()
            actions.append(
                {
                    "action": "stop_workers_before_client_restart",
                    "pids": stopped,
                    "reason": restart_reason,
                    **action_context(args, health, main_mb),
                }
            )
        closed = close_league_client()
        started = start_league_client()
        actions.append(
            {
                "action": "restart_league_client",
                "reason": restart_reason,
                "closed_pids": closed,
                "start": started,
                **action_context(args, health, main_mb),
            }
        )
        ready = wait_for_lcu_ready(args)
        health = ready.get("health") or lcu_health()
        main_mb = float(ready.get("league_main_mb") or league_main_mb())
        actions.append({"action": "wait_for_lcu_ready", **ready})

    workers = snowball_workers()
    if (
        len(workers) < args.workers
        and health["ok"]
        and 0 < main_mb <= args.worker_start_max_client_mb
    ):
        missing = args.workers - len(workers)
        for idx in range(len(workers) + 1, len(workers) + missing + 1):
            started_worker = start_snowball_worker(args, idx)
            actions.append({"action": "start_worker", **started_worker})
        workers = snowball_workers()
    elif not workers and health["ok"] and main_mb > args.worker_start_max_client_mb:
        actions.append(
            {
                "action": "keep_worker_stopped",
                "reason": f"LeagueClient memory {main_mb:.1f}MB > start max {args.worker_start_max_client_mb:.1f}MB",
                **action_context(args, health, main_mb),
            }
        )
    elif len(workers) > args.workers:
        stopped = stop_extra_snowball_workers(args.workers)
        actions.append(
            {
                "action": "stop_workers_too_many",
                "pids": stopped,
                "reason": f"active workers {len(workers)} > target {args.workers}",
            }
        )

    record = {
        "ts": iso_now(),
        "league_main_mb": main_mb,
        "lcu": health,
        "workers": snowball_workers(),
        "latest_capture_age_min": latest_age,
        "actions": actions,
    }
    append_state(args.state_file, record)
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch and recover the local Mayhem LCU crawler.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--seed-riot-id-file", type=Path, default=DEFAULT_SEED_FILE)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--check-interval-sec", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--restart-client", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--degraded-workers", type=int, default=1)
    parser.add_argument("--degrade-client-mb", type=float, default=4500.0)
    parser.add_argument("--client-restart-mb", type=float, default=6000.0)
    parser.add_argument("--worker-stop-client-mb", type=float, default=5000.0)
    parser.add_argument("--worker-start-max-client-mb", type=float, default=3500.0)
    parser.add_argument("--client-ready-timeout-sec", type=int, default=600)
    parser.add_argument("--safe-restart-phase", action="append", default=["None"])
    parser.add_argument("--target-games", type=int, default=50000)
    parser.add_argument("--max-players", type=int, default=50000)
    parser.add_argument("--history-window", type=int, default=20)
    parser.add_argument("--games-per-player", type=int, default=12)
    parser.add_argument("--claim-timeout-sec", type=int, default=300)
    parser.add_argument("--player-requeue-cooldown-sec", type=int, default=45)
    parser.add_argument("--manual-seed-pending-cap", type=int, default=40)
    parser.add_argument("--max-depth", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.db = args.db.resolve()
    args.seed_riot_id_file = args.seed_riot_id_file.resolve()
    args.log_dir = args.log_dir.resolve()
    args.state_file = args.state_file.resolve()

    while True:
        record = check_once(args)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if args.once:
            break
        time.sleep(args.check_interval_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
