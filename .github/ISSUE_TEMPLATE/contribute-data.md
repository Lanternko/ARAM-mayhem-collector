---
name: Contribute Match Data
about: Share Mayhem / ARAM matches you collected with the LCU collector
title: "[data] <patch> - <N> games"
labels: ["data-contribution"]
assignees: []
---

## How to file this issue

1. Run the collector for a while (see [CONTRIBUTING.md](../../CONTRIBUTING.md)).
2. Export a PUUID-free SQLite snapshot:
   ```powershell
   python scripts/lcu_collector.py export-share --queue 2400
   ```
3. **Drag-and-drop the `data/share/share_<timestamp>.db` file into the comment box below.**
4. Paste the summary that `export-share` printed in your terminal into the box below.

> GitHub Issue attachments cap at **25 MB**. If your export is bigger, re-run with
> `--patch-prefix 16.10` (or similar) and submit one issue per patch.

---

## Export summary

```
<paste the lines that export-share printed: games / queues / blue_wr / patches / file size>
```

## Collection notes (optional)

- **Region / server**: (e.g. TW / KR / NA)
- **Approximate collection period**: (e.g. 2026-05-10 ~ 2026-05-15)
- **Anything unusual**: (e.g. only ranked games, one specific account, etc.)

## Privacy checklist

- [ ] I ran `export-share` (NOT a raw `games.db` copy) - the attached file contains only the `games` table, no PUUIDs.
- [ ] I understand the data will be merged into the public tier-list dataset.
