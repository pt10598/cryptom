# 現貨價差監控

同一個專案可在 Windows 本機開網頁，也可部署到 Heroku。公開行情免 API Key。自動盤口為 Bybit、OKX、Bitget、Kraken。預設 35 個 USDT 幣對，加上 ETH/BTC、SOL/BTC、BTC/USDC、ETH/USDC 候選，共 39 個。ATRIX 的 USDT/TWD 自動換算等待官方可用的報價來源。頁面提供估算價差、一檔容量和正淨利持續秒數。程式僅監控行情。

## Windows 本機啟動

解壓縮，進到包含 app.py 的資料夾。在 PowerShell 執行：

```powershell
py -m pip install -r requirements.txt
py -m uvicorn app:app --host 127.0.0.1 --port 8000
```

建議使用 Python 3.12。用瀏覽器開 <http://127.0.0.1:8000>。按 Ctrl+C 關閉。

## GitHub → Heroku

1. 把此資料夾**裡面的檔案**放在 GitHub repo 根目錄，包含 `.python-version`、`Procfile` 和 `requirements.txt`。
2. 在 Heroku 建立使用 Common Runtime 的 Python app。在 Deploy 選 GitHub、連線 repo，部署分支。可選擇啟用自動部署。
3. 在 Resources 確認 web dyno 已啟用，使用一個 web dyno 即可。它會在同一程序接行情與提供網頁。
4. 打開 Heroku 的 app 網址，查看上方四家交易所的狀態。若要在沒人開頁面時繼續收行情，請選 Basic 或以上。重新部署或 dyno 重啟時會重新連線。

Heroku 網址上的行情可由任何取得網址的人讀取。這個版本沒有帳號、API Key、交易指令。若某家交易所不接受 Heroku 所在區域的連線，頁面會顯示重連錯誤。可用本機比對驗證；要使用主機商及交易所均支援的部署區域。

## 設定

本機透過 PowerShell 環境變數設定，例如 `$env:NOTIONAL_USDT = "50"`。Heroku 使用 Settings → Config Vars。變更後重新啟動。

| 名稱 | 預設 | 說明 |
|---|---|---|
| `PAIRS` | 39 個候選幣對 | 逗號分隔，最多 50 個，格式 `ETH/BTC`。舊格式 `BTCUSDT` 仍可使用。若舊版已設 PAIRS，更新或清空它才能看到新版預設。 |
| `NOTIONAL_USDT`、`NOTIONAL_USDC` | 各 `100` | 相應報價幣的買入試算金額 |
| `NOTIONAL_BTC`、`NOTIONAL_ETH` | `0.001`、`0.05` | 相應報價幣的買入試算金額 |
| `BYBIT_FEE_PCT`、`OKX_FEE_PCT`、`BITGET_FEE_PCT`、`KRAKEN_FEE_PCT` | 各 `0.10` | 試算值，請改成你的帳戶和幣對實際 taker 費率 |
| `BUFFER_PCT` | `0.10` | 額外預留成本，以買入試算金額為基礎 |
| `MAX_AGE_MS` | `1000` | 報價由後端收到後可保留的時間 |
| `MAX_SKEW_MS` | `500` | 兩邊在後端的接收時間最大差值 |

網頁約每 200 ms 更新一次估算。報價年齡從後端收到訊息時起算。持續秒數按照後端取樣估計，只在連續取樣皆符合掛單量、時效與正淨利時累計。估算以兩邊第一檔掛單計算，未含實際轉帳與庫存再平衡成本。可從 `/api/snapshot` 取得同一份 JSON。


跨幣對仍比較同一市場，例如各平台的 ETH/BTC，淨利與容量以 BTC 顯示。必須有至少兩家回傳足量且夠新的盤口。候選幣對不一定在各所共同掛牌。排行榜按報酬率排列。此版沒有用其他報價做換算或串接三角套利。

ATRIX 尚無經確認可接入的官方報價端點，目前不顯示台幣試算。

Bitget 公開 API 是否能從你目前的 Heroku 區域連線，請在部署後看連線狀態。美國列在 Bitget 條款的受限地區。


