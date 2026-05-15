# ARAM Mayhem Database

> 2026 ARAM Mayhem 英雄 + augment 勝率 tier list — 從 LCU 真實對局抓取，每張 augment 卡 hover 顯示中文效果。

🔗 **Live site**: **<https://lanternko.github.io/ARAM-Mayhem-Database/>**

⭐ **覺得有用請按 Star ↗ 讓更多人看到** — 這是一個人下班時間做的小專案，多一顆星都是動力。

---

## 為什麼這個專案存在

Riot 公開 API 從 patch 14.x 開始**整場移除 Mayhem (queueId 2400)**，dev key 完全拿不到對戰資料。OP.GG / U.GG 之類網站也因此沒有 Mayhem 統計。

但 League 客戶端的本機 LCU API 還能查到自己 + 最近對手的 match 詳細資料。本專案：

1. 跑一個本機 collector 從你的 LCU snowball 擴張（self → 好友 → 對手 → 對手的對手 …）
2. 把每場 Mayhem 對局的 10 位玩家英雄 + augment + 勝負存進 SQLite
3. 每隔幾天合併資料、產生新版 tier list 推上 GitHub Pages

目前資料量 ~20,000 場 Mayhem 對局（patch 16.10）。

---

## 網站功能

- **英雄分 tier**（OP / T1–T5）按 Bayesian smoothed 勝率
- **點英雄**展開該英雄最適配 / 最不適配的 augment：
  - 彩色（Prismatic）/ 金色 / 銀色 各取 5 個最佳 + 5 個最差
  - 每張 augment hover 顯示中文效果敘述
- **角色 filter**（刺客 / 戰士 / 法師 / 射手 / 輔助 / 坦克）即時過濾
- **搜尋框**支援中文、英文 alias、角色關鍵字
- **手機 layout** 自動切換成單欄

---

## 資料如何蒐集（想貢獻資料看這）

跑本機 collector：

```powershell
# 1. 安裝
git clone https://github.com/Lanternko/ARAM-Mayhem-Database.git
cd ARAM-Mayhem-Database
python -m pip install -e .

# 2. 打開 League 客戶端（不需要在玩，只要客戶端在線即可）

# 3. 啟動 collector（最簡單一行）
python scripts/lcu_collector.py auto-collect --rounds 50 --target-games 500 \
    --max-players 1000 --opgg-tier platinum --opgg-tier gold

# 4. 查目前蒐集多少場
python scripts/lcu_collector.py status

# 5. 合併別人的 db（不同帳號跑的）
python scripts/lcu_collector.py merge-db \
    --out-db data/lcu/games_merged.db \
    data/lcu/games_account_a.db data/lcu/games_account_b.db
```

詳細 collector 文件見 [`CLAUDE.md`](CLAUDE.md) 的 LCU Collector 節。

---

## 自己 build tier list 網站

```powershell
# 從 data/lcu/games.db 生成 docs/index.html
python scripts/build_tier_list.py --site-url "https://你的網址/"

# 本機預覽
start docs/index.html
```

部署到 GitHub Pages：在 repo Settings → Pages → Branch `main` / Folder `/docs` 即可。

完整部署工作流見 [`.claude/skills/deploy-tier-list/SKILL.md`](.claude/skills/deploy-tier-list/SKILL.md)（Claude Code 使用者可直接喊「更新網站」就會自動 build + commit + push）。

---

## 技術細節

- **Bayesian smoothing**: champion winrate prior `0.5, k=200`；per-(champion, augment) winrate 用該英雄自己的 baseline 當 prior, `k=20` — 這樣 augment 的「lift」訊號才不會被英雄本身的強弱蓋過去
- **Tier cutoffs (Bayes WR)**: OP ≥ 55%, T1 ≥ 52%, T2 ≥ 50%, T3 ≥ 48%, T4 ≥ 46%, T5 < 46%
- **Min sample 過濾**: 每位英雄至少 50 場；每 (英雄, augment) pair 至少 15 場
- **Augment 中文敘述**: 兩階段解析 — `kiwi.bin.json` 解 `AugmentPlatformId → DescriptionTra` key，再用 `zh_TW lol.stringtable.json` 解 key → 中文。所有資料來自 [CommunityDragon](https://raw.communitydragon.org/) 鏡像
- **英雄圖示**: Data Dragon CDN
- **前端**: 純 HTML / CSS / JS（無框架，單檔 ~460KB，全部 inline）
- **無後端**: GitHub Pages 靜態託管

詳細模型設計見 [`PLAN.md`](PLAN.md)（v3, 含 Codex review）。

---

## Stack

- **Python 3.13**, polars, click, httpx, psutil
- **SQLite** — LCU 對局 storage
- **PyTorch 2.11** — 後續會用來訓練 NN 預測雙方英雄組合勝率（見 `PLAN.md`）
- **Pure HTML/CSS/JS** — 前端零依賴

---

## NEVER（給協作者的雷區清單）

- **Never** 用 Riot Dev API 抓 Mayhem — queueId 2400 在 API 層級被整場移除
- **Never** 把 `data/lcu/games.db` commit 到 git — 內含 LCU puuid（雖然不是公開資料，但避免）
- **Never** publish 從 `/site` — GitHub Pages 只接受 `/(root)` 或 `/docs`
- **Never** `git add -A` 來部署網站 — 工作樹常有 WIP 腳本，只 stage `docs/index.html`

完整 rules 見 [`CLAUDE.md`](CLAUDE.md) / [`AGENTS.md`](AGENTS.md) 的 NEVER 節。

---

## 法律免責

This project isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing League of Legends. League of Legends and Riot Games are trademarks or registered trademarks of Riot Games, Inc. League of Legends © Riot Games, Inc.

---

## License

MIT

---

## 支持這個專案

- ⭐ **Star** 是最直接的鼓勵
- 🐛 **Issue** 回報 bug / 提建議
- 🎮 **跑 collector** 一起累積資料
- 📣 **分享給朋友** — 越多人玩 Mayhem，這份 tier list 越準
