# -*- coding: utf-8 -*-
"""清实体噪声（2026-11-06）。

两类处理：
1. 度数为 0 的孤儿实体：没有一条记忆指向它，整行删掉。
2. 被当成"人"来写画像的抽象词/长句/视频梗：保留行和边（记忆还需要这些标签），
   但把画像清空、profile_at 置空，并打上 skip_profile=1，避免以后再被写。

跑完直接 os._exit，防 wx / 模型客户端把进程挂住。
"""
import os
import sqlite3
import sys
import time

DB = r"$MEMORY_ROOT/data/brain.db"
BAK = r"$MEMORY_ROOT/data/backup/brain.db.bak-entclean-%s" % time.strftime("%Y%m%d-%H%M%S")

# 噪声实体：名字不是指称对象，是抽象词、整句、或视频梗串味
JUNK = {
    12: "朋友（画像写的是他，与 他（主人） 重复）",
    33: "微博（模板腔，无实体性）",
    35: "DeepSeek（《商务捕鲸》视频梗串味）",
    36: "ChatGPT（同上，与 Claude 内容几乎逐字重复）",
    37: "Claude（同上）",
    38: "月亮旧了…（整句话当实体）",
    43: "晚饭（话题碎片，不是实体）",
    52: "表情包（模板腔）",
    92: "套壳（抽象词，当成人在写）",
    95: "B站（画像内容是「对方」的，张冠李戴）",
    99: "称呼（模板腔：身份/喜好/雷点）",
    111: "权限（同上）",
    114: "日记（模板腔）",
    115: "回忆（幻觉成私聊伙伴，其实是折叠概念）",
    116: "小愿望（同上）",
}

# 本来就没画像、但同属抽象词的一批：只打 skip 标记，防止以后被写成"人"
SKIP_ONLY = {
    13: "情绪表达",
    14: "个性特点",
    16: "未来计划",
    107: "名字",
    112: "喜好",
}


def main():
    src = sqlite3.connect(DB)
    src.execute("PRAGMA busy_timeout=15000")
    dst = sqlite3.connect(BAK)
    with dst:
        src.backup(dst)
    dst.close()
    print("备份 ->", BAK)

    c = src
    cols = [r[1] for r in c.execute("PRAGMA table_info(entities)")]
    if "skip_profile" not in cols:
        c.execute("ALTER TABLE entities ADD COLUMN skip_profile INTEGER DEFAULT 0")
        c.commit()
        print("加列 entities.skip_profile")

    # ---- 1. 孤儿实体 ----
    orphans = [r[0] for r in c.execute(
        """SELECT e.id FROM entities e
           WHERE NOT EXISTS (SELECT 1 FROM memory_entities me WHERE me.entity_id=e.id)"""
    )]
    names = {}
    for eid in orphans:
        row = c.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()
        names[eid] = row[0] if row else "?"
    for eid in orphans:
        c.execute("DELETE FROM memory_entities WHERE entity_id=?", (eid,))
        c.execute("DELETE FROM entities WHERE id=?", (eid,))
    c.commit()
    print(f"删孤儿实体 {len(orphans)} 个：", "、".join(f"{i}:{names[i]}" for i in orphans))

    # ---- 2. 噪声画像 ----
    cleared = []
    for eid, why in JUNK.items():
        row = c.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()
        if not row:
            continue
        c.execute(
            "UPDATE entities SET profile='', profile_at=0, skip_profile=1 WHERE id=?", (eid,))
        cleared.append(f"{row[0]}")
    c.commit()
    print(f"清画像 {len(cleared)} 个：", "、".join(cleared))

    skipped = []
    for eid, nm in SKIP_ONLY.items():
        if c.execute("SELECT 1 FROM entities WHERE id=?", (eid,)).fetchone():
            c.execute("UPDATE entities SET skip_profile=1 WHERE id=?", (eid,))
            skipped.append(nm)
    c.commit()
    if skipped:
        print(f"只打标记 {len(skipped)} 个：", "、".join(skipped))

    # ---- 3. 对账 ----
    print("\n剩余实体：")
    for r in c.execute(
        """SELECT e.id, e.name, e.type,
                  (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) d,
                  COALESCE(e.skip_profile,0), length(COALESCE(e.profile,''))
           FROM entities e ORDER BY d DESC"""
    ):
        print(f"  #{r[0]:<4}{r[1][:22]:<24}{r[2]:<7}deg={r[3]:<4}skip={r[4]}  plen={r[5]}")
    print("\n合计实体", c.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
          "／画像", c.execute("SELECT COUNT(*) FROM entities WHERE profile IS NOT NULL AND profile<>''").fetchone()[0],
          "／关联", c.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0])
    c.close()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
