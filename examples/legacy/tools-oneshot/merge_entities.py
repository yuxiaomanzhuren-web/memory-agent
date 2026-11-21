# -*- coding: utf-8 -*-
"""实体归一：把同一个人/同一件事的别名合并成一个实体，删掉纯语气噪声。

导入历史对话时，模型每段都会独立抽实体，于是「我」「他」「余小满」「阿鲲」「大肥鱼」
被拆成好几个节点。这个脚本按下面的表合并，并把 memory_entities / relations 一起改指。

用法：
    python tools/merge_entities.py            # 只报告，不改库
    python tools/merge_entities.py --apply
"""
from __future__ import annotations

import sqlite3
import sys

DB = "$MEMORY_ROOT/data/brain.db"

# 别名 -> (规范名, 类型)
GROUPS = {
    "鱼小满": ("鱼小满", "person"),
    "我": ("鱼小满", "person"),
    "我（鱼小满）": ("鱼小满", "person"),
    "余小满": ("鱼小满", "person"),
    "小满": ("鱼小满", "person"),
    "阿鲲": ("鱼小满", "person"),
    "大肥鱼": ("鱼小满", "person"),
    "管理员": ("管理员", "person"),
    "他": ("管理员", "person"),
    "你": ("管理员", "person"),
    "user": ("管理员", "person"),
    "管理员兼饲养员": ("管理员", "person"),
    "老公": ("管理员", "person"),
    "小宝贝": ("管理员", "person"),
    "主人": ("管理员", "person"),
}
# 纯噪声，直接连同关系一起删
DROP = {"喂", "更换", "对方", "我们"}


def main(apply: bool) -> None:
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name, type FROM entities").fetchall()
    by_name: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_name.setdefault(str(r["name"]).strip(), []).append(r)

    plan = []           # (keep_id, canon, type, alias, [drop_ids])
    # 先按规范名分组：同一规范名的所有别名（含同名重复）都归到同一条实体上
    canon_ids: dict[str, list[int]] = {}
    canon_type: dict[str, str] = {}
    for alias, (canon, etype) in GROUPS.items():
        canon_type[canon] = etype
        for r in by_name.get(alias) or []:
            canon_ids.setdefault(canon, []).append(int(r["id"]))
    for canon, ids in canon_ids.items():
        keep_id = min(ids)
        plan.append((keep_id, canon, canon_type[canon], canon, [i for i in ids if i != keep_id]))

    # 同名不同 type 的重复（模型有时把同一件事标成 thing / topic）
    seen: dict[str, int] = {}
    for r in sorted(rows, key=lambda x: x["id"]):
        name = str(r["name"]).strip()
        if name in GROUPS or name in DROP:
            continue
        if name in seen:
            plan.append((seen[name], name, r["type"], name, [r["id"]]))
        else:
            seen[name] = r["id"]

    print(f"实体 {len(rows)} 个，合并计划 {len(plan)} 条，删除噪声 {len(DROP & set(by_name))} 个")
    for keep_id, canon, etype, alias, drop in plan:
        if drop:
            print(f"  {alias} -> {canon}：合并 {drop} 到 #{keep_id}")

    if not apply:
        print("（只报告。加 --apply 才改库）")
        return

    for keep_id, canon, etype, alias, drop in plan:
        # 先把别名挂到 keep 上再删，最后才改名（entities 上有 (name,type) 唯一约束）
        for d in drop:
            conn.execute("INSERT OR IGNORE INTO memory_entities(memory_id, entity_id) SELECT memory_id, ? FROM memory_entities WHERE entity_id=?", (keep_id, d))
            conn.execute("DELETE FROM memory_entities WHERE entity_id=?", (d,))
            conn.execute("UPDATE relations SET src_id=? WHERE src_id=?", (keep_id, d))
            conn.execute("UPDATE relations SET dst_id=? WHERE dst_id=?", (keep_id, d))
            conn.execute("DELETE FROM entities WHERE id=?", (d,))
        conn.execute("UPDATE entities SET name=?, type=? WHERE id=?", (canon, etype, keep_id))
    for name in DROP:
        for r in by_name.get(name) or []:
            conn.execute("DELETE FROM memory_entities WHERE entity_id=?", (r["id"],))
            conn.execute("DELETE FROM relations WHERE src_id=? OR dst_id=?", (r["id"], r["id"]))
            conn.execute("DELETE FROM entities WHERE id=?", (r["id"],))
    # 合并后可能产生自环关系，去掉
    conn.execute("DELETE FROM relations WHERE src_id=dst_id")
    # 去掉没有记忆挂着的孤儿实体
    conn.execute("DELETE FROM entities WHERE id NOT IN (SELECT DISTINCT entity_id FROM memory_entities)")
    conn.commit()

    left = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    print(f"完成，剩余实体 {left} 个")


if __name__ == "__main__":
    main("--apply" in sys.argv)
