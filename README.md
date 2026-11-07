# 收件匣（claude-inbox）

給 Claude Code 用的收件匣。agent 要你做決定時，不在對話裡等，而是把題目放進一個網頁。你有空時一次看完、一次回答，答案會自動回到 agent 手上。

另外附一份「總PM」操作說明：一個常駐對話帶好幾條工作線，你只看收件匣。

## 你會得到什麼

| 東西 | 做什麼 |
|---|---|
| `inbox` 指令 | agent 用 `inbox ask` 出題；`inbox take`／`inbox wait` 取回答 |
| 網頁 | 看題、選選項、寫補充、「這題先送」或「送出全部已答」 |
| hook | 你回答後，agent 下一輪開始時自動收到答案，不用常駐轉送程式 |
| `inbox` 技能 | 教 agent 什麼時候出題、怎麼出題、出完之後做什麼 |
| `pm` 技能 | 總PM 操作說明，輸入 `/pm` 啟用 |

需要 Python 3.9 以上和 Claude Code，支援 macOS、Linux 與 WSL（原生 Windows 不支援）。沒有其他依賴，也不叫任何 AI；題目的文字就是 agent 寫的那一份。

## 安裝

在 Claude Code 裡：

```
/plugin marketplace add <這個 repo 的位置>
/plugin install inbox@claude-inbox
```

本機試用不安裝：`claude --plugin-dir <這個資料夾>`。

## 使用

1. 對 agent 說「有要我決定的事就放收件匣」，或在專案的 `CLAUDE.md` 寫一句。
2. agent 出題後會印出網址，預設是 `http://localhost:7799/?t=<token>`。要用印出來的完整網址開；之後忘了網址就跑 `inbox url`。
3. 你在網頁上回答。agent 在下一輪（或它背景跑的 `inbox wait` 結束時）收到答案。

手動出題看看。`inbox` 指令是給 agent 的 Bash 用的；你自己在終端機試的話，改用 `python3 <plugin 資料夾>/inbox/inbox.py`，後面參數一樣：

```bash
inbox ask --title "要不要上新版？" --option "A=上" --option "B=不上" --recommend A
inbox list          # 看題目與狀態
inbox close <題號>  # 處理完收起來
```

## 從手機看

預設只開在本機。要用手機看，編輯 `~/.claude-inbox/config.json`：

```json
{ "host": "你的 Tailscale 或內網位址", "port": 7799 }
```

位址只支援 IPv4。token 不分本機或遠端，第一次使用時自動產生，寫進同一個檔（權限 600，資料夾權限 700）。改位址或換 token 後都要重新啟動服務：`inbox stop` 再 `inbox start`。執行中的服務不會自動套用新設定，舊 token 在它重啟前仍然有效。

## 資料放哪

`~/.claude-inbox/`（可用環境變數 `CLAUDE_INBOX_HOME` 改）：

| 檔 | 內容 |
|---|---|
| `questions/*.json` | 題目，一題一檔 |
| `events.jsonl` | 你的回答、送達紀錄、收起紀錄，只增不改 |
| `config.json` | 位址、埠、token |

題目與答案都是明文，不要放密碼或金鑰。

## 答案送到哪個對話

答案送回出題的那個對話。那個對話 30 分鐘內沒來拿，同一個專案（同一個 git 根目錄）裡的其他對話也拿得到。每則答案只送出一次。

## 安全

- 只收 `Host` 與 `Origin` 符合的請求，擋掉別的網頁偷打你的本機服務。
- 所有 API 都要 token，本機也一樣：答案會被放進 agent 的上下文，不能讓同一台機器上的其他程式隨便寫入。
- 頁面把題目文字當純文字顯示，網址才變成連結。
- 回答只在你的機器上流動，沒有任何外送。

## 開發

```bash
python3 -m unittest discover tests
```

## 授權

MIT
