# 台股盤勢追蹤 — GitHub Actions 資料管線設定流程

## 這在解決什麼問題

Claude 排程執行的沙箱,對外網路走白名單,`api.finmindtrade.com`、TWSE、TPEx 全部被擋(403)。
以前繞路的作法是把原始資料讀進模型 context 再逐格寫出,單次曾燒掉約 160 萬 token。

改成兩段式之後:

```
每天 22:00(台灣)  GitHub Actions(網路正常)
                   → 抓 FinMind 原始日線/法人/融資券/營收/本益比
                   → 在那邊算完 MA/KD/MACD/RSI/乖離/百分位,並預先判定訊號
                   → 輸出約 25 KB 的 daily.json,commit 回 repo
                            ↓
每天 23:00(台灣)  Claude 排程(只能連 raw.githubusercontent.com)
                   → curl 這個 daily.json
                   → 寫報告、推播
```

原始資料(數百 KB~數 MB)完全不經過模型。實測沙箱可連 `raw.githubusercontent.com`(200)。

---

## 一、建立 repo

1. 到 https://github.com 註冊/登入。
2. 右上角 **+** → **New repository**。
3. Repository name 隨意,例如 `tw-stock-daily`。
4. 選 **Public**(公開)。
   > 為什麼要公開:私人 repo 的 raw 連結需要 token,而 token 就得寫進排程 prompt 裡。
   > 股價是公開資料,放公開 repo 沒有隱私問題 — 只要別把持股部位或成本寫進去。
5. 勾選 **Add a README file**,按 **Create repository**。

## 二、放檔案

在 repo 頁面用 **Add file → Create new file**(或 Upload files)建立以下結構:

```
tw-stock-daily/
├─ fetch_and_compute.py          ← 主程式:抓取 + 計算 + 訊號預判
├─ indicators.py                 ← 技術指標(兩套獨立實作,互相交叉驗算)
├─ config.json                   ← 追蹤標的、候選池、篩選門檻
├─ test_offline.py               ← 離線測試(可選,不影響運作)
└─ .github/workflows/daily.yml   ← 排程設定
```

> 建立 `.github/workflows/daily.yml` 時,在檔名欄直接打完整路徑
> `.github/workflows/daily.yml`,GitHub 會自動建出資料夾。

`data/` 和 `daily.json` 不用自己建,第一次執行後會自動產生。

## 三、第一次執行(重要)

1. 進入 repo 的 **Actions** 頁籤,如果出現綠色按鈕 *I understand my workflows, go ahead and enable them*,按下去。
2. 左側點 **台股盤勢資料管線** → 右側 **Run workflow**。
3. 把 **full_refresh** 勾成 `true`(第一次要抓完整歷史)→ 按 **Run workflow**。
4. 等 5～15 分鐘。第一次要抓約 37 檔 × 620 天,會比較久。
5. 跑完後回 repo 首頁,應該看得到新的 `daily.json` 和 `data/` 資料夾。

之後每個工作日 22:00(台灣時間)會自動執行,只抓增量,約 3～5 分鐘。

## 四、拿到 raw 連結

把下面的 `<USER>` 換成你的 GitHub 帳號、`<REPO>` 換成 repo 名稱:

```
https://raw.githubusercontent.com/<USER>/<REPO>/main/daily.json
```

在瀏覽器打開確認看得到 JSON 內容(不是 404)。
> 若 repo 預設分支叫 `master` 而不是 `main`,網址要跟著改。

## 五、換掉 Claude 排程的 prompt

把 `scheduled_prompt.md` 的內容整段貼進排程任務的 prompt,並把裡面的
`<USER>/<REPO>` 換成你的實際路徑。

原本那份 6000+ token 的長 prompt 可以整個丟掉 — 篩選條件、風險約束、訊號檢核清單
都已經寫進 `config.json` 和 `fetch_and_compute.py`,不需要每天重述一次給模型看。

## 六、時間設定

