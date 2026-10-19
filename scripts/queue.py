#!/usr/bin/env python3
"""發脆台草稿佇列。

drafts/<id>.json 是每篇貼文（發脆台網頁或追蹤台寫進來）：
  status：草稿／已排程／待發／處理中／已發布／失敗
  listing：物件編號（空白＝知識文）、text、reply、pick、scheduledAt、hook、topic

  python3 scripts/queue.py queue    把「待發」與「已排程且到時間」的草稿轉成 publish/ 發文指令
  python3 scripts/queue.py settle   發文後把結果（已發布＋連結／失敗＋原因）寫回草稿
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAFTS, LISTINGS, PUBLISH = ROOT / "drafts", ROOT / "listings", ROOT / "publish"
PUBLIC = ROOT / "data" / "listings.json"
TZ = timezone(timedelta(hours=8))


def now():
    return datetime.now(TZ)


def load(p, d=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return d


def save(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def parse_t(s):
    if not s:
        return None
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=TZ)


def slug_of(d):
    code = (d.get("listing") or "").strip()
    return code.lower() if code else "k-" + d["id"].lower()


def drafts():
    for f in sorted(DRAFTS.glob("*.json")):
        d = load(f)
        if isinstance(d, dict):
            d.setdefault("id", f.stem)
            yield f, d


def cmd_queue():
    pub = {x.get("code"): x for x in (load(PUBLIC, {}) or {}).get("listings", [])}
    n = 0
    for f, d in drafts():
        st = d.get("status")
        at = parse_t(d.get("scheduledAt"))
        if not (st == "待發" or (st == "已排程" and at and at <= now() + timedelta(minutes=2))):
            continue
        slug = slug_of(d)
        if (PUBLISH / slug).exists():
            print(f"⏳ {d['id']}：{slug} 還有一篇在發，下一輪再處理")
            continue
        dd = LISTINGS / slug
        (dd / "photos").mkdir(parents=True, exist_ok=True)
        for old in (dd / "photos").glob("*"):
            old.unlink()
        (dd / "post.txt").write_text(d.get("text", "").strip() + "\n", encoding="utf-8")
        for name, val in (("reply.txt", d.get("reply")), ("pick.txt", d.get("pick"))):
            p = dd / name
            v = (val or "").strip()
            p.write_text(v + "\n", encoding="utf-8") if v else (p.unlink() if p.exists() else None)
        code = (d.get("listing") or "").strip()
        album = (pub.get(code) or {}).get("photos", "") if code else ""
        if not album and code and (dd / "album.txt").exists():
            album = (dd / "album.txt").read_text(encoding="utf-8").strip()
        if album:
            (dd / "album.txt").write_text(album + "\n", encoding="utf-8")
        mode = "dry" if d.get("dry") else ("force" if d.get("force") else "post")
        (PUBLISH / slug).write_text(f"{mode} {d['id']}\n", encoding="utf-8")
        d.update(status="處理中", error="", queuedAt=now().isoformat(timespec="seconds"))
        save(f, d)
        n += 1
        print(f"➡️ {d['id']} → publish/{slug}")
    print(f"排入 {n} 篇")


def cmd_settle():
    for f, d in drafts():
        if d.get("status") != "處理中":
            continue
        dd = LISTINGS / slug_of(d)
        ok = next((e for e in reversed(load(dd / "posted.json", []) or []) if e.get("tracker_id") == d["id"]), None)
        bad = next((e for e in reversed(load(dd / "failed.json", []) or []) if e.get("tracker_id") == d["id"]), None)
        dry = next((e for e in reversed(load(dd / "dryrun.json", []) or []) if e.get("tracker_id") == d["id"]), None) if d.get("dry") else None
        if dry:
            d.update(status="草稿", dry=False, error="", dryOkAt=dry.get("at"), dryPhotos=dry.get("photos"))
        elif ok:
            d.update(status="已發布", permalink=ok.get("permalink", ""), post_id=ok.get("post_id"), postedAt=ok.get("posted_at"), error="")
        elif bad:
            d.update(status="失敗", error=bad.get("error", "")[:200])
        elif not (PUBLISH / slug_of(d)).exists():
            d.update(status="失敗", error="沒有發出，也沒有錯誤紀錄（請再按一次發送）")
        else:
            continue
        save(f, d)
        print(f"✔ {d['id']}：{d['status']}")


if __name__ == "__main__":
    {"queue": cmd_queue, "settle": cmd_settle}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
