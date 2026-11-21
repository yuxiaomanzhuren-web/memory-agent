"""
P3 一次性 ETL：把分散的旧记忆搬进唯一的 memory-core。

原则（见 $MEMORY_ROOT\\docs\\single-subject-protocol.md）：
  · 只经 POST /remember 写入，绝不直接改 brain.db；
  · 人设类文件不进记忆库（角色设定的唯一来源是 DSH 角色卡）；
  · 带 source 标签与原始时间，便于追溯；
  · 与库里已有内容做相似度查重，避免把遗言/聊天记录再灌一遍。

用法：
  python p3_migrate_legacy.py            # dry-run，只报告计划
  python p3_migrate_legacy.py --apply    # 真正写入
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8780"
TOKEN = "local-token"
BRAIN_DB = r"$MEMORY_ROOT\data\brain.db"
LEGACY_DB = r"<LOCAL_PATH>"
DY_BRAIN = Path(r"E:\dybot\data\brain")


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", "Authorization": "Bearer " + TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode("utf-8"))


def grams(text, n=2):
    s = re.sub(r"[\s，。、！？!?~～…：:；;「」『』（）()\[\]【】#>\-—·｜|]", "", str(text))
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def norm(text):
    return re.sub(r"\s+", "", str(text))


def similarity(a, b):
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    j = len(ga & gb) / len(ga | gb)
    na, nb = norm(a), norm(b)
    if na and nb and (na in nb or nb in na):
        return max(j, 0.95)
    return j


def load_existing():
    con = sqlite3.connect("file:" + BRAIN_DB.replace("\\", "/") + "?mode=ro", uri=True)
    rows = con.execute("select id, text from memories where status is null or status != 'deleted'").fetchall()
    con.close()
    return [(int(r[0]), str(r[1])) for r in rows]


def candidates():
    out = []

    # 1) 桥接旧库的遗言（6 条，其中 5 条已以 legacy-will 导入过）
    con = sqlite3.connect("file:" + LEGACY_DB.replace("\\", "/") + "?mode=ro", uri=True)
    for sid, text, tone, created in con.execute(
        "select id, text, tone, created_at from summaries order by created_at asc"
    ):
        if not text or len(str(text).strip()) < 20:
            continue
        out.append({
            "text": "〔遗言·留给下一个自己〕" + str(text).strip(),
            "kind": "reflection",
            "importance": 0.9,
            "source": "legacy-will",
            "created_at": (float(created) / 1000.0) if created else None,
            "from": f"memory.sqlite#{sid}",
        })
    con.close()

    # 2) 记得的事：一行一条事实
    f = DY_BRAIN / "记得的事.md"
    if f.exists():
        mt = f.stat().st_mtime
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            fact = line[2:].strip()
            if len(fact) < 6:
                continue
            out.append({
                "text": fact,
                "kind": "preference",
                "importance": 0.62,
                "source": "dy-import:brain",
                "created_at": mt,
                "from": "记得的事.md",
            })

    # 3) 最近.md：整段一条
    f = DY_BRAIN / "最近.md"
    if f.exists():
        body = f.read_text(encoding="utf-8").strip()
        if len(body) > 20:
            out.append({
                "text": body,
                "kind": "reflection",
                "importance": 0.6,
                "source": "dy-import:brain",
                "created_at": f.stat().st_mtime,
                "from": "最近.md",
            })

    # 4) 群里的记录.md：按日期合并，避免一行一条刷屏
    f = DY_BRAIN / "群里的记录.md"
    if f.exists():
        by_day = {}
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"-?\s*(2026/\d+/\d+)\s", line)
            if m:
                by_day.setdefault(m.group(1), []).append(line.strip().lstrip("- "))
        for day, lines in sorted(by_day.items()):
            text = f"【抖音群 {day}】" + " / ".join(lines)
            out.append({
                "text": text[:1200],
                "kind": "episode",
                "importance": 0.35,
                "source": "dy-import:brain",
                "created_at": f.stat().st_mtime,
                "from": f"群里的记录.md#{day}",
            })

    # 5) 贴吧学的.md：按 ## 段落，一条一段（这是她自己学到的说话方式）
    f = DY_BRAIN / "贴吧学的.md"
    if f.exists():
        txt = f.read_text(encoding="utf-8")
        parts = re.split(r"(?=^## )", txt, flags=re.M)
        for part in parts:
            if not part.strip().startswith("## "):
                continue
            out.append({
                "text": part.strip()[:1200],
                "kind": "reflection",
                "importance": 0.5,
                "source": "web:slang",
                "created_at": f.stat().st_mtime,
                "from": "贴吧学的.md",
            })

    # 人设.md / 口气.md 不进库：那是角色设定，不是记忆。封存即可。
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.52)
    args = ap.parse_args()

    existing = load_existing()
    cands = candidates()
    print(f"库里现有 {len(existing)} 条；待评估 {len(cands)} 条；阈值 {args.threshold}")
    print("-" * 70)

    plan, skipped = [], []
    pool = list(existing)
    for c in cands:
        best, best_id = 0.0, None
        for mid, txt in pool:
            s = similarity(c["text"], txt)
            if s > best:
                best, best_id = s, mid
        if best >= args.threshold:
            skipped.append((c, best, best_id))
        else:
            plan.append(c)
            pool.append((-1, c["text"]))

    print(f"计划写入 {len(plan)} 条，查重跳过 {len(skipped)} 条")
    print("\n[写入样例]")
    for c in plan[:8]:
        print(f"  + {c['source']:<20} {c['text'][:64]}")
    if len(plan) > 8:
        print(f"  ... 还有 {len(plan) - 8} 条")
    print("\n[跳过样例]")
    for c, s, mid in skipped[:8]:
        print(f"  - #{mid} {s:.2f} {c['text'][:56]}")
    if len(skipped) > 8:
        print(f"  ... 还有 {len(skipped) - 8} 条")

    if not args.apply:
        print("\n这是 dry-run。确认无误后加 --apply 执行。")
        return 0

    ok, fail = 0, 0
    for c in plan:
        payload = {
            "text": c["text"],
            "kind": c["kind"],
            "importance": c["importance"],
            "valence": 0,
            "arousal": 0.1,
            "source": c["source"],
            "visibility": "private",
            "meta": {"migrated": True, "p3": True, "from": c["from"]},
        }
        if c["created_at"]:
            payload["created_at"] = c["created_at"]
        try:
            res = post("/remember", payload)
            if res.get("data", {}).get("id"):
                ok += 1
            else:
                fail += 1
                print("  ! 未返回 id:", json.dumps(res, ensure_ascii=False)[:160])
        except Exception as e:
            fail += 1
            print("  ! 写入失败:", e)
    print(f"\n执行完成：成功 {ok} 条，失败 {fail} 条")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
