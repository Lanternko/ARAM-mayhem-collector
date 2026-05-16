<!-- lines: 132 -->
# aram-winrate-nn — ARAM 英雄組合勝率預測 NN，Python / PyTorch

## Why
輸入一場 League of Legends Mayhem (queueId=2400) 或 ARAM (450) 的雙方英雄組合 (5v5)，輸出藍方獲勝機率。
目標是驗證「提供英雄組合資訊後，模型準確率能否超過藍方 base rate (~51%)」— 已達成。

**主資料源是 Mayhem (queueId=2400)**，30k+ 場；ARAM (450) 因 Riot 公開 API 不限制曾收 ~7k 場但已棄。
Repo 名留 `aram-winrate-nn` 是歷史包袱，**所有訓練 / tier list / 推薦都該預設 Mayhem**。`train.py` / `train_tier2.py` / `tier_list.py` queue-agnostic — 吃任何符合 schema 的 parquet，不檢查 queue。

## Architecture
- **Python 3.13**, PyTorch 2.11, polars, scikit-learn, httpx, psutil, click
- `src/aram_nn/ingest/` — Riot API 爬蟲：`riot_client.py` / `snowball.py` / `extract.py`
- `src/aram_nn/lcu/` — 本機 LCU collector / graph snowball：`process.py` / `client.py` / `poller.py` / `snowball.py`
- `src/aram_nn/models/` — `logreg.py` / `deepsets.py`
- `src/aram_nn/train.py` / `eval.py` / `data.py` — 訓練 pipeline（完成）
- `data/raw/` — parquet 原始資料；`data/lcu/games.db` — LCU SQLite 資料庫（`games` + `crawl_seen` set + `crawl_queue` priority frontier）
- `scripts/` — `probe_user.py`, `probe_queues.py`, `lcu_collector.py`, `build_tier_list.py`
- `docs/index.html` — 公開 tier-list 網站（GitHub Pages, `main` branch `/docs` folder）→ https://lanternko.github.io/ARAM-Mayhem-Database/
- `data/cache/` — `kiwi.bin.json` + `lol_stringtable_zh_tw.json` (CommunityDragon mirror, ~30 MB) 用來解析 Mayhem augment 中文敘述
- 深度技術決策見 `PLAN.md`（v3，已經 Codex review）；部署流程見 `.claude/skills/deploy-tier-list/SKILL.md`

## Commands
> 範例使用 bash `\` 換行。**PowerShell 改用 backtick `` ` ``（行尾不能有空白）**，或整段貼成一行 — `\` 在 PS 不是換行符會直接報 `unexpected extra argument`。

```bash
# 安裝（editable，每次加新 entry point 後要重跑）
python -m pip install -e .

# 資料抓取：從指定 Riot ID snowball，不過濾 patch = 全收
python -m aram_nn.ingest.snowball \
    --region tw \
    --seed-riot-id "Name#TAG" \
    --target-matches 500 \
    --out data/raw/tw_aram_all_patch.parquet

# 資料抓取：過濾特定 patch
python -m aram_nn.ingest.snowball \
    --region tw \
    --seed-riot-id "Name#TAG" \
    --target-matches 2000 \
    --patch 16.9 \
    --out data/raw/tw_aram_16_9.parquet

# 診斷：查某 Riot ID 最近打了哪些 queue
python scripts/probe_user.py --region tw --riot-id "Name#TAG" --count 100

# Tier list 網站重 build（部署 → `/deploy-tier-list` skill）
python scripts/build_tier_list.py --site-url "https://lanternko.github.io/ARAM-Mayhem-Database/"
```

## LCU Collector (Mayhem data, local client only)

Riot blocks queueId=2400 in the public API.  The League client's own local APIs work.

```powershell
# Run BEFORE launching League — leave open in a separate terminal
python scripts/lcu_collector.py collect          # captures both ARAM (450) + Mayhem (2400)
python scripts/lcu_collector.py collect --queue 2400  # Mayhem only

