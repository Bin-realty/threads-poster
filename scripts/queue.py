#!/usr/bin/env python3
"""Threads-Bin 佇列。

drafts/<id>.json：每篇貼文（status：草稿／已排程／待發／處理中／已發布／失敗／刪除中／已刪除）
schedule.json  ：自動循環（可多組）{plans:[{id, name, active, times:["08:00"], weekdays:[1..7], queue:[draft ids], cursor}], fired:{"<組id>@YYYY-MM-DDTHH:MM": id}}
                 舊格式（只有一組 {active, times, weekdays, queue, cursor}）讀進來自動轉成 plans[0]，id 為 p1

  queue   把「待發」「已排程且到時間」「輪播到點」轉成 publish/ 指令；「刪除中」轉成 delete/ 指令
  settle  發文／刪文後把結果寫回草稿
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAFTS, LISTINGS, PUBLISH, DELETE = ROOT / "drafts", ROOT / "listings", ROOT / "publish", ROOT / "delete"
PUBLIC, SCHED = ROOT / "data" / "listings.json", ROOT / "schedule.json"
TZ = timezone(timedelta(hours=8))
GUARD_H = 72


def now():
    return datetime.now(TZ)


def load(p, d=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return d


def save(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
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
    out = []
    for f in sorted(DRAFTS.glob("*.json")):
        d = load(f)
        if isinstance(d, dict):
            d.setdefault("id", f.stem)
            out.append((f, d))
    return out


def recently_posted(code, all_d):
    """同一物件 72 小時內發過就擋（避免洗版）"""
    if not code:
        return False
    for _, d in all_d:
        if d.get("listing") == code and d.get("status") == "已發布":
            t = parse_t(d.get("postedAt"))
            if t and now() - t < timedelta(hours=GUARD_H):
                return True
    return False


def enqueue(f, d, pub):
    slug = slug_of(d)
    if (PUBLISH / slug).exists():
        print(f"⏳ {d['id']}：{slug} 還有一篇在發，下一輪再處理")
        return False
    dd = LISTINGS / slug
    (dd / "photos").mkdir(parents=True, exist_ok=True)
    src_album = d.get("album")
    if src_album and src_album != slug and (LISTINGS / src_album / "album").is_dir():
        import shutil
        for sub in ("album", "thumbs"):
            if (LISTINGS / src_album / sub).is_dir():
                shutil.rmtree(dd / sub, ignore_errors=True)
                shutil.copytree(LISTINGS / src_album / sub, dd / sub)
        meta = LISTINGS / src_album / "album_meta.json"
        if meta.exists():
            (dd / "album_meta.json").write_text(meta.read_text(encoding="utf-8"), encoding="utf-8")
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
    PUBLISH.mkdir(exist_ok=True)
    (PUBLISH / slug).write_text(f"{mode} {d['id']}\n", encoding="utf-8")
    d.update(status="處理中", error="", queuedAt=now().isoformat(timespec="seconds"))
    save(f, d)
    print(f"➡️ {d['id']} → publish/{slug}")
    return True


def norm_sched(s):
    """舊的單組格式轉成 {plans:[...], fired:{}}"""
    if isinstance(s.get("plans"), list):
        s.setdefault("fired", {})
        return s
    plan = {"id": "p1", "name": "循環 1", "active": bool(s.get("active")), "times": s.get("times") or [],
            "weekdays": s.get("weekdays") or [], "queue": s.get("queue") or [], "cursor": s.get("cursor", 0)}
    fired = {(k if "@" in k else "p1@" + k): v for k, v in (s.get("fired") or {}).items()}
    return {"plans": [plan] if plan["queue"] else [], "fired": fired}


def run_plan(plan, fired, t, all_d, pub):
    """一組自動循環：到點就從這組的文案輪流挑一篇發"""
    n, pid = 0, plan.get("id") or "p1"
    for hhmm in sorted(plan["times"]):
        try:
            hh, mm = map(int, hhmm.split(":"))
        except Exception:
            continue
        slot = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
        key = f"{pid}@{slot.strftime('%Y-%m-%dT%H:%M')}"
        if key in fired or not (slot - timedelta(minutes=2) <= t <= slot + timedelta(minutes=40)):
            continue
        by_id = {d["id"]: (f, d) for f, d in all_d}
        q = [i for i in plan["queue"] if i in by_id]
        if not q:
            break
        cur = plan.get("cursor", 0) % len(q)
        picked = None
        for k in range(len(q)):
            i = q[(cur + k) % len(q)]
            f0, src = by_id[i]
            if recently_posted(src.get("listing"), all_d):
                continue
            picked = (f0, src, (cur + k + 1) % len(q))
            break
        if not picked:
            fired[key] = ""
            print(f"🔁 {key}：這組每篇都在 72 小時內發過，這一輪跳過")
            continue
        f0, src, nxt = picked
        rid = f"R{slot.strftime('%Y%m%d%H%M')}" + ("" if pid == "p1" else pid)
        copy = {k: v for k, v in src.items() if not k.startswith("_")}
        copy.update(id=rid, status="待發", source="rotation", plan=pid, from_id=src["id"], library=False, scheduledAt=slot.isoformat(timespec="seconds"),
                    createdAt=t.isoformat(timespec="seconds"), updatedAt=t.isoformat(timespec="seconds"), error="")
        rf = DRAFTS / f"{rid}.json"
        save(rf, copy)
        all_d.append((rf, copy))
        if enqueue(rf, copy, pub):
            fired[key] = rid
            plan["cursor"] = nxt
            n += 1
    return n


def cmd_queue():
    pub = {x.get("code"): x for x in (load(PUBLIC, {}) or {}).get("listings", [])}
    all_d = drafts()
    n = 0
    # 1) 單篇：待發、到時間的已排程
    for f, d in all_d:
        st = d.get("status")
        at = parse_t(d.get("scheduledAt"))
        if st == "待發" or (st == "已排程" and at and at <= now() + timedelta(minutes=2)):
            if recently_posted(d.get("listing"), all_d) and not d.get("force"):
                d.update(status="失敗", error=f"同一物件 {GUARD_H} 小時內發過，已跳過（避免洗版）")
                save(f, d)
                print(f"🚫 {d['id']}：72 小時防洗版")
                continue
            n += enqueue(f, d, pub)
    # 2) 刪文
    for f, d in all_d:
        if d.get("status") == "刪除中" and d.get("post_id"):
            slug = slug_of(d)
            DELETE.mkdir(exist_ok=True)
            p = DELETE / slug
            ids = set(p.read_text(encoding="utf-8").split()) if p.exists() else set()
            ids.add(str(d["post_id"]))
            p.write_text("\n".join(sorted(ids)) + "\n", encoding="utf-8")
            print(f"🗑 {d['id']} → delete/{slug}")
    # 3) 自動循環（可多組，每組各自的時間、星期、文案）
    s = load(SCHED, None)
    if s:
        s = norm_sched(s)
        t = now()
        for plan in s["plans"]:
            if plan.get("active") and plan.get("queue") and plan.get("times") and \
                    t.isoweekday() in (plan.get("weekdays") or [1, 2, 3, 4, 5, 6, 7]):
                n += run_plan(plan, s["fired"], t, all_d, pub)
        # 清掉 7 天前的 fired 紀錄
        cutoff = (t - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
        s["fired"] = {k: v for k, v in s["fired"].items() if k.split("@")[-1] >= cutoff}
        save(SCHED, s)
    print(f"排入 {n} 篇")


def cmd_settle():
    for f, d in drafts():
        st = d.get("status")
        dd = LISTINGS / slug_of(d)
        if st == "處理中":
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
        elif st == "刪除中":
            ent = next((e for e in load(dd / "posted.json", []) or [] if str(e.get("post_id")) == str(d.get("post_id"))), None)
            if ent and ent.get("deleted"):
                d.update(status="已刪除", deletedAt=now().isoformat(timespec="seconds"))
            elif not (DELETE / slug_of(d)).exists() and not (ent and ent.get("deleted")):
                d.update(status="失敗", error="刪文沒有成功，請再試一次")
            else:
                continue
        else:
            continue
        save(f, d)
        print(f"✔ {d['id']}：{d['status']}")


if __name__ == "__main__":
    {"queue": cmd_queue, "settle": cmd_settle}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
