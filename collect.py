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
import subprocess
import sys
from pathlib import Path


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

    print()
    print(f"[collect] Exporting Mayhem games to {out} ...")
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
    print(f"[collect] Done! Please open a GitHub Issue and attach: {out}")
    print(f"          Issue title suggestion: [Data] {args.platform or 'SERVER'} - <N> games")


if __name__ == "__main__":
    main()
