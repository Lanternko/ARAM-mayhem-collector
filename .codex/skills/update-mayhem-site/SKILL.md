---
name: update-mayhem-site
description: Refresh and deploy the ARAM Mayhem tier-list website for this repo. Use when the user asks to update new Mayhem data to the website, rebuild docs/index.html, publish the GitHub Pages tier list, or asks for the website update SOP.
---

# Update Mayhem Site

Use this skill for the `aram-winrate-nn` GitHub Pages tier-list site.

## Workflow

1. Inspect `git status --short --branch` first.
2. Rebuild the site from the repo root:

```powershell
python scripts/build_tier_list.py --site-url "https://lanternko.github.io/ARAM-Mayhem-Database/"
```

3. Review the generated `docs/index.html` diff. Confirm the sample count, patch label, and canonical / OG URL look correct.
4. Stage only intended files. For a normal data refresh, stage only:

```powershell
git add docs/index.html
```

If this skill itself was created or updated, also stage only the skill folder:

```powershell
git add .codex/skills/update-mayhem-site
```

5. Commit with the local date or patch in the message, for example:

```powershell
git commit -m "Refresh tier list 2026-05-17"
```

6. Push `main`:

```powershell
git push origin main
```

GitHub Pages redeploys automatically after the push.

7. After a successful deploy, always report the shipped changes back to the user. The post-deploy summary must include:
   - the main user-visible changes that were deployed,
   - which files were intentionally staged / deployed,
   - whether any skill files were modified as part of this deploy (`yes` / `no`),
   - what verification ran (for example rebuild success, syntax check, quick visual check),
   - any known caveat or leftover local-only change that was not deployed.

## Guardrails

- Always output to `docs/index.html`; never publish from `/site`.
- Always pass `--site-url "https://lanternko.github.io/ARAM-Mayhem-Database/"` so canonical and OG metadata are populated.
- Never use `git add -A` or `git add .`.
- Do not stage unrelated WIP files, especially crawler scripts, experiments, raw data, cache files, or local DB files.
- If `scripts/build_tier_list.py` has unrelated local edits, do not stage it unless the user explicitly wants the generator changes included.
- Do not end with only "deploy completed"; always leave the user with a short shipped-change recap so they can recover context later.
- Default build settings are owned by `scripts/build_tier_list.py`: queue `2400`, patch prefix `16.10`, `docs/index.html`, `min-games 50`, and `min-pair-games 15`.
