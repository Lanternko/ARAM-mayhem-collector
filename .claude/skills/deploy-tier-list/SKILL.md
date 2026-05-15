---
name: deploy-tier-list
description: "Rebuild docs/index.html from data/lcu/games.db and publish to GitHub Pages (Lanternko/ARAM-Mayhem-Database, branch main, folder /docs). Use when the user wants to refresh / publish / deploy / update the tier list site, ship new patch data, or push augment-winrate changes online. Triggers on: deploy tier list, publish tier list, update tier list site, refresh site, rebuild site, push to pages, ship to pages, ship tier list, 更新網站, 更新 tier list, 重新部署, 重建網站, 把 tier list 推上去, 部署 tier list, /deploy-tier-list."
metadata:
  version: "1.0"
  last_updated: "2026-05-15"
  status: active
  scope: project
---

# deploy-tier-list — Rebuild & ship `docs/index.html`

ARAM Mayhem tier-list site is published via **GitHub Pages → branch `main` → folder `/docs`**. The whole pipeline is one Python build + three git commands. This skill encapsulates that flow so we don't paste the canonical URL by hand every time.

**Live URL**: https://lanternko.github.io/ARAM-Mayhem-Database/

## What it does

1. Re-runs `scripts/build_tier_list.py` against `data/lcu/games.db` with the **canonical site URL hardcoded** (`--site-url https://lanternko.github.io/ARAM-Mayhem-Database/`) so OG / Twitter / `<link rel=canonical>` are correct.
2. Stages **only `docs/index.html`** — never `data/`, never the script (unless the user is also pushing code changes), never other untracked artifacts.
3. Commits with a date- or patch-stamped message.
4. Pushes to `origin main`. Pages auto-rebuilds in ~30–60 s.

## When to invoke

Auto-trigger on any rephrase of "deploy/update/publish the tier list site". Explicit `/deploy-tier-list` always invokes.

**Don't trigger** when:
- The user only changed the build script (CSS / layout tweaks) and hasn't asked to push — just rebuild locally so they can review.
- The user is asking about deploy *mechanics* ("how do I…") — answer with text, don't push.
- `data/lcu/games.db` is mid-write by a running collector — refuse and tell the user to stop the collector first (writing while open SQLite can corrupt).

## The canonical command

```powershell
# 1. Rebuild (always include --site-url; do NOT omit, or canonical/OG tags break)
python scripts/build_tier_list.py --site-url "https://lanternko.github.io/ARAM-Mayhem-Database/"
```

Defaults the script already applies (do not override unless the user asks):
- `--db data/lcu/games.db`
- `--queue 2400` (Mayhem)
- `--patch-prefix 16.10`
- `--out docs/index.html`
- `--min-games 50`, `--min-pair-games 15`
- `--build-date` = today

Override `--patch-prefix` when the user mentions a different patch ("用 16.11 重新出一份"). Override `--queue 450` if the user explicitly asks for ARAM, but warn that ARAM sample is currently tiny (~139 games).

## Stage / commit / push (the safe subset)

```powershell
# 2. Stage ONLY the regenerated artifact. Never `git add .` / `git add -A` —
#    the working tree always carries unrelated WIP scripts under scripts/.
git add docs/index.html

# 3. Commit. Message convention: "Refresh tier list <date>" or
#    "Refresh tier list <patch>" if a new patch is the reason.
git commit -m "Refresh tier list 2026-05-15"

# 4. Push to main; Pages auto-deploys.
git push origin main
```

If the script itself changed and the user wants those changes shipped together, add `scripts/build_tier_list.py` to the same commit — but check `git diff --cached` first so we don't accidentally include unrelated WIP.

## Pre-flight checks (do these silently, only surface failures)

| Check | What goes wrong if skipped |
|---|---|
| `data/lcu/games.db` exists and not zero-size | Script crashes with cryptic SQLite error |
| `data/cache/kiwi.bin.json` & `lol_stringtable_zh_tw.json` present **or** internet reachable | Augment descriptions silently come out empty |
| Working tree clean wrt `docs/` (no manual edits) | `git add docs/index.html` would commit stale hand-edits |
| Current branch is `main` | Pushing a non-main branch won't trigger Pages |
| `origin` URL is `Lanternko/ARAM-Mayhem-Database` | Old `ARAM-mayhem-collector` URL still works via redirect but warn the user |

## After push — verification

Pages takes 30–60 s to rebuild. Tell the user:

> Push 完成 (`<short_sha>`). Pages 會在 30–60 秒內重新部署，重新整理 https://lanternko.github.io/ARAM-Mayhem-Database/ 即可看到新版。

Do **not** poll or curl the live URL — Pages CDN caches aggressively and a 200 right after push doesn't prove the new build is live. Trust the GitHub Actions log if the user wants confirmation: https://github.com/Lanternko/ARAM-Mayhem-Database/actions

## NEVER

- **Never** `git add -A` / `git add .` — staging area always carries unrelated WIP across many files.
- **Never** include `data/` in commits — it's in `.gitignore` for a reason (LCU puuids inside).
- **Never** force-push to `main` — Pages live URL would temporarily 404 and Discord/Twitter previews could break.
- **Never** edit `docs/index.html` by hand and commit — it's a build artifact; changes get clobbered next rebuild.
- **Never** drop `--site-url` — `og:url` / `<link rel=canonical>` would point to nothing, breaking social previews.
- **Never** swap `/docs` for `/site` — GitHub Pages only supports `/(root)` and `/docs` in branch mode (we hit this once already; see commit `3806df6`).