## 正淨利紀錄

部署此版後，後端即使無人開網頁，也會在服務運作期間記錄每個方向的正淨利區間，包括首次及最後一次觀察時間、最高估算淨利、最高估算淨報酬與最高一檔容量。單次取樣為正也會記錄，顯示持續 0 秒。每秒批次儲存，網頁約每 5 秒讀取最新 100 筆。這些是估算機會，沒有進行實際交易。舊版未儲存的觀察無法補回。

本機預設儲存至目前目錄的 SQLite 檔，或使用 HISTORY_PATH 自訂路徑。Heroku 若要跨重新部署或重啟保留歷史，需在同一個 app 連接 Heroku Postgres，確認 Heroku 有設定 DATABASE_URL。未連接時會以 dyno 內 SQLite 暫存，網頁會標示。使用一個 web dyno。資料庫網址請放 Config Vars。

新版擴充為 39 個候選。若 Heroku 自訂過 PAIRS，需清除舊值或把新交易對加進去。幣對在至少兩家回傳新鮮且足量的報價時才會試算。

## v4 轉幣搬磚試算

首頁連到 `/transfer`。選 A、B 與 USDT 現貨交易對，再輸入起始本金、USDT 轉入 A 的費用、標的幣轉入 B 的總扣量、USDT 回程費。費率使用現有的各所吃單費設定。買入手續費以標的幣扣除、賣出以 USDT 扣除；若實際使用折抵幣或其他計費方式，請依實際成本解讀。兩腿都檢查一檔掛單量。充提狀態、網路、最低金額、精度和預估到帳時間需依交易所自行核對。本頁不會送出交易或轉帳指令。

目前盤口試算使用當下兩邊的新鮮報價。歷史回放輸入起始時間，程式依「到 A 時刻 → A 第一筆可用報價 → 到 B 時刻 → B 第一筆可用報價」重算。目標時刻後 5 秒內有樣本才進行，不會回用更早的報價。盤口約每 2 秒取樣，保留約一小時在 dyno 記憶體，重新部署後從零開始收樣本。原有正淨利事件仍存 PostgreSQL。回放是依使用者設定的假設到帳時間，結果不代表實際成交。

## v4 IG／OANDA 行情

`/markets` 和 `/api/markets` 共用唯讀行情。為避免將券商帳戶報價公開，必須先設定 `MARKETS_PASSWORD` 才會啟動兩家券商行情。有設定時開頁會要求登入，帳號填 `viewer`，密碼填設定值。Heroku 請用 HTTPS。所有值放 Heroku Settings → Config Vars 或本機環境變數。

| 變數 | 用途 |
|---|---|
| `MARKETS_PASSWORD` | 行情頁登入密碼，建議設定一組獨立強密碼 |
| `OANDA_MODE` | `practice`（預設）或 `live` |
| `OANDA_ACCOUNT_ID` / `OANDA_TOKEN` | 對應環境的 OANDA v20 帳戶 ID 與 Token |
| `IG_MODE` | `demo`（預設）或 `live` |
| `IG_API_KEY` / `IG_IDENTIFIER` / `IG_PASSWORD` | IG API Key、登入帳號與密碼 |
| `IG_MARKETS_JSON` | 商品對照 JSON，例如 `{"EUR/USD":{"epic":"填入自己帳戶確認過的EPIC","divisor":10000}}` |

IG 要確認 EPIC 是同規格的現貨外匯或現貨金屬 CFD；`divisor` 是 IG 報價除數，例如畫面 10850 對應 1.0850 時填 10000，已直接以同一單位報價則填 1。以帳戶實際商品規格與報價驗證。OANDA 依帳戶支援商品訂閱串流。IG 每 4 秒讀取一次合併 REST 快照。只在兩邊來源時間足夠接近且為同類正式／模擬環境時計算方向差額，WTI 與 Brent 只列報價。部分帳戶、地區或 API 權限可能不支援，頁面會顯示 HTTP 狀態。FTMO、Topstep 不在本次接入範圍。
