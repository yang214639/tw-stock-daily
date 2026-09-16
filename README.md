# 台股盤勢追蹤資料管線

每個工作日在 GitHub Actions 上抓取台股資料、計算技術/籌碼/評價指標、預先判定訊號,
輸出一份約 25 KB 的 `daily.json`,供 Claude 排程任務讀取後產生報告。

**設定流程請看 [SETUP.md](SETUP.md)。**

| 檔案 | 用途 |
|---|---|
| `fetch_and_compute.py` | 抓取、計算、訊號預判、輸出 daily.json |
| `indicators.py` | KD / MACD / RSI / MA,兩套獨立實作互相交叉驗算 |
| `config.json` | 追蹤標的、候選池、篩選門檻 |
| `scheduled_prompt.md` | 貼進 Claude 排程任務的 prompt |
| `test_offline.py` | 用合成資料跑的離線端對端測試 |
| `daily.json` | 每日輸出(自動產生) |
| `data/` | 日線快取,供增量更新(自動產生) |

指標定義:KD 9日(K=2/3·K₋₁+1/3·RSV,D 同法,種子 50)、MACD 12/26/9(EMA 以前 n 日
SMA 起始)、RSI Wilder 平滑。每次執行都會用兩套獨立實作交叉比對,不一致就不輸出。
