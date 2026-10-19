# 阿斌 Threads 物件自動發文

用 Threads 官方 API，把物件照片＋文案一次發到 @a0958810178。

## ⚠️ 這個空間是公開的
- 只放**已確定要公開**的照片與文案（本來就會發到 Threads 上）
- **不要**放屋主姓名、電話、門牌號碼、委託資料
- Threads 鑰匙**加密**存放（token.enc），解密金鑰只在 GitHub Secrets

## 物件資料夾格式
```
listings/<英文代號>/
  post.txt      主文（500 字內）
  reply.txt     發文後自動補的第一則留言（可省略）
  photos/       照片 01.jpg、02.jpg…（依檔名排序，最多 20 張，第一張是封面）
  posted.json   發文紀錄（系統自動產生）
```

## 怎麼發文
在 `publish/` 放一個跟物件代號同名的檔案，內容寫：
- `post`：正式發布
- `dry`：試跑，檢查照片與文字但**不發布**
- `force`：忽略「3 天內不重複發」的防洗版限制

推上 GitHub 後約 1～2 分鐘自動發出，結果在 Actions 頁面。

也可以在 GitHub 網頁 → Actions → 「發文 Post to Threads」→ Run workflow 手動發。

## 鑰匙
- 每週一凌晨自動續期（Actions →「鑰匙續期」）
- 換新鑰匙：更新 Secret `THREADS_TOKEN` 後，手動執行「鑰匙續期」選 `reseed`
- Threads 帳號要維持**公開**，否則無法續期
