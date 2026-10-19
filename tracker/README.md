# 追蹤台（聖斌客戶追蹤台）原始碼

- 線上版：https://claude.ai/artifact/X1cGrdja16py9etPVRceza（Claude artifact，資料庫也在 artifact 裡）
- 這裡的 index.html 是頁面原始碼備份。**改完 push 不會自動上線**：要在「房仲工作系統」專案對話說「發布追蹤台」，由那邊的 Claude 抓 repo 最新檔發布。
- 頁面在 Claude 裡載入時會拿到 window.claude（db / sample / mcp）；離線測試用 Playwright 模擬（見 CLAUDE.md「測試」）。
- 分頁：銷售（clients、leads）、庫存（listings）、行程（events、calendarOps）、Threads（posts、coListings、coCleanup、comments、jobs）。
