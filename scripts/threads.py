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
    if method == "GET":
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
    sha = os.environ.get("GITHUB_SHA", "main")
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


def post_listing(slug, token, mode):
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
                 "photos": len(photos)})
    hist_f.write_text(json.dumps(hist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return link


def cmd_process():
    jobs = sorted(p for p in PUBLISH.glob("*") if p.is_file() and not p.name.startswith("."))
    if not jobs:
        log("publish/ 沒有待發的指令")
        return
    token, _ = load_token()
    summary, failed = [], False
    for job in jobs:
        slug = job.stem
        mode = (job.read_text(encoding="utf-8").strip().lower() or "post").split()[0]
        try:
            link = post_listing(slug, token, mode)
            summary.append(f"- {slug}：" + ("試跑通過" if mode == "dry" else f"已發布 {link}"))
            job.unlink()
        except Exception as e:
            failed = True
            summary.append(f"- {slug}：❌ {e}")
            log(f"❌ {slug}：{e}")
            job.unlink()
    write_summary("## 發文結果\n" + "\n".join(summary))
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


def cmd_check():
    token, source = load_token()
    me = call("GET", f"{API}/me", {"fields": "username", "access_token": token})
    lim = call("GET", f"{API}/me/threads_publishing_limit",
               {"fields": "quota_usage,config", "access_token": token})
    usage = (lim.get("data") or [{}])[0].get("quota_usage", "?")
    log(f"✅ 鑰匙有效，帳號 @{me.get('username')}（來源：{source}），24 小時內已用 {usage}/250 篇")
    write_summary(f"## 鑰匙檢查\n✅ 帳號 @{me.get('username')}，24 小時內已發 {usage}/250 篇")


def write_summary(md):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(md + "\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"process": cmd_process, "refresh": cmd_refresh, "check": cmd_check}.get(
        cmd, lambda: sys.exit(__doc__))()
