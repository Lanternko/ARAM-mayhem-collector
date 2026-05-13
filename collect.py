"""One-command Mayhem data collector.

Runs a BFS snowball crawl against the local League client, then exports
the collected games to a parquet file ready for contribution.

Usage
-----
    python collect.py                        # default: 4 workers, out=my_games.parquet
    python collect.py --workers 8            # more parallel workers (benchmark: 8 is fastest on most machines)
    python collect.py --out jerry_tw.parquet # custom output filename

Requirements
------------
  - League of Legends client must be running and logged in before you run this.
  - pip install -r requirements.txt
"""

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


DB_PATH = Path("data/lcu/games.db")


def _frontier_active() -> bool:
    """Return True if any worker is still running (in_progress > 0 or pending > 0)."""
    if not DB_PATH.exists():
        return False
    try:
        con = sqlite3.connect(str(DB_PATH))
        row = con.execute(
            "SELECT COUNT(*) FROM crawl_queue WHERE status IN ('pending', 'in_progress')"
        ).fetchone()
        con.close()
        return (row[0] if row else 0) > 0
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Mayhem game data via LCU and export to parquet.")
    parser.add_argument("--workers", type=int, default=4, help="Parallel crawl workers (default: 4)")
    parser.add_argument("--out", default="my_games.parquet", help="Output parquet filename (default: my_games.parquet)")
    parser.add_argument("--platform", default="", help="Your server tag, e.g. TW2, KR, EUW1 (optional, metadata only)")
    args = parser.parse_args()

    out = Path(args.out)
    platform_args = ["--platform", args.platform] if args.platform else []

    print(f"[collect] Starting {args.workers}-worker snowball crawl. Make sure League client is open.")
    print(f"[collect] Output will be saved to: {out}")
    print()

    subprocess.run(
        [
            sys.executable, "lcu_collector.py", "snowball-workers",
            "--workers", str(args.workers),
            "--target-games", "20000",
            "--max-players", "20000",
            "--games-per-player", "20",
            "--max-depth", "6",
            "--seed-ladder", "--seed-apex",
        ],
        check=True,
    )

    # snowball-workers exits immediately after spawning child processes.
    # Wait until all children finish (frontier empty).
    print("[collect] Workers running in background — waiting for them to finish...")
    last_total = 0
    while _frontier_active():
        try:
            con = sqlite3.connect(str(DB_PATH))
            total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
            pending = con.execute(
                "SELECT COUNT(*) FROM crawl_queue WHERE status='pending'"
            ).fetchone()[0]
            in_prog = con.execute(
                "SELECT COUNT(*) FROM crawl_queue WHERE status='in_progress'"
            ).fetchone()[0]
            con.close()
            if total != last_total:
                print(f"  games={total}  pending={pending}  in_progress={in_prog}")
                last_total = total
        except Exception:
            pass
        time.sleep(10)

    print()
    con = sqlite3.connect(str(DB_PATH))
    final_total = con.execute(
        "SELECT COUNT(*) FROM games WHERE queue_id=2400"
    ).fetchone()[0]
    con.close()
    print(f"[collect] Crawl complete. {final_total} Mayhem games in database.")

    print(f"[collect] Exporting to {out} ...")
    subprocess.run(
        [
            sys.executable, "lcu_collector.py", "export",
            "--queue", "2400",
            "--out", str(out),
            *platform_args,
        ],
        check=True,
    )

    print()
    print(f"[collect] Done!  ->  {out}  ({final_total} games)")
    print()
    print(f"  上傳到這裡: https://github.com/Lanternko/ARAM-mayhem-collector/discussions/1")
    print(f"  留言格式: 伺服器：{args.platform or 'TW2'}  場次數：{final_total}")


if __name__ == "__main__":
    main()
