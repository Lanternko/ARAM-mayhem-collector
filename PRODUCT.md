# ARAM Mayhem Database — PRODUCT context

## Product Purpose

一個給台灣英雄聯盟玩家的 **ARAM Mayhem** (queueId 2400) 中文 tier list 網站。
資料來自本機 League 客戶端 LCU snowball ~20k 場真實對局，純靜態 HTML，部署在 GitHub Pages。
**為什麼這站存在**：Riot 公開 API 已在 14.x 起整場移除 Mayhem queue，op.gg / u.gg 之類站台拿不到資料；本站是少數能看 Mayhem 統計的選擇。

## Register

**brand**。設計就是產品本體 — 不是內部工具、不是 dashboard。設計的功能是「讓 TW 玩家在比賽間隙能 30 秒內找到答案、並覺得這站有人在認真做」。

## Users

- 台灣 LoL 玩家（zh-Hant 中心，英雄英文 alias 為輔）
- 主要場景：手機在 lobby 等待時快速查英雄 → augment；少數會用桌機長時間瀏覽
- 心智狀態：剛打完上一場 / 等下一場開打，秒讀為王，沒耐心讀長文

## Brand Voice

- 中文為主、playful 但不幼稚
- 跟 Mayhem 本身的調性同步：augment 名稱本身就很跳（「斗內」「阿嬤的辣油」「棒棒回力鏢」）
- 文字寫得像「會 ARAM 的朋友隨手做的整理」，而不是工作報告

## Anti-References

- **op.gg / u.gg / mobalytics 的 SaaS-cream + 圓角卡片風** — generic、認不出哪一站
- **中國站台的簡體 + 神秘 QQ 群風** — 用詞與語境跟 TW 玩家斷層
- **Neon-cyberpunk 電競風** — 過時且每個 LoL 同人都這樣
- **Editorial Magazine 過度文人風** — Fraunces 之流，不適合工具型內容

## Strategic Principles

1. **訊息密度高**：172 英雄 × 199 augments；網格 + 互動篩選優先於分頁
2. **稀有度語言一致**：站台視覺呼應 Mayhem 內 augment 的 Prismatic / Gold / Silver 稀有度系統（OP = 棱彩、T1 = 金屬紅、T2–T5 純色）
3. **手機原生**：每行 6 個英雄 + 5 個 augment 的固定 grid，桌面 auto-fill
4. **無後端**：靜態檔 + Riot Data Dragon / CommunityDragon 公開 CDN 圖示，零維運成本
5. **資料新鮮度可見**：每次重 build 顯示 patch + 日期 + 樣本場數
