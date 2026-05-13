# ARAM Mayhem 資料收集器

幫助訓練「**看到這 10 隻，你有多少機率贏？**」的 AI 模型。  
只需三步，10–30 分鐘，不需要任何程式知識。

---

## 步驟一：下載執行檔

前往 **[Releases 頁面](https://github.com/Lanternko/ARAM-mayhem-collector/releases/latest)** 下載 `ARAM-collector.exe`

---

## 步驟二：執行收集

1. **開啟 League of Legends 客戶端並登入帳號**
2. **雙擊 `ARAM-collector.exe`**
3. 等待視窗自動完成（10–30 分鐘）

執行時你會看到這樣的輸出，代表正在收集中：

```
[collect] Starting 4-worker snowball crawl...
  games=100  pending=500  in_progress=8
  games=200  pending=450  in_progress=8
  ...
[collect] Done!  ->  my_games.parquet
按任意鍵關閉視窗...
```

完成後，`my_games.parquet` 會出現在**和執行檔同一個資料夾**。

---

## 步驟三：上傳資料

前往 **[📦 上傳資料在這裡](https://github.com/Lanternko/ARAM-mayhem-collector/discussions/1)**，把 `my_games.parquet` **直接拖進留言框**，然後送出。

建議留言格式：
```
伺服器：TW2
場次數：3200
```

> 需要 GitHub 帳號才能上傳。[免費註冊](https://github.com/signup)，不需要信用卡。

---

## 常見問題

**Q：my_games.parquet 在哪裡？**  
在 `ARAM-collector.exe` 同一個資料夾裡。

**Q：程式卡住了怎麼辦？**  
直接關掉視窗，下次重跑會從上次繼續（資料不會遺失）。

**Q：我不在 TW2 伺服器怎麼辦？**  
用命令提示字元（cmd）執行：  
```
ARAM-collector.exe run --platform KR
```
把 `KR` 換成你的伺服器代碼（`EUW1` / `NA1` / `JP1` / `SG2`）。

**Q：我的資料安全嗎？**  
`my_games.parquet` 裡只有**英雄 ID、勝負、遊戲時長、版本號**，完全不含帳號名稱或 ID。

---

## 進階用法（熟悉 Python 的使用者）

如果你不想用 exe，可以直接用 Python：

```bash
# 下載 ZIP 解壓縮後，進入資料夾
pip install -r requirements.txt

# 一鍵收集（等同雙擊 exe）
python lcu_collector.py run --platform TW2

# 收集完後查看狀態
python lcu_collector.py status
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