python scripts/lcu_collector.py status           # see how many games captured
python scripts/lcu_collector.py metrics          # record growth / speed / seed-efficiency snapshots
python scripts/lcu_collector.py snowball --target-games 500 --max-players 200
python scripts/lcu_collector.py snowball --target-games 5000 --max-players 5000 --games-per-player 6 --seed-ladder --seed-apex
python scripts/lcu_collector.py snowball --target-games 5000 --max-players 5000 --games-per-player 6 --seed-riot-tier --riot-tier GOLD --riot-page-limit 3
python scripts/lcu_collector.py snowball-workers --workers 3 --target-games 5000 --max-players 5000 --games-per-player 4 --seed-ladder --seed-apex
python scripts/lcu_collector.py snowball-workers --workers 8 --target-games 5000 --max-players 5000 --games-per-player 4 --seed-riot-tier --riot-tier GOLD --riot-page-limit 3
python scripts/lcu_collector.py seed-opgg --tier diamond --region tw --pages 2 --topn 200 --out data/seeds/opgg_tw.txt
python scripts/lcu_collector.py seed-opgg-plan --region tw --tier diamond --tier emerald --tier platinum --tier gold --pages-per-tier 80 --topn-total 0 --out data/seeds/opgg_tw.txt
python scripts/lcu_collector.py snowball --seed-riot-id-file data/seeds/opgg_tw.txt --target-games 5000 --max-players 5000
python scripts/lcu_collector.py snowball --db data/lcu/games_account_a.db --target-games 5000 --max-players 5000
python scripts/lcu_collector.py snowball --db data/lcu/games_account_b.db --target-games 5000 --max-players 5000
python scripts/lcu_collector.py merge-db --out-db data/lcu/games_merged.db data/lcu/games_account_a.db data/lcu/games_account_b.db
python scripts/lcu_collector.py dataset --queue 2400 --patch-prefix 16.9 --topn 20 --min-games 30
python scripts/lcu_collector.py stats --queue 2400 --patch-prefix 16.9 --out-dir data/stats/mayhem_16_9
python scripts/lcu_collector.py family-stats --queue 2400
python scripts/lcu_collector.py auto-collect --rounds 20 --target-games 500 --max-players 1000 --opgg-tier platinum --opgg-tier gold --opgg-pages-per-round 2
python scripts/lcu_collector.py export --out data/raw/lcu_games.parquet
python scripts/lcu_collector.py export --queue 2400 --out data/raw/mayhem_games.parquet
```

`--seed-riot-tier` 會先用 Riot `account-v1 by-puuid` 轉成 `GameName#TagLine`，再用 LCU `/lol-summoner/v2/summoners/names` 橋接成 36-char LCU puuid；這條路可行，但 seed 速度會比 friend / apex 慢。

LCU retains only the **last ~20 games**.  Run the collector every session or you'll miss games.
`snowball` 會從 self / friends / discovered participants 擴張；**exact match dedupe 一律用 `game_id`**，不要用 10 人英雄組合作唯一鍵。
`crawl_seen` + `crawl_queue` 讓 snowball 可中斷續跑；queue 依發現該玩家的最新對戰時間排序，越新的 match 衍生 ID priority 越高。`crawl_seen` 就是 persistent puuid set；worker 另外有 local puuid cache 減少重複 DB enqueue。
`snowball-workers` 會開多個背景 worker 共用同一個 frontier；預設只有第一個 worker 負責 seed，其他 worker 直接消化 queue，避免重複 startup 成本。
`--seed-riot-id-file` 可吃一行一筆的 `Name#TAG`，也接受 OPGG summoner/profile URL；crawler 會先解析成 Riot ID，再經 LCU bridge 成本地 puuid 後入 queue。
多 client 時，每個 client 應各自寫自己的 `--db`；`merge-db` 只合併 `games` 表並以 `game_id` 去重，`crawl_seen` / `crawl_queue` frontier 不要跨 client 合併。
`games.participants_json` 會保留 10 位玩家的 `teamId / championId / augments`；`stats` 會輸出英雄勝率、augment 勝率、英雄×augment 勝率 CSV。
`dataset` 會直接在 terminal 印出目前資料集摘要與英雄勝率排行，英雄名稱優先從 LCU static data 解析。
Database: `data/lcu/games.db` (SQLite) — safe to interrupt and resume.

## Stall Playbook

- `metrics` 若出現 `Mayhem +0`、`current_patch +0`，但 `done_delta` 持續增加，代表 crawler 活著但目前 seed family 已低產值，不要只看 worker 是否存在。
- `recent-active reseed` 若能短暫把 queue 打開、但 log 幾乎整排都是 `source=match` + `target_games=0`，代表目前 active subgraph 已吃乾，應換 seed family 而不是重複 recent-active。
- `seed-opgg-plan --resume` 只有在 `data/seeds/opgg_tw_state.json` 與 `data/seeds/opgg_tw_history.jsonl` 都前進時，才算成功 refresh；若 `manual_riot_id seed progress` 反覆出現 `resolved=0 / enqueued=0`，視為目前 OPGG page window 已耗盡。
- `apex` / `ladder` / `riot_tier` 是 TW Mayhem 上**已驗證的 dead seed family**（2026-05-15 量測：合計 2,890 done puuids、0 transitive captures）；`snowball` 別再花 LCU bandwidth 跑這幾個 root，除非換 region 或大版本後再重驗。用 `python scripts/lcu_collector.py family-stats --queue 2400` 隨時看當前 per-family ROI。
- `manual_riot_id`（OPGG）**是** productive seed family — 同一次量測 199 captures / 2,385 puuids、blue_wr=0.528。**舊** log 看到的「manual yield=0」其實是 attribution bug（transitive captures 被歸到 immediate `match` source 而非 root family），已於 `crawl_seen.seed_family` 修；別根據舊結論判 OPGG seed 沒用。
- `suggested players` 是下一個高價值 seed family，但只在 `gameflow phase=Lobby` 時存在；若 phase=`None` 且 `suggested_players=0`，下一個最有價值的 move 是使用者先進 lobby。
- LCU 所謂「憑證過期」通常不是 cert 真過期，而是 League 重啟後 `port/token` 換掉或 `/lol-*` 尚未 ready；先重抓 credentials 與 `current_summoner`，不要先怪 cert。
- **Persisted backoff 會卡 OPGG seeding**：`source_family_backoff_until` 寫進 `crawl_runtime_state` 後，下一個 snowball 啟動時讀回，會印 `[snowball] startup skip  source=manual_riot_id  reason=backoff` 並讓整批 OPGG seed 不入 queue（newly_seeded=0）。先 `DELETE FROM crawl_runtime_state WHERE state_key LIKE 'backoff:%'` 再啟動，或用 `auto-collect`（每 round 自動清）。
- **`source-family backoff` 出現 ≠ run 沒在生產**：backoff 只擋 fresh seeding，已在 queue 裡的 match-source 子節點繼續處理，rate 可能還是 ~30+/min。判 round 死活以 throughput 為準，不要看到 backoff 就 abort。

