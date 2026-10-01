# ginzan-watch

每 5 分鐘（GitHub 排程，盡力而為）檢查銀山溫泉溫泉街旅館在 2027/1/2、1/3、1/4 入住是否有空房，
新出現的空房會推播到手機的 ntfy App。

## 設定
1. 手機安裝 ntfy（iOS / Android），訂閱一個只有你知道的主題名稱。
2. Repo Settings → Secrets and variables → Actions，新增：
   - `NTFY_TOPIC`：上面的主題名稱
   - `RAKUTEN_APP_ID`、`RAKUTEN_ACCESS_KEY`：在 https://webservice.rakuten.co.jp/ 建立應用程式取得
3. Actions → check vacancies → Run workflow，mode 填 `test-notify`，手機應收到測試通知。

人數、房數、日期與旅館清單都在 `config.json`。
