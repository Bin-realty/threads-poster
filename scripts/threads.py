#!/usr/bin/env python3
"""Threads 物件自動發文工具（Bin Realty）

指令：
  python scripts/threads.py process   處理 publish/ 資料夾裡的發文指令
  python scripts/threads.py refresh   續期 Threads 長效鑰匙（存在超過 24 小時才會續）
  python scripts/threads.py check     檢查鑰匙是否有效

鑰匙存放：
  - 第一次使用 GitHub Secret「THREADS_TOKEN」
  - 續期後的新鑰匙用 GitHub Secret「THREADS_APP_SECRET」加密，存成 token.enc
"""
import json
import re
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://graph.threads.com/v1.0"
GRAPH = "https://graph.threads.com"
ROOT = Path(__file__).resolve().parent.parent
TOKEN_ENC = ROOT / "token.enc"
TOKEN_META = ROOT / "token_meta.json"
LISTINGS = ROOT / "listings"
PUBLISH = ROOT / "publish"
SYNC = ROOT / "sync"
OUTBOX = ROOT / "outbox"
STATS = ROOT / "stats.json"
COMMENTS = ROOT / "comments.json"
REPLY_LOG = ROOT / "reply_log.json"
REPLY_CFG = ROOT / "config" / "reply.json"
METRICS = ["views", "likes", "replies", "reposts", "quotes", "shares"]
REPO = os.environ.get("GITHUB_REPOSITORY", "Bin-realty/threads-poster")
TZ = timezone(timedelta(hours=8))
MAX_TEXT = 500
MAX_CAROUSEL = 20
REPOST_GUARD_HOURS = 72
IMAGE_EXT = {".jpg", ".jpeg", ".png"}


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


# ---------- 鑰匙 ----------
def _key():
    k = os.environ.get("THREADS_APP_SECRET", "")
    if not k:
        sys.exit("缺少 GitHub Secret：THREADS_APP_SECRET")
    return k


def _openssl(args, data):
    env = dict(os.environ, TP_KEY=_key())
    r = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-a", "-A",
         "-pass", "env:TP_KEY", *args],
        input=data, capture_output=True, env=env)
    if r.returncode != 0:
        sys.exit("鑰匙加解密失敗（THREADS_APP_SECRET 可能被換過）")
    return r.stdout


def load_token():
    if os.environ.get("RESEED") == "1" or not TOKEN_ENC.exists():
        t = os.environ.get("THREADS_TOKEN", "").strip()
        if not t:
            sys.exit("缺少 GitHub Secret：THREADS_TOKEN")
        return t, "secret"
    return _openssl(["-d"], TOKEN_ENC.read_bytes()).decode().strip(), "file"