`.github/workflows/daily.yml` 裡的 cron 用 **UTC**:

| cron | 台灣時間 | 說明 |
|---|---|---|
| `0 14 * * 1-5` | 22:00 週一～週五 | 預設。此時收盤價、三大法人、融資券餘額都已公布 |
| `0 9 * * 1-5` | 17:00 | 較早,但融資融券餘額可能還沒更新 |

**Claude 排程要排在 Actions 之後至少 30 分鐘**,建議台灣時間 23:00(= `0 15 * * 1-5` UTC)。
GitHub 的 cron 在尖峰時可能延遲 5～20 分鐘,留點緩衝。

## 七、(可選)申請 FinMind token

匿名呼叫有較低的流量上限,標的一多容易被限流(腳本會自動退避重試,但會拖慢)。
到 https://finmindtrade.com 註冊免費帳號拿 token,然後:

repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
- Name: `FINMIND_TOKEN`
- Secret: 貼上你的 token

workflow 已經寫好會自動讀取,不用改程式。

---

## 疑難排解

**Actions 顯示紅色 ✗**
點進去看日誌。最常見是 FinMind 限流 — 腳本會退避重試,若整批失敗,隔天會自動補上
(價格是增量快取,不會掉資料)。連續失敗兩天以上再手動 Run 一次。

**報告說「資料日期已過期 N 天」**
代表 Actions 沒跑。先看 Actions 頁籤有沒有紅色失敗;若整個排程沒觸發,見下一則。

**排程突然不跑了**
GitHub 對「超過 60 天沒有人為活動」的 repo 會自動停用排程 workflow,而 bot 自己的
commit **不算**人為活動。GitHub 會寄信通知你,信裡有重新啟用的連結。
想避免的話,每隔一兩個月手動改一下 README 並 commit 一次就行。

**想調整追蹤標的或篩選門檻**
只改 `config.json`,程式和排程 prompt 都不用動(prompt 是泛用的,追蹤名單以
daily.json 為準)。在 GitHub 網頁上點開 `config.json` → 右上角鉛筆圖示 → 改完
**Commit changes**,下次執行就生效。

- `fixed` — 固定追蹤,做**減碼**訊號檢核
- `reverse` — 反向觀察,做**進場**訊號檢核
- `candidates` / `candidate_names` — 每日篩選的候選池
- `screen` — 各項風險約束的門檻數字
- 每檔的 `note` 欄位會被帶進報告,可以寫「特別盯融資餘額」這類提醒

**想確認指標算得對不對**
`python fetch_and_compute.py --selftest` 會用合成資料跑兩套獨立實作交叉比對。
正式執行時每一檔也都會跑同樣的交叉驗算,不一致就不輸出該檔的數字。

## 安全性

**這個 repo 是公開的,以下內容任何人都看得到:** `config.json` 的追蹤標的與候選池、
`daily.json` 的計算結果、`data/` 的日線快取。這等於公開你在盯哪些股票、以及你在對
3324/2308/1503/3231 找減碼訊號。若你不希望被推測持股,就把 `fixed` 換成不敏感的名單,
或改用私人 repo(代價是 raw 連結需要 token,而 token 得寫進排程 prompt)。
**絕對不要**把持股張數、成本價、帳戶資訊寫進任何檔案。

**程式本身的行為邊界:**

| 項目 | 狀況 |
|---|---|
| 網路行為 | 只有 HTTP GET,不送出任何資料。抓 FinMind 與 TWSE,無其他連線 |
| 危險呼叫 | 無 `eval` / `exec` / `pickle` / `subprocess` / shell 執行 |
| 外部資料處理 | 全部經過 `float()` + try/except 解析,不做動態執行 |
| 檔案寫入 | 只寫 `daily.json` 與 `data/price_<代號>.csv`,代號已過濾非英數字元 |
| Token 外洩 | 日誌一律經過 `_scrub()` 抹除 token;GitHub 另有 secret 遮罩 |
| 第三方程式碼執行 | workflow 只由 `schedule` 與 `workflow_dispatch` 觸發,不吃 fork PR |
| 套件供應鏈 | `pandas` / `numpy` 已釘版本 |

