# 貢獻 Mayhem 對局資料

這份指南給想要把自己跑出來的 Mayhem 對局資料貢獻到[公開 tier list](https://lanternko.github.io/ARAM-Mayhem-Database/) 的玩家。流程設計成**只送出無 PUUID 的安全檔**、**全程公開稽核**（每筆貢獻都是一個 GitHub Issue）。

---

## 為什麼要這樣設計

- **Riot 把 Mayhem 從公開 API 整場移除** → tier list 完全靠玩家自己用本機 LCU 收
- 你的 `data/lcu/games.db` 裡有 `crawl_seen` / `crawl_queue` 等表存著 PUUID（爬蟲狀態），**不適合公開分享**
- 但 `games` 表本身只存 game_id / 雙方英雄 / augments / 勝負 — 把這張表單獨剝出來就完全乾淨
- 所以貢獻流程 = 跑一個 `export-share` 命令產生純淨檔 → 開 GitHub Issue 附檔

---

## 一次性安裝

```powershell
# 1. Clone repo
git clone https://github.com/Lanternko/ARAM-Mayhem-Database.git
cd ARAM-Mayhem-Database

# 2. 安裝 Python 套件（需要 Python 3.13+）
python -m pip install -e .
```

---

## 每次貢獻流程（4 步）

### 1. 打開 League 客戶端
不需要在玩，客戶端登入在線即可（collector 透過本機 LCU API 抓你 + 最近對手的對戰）。

### 2. 跑 collector 一段時間
最簡單一行（PowerShell 整段貼成一行）：
```powershell
python scripts/lcu_collector.py auto-collect --rounds 50 --target-games 500 --max-players 1000 --opgg-tier platinum --opgg-tier gold
```
跑越久收越多。中斷再跑會自動續傳（SQLite + crawl frontier 都是持久化的）。

查目前蒐集多少場：
```powershell
python scripts/lcu_collector.py status
```

### 3. 匯出 + 自動開 Issue（推薦）
```powershell
python scripts/lcu_collector.py export-share --queue 2400 --patch-prefix 16.10 --auto-issue
```
這一行會：
1. 產出兩個檔到 `data/share/`：
   - `share_<時間戳>.db` — SQLite 原檔（無 PUUID）
   - `share_<時間戳>.db.zip` — **要上傳的是這個**（GitHub 不接受 `.db` 副檔名）
2. **自動開瀏覽器**到 [Lanternko/ARAM-Mayhem-Database 的 Issue 頁](https://github.com/Lanternko/ARAM-Mayhem-Database/issues/new?template=contribute-data.md)，title / 摘要 / 隱私 checklist 全部 pre-fill 好

你只需要在瀏覽器分頁裡：
- **把 `share_<時間戳>.db.zip` 拖進留言框**（不是 `.db`！）
- 按 **Submit new issue**

> ⚠ 為什麼預設加 `--patch-prefix 16.10`：tier list 是 patch-sensitive，混 patch 資料會稀釋訊號。你可以改成當前最新 patch（例如 `--patch-prefix 16.11`）。若不加，會把所有歷史 patch 一起送，腳本會印警告。

終端會印類似：
```
[export-share] wrote D:\...\data\share\share_<時間戳>.db.zip
  games    : 500 (filtered from 38484)
  queues   : 2400=500
  blue_wr  : 0.518
  patches  : 16.10.776=500
  file size: 78.1KB (zipped from 142.3KB)
  contents : games table only - no PUUIDs, no crawl frontier
  open dir : file:///D:/.../data/share   (Ctrl+click in most terminals)

Pre-filled GitHub Issue ready:
  title : [data] 16.10 - 500 games
  ...
Opening in your default browser...
```

> 為什麼還是要手動拖檔？GitHub REST API 沒有「附檔到 Issue」的 endpoint，drag-drop 是唯一的途徑。

可選參數：
- `--patch-prefix 16.10` — 只匯出特定 patch（檔案太大時用）
- `--queue 450 --queue 2400` — 同時匯出 ARAM 和 Mayhem
- `--out my_share.db` — 自訂輸出路徑
- `--no-auto-issue` — 不想開瀏覽器（預設行為，省略 `--auto-issue` 即可）
- `--print-issue-url` — 只印 URL 不開瀏覽器（給 server / CI 環境用）

### 4. （手動 fallback）只想自己開 Issue
若不加 `--auto-issue`，產完 `.db` 後自己到 [Issues 頁面](https://github.com/Lanternko/ARAM-Mayhem-Database/issues/new/choose) → 選 **"Contribute Match Data"** template → 拖檔 + 貼摘要 + Submit。

> ⚠ GitHub Issue 附件**上限 25 MB**。一般 500-5000 場應該都遠低於此。如果超過，用 `--patch-prefix` 分多檔案多 Issue 提交。

---

## 維護者怎麼處理收進來的檔

（自己 fork 來跑也可以這樣 merge 別人的）

```powershell
# 1. 從 Issue 把附檔（.db.zip）下載到 data/share/incoming/
# 2. 先驗證（verify-share 會自動解 .zip）
python scripts/lcu_collector.py verify-share data/share/incoming/*.zip

# 3. 沒問題後解壓再合進主 db（merge-db 只吃 .db，所以先 Expand-Archive 一下）
Get-ChildItem data/share/incoming/*.zip | ForEach-Object { Expand-Archive $_.FullName -DestinationPath data/share/incoming -Force }
python scripts/lcu_collector.py merge-db --out-db data/lcu/games_merged.db --glob "data/share/incoming/*.db"

# 4. 確認合進 N 場、blue_wr 正常後，再 overwrite 主 db
```

`verify-share` 會檢查：schema 正確、沒夾帶 PUUID 表、沒有 PUUID-like 欄位在 participants_json、每場 5+5 英雄、blue_wr 落在合理區間 (0.40-0.60)、game_id 不重複、queue 是 450/2400。任何異常會印 `WARN` + 原因。

去重靠 Riot match id (`game_id`)，所以同一場被多人貢獻也只算一次。

---

## 常見問題

**Q: 我的 game_id 跟別人重複會怎樣？**
合併時 `INSERT OR IGNORE` 跳過，不會影響統計。

**Q: 我跑了一週才 200 場，這樣值得貢獻嗎？**
值得。LCU 每帳號每 session 只能看最近 ~20 場，多元 seed 比量重要。

**Q: 我可以直接送 PR 嗎？**
不建議。`.db` 是 binary，commit 進 git 會永久留存（包含未來想撤回的情境）。Issue 附件可以隨 Issue 一起刪。

**Q: 我貢獻的資料什麼時候會出現在網站？**
看維護者 batch 的頻率（目前約週更）。每次 batch 完 commit message 會列哪些 Issue 已併入。

---

## 法律 / 隱私

- 你貢獻的資料只含 game_id（Riot 內部編號）、英雄 IDs、augment IDs、勝負 — **沒有任何 PUUID / 召喚師名稱 / 帳號識別**
- 資料隨 [tier list 網站](https://lanternko.github.io/ARAM-Mayhem-Database/) 公開可下載（MIT License）
- 不會被用於訓練商業模型或轉售