def save_token(token, expires_in):
    TOKEN_ENC.write_bytes(_openssl([], token.encode()))
    now = datetime.now(TZ)
    TOKEN_META.write_text(json.dumps({
        "refreshed_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(seconds=int(expires_in))).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- HTTP ----------
def call(method, url, params=None, retries=3):
    params = dict(params or {})
    data = None
    if method in ("GET", "DELETE"):
        url = url + "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, method=method)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            safe = body.replace(params.get("access_token", "\0"), "***")
            if e.code >= 500 and attempt < retries:
                log(f"伺服器暫時錯誤 {e.code}，{attempt * 5} 秒後重試")
                time.sleep(attempt * 5)
                continue
            raise RuntimeError(f"Threads API 錯誤 {e.code}：{safe}") from None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(attempt * 5)
                continue
            raise RuntimeError(f"連線失敗：{e}") from None


def wait_ready(cid, token, timeout=300):
    start = time.time()
    while time.time() - start < timeout:
        st = call("GET", f"{API}/{cid}", {"fields": "status,error_message", "access_token": token})
        s = st.get("status")
        if s in ("FINISHED", "PUBLISHED"):
            return
        if s in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"媒體處理失敗：{st.get('error_message', s)}")
        time.sleep(4)
    raise RuntimeError("媒體處理超時（5 分鐘）")


# ---------- 發文 ----------
def raw_url(rel_path):
    sha = os.environ.get("PHOTO_SHA") or os.environ.get("GITHUB_SHA", "main")
    return f"https://raw.githubusercontent.com/{REPO}/{sha}/" + urllib.parse.quote(rel_path)


def listing_files(slug):
    d = LISTINGS / slug
    if not d.is_dir():
        raise RuntimeError(f"找不到物件資料夾 listings/{slug}")
    text_file = d / "post.txt"
    if not text_file.exists():
        raise RuntimeError(f"listings/{slug} 缺少 post.txt")
    text = text_file.read_text(encoding="utf-8").strip()
    reply_file = d / "reply.txt"
    reply = reply_file.read_text(encoding="utf-8").strip() if reply_file.exists() else ""
    photos_dir = d / "photos"
    photos = sorted(p for p in photos_dir.iterdir() if p.suffix.lower() in IMAGE_EXT) if photos_dir.is_dir() else []
    return d, text, reply, photos


def validate(slug, text, reply, photos):
    problems = []
    if not text:
        problems.append("主文是空的")
    if len(text) > MAX_TEXT:
        problems.append(f"主文 {len(text)} 字，超過 {MAX_TEXT} 字上限")
    if len(reply) > MAX_TEXT:
        problems.append(f"留言 {len(reply)} 字，超過 {MAX_TEXT} 字上限")
    if len(photos) > MAX_CAROUSEL:
        problems.append(f"照片 {len(photos)} 張，超過 {MAX_CAROUSEL} 張上限")
    for p in photos:
        if p.stat().st_size > 8 * 1024 * 1024:
            problems.append(f"{p.name} 超過 8MB")
    if problems:
        raise RuntimeError(f"{slug} 檢查未通過：" + "；".join(problems))


def recent_post(d):
    f = d / "posted.json"
    if not f.exists():
        return None
    hist = json.loads(f.read_text(encoding="utf-8"))
    if not hist:
        return None
    last = hist[-1]
    t = datetime.fromisoformat(last["posted_at"])
    if datetime.now(TZ) - t < timedelta(hours=REPOST_GUARD_HOURS):
        return last
    return None


def parse_job(job):
    """publish/<slug> 內容：<mode> [排程時間ISO] [追蹤台貼文ID]"""
    parts = (job.read_text(encoding="utf-8").strip() or "post").split()
    mode = parts[0].lower()
    at = tid = None
    for x in parts[1:]:
        if re.match(r"^\d{4}-\d{2}-\d{2}T", x):
            at = datetime.fromisoformat(x.replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=TZ)
        else:
            tid = x
    return mode, at, tid


def is_due(at):
    return at is None or at <= datetime.now(TZ) + timedelta(minutes=2)


def record_failure(slug, tid, err):
    d = LISTINGS / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / "failed.json"
    hist = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    hist.append({"at": datetime.now(TZ).isoformat(timespec="seconds"), "tracker_id": tid, "error": str(err)[:300]})
    f.write_text(json.dumps(hist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def post_listing(slug, token, mode, tid=None):
    d, text, reply, photos = listing_files(slug)
    validate(slug, text, reply, photos)
    log(f"{slug}：主文 {len(text)} 字、照片 {len(photos)} 張、留言 {'有' if reply else '無'}，模式 {mode}")

    if mode != "force" and mode != "dry":
        last = recent_post(d)
        if last:
            raise RuntimeError(f"{slug} 在 {last['posted_at']} 剛發過（{REPOST_GUARD_HOURS} 小時內），"
                               f"為避免洗版已跳過。確定要再發請在指令檔寫 force")

    rel = lambda p: p.relative_to(ROOT).as_posix()
    if len(photos) >= 2:
        children = []
        for i, p in enumerate(photos, 1):
            r = call("POST", f"{API}/me/threads", {
                "media_type": "IMAGE", "image_url": raw_url(rel(p)),
                "is_carousel_item": "true", "access_token": token})
            children.append(r["id"])
            log(f"  照片 {i}/{len(photos)} 已送出（{p.name}）")
        for c in children:
            wait_ready(c, token)
        r = call("POST", f"{API}/me/threads", {
            "media_type": "CAROUSEL", "children": ",".join(children),
            "text": text, "access_token": token})
    elif len(photos) == 1:
        r = call("POST", f"{API}/me/threads", {
            "media_type": "IMAGE", "image_url": raw_url(rel(photos[0])),
            "text": text, "access_token": token})
    else:
        r = call("POST", f"{API}/me/threads", {"media_type": "TEXT", "text": text, "access_token": token})
    container = r["id"]
    wait_ready(container, token)
    log("  內容容器處理完成")

    if mode == "dry":
        log(f"✅ 試跑成功：{slug} 所有照片與文字都通過 Threads 檢查（未發布）")
        if tid:
            f = d / "dryrun.json"
            h = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
            h.append({"at": datetime.now(TZ).isoformat(timespec="seconds"), "tracker_id": tid, "photos": len(photos)})
            f.write_text(json.dumps(h[-20:], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return None

    pub = call("POST", f"{API}/me/threads_publish", {"creation_id": container, "access_token": token})
    post_id = pub["id"]
    info = call("GET", f"{API}/{post_id}", {"fields": "permalink", "access_token": token})
    link = info.get("permalink", "")
    log(f"✅ 已發布：{link}")

    reply_id = None
    if reply:
        time.sleep(5)
        rc = call("POST", f"{API}/me/threads", {
            "media_type": "TEXT", "text": reply, "reply_to_id": post_id, "access_token": token})
        wait_ready(rc["id"], token)
        rp = call("POST", f"{API}/me/threads_publish", {"creation_id": rc["id"], "access_token": token})
        reply_id = rp["id"]
        log("✅ 留言已發布")

    hist_f = d / "posted.json"
    hist = json.loads(hist_f.read_text(encoding="utf-8")) if hist_f.exists() else []
    hist.append({"posted_at": datetime.now(TZ).isoformat(timespec="seconds"),
                 "post_id": post_id, "reply_id": reply_id, "permalink": link,
                 "photos": len(photos), "tracker_id": tid})
    hist_f.write_text(json.dumps(hist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return link


# ---------- 從 Google 相簿匯入照片 ----------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-TW"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def album_photo_urls(link):
    import re
    html, _ = fetch(link)
    html = html.decode("utf-8", errors="replace")
    seen, out = set(), []
    for m in re.finditer(r"https://lh3\.googleusercontent\.com/pw/[A-Za-z0-9_\-]+", html):
        u = m.group(0)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_pick(text, n):
    picks = []
    for part in text.replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            picks += list(range(int(a), int(b) + 1))
        else:
            picks.append(int(part))
    return [i for i in picks if 1 <= i <= n]


def cmd_import():
    """發文前：有 album.txt、還沒有 photos/ 的物件，先從 Google 相簿下載照片"""
    changed = False
    for job in sorted(PUBLISH.glob("*")):
        if not job.is_file() or job.name.startswith("."):
            continue
        d = LISTINGS / job.stem
        album = d / "album.txt"
        photos = d / "photos"
        has_saved = (d / "album").is_dir() and any((d / "album").glob("*.jpg"))
        if (not album.exists() and not has_saved) or (photos.is_dir() and any(photos.iterdir())):
            continue
        link = album.read_text(encoding="utf-8").strip().split()[0] if album.exists() else ""
        pick_f = d / "pick.txt"
        meta_f = d / "album_meta.json"
        meta = json.loads(meta_f.read_text(encoding="utf-8")) if meta_f.exists() else {}
        saved = sorted((d / "album").glob("*.jpg")) if (d / "album").is_dir() else []
        photos.mkdir(parents=True, exist_ok=True)
        if saved and (not link or meta.get("link") == link or meta.get("source") == "upload"):
            # 用「同步照片」存下來的相簿，編號和追蹤台縮圖一致
            order = parse_pick(pick_f.read_text(encoding="utf-8"), len(saved)) if pick_f.exists() else list(range(1, len(saved) + 1))
            order = order[:MAX_CAROUSEL]
            for n, i in enumerate(order, 1):
                (photos / f"{n:02d}.jpg").write_bytes(saved[i - 1].read_bytes())
            log(f"✅ {job.stem}：用已同步相簿（{len(saved)} 張，{meta.get('synced_at','')}），選 {len(order)} 張")
        else:
            if not link:
                raise SystemExit(f"{job.stem}：沒有照片，也沒有相簿連結")
            urls = album_photo_urls(link)
            if not urls:
                raise SystemExit(f"{job.stem}：相簿裡找不到照片（連結失效或相簿未公開分享）")
            order = parse_pick(pick_f.read_text(encoding="utf-8"), len(urls)) if pick_f.exists() else list(range(1, len(urls) + 1))
            order = order[:MAX_CAROUSEL]
            for n, i in enumerate(order, 1):
                data, ctype = fetch(urls[i - 1] + "=w2048-h2048-rj")
                if not data.startswith(b"\xff\xd8"):
                    raise SystemExit(f"{job.stem}：第 {i} 張下載到的不是 JPEG（{ctype}）")
                (photos / f"{n:02d}.jpg").write_bytes(data)
            log(f"✅ {job.stem}：相簿共 {len(urls)} 張，已下載 {len(order)} 張")
        changed = True
    if not changed:
        log("沒有需要從相簿匯入的照片")


def cmd_sync():
    """同步照片：sync/<slug> 內容是 Google 相簿連結 → 整本相簿存到 listings/<slug>/album/，縮圖存到 thumbs/"""
    from PIL import Image, ImageOps
    import io
    jobs = sorted(p for p in SYNC.glob("*") if p.is_file() and not p.name.startswith("."))
    if not jobs:
        log("sync/ 沒有同步指令")
        return
    summary = []
    for job in jobs:
        slug = job.stem
        d = LISTINGS / slug
        try:
            link = job.read_text(encoding="utf-8").strip().split()[0]
            if not link:
                raise SystemExit(f"{job.stem}：沒有照片，也沒有相簿連結")
            urls = album_photo_urls(link)
            if not urls:
                raise RuntimeError("相簿裡找不到照片（連結失效或相簿未公開分享）")
            for sub in ("album", "thumbs"):
                (d / sub).mkdir(parents=True, exist_ok=True)
                for old in (d / sub).glob("*"):
                    old.unlink()
            for i, u in enumerate(urls, 1):
                data, ctype = fetch(u + "=w2048-h2048-rj")
                if not data.startswith(b"\xff\xd8"):
                    raise RuntimeError(f"第 {i} 張下載到的不是 JPEG（{ctype}）")
                (d / "album" / f"{i:02d}.jpg").write_bytes(data)
                im = ImageOps.fit(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB"), (240, 240))
                im.save(d / "thumbs" / f"{i:02d}.jpg", quality=78)
            (d / "album.txt").write_text(link + "\n", encoding="utf-8")
            (d / "album_meta.json").write_text(json.dumps({"link": link, "count": len(urls),
                "synced_at": datetime.now(TZ).isoformat(timespec="seconds")}, ensure_ascii=False) + "\n", encoding="utf-8")
            summary.append(f"- {slug}：✅ 同步 {len(urls)} 張")
            log(f"✅ {slug}：同步 {len(urls)} 張")
        except Exception as e:
            (d).mkdir(parents=True, exist_ok=True)
            (d / "sync_error.txt").write_text(f"{datetime.now(TZ).isoformat(timespec='seconds')} {e}\n", encoding="utf-8")
            summary.append(f"- {slug}：❌ {e}")
            log(f"❌ {slug}：{e}")
        job.unlink()
    write_summary("## 同步照片\n" + "\n".join(summary))


def cmd_process():
    jobs = sorted(p for p in PUBLISH.glob("*") if p.is_file() and not p.name.startswith("."))
    due = []
    for job in jobs:
        mode, at, tid = parse_job(job)
        if is_due(at):
            due.append((job, mode, tid))
        else:
            log(f"⏰ {job.stem}：排程 {at.astimezone(TZ):%m/%d %H:%M}，時間未到")
    if not due:
        log("publish/ 沒有到期的發文指令")
        return
    token, _ = load_token()
    summary, failed = [], False
    for job, mode, tid in due:
        slug = job.stem
        try:
            link = post_listing(slug, token, mode, tid)
            summary.append(f"- {slug}：" + ("試跑通過" if mode == "dry" else f"已發布 {link}"))
            job.unlink()
        except Exception as e:
            failed = True
            summary.append(f"- {slug}：❌ {e}")
            log(f"❌ {slug}：{e}")
            record_failure(slug, tid, e)
            job.unlink()
    write_summary("## 發文結果\n" + "\n".join(summary))
    if failed:
        sys.exit(1)


def cmd_delete():
    """刪文：delete/<slug> 檔案內每行一個 post_id（物件成交／下架時由追蹤台排入）"""
    ddir = ROOT / "delete"
    jobs = sorted(p for p in ddir.glob("*") if p.is_file() and not p.name.startswith(".")) if ddir.exists() else []
    if not jobs:
        log("delete/ 沒有待刪的貼文")
        return
    token, _ = load_token()
    summary, failed = [], False
    for job in jobs:
        slug = job.stem
        for pid in [x.strip() for x in job.read_text(encoding="utf-8").split() if x.strip()]:
            try:
                call("DELETE", f"{API}/{pid}", {"access_token": token})
                ok = f"已刪除 {pid}"
            except RuntimeError as e:
                if "does not exist" in str(e) or "Unsupported delete" in str(e) or "(#100)" in str(e):
                    ok = f"{pid} 已不存在（可能已手動刪除）"
                else:
                    failed = True
                    summary.append(f"- {slug}：❌ {pid} {e}")
                    log(f"❌ {slug} {pid}：{e}")
                    continue
            log(f"✅ {slug}：{ok}")
            summary.append(f"- {slug}：{ok}")
            hist_f = LISTINGS / slug / "posted.json"
            if hist_f.exists():
                hist = json.loads(hist_f.read_text(encoding="utf-8"))
                for h in hist:
                    if h.get("post_id") == pid and not h.get("deleted"):
                        h["deleted"] = datetime.now(TZ).isoformat(timespec="seconds") + " 自動刪文（成交／下架）"
                hist_f.write_text(json.dumps(hist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        job.unlink()
    write_summary("## 刪文結果\n" + "\n".join(summary))
    if failed:
        sys.exit(1)


def cmd_refresh():
    token, source = load_token()
    if source == "secret":
        # 剛從 Meta 產生的鑰匙未滿 24 小時不能續期，先加密存起來，下週自動續期
        save_token(token, 60 * 86400)
        log("✅ 已把 THREADS_TOKEN 加密存入 token.enc，之後每週自動續期")
        write_summary("## 鑰匙初始化\n✅ 已加密保存，之後每週自動續期")
        return
    if TOKEN_META.exists():
        meta = json.loads(TOKEN_META.read_text(encoding="utf-8"))
        age = datetime.now(TZ) - datetime.fromisoformat(meta["refreshed_at"])
        if age < timedelta(hours=25):
            log("鑰匙 25 小時內剛續期過，這次略過")
            return
    r = call("GET", f"{GRAPH}/refresh_access_token",
             {"grant_type": "th_refresh_token", "access_token": token})
    save_token(r["access_token"], r.get("expires_in", 5184000))
    days = int(r.get("expires_in", 5184000)) // 86400
    log(f"✅ 鑰匙已續期，新的有效期約 {days} 天")
    write_summary(f"## 鑰匙續期\n✅ 已續期，約 {days} 天後到期（系統每週自動續期）")


APP_ID = "1776980380267124"
REDIRECT = "https://bin-realty.github.io/threads/"


def cmd_oauth():
    """用授權碼換長效鑰匙（授權頁明確勾選全部權限，含刪文）"""
    raw = os.environ.get("OAUTH_CODE", "").strip()
    m = re.search(r"code=([^&#\s]+)", raw)  # 貼整個網址也可以
    if m:
        raw = urllib.parse.unquote(m.group(1))
    code = raw.split("#")[0].strip()
    if not code:
        sys.exit("缺少授權碼")
    r = call("POST", f"{GRAPH}/oauth/access_token", {
        "client_id": APP_ID, "client_secret": _key(), "code": code,
        "grant_type": "authorization_code", "redirect_uri": REDIRECT}, retries=1)
    short = r["access_token"]
    r2 = call("GET", f"{GRAPH}/access_token", {
        "grant_type": "th_exchange_token", "client_secret": _key(), "access_token": short}, retries=1)
    save_token(r2["access_token"], r2.get("expires_in", 5184000))
    log("✅ 已用授權碼換到新的長效鑰匙並加密保存")
    write_summary("## 授權\n✅ 新鑰匙已保存（含刪文權限）")


def cmd_cleanup():
    """清理貼文。CLEAN_MODE=list（只列出）或 delete；規則：沒有別人留言的刪除＋內文含 CLEAN_KEYWORD 的一律刪。
    為保護隱私，報告只寫日期、編號與留言數，不寫內文。"""
    mode = os.environ.get("CLEAN_MODE", "list")
    kw = [k for k in os.environ.get("CLEAN_KEYWORD", "").replace("，", ",").split(",") if k.strip()]
    token, _ = load_token()
    me = call("GET", f"{API}/me", {"fields": "id,username", "access_token": token})
    myname = me.get("username")
    posts, url, params = [], f"{API}/me/threads", {"fields": "id,timestamp,text,media_type,is_quote_post", "limit": "100", "access_token": token}
    while url:
        r = call("GET", url, params)
        posts += r.get("data", [])
        url = (r.get("paging") or {}).get("next")
        params = {}
    rows, todo = [], []
    for p in posts:
        if p.get("media_type") == "REPOST_FACADE":
            continue
        try:
            rep = call("GET", f"{API}/{p['id']}/replies", {"fields": "id,username", "limit": "100", "access_token": token})
            others = [x for x in rep.get("data", []) if x.get("username") != myname]
        except RuntimeError:
            others = None
        hit = any(k.strip() in (p.get("text") or "") for k in kw)
        n = "?" if others is None else len(others)
        delete = hit or (others is not None and len(others) == 0)
        why = "關鍵字" if hit else ("無他人留言" if delete else "保留")
        rows.append(f"| {str(p.get('timestamp',''))[:10]} | {p['id']} | {n} | {why} |")
        if delete:
            todo.append(p["id"])
    head = f"## 貼文清理（{mode}）\n共 {len(rows)} 篇，符合刪除條件 {len(todo)} 篇，保留 {len(rows)-len(todo)} 篇\n\n| 日期 | 編號 | 他人留言數 | 判斷 |\n|---|---|---|---|\n"
    done = 0
    if mode == "delete":
        for pid in todo:
            try:
                call("DELETE", f"{API}/{pid}", {"access_token": token}, retries=1)
                done += 1
                time.sleep(1)
            except RuntimeError as e:
                log(f"❌ {pid}：{str(e)[:120]}")
        head += f"\n**實際刪除 {done} / {len(todo)} 篇**\n\n"
    log(head)
    write_summary(head + "\n".join(rows))


def cmd_scopes():
    """檢查鑰匙實際有哪些權限（不發文、不刪文）"""
    token, _ = load_token()
    tests = [
        ("threads_basic", "GET", f"{API}/me", {"fields": "id,username"}),
        ("threads_read_replies", "GET", f"{API}/me/replies", {"fields": "id", "limit": "1"}),
        ("threads_manage_insights", "GET", f"{API}/me/threads_insights", {"metric": "views"}),
    ]
    lines = []
    for name, m, url, p in tests:
        try:
            call(m, url, dict(p, access_token=token), retries=1)
            lines.append(f"- {name}：✅")
        except RuntimeError as e:
            lines.append(f"- {name}：❌ {str(e)[:160]}")
    try:
        r = call("GET", "https://graph.threads.net/v1.0/debug_token", {"input_token": token, "access_token": token}, retries=1)
        sc = (r.get("data") or {}).get("scopes")
        lines.append(f"- debug_token scopes：{sc}")
    except RuntimeError as e:
        lines.append(f"- debug_token：{str(e)[:120]}")
    for l in lines:
        log(l)
    write_summary("## 權限檢查\n" + "\n".join(lines))


def cmd_check():
    token, source = load_token()
    me = call("GET", f"{API}/me", {"fields": "username", "access_token": token})
    lim = call("GET", f"{API}/me/threads_publishing_limit",
               {"fields": "quota_usage,config", "access_token": token})
    usage = (lim.get("data") or [{}])[0].get("quota_usage", "?")
    log(f"✅ 鑰匙有效，帳號 @{me.get('username')}（來源：{source}），24 小時內已用 {usage}/250 篇")
    write_summary(f"## 鑰匙檢查\n✅ 帳號 @{me.get('username')}，24 小時內已發 {usage}/250 篇")


# ---------- 數據與留言 ----------
def all_posted(max_days=None):
    out = []
    now = datetime.now(TZ)
    for f in sorted(LISTINGS.glob("*/posted.json")):
        for e in json.loads(f.read_text(encoding="utf-8")):
            if e.get("deleted") or not e.get("post_id"):
                continue
            t = datetime.fromisoformat(e["posted_at"])
            if max_days and now - t > timedelta(days=max_days):
                continue
            out.append(dict(e, slug=f.parent.name))
    return out


def _load(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _dump(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def cmd_insights():
    """每篇已發布貼文的觀看與互動數 → stats.json"""
    token, _ = load_token()
    posts, errs = {}, []
    for e in all_posted(max_days=90):
        try:
            r = call("GET", f"{API}/{e['post_id']}/insights", {"metric": ",".join(METRICS), "access_token": token}, retries=2)
            m = {x["name"]: (x.get("values") or [{}])[0].get("value", (x.get("total_value") or {}).get("value", 0)) for x in r.get("data", [])}
            posts[e["post_id"]] = dict({k: m.get(k, 0) for k in METRICS}, tracker_id=e.get("tracker_id"), slug=e["slug"],
                                       posted_at=e["posted_at"], permalink=e.get("permalink", ""))
        except Exception as ex:
            errs.append(f"{e['post_id']}: {str(ex)[:120]}")
    _dump(STATS, {"updated_at": datetime.now(TZ).isoformat(timespec="seconds"), "posts": posts, "errors": errs[:10]})
    log(f"✅ 數據更新 {len(posts)} 篇，失敗 {len(errs)}")
    write_summary(f"## 數據\n更新 {len(posts)} 篇" + ("\n" + "\n".join(errs[:5]) if errs else ""))


def _paged(url, params, token, limit_pages=5):
    items, n = [], 0
    r = call("GET", url, dict(params, access_token=token), retries=2)
    while True:
        items += r.get("data", [])
        n += 1
        nxt = (r.get("paging") or {}).get("next")
        if not nxt or n >= limit_pages:
            return items
        with urllib.request.urlopen(nxt, timeout=60) as resp:
            r = json.loads(resp.read())


def _reply(token, to_id, text):
    rc = call("POST", f"{API}/me/threads", {"media_type": "TEXT", "text": text, "reply_to_id": to_id, "access_token": token})
    wait_ready(rc["id"], token)
    return call("POST", f"{API}/me/threads_publish", {"creation_id": rc["id"], "access_token": token})["id"]


def cmd_comments():
    """抓自己貼文底下的留言 → comments.json；送出 outbox/ 的手動回覆；（開啟時）誠實版自動回覆"""
    import random
    posted = all_posted(max_days=21)
    outbox = sorted(p for p in OUTBOX.glob("*") if p.is_file() and not p.name.startswith(".")) if OUTBOX.exists() else []
    if not posted and not outbox:
        log("沒有近期貼文，也沒有待送回覆")
        return
    token, _ = load_token()
    me = call("GET", f"{API}/me", {"fields": "username", "access_token": token}).get("username")
    rlog = _load(REPLY_LOG, {})
    # 1) 手動回覆
    for f in outbox:
        cid = f.stem
        text = f.read_text(encoding="utf-8").strip()
        try:
            rid = _reply(token, cid, text)
            rlog[cid] = {"at": datetime.now(TZ).isoformat(timespec="seconds"), "by": "manual", "reply_id": rid, "text": text}
            log(f"✅ 已回覆留言 {cid}")
        except Exception as ex:
            rlog[cid] = {"at": datetime.now(TZ).isoformat(timespec="seconds"), "by": "manual", "error": str(ex)[:200], "text": text}
            log(f"❌ 回覆 {cid} 失敗：{ex}")
        f.unlink()
        time.sleep(3)
    # 2) 抓留言
    items = []
    for e in posted:
        try:
            conv = _paged(f"{API}/{e['post_id']}/conversation", {"fields": "id,text,username,timestamp,replied_to", "reverse": "false"}, token)
        except Exception as ex:
            log(f"⚠️ {e['post_id']} 讀留言失敗：{ex}")
            continue
        mine_to = {(c.get("replied_to") or {}).get("id") for c in conv if c.get("username") == me}
        for c in conv:
            if c.get("username") == me:
                continue
            items.append({"id": c["id"], "post_id": e["post_id"], "tracker_id": e.get("tracker_id"), "slug": e["slug"],
                          "permalink": e.get("permalink", ""), "username": c.get("username", ""), "text": c.get("text", ""),
                          "timestamp": c.get("timestamp", ""), "answered": c["id"] in mine_to or "reply_id" in rlog.get(c["id"], {})})
    # 3) 自動回覆（預設關閉）
    cfg = _load(REPLY_CFG, {"enabled": False})
    if cfg.get("enabled"):
        sent = 0
        for it in items:
            if it["answered"] or it["id"] in rlog or sent >= cfg.get("max_per_run", 8):
                continue
            txt = it["text"] or ""
            if any(k in txt for k in cfg.get("ignore", [])):
                continue
            rule = next((r for r in cfg.get("rules", []) if any(k in txt for k in r["keywords"])), None)
            if not rule:
                continue
            msg = random.choice(rule["replies"])
            try:
                rid = _reply(token, it["id"], msg)
                rlog[it["id"]] = {"at": datetime.now(TZ).isoformat(timespec="seconds"), "by": "auto:" + rule["id"], "reply_id": rid, "text": msg}
                it["answered"] = True
                sent += 1
                log(f"🤖 自動回覆 {it['username']}（{rule['id']}）")
                time.sleep(random.randint(20, 40))
            except Exception as ex:
                rlog[it["id"]] = {"at": datetime.now(TZ).isoformat(timespec="seconds"), "by": "auto", "error": str(ex)[:200]}
    for it in items:
        if it["id"] in rlog:
            it["reply"] = rlog[it["id"]]
    old = _load(COMMENTS, {}).get("items")
    if old != items:
        _dump(COMMENTS, {"updated_at": datetime.now(TZ).isoformat(timespec="seconds"), "items": items})
    _dump(REPLY_LOG, rlog) if rlog != _load(REPLY_LOG, {}) else None
    log(f"留言 {len(items)} 則，未回 {sum(1 for i in items if not i['answered'])} 則")


def write_summary(md):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(md + "\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"process": cmd_process, "refresh": cmd_refresh, "check": cmd_check, "import": cmd_import, "sync": cmd_sync, "insights": cmd_insights, "comments": cmd_comments, "delete": cmd_delete, "scopes": cmd_scopes, "oauth": cmd_oauth, "cleanup": cmd_cleanup}.get(
        cmd, lambda: sys.exit(__doc__))()
