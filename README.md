# aram-mayhem-collector

Collect **League of Legends ARAM Mayhem** (queueId 2400) match data via the local League Client (LCU) and contribute it to a shared dataset.

Riot's public API blocks Mayhem match data entirely. The only way to get it is through the local client running on your own machine.

---

## Requirements

- Windows / macOS / Linux with League of Legends installed
- Python 3.10+
- League client **open and logged in** before running

```bash
pip install -r requirements.txt
```

---

## Collect & contribute in 3 steps

**Step 1 — Open your League client and log in.**

**Step 2 — Run the collector** (leave it running until it finishes, ~10–30 min):

```bash
python collect.py --platform TW2
# Replace TW2 with your server tag: KR, EUW1, NA1, JP1, etc.
```

This crawls your match history and your opponents' match histories (BFS snowball), then exports to `my_games.parquet`.

**Step 3 — Submit your data:**

Open a [GitHub Issue](../../issues/new) with the title:
```
[Data] <SERVER> - <N> games
```
and drag-and-drop `my_games.parquet` into the comment box.

That's it. The parquet file contains only champion IDs, win/loss, duration, and patch — **no summoner names, no PUUIDs.**

---

## Advanced options

```bash
python collect.py --workers 8      # more workers = faster (benchmark: 8 optimal)
python collect.py --out custom.parquet

# Direct CLI (more control)
python lcu_collector.py snowball-workers --workers 8 --target-games 10000 --max-players 10000 --games-per-player 20 --max-depth 6 --seed-ladder --seed-apex
python lcu_collector.py status
python lcu_collector.py export --queue 2400 --out my_games.parquet --platform TW2
```

---

## For maintainers: merging contributions

```bash
# Download all attached parquets from issues into contributions/
python merge.py contributions/*.parquet --out merged_mayhem.parquet
```

Deduplication is automatic — `match_id` (= `LCU_<game_id>`) is a globally unique Riot game ID.

---

## Data schema

| Column | Type | Description |
|--------|------|-------------|
| `match_id` | str | `LCU_<game_id>` — globally unique |
| `queue_id` | int | 2400 = Mayhem, 450 = ARAM |
| `patch` | str | e.g. `16.9.772` |
| `platform` | str | Server tag, e.g. `TW2` |
| `blue_champions` | list[int] | Champion IDs, sorted ascending |
| `red_champions` | list[int] | Champion IDs, sorted ascending |
| `blue_wins` | bool | True if blue team won |
| `duration_sec` | int | Game duration in seconds |
| `game_creation_ms` | int | Unix timestamp (ms) |

---

## Privacy

The collected parquet file contains **no personally identifiable information**. Champion IDs and match outcomes only. Do not submit your `games.db` file — it contains internal crawl data including player identifiers.

---

## How it works

The collector uses the [League Client Update (LCU) API](https://www.mingweisamuel.com/lcu-schema/tool/) — a local HTTP server that the League client exposes on your machine. No Riot API key is required. The BFS snowball starts from your own match history and expands to opponents and teammates up to 6 hops away.
