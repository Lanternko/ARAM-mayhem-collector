# ARAM Mayhem 對戰資料收集器

幫助訓練「**看到這 10 隻，你有多少機率贏？**」的 AI 模型。

Riot 的公開 API 封鎖了 Mayhem (queueId 2400) 的對戰記錄。唯一取得方式是透過你電腦上的 League Client 本地 API。**每個人貢獻的資料越多，模型就越準確。**

---

## 步驟一：下載

點右上角綠色 **Code → Download ZIP**，解壓縮到任意資料夾。

或用 git：
```bash
git clone https://github.com/Lanternko/ARAM-mayhem-collector.git
```

## 步驟二：安裝並收集（一鍵）

1. 確認 League Client 已開啟並登入
2. 進入解壓縮的資料夾，**雙擊 `start.bat`**

> 需要 Python 3.10+。如果沒安裝：[python.org/downloads](https://www.python.org/downloads/)（安裝時勾選 **Add Python to PATH**）

程式會自動安裝依賴、開始收集，結束後產生 `my_games.parquet`。預計 **10–30 分鐘**。

你會看到這樣的輸出代表正在收集：
```
[saved] Mayhem  game_id=413476095  patch=16.9.772  total_saved=1
[saved] Mayhem  game_id=413477201  patch=16.9.772  total_saved=2
...
[export] 3200 games → my_games.parquet
```

## 步驟三：上傳資料

前往 **[📦 上傳資料在這裡](https://github.com/Lanternko/ARAM-mayhem-collector/discussions/1)**，在留言區回覆並附上 `my_games.parquet`（直接拖進去即可）。

留言格式建議：
```
伺服器：TW2
場次數：3200
```

---

## 常見問題

**Q：程式卡住了怎麼辦？**
直接關掉視窗，下次重跑會從上次繼續（資料不會遺失）。

**Q：我不在 TW 伺服器怎麼辦？**
用文字編輯器打開 `start.bat`，把 `TW2` 改成你的伺服器代碼（`KR` / `EUW1` / `NA1` / `JP1`）。

**Q：我的資料安全嗎？**
`my_games.parquet` 裡只有**英雄 ID、勝負、遊戲時長、版本號**，不含任何帳號名稱或 ID。

**Q：需要 GitHub 帳號嗎？**
上傳時需要。[免費註冊](https://github.com/signup)，不需要信用卡。

---

## 進階（給熟悉 Python 的使用者）

```bash
cd aram-mayhem-collector
pip install -r requirements.txt

# 收集
python collect.py --workers 8 --platform TW2

# 查看進度
python lcu_collector.py status

# 手動 export
python lcu_collector.py export --queue 2400 --out my_games.parquet --platform TW2
```

---

## 資料格式

| 欄位 | 說明 |
|------|------|
| `match_id` | 全球唯一比賽 ID |
| `queue_id` | 2400 = Mayhem |
| `patch` | 版本，例如 `16.9.772` |
| `platform` | 伺服器，例如 `TW2` |
| `blue_champions` | 藍方英雄 ID 列表 |
| `red_champions` | 紅方英雄 ID 列表 |
| `blue_wins` | 藍方是否獲勝 |
| `duration_sec` | 遊戲時長（秒）|
