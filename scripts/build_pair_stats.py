"""Build anchor-conditional champion pair synergy JSON for the recommender."""
from __future__ import annotations

from pathlib import Path

import click

from aram_nn.pair_synergy import build_pair_synergy, save_pair_synergy


@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/lcu/games.db"))
@click.option("--queue", "queue_id", type=int, default=2400, help="450=ARAM, 2400=Mayhem")
@click.option("--patch-prefix", default="16.10", help='e.g. "16.10" or "" for all patches')
@click.option("--min-pair", type=int, default=30, show_default=True)
@click.option("--shrink-k", type=float, default=40.0, show_default=True)
@click.option("--out", "out_path", type=click.Path(path_type=Path),
              default=Path("models/pair_synergy_16_10.json"))
def main(
    db: Path,
    queue_id: int,
    patch_prefix: str,
    min_pair: int,
    shrink_k: float,
    out_path: Path,
) -> None:
    patch_prefix = patch_prefix or None
    click.echo(
        f"[pair] db={db}  queue={queue_id}  patch_prefix={patch_prefix}  "
        f"min_pair={min_pair}  shrink_k={shrink_k:g}"
    )
    stats = build_pair_synergy(
        db,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
        min_pair=min_pair,
        shrink_k=shrink_k,
    )
    save_pair_synergy(stats, out_path)
    click.echo(
        f"[pair] wrote {out_path}  rows={len(stats.rows):,}  "
        f"matches={stats.total_matches:,}"
    )


if __name__ == "__main__":
    main()