**資料信任邊界:**排程端把 `daily.json` 當資料而非指令(prompt 內已明寫)。
若 repo 被竄改,最壞情況是報告數字錯誤或出現異常文字,不會讓模型去執行別的事。

## 調整追蹤標的:實際怎麼做

### 加一檔固定追蹤

在 `config.json` 的 `fixed` 陣列加一筆:

```json
{"id": "2454", "name": "聯發科", "market": "twse", "note": ""}
```

`market` 填 `twse`(上市)或 `tpex`(上櫃)。上櫃會自動標記「單一來源、未經官方
二次驗證」;上市會多做一次 TWSE 官方收盤價比對。

下次執行時它沒有快取,會自動抓滿完整歷史(約多花 1～2 分鐘),不需要手動 full refresh。

### 換掉 / 移除一檔

直接從陣列刪掉那一筆就好。`data/price_<代號>.csv` 會留著——**這是刻意的**,
之後若想加回來,快取還在就不用重抓。檔案只有幾十 KB,留著不影響。
真想清乾淨就在 GitHub 上把該檔案刪掉。

### 加候選池

`candidates` 加代號、`candidate_names` 加中文名。每多一檔,每天只多一次 API 呼叫
(篩選第一階段只需要價格,通過後才抓法人和本益比),加到 50～60 檔都還好。
超過大約 80 檔就建議去申請 FinMind token,否則容易被限流。

### 改反向觀察標的

`reverse` 裡的標的走**進場**訊號邏輯。想多加一檔(例如 006208)就再加一筆;
想把某檔從「找減碼」改成「找進場」,就把它從 `fixed` 搬到 `reverse`。
ETF 記得加 `"type": "etf"`,它會自動跳過月營收。

### 改完怎麼確認沒打錯

工作流程每次執行的第一步就是檢查 `config.json`,有問題會直接紅字失敗、不會浪費
一整輪抓取。想先在本機確認:

```
python fetch_and_compute.py --check-config
```

它會抓出代號打錯、門檻寫成文字、候選池與固定追蹤重複、缺中文名等問題。

## 已知限制(誠實揭露)

- **3324 雙鴻是上櫃股**,TPEx 官方端點會擋非瀏覽器請求,只能用 FinMind 單一來源,
  無官方二次驗證。上市股票會額外用 TWSE 官方端點驗證收盤價。
- **主力分點進出**:FinMind 免費版沒有,該檢核項固定回報「資料不足」。
- **ETF 折溢價/淨值**:未串接來源,0050 該項固定回報「查無資料」。
- **趨勢線跌破/突破**:用「季線 + 季線斜率」當代理指標,不是真的畫趨勢線。
- **新聞面檢核項**:管線不碰新聞,標記 `needs_news`,由排程端最多 6 次搜尋補上。
- **除權息未還原(最需要注意的一項)**:FinMind 免費版的 `TaiwanStockPrice` 是原始
  價格,沒有還原權值。除息當天股價會有一段「非交易性」的下跌,均線、乖離、KD
  會因此輕微失真,影響通常持續到該筆資料滾出計算視窗為止。台股除權息旺季是 7～9 月,
  現在正好在區間內。管線只能擋掉單日超過 ±11%(台股漲跌幅上限外)的明顯斷層,
  **5% 上下的除息缺口擋不掉**。看到某檔均線莫名下彎時,先查它的除息日。
- 換個地方抓資料**不會讓資料變準**,來源一樣是 FinMind/TWSE。
- 訊號是用固定門檻機械判定的(例如「融資5日 ≥ +10%」),打勾不代表結論,
  只代表「這個數字越過了你設的線」。數字看起來精確,不等於判斷正確。
