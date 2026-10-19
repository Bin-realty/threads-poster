# 房仲工作系統 — 給 Claude Code 的說明

使用者：蘇聖斌（台中房仲，台灣房屋松竹十一期特許加盟店專案經理）。目標：月月成交、月收 30 萬以上；這套系統之後要賣給其他房仲，**介面越簡單越好，每個按鈕的字一般人要看得懂**。
一律用繁體中文回覆。先做再講結果，不重述過程。改功能前先確認需求，改完一定自己測過。

## 系統全貌（三塊）
| 區塊 | 在哪裡 | 誰負責改 |
|---|---|---|
| 追蹤台（客戶、庫存、行程、Threads 文案） | 頁面：本 repo `docs/tracker.html`（https://bin-realty.github.io/threads-poster/tracker.html）。資料：**私人 repo `Bin-realty/realty-data`** 的 `data/<collection>/<id>.json`（本機 `C:\Users\GIGABYTE\realty-data`）。舊版 artifact `https://claude.ai/artifact/X1cGrdja16py9etPVRceza` 當備份；`tracker/index.html` 是舊版原始碼 | 全部 Code：改頁面 push 本 repo；改資料 push realty-data。搬家步驟見 realty-data 的 MIGRATION.md |
| Threads-Bin（排程發 Threads 貼文） | `docs/index.html`（GitHub Pages：https://bin-realty.github.io/threads-poster/）＋ `scripts/*.py`（GitHub Actions） | Code 改完直接 push 到 main 就上線 |
| 每日晨報、口述回報、Google 日曆、售資 PDF | Code 的排程任務（指示在 realty-data `docs/晨報指示.md`）＋Code 對話（口述回報直接改 realty-data 的 data/ 並 push；日曆用 Google Calendar 連接器） | Code |

**本 repo 是公開的**（GitHub Pages 免費版要公開）。客戶、屋主、電話、門牌、底價、規則文件一律只放 realty-data，**絕不 commit 進 threads-poster**。

雲端執行器（舊）：排程任務「［發脆］執行器」是舊追蹤台用來叫醒 Claude 寫 repo 的中間人。新版追蹤台直接讀寫 repo，不需要它；搬家完成後可停用。

## Threads-Bin（docs/index.html）
- 五個書籤：已發 › 數據 › 留言 › 草稿 › 排程。
- 狀態流程：草稿（可改）→「確認，可以發了」→ **待排**（鎖定）→ 排程分頁勾選、選時間、「開始排程發文」→ 已排程（鎖定，「取消」退回草稿）→ 待發 → 處理中 → 已發布 → 刪除中 → 已刪除。失敗 → 回草稿分頁「需要處理」。
- 草稿編輯器：文案、上傳照片、貼 Google 相簿連結同步（寫 `sync/<slug>`）、點照片排順序（先點＝封面）。
- 後端：`scripts/queue.py` 只處理 待發／已排程／刪除中／輪播；`threads.py` 發文、刪文、同步相簿、抓數據與留言。`drafts/<id>.json` 是每篇貼文；`listings/<slug>/album|thumbs` 是照片；`data/listings.json` 是追蹤台同步過來的物件規格（不含屋主、底價、門牌號）。
- 同事物件：草稿 `ext {coId,name,ask,agent,spec}`、`listing` 空字串；72 小時防洗版不擋同事物件。
- 登入：頁面用使用者自己貼的 GitHub fine-grained token（threads-poster＋realty-data 的 Contents 讀寫），存瀏覽器 localStorage `fct_pat`（追蹤台同網域共用）。**Code 不要碰、不要印任何 token**（`token.enc`、Secrets、`fct_pat`）。

