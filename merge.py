"""Merge multiple parquet contributions into one deduplicated dataset.

Usage
-----
    python merge.py contribution1.parquet contribution2.parquet --out merged.parquet
    python merge.py contributions/*.parquet --out data/merged_mayhem.parquet

Deduplication key: match_id  (= "LCU_<game_id>", globally unique Riot game ID)
"""

import argparse
import sys
from pathlib import Path

try:
    import polars as pl
except ImportError:
    print("ERROR: polars not installed.  Run: pip install polars")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge and deduplicate parquet contributions.")
    parser.add_argument("files", nargs="+", help="Parquet files to merge")
    parser.add_argument("--out", default="merged.parquet", help="Output path (default: merged.parquet)")
    parser.add_argument("--queue", type=int, default=0, help="Filter to a single queue_id (0 = all queues)")
    args = parser.parse_args()

    paths = [Path(f) for f in args.files]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"ERROR: files not found: {missing}")
        sys.exit(1)

    print(f"[merge] Loading {len(paths)} file(s)...")
    dfs = []
    for p in paths:
        df = pl.read_parquet(p)
        print(f"  {p.name}: {len(df)} rows")
        dfs.append(df)

    merged = pl.concat(dfs, how="diagonal_relaxed")
    before = len(merged)
    merged = merged.unique("match_id").sort("game_creation_ms")
    after = len(merged)

    if args.queue:
        merged = merged.filter(pl.col("queue_id") == args.queue)
        print(f"[merge] Filtered to queue_id={args.queue}: {len(merged)} games")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    merged.write_parquet(out, compression="zstd")

    dupes = before - after
    print(f"[merge] {before} rows → {after} unique games ({dupes} duplicates removed) → {out}")

    by_q = merged.group_by("queue_id").agg(pl.len().alias("count")).sort("queue_id")
    for row in by_q.iter_rows(named=True):
        print(f"  queue_id={row['queue_id']}: {row['count']} games")

    by_p = merged.group_by("platform").agg(pl.len().alias("count")).sort("count", descending=True)
    if by_p.height > 1 or (by_p.height == 1 and by_p["platform"][0]):
        print("  platforms:", dict(zip(by_p["platform"].to_list(), by_p["count"].to_list())))


if __name__ == "__main__":
    main()