## NEVER

- **Never filter queue inside `extract_row`** — 它只驗結構（10 人、雙方各 5 人、有 win flag）；queue 過濾由 caller (snowball.py) 負責。違反此原則曾導致全部場次被誤判為 parse error。
- **Never sort champions by position / slot index** — 隊內位置在 ARAM 無意義；`blue_champions` / `red_champions` 永遠以 `championId` 升序排列，否則 model 會學到 position spurious feature。
- **Never use random train/val/test split** — 用時間切分（`game_creation_ms`）；隨機切會讓同 meta 的場洩漏進 val/test。
- **Never add label smoothing** — calibration 用 post-hoc temperature scaling on val set；pre-hoc smoothing 讓 ECE 難以解讀。
- **Never train on cross-patch data without patch feature** — 不同 patch 的英雄平衡差異大；若跨 patch 訓練，至少要加 `patch` embedding 或 per-patch evaluation。
- **Never call `match_ids_by_puuid` without `queue=450`** during snowball — 不 filter queue 只用在 diagnostic scripts，大量 ingest 一定要加 queue filter 避免收 SR / Arena 場。
- **Never dedupe matches by champion-composition hash alone** — 不同真實對局可能剛好出現同一組 10 隻英雄；crawl / dataset exact dedupe 必須用 `game_id`，composition hash 只能當分析輔助欄位。
- **Never hardcode routing host** — TW 的 match-v5 走 `sea.api.riotgames.com`，account-v1 走 `asia.api.riotgames.com`，platform (league-exp) 走 `tw2.api.riotgames.com`；三個不同，搞混會 404。
- **Never pass `--patch ""`** in PowerShell to CLI — PowerShell 5.1 會把空字串吃掉導致 Click argument shift；省略 `--patch` 即為全收（預設值已是空字串）。
- **Never publish the tier-list site from `/site`** — GitHub Pages 「Deploy from a branch」只接受 `/(root)` 或 `/docs`；用 `/site` Save 不會生效。永遠輸出到 `docs/index.html`（`build_tier_list.py` 預設）。
- **Never `git add -A` / `git add .` when deploying the site** — 工作樹常有未追蹤的 WIP scripts；只 stage `docs/index.html`（必要時加 `scripts/build_tier_list.py`）。詳見 deploy-tier-list skill。

## Riot API 注意事項
- Dev Key 每 24 小時過期，Python 端 401 / 403 都視為 key expired → 提示 regenerate
- Rate limit: 20 req/sec, 100 req/2min（binding constraint）；`riot_client.py` 已內建 bucket throttle
- Mayhem (queueId=2400) 被 Riot **在 API 層級整場移除**，公開 dev key 完全拿不到，不要嘗試
- Mayhem 資料唯一合法管道：本機 LCU (`127.0.0.1:{port}`) + Live Client Data (`127.0.0.1:2999`)；見 LCU Collector 節

## Model 設計原則（快速參照，詳見 PLAN.md）
- Tier 0 (LR baseline) 必跑才能判斷 NN 是否有效
- Tier 1 輸入 `[diff=(sum_blue−sum_red), total=(sum_blue+sum_red)]`，不是只有 diff — 純 diff 會丟掉兩隊共有的上下文
- Logit 必須對 swap-teams 反對稱：`logit(blue, red) = −logit(red, blue)`
- acc > 65% = data leak，立刻檢查 split

## How to edit this file
- Keep the whole file under 300 lines. If a new rule pushes past that, remove a stale one.
- Every rule states Why in the same line or the next.
- No style/formatting rules here — those live in the linter config.
- When a mistake repeats, abstract it into one concise rule and add it.
- After editing, update the top-of-file summary if sections changed.