## 追蹤台（docs/tracker.html；照 tracker/index.html 原樣，只換資料層）
- 資料層 `gdb` 模仿舊 `window.claude.use("db")`（collection/onSnapshot/add/doc.update/set/delete），讀寫 realty-data；寫入排隊、衝突重讀再寫。新增 id：clients＝`c`+三位數編號、listings＝L 編號、其他亂數。
- 不再叫醒雲端執行器：送到 Threads-Bin／待發／排程直接寫 threads-poster `drafts/<id>.json`；同步照片寫 `sync/<slug>`；回覆留言寫 `outbox/<id>.txt`、不回寫 `handled/<id>.txt`；同事物件刪除直接改 drafts 與 schedule.json；物件規格寫 `data/listings.json`。
- 貼文狀態、數據、留言、縮圖從 threads-poster 讀（drafts、stats.json、comments.json、reply_log.json、listings/<slug>/thumbs），只疊在畫面上、不回寫 realty-data。
- AI 建議／整理／AI 寫文案：按鈕提示「到 Claude Code 對話說…」，由 Code 在對話裡做。Google 日曆：頁面只記 calendarOps，聖斌說「同步日曆」時 Code 讀 calendarOps 用連接器處理，做完把 status 改「完成」、寫 doneAt。
- collections：clients（買方）、leads（待核對）、listings（庫存＋開發中屋主）、events（行程）、calendarOps（Google 日曆待同步）、posts、coListings（同事物件）、coCleanup、comments、jobs。
- 客戶分類與追蹤頻率：斡旋買方每天、A買 2 天、B買每週 2 次、C買每週 1 次。
- 庫存狀態：一般委託中／專任委託中／開發中／斡旋中／已成交／下架。forBuyer＝買帶開發；devHot＝有機會委託。
- 空白欄位存 ""（頁面紅框），不寫「未記錄」。售價格式「3,080萬」。地址以「台中市」開頭。屋齡由完工日（民國 111.02.23）推算，不抄廣告。
- 刪除鈕都是按兩次；第一下不改字，用 toast 提示。
- 行程：完成（留紀錄）、取消（追蹤台＋Google 日曆都移掉，客戶卡留一行）、刪除（只刪追蹤台）；日曆改動寫 calendarOps，等 Code 處理。

## 文案鐵則（AI 寫文案、發文前檢查都要守）
- 署名固定三行，一字不改：
  ```
  蘇聖斌 0958-810-178（手機可加LINE）
  經紀人：林建宏 (103)00667｜營業員：蘇聖斌 (111)416424
  台灣房屋 松竹十一期特許加盟店／尚峰不動產有限公司
  ```
- 只稱「聖斌／蘇聖斌」，不用綽號。主文不放外部連結；LINE 連結只放自動留言（line.me/R/ti/p/@a0888，不可寫「@a0888」）。
- 門牌只到路名；不可出現 三瀛／獨家／未曝光／底價／內部／同業；有車位時建坪要註明「（含車位）」；只用資料裡的事實，不編數字、不保證。
- 主文 ≤500 字；同物件 72 小時內不重發。

## 開發慣例
- **每次改完一定要 commit＋push 到 GitHub main，不留在本機。**本機（聖斌的電腦或 Code 的工作環境）只是副本，電腦掛了沒 push 的改動就不見。資料檔（data/）也一樣：記完客戶回報馬上 push。
- 單一 HTML、無框架、無 build；CSS/JS 都在檔內。深色模式用 CSS 變數。手機寬度（390px）不可橫向捲動。
- 改 UI 文字時想「賣給別的房仲看得懂嗎」：不用「看」「排貼文進某天」這種字，用「預覽」「開始排程發文」。
- 寫死在頁面的署名、電話、證號、LINE、明星學區名單、repo 名稱，之後要做成設定頁（待辦）。

## 測試
- 這台電腦（聖斌的 Windows）沒有 node／Python／Playwright。改用 Claude 內建瀏覽器：本機跑一個假的 GitHub API（PowerShell HttpListener，資料用 repo 副本），頁面把 `api.github.com` 換成 localhost，跑一遍主要按鈕，確認無錯誤、390px 無橫向溢出。**不要拿真的 repo 測寫入。**
- Threads 真的發文前可在草稿加 `"dry": true` 試跑。

## 待辦（做之前先跟聖斌確認）
0. **搬家（realty-data 的 MIGRATION.md）**：第 1、2 步已做（2026-10-19），等聖斌驗收新追蹤台；驗收過才做第 3 步資料整理、第 4 步晨報排程。
1. 實測草稿編輯器「上傳照片」「同步 Google 相簿」照片真的進 `listings/<slug>/` 並隨貼文發出。
2. 同事物件草稿在 queue.py 發文時規格、照片正常。
3. 設定頁：署名／電話／證號／LINE／學區名單／repo 可自行填。
4. 之後再談多使用者後端（每個使用者各自的 Threads 鑰匙與 repo 不可行，要有真正的後端）。
