#!/usr/bin/env python3
"""把追蹤台「發脆」分頁的待發貼文，轉成 GitHub 發文指令。

用法：python3 scripts/from_tracker.py <posts資料夾> <listings資料夾>
  posts/<id>.json     ArtifactData 匯出的 posts 文件（status=待發）
  listings/<L00X>.json ArtifactData 匯出的庫存文件
輸出：listings/<l00x>/ 的 post.txt、reply.txt、album.txt、pick.txt，以及 publish/<l00x>
印出每篇的 post_id → slug 對照（JSON），之後用來回寫追蹤台。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(p):
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    return d.get("data", d)


def main(posts_dir, listings_dir):
    out = []
    for f in sorted(Path(posts_dir).glob("**/*.json")):
        post = load(f)
        if post.get("status") not in ("待發", "已排程"):
            continue
        code = post["listing"]
        lf = next(Path(listings_dir).glob(f"**/{code}.json"), None)
        if not lf:
            out.append({"post": f.stem, "error": f"找不到庫存 {code}"})
            continue
        lst = load(lf)
        album = str(lst.get("photos") or "").strip()
        if "photos.app.goo.gl" not in album and "photos.google.com" not in album:
            out.append({"post": f.stem, "error": f"{code} 沒有 Google 相簿連結"})
            continue
        slug = code.lower()
        d = ROOT / "listings" / slug
        d.mkdir(parents=True, exist_ok=True)
        # 每次重新從相簿抓：清掉舊照片
        for old in (d / "photos").glob("*") if (d / "photos").exists() else []:
            old.unlink()
        (d / "post.txt").write_text(post["text"].strip() + "\n", encoding="utf-8")
        reply = (post.get("reply") or "").strip()
        rp = d / "reply.txt"
        rp.write_text(reply + "\n", encoding="utf-8") if reply else (rp.unlink() if rp.exists() else None)
        (d / "album.txt").write_text(album + "\n", encoding="utf-8")
        pk = d / "pick.txt"
        pick = (post.get("pick") or "").strip()
        pk.write_text(pick + "\n", encoding="utf-8") if pick else (pk.unlink() if pk.exists() else None)
        at = (post.get("scheduledAt") or "").strip() if post.get("status") == "已排程" else ""
        pid = f.stem
        (ROOT / "publish" / slug).write_text(" ".join(x for x in ("post", at, pid) if x) + "\n", encoding="utf-8")
        out.append({"post": pid, "listing": code, "slug": slug, "at": at})
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
