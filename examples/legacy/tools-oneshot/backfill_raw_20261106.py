# -*- coding: utf-8 -*-
"""把 memories 里那批「原文全文」倒回 L0。

来源：桥接以前把整轮对话直接存成一条 700 字的记忆（`【QQ私聊 2026-11-01 21:07】` 开头）。
这些本来就是原话，属于 L0 该管的东西，只是当年没地方放，才塞进了 memories。

现在 L0 有了，把它们拆回一条条消息——将来想翻「那天到底怎么说的」，
从这里翻，而不是从 memories 里翻。
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "memory-core"))
from memory_core.rawlog import RawLog  # noqa: E402

DB = os.path.join(os.path.dirname(__file__), "..", "data", "brain.db")
RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw")

HEAD = re.compile(r"^【(QQ私聊|QQ群|抖音)[^】]*?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})】\s*$")
LINE = re.compile(r"^(他|我|她)：(.*)$")


def existing_sources() -> set[str]:
    got: set[str] = set()
    if not os.path.isdir(RAW_DIR):
        return got
    for name in os.listdir(RAW_DIR):
        if not name.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(RAW_DIR, name), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    src = rec.get("src")
                    if src:
                        got.add(str(src))
        except OSError:
            continue
    return got


def parse(text: str):
    """返回 (session, ts, [(role, text), ...])；认不出来返回 None。"""
    lines = [ln.rstrip() for ln in str(text or "").splitlines()]
    if not lines:
        return None
    m = HEAD.match(lines[0].strip())
    if not m:
        return None
    kind, day, hhmm = m.group(1), m.group(2), m.group(3)
    try:
        ts = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").timestamp()
    except ValueError:
        return None
    session = "group" if kind == "QQ群" else "private"
    msgs: list[tuple[str, str]] = []
    for ln in lines[1:]:
        mm = LINE.match(ln.strip())
        if mm:
            who, body = mm.group(1), mm.group(2).strip()
            if body:
                msgs.append(["user" if who == "他" else "assistant", body])
            continue
        # 续行：她的回复经常不在每行重复「我：」，
        # 直接接给上一条，否则半段话会被丢掉（#204、#205 就是这么只剩一句的）。
        body = ln.strip()
        if body and msgs:
            msgs[-1][1] = msgs[-1][1] + "\n" + body
    if not msgs:
        return None
    return session, ts, [(role, body) for role, body in msgs]


def main(apply: bool = False):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT id, status, text FROM memories
           WHERE text LIKE '【QQ%' OR text LIKE '【抖音%'
           ORDER BY id"""
    ).fetchall()
    done = existing_sources()
    raw = RawLog(RAW_DIR)
    todo, skip, bad = [], 0, 0
    for r in rows:
        src = f"mem:{r['id']}"
        if src in done:
            skip += 1
            continue
        got = parse(r["text"])
        if not got:
            bad += 1
            continue
        todo.append((r["id"], src, got))

    total_msgs = sum(len(g[2]) for _, _, g in todo)
    print(f"候选记忆 {len(rows)} 条｜待回填 {len(todo)} 条｜已存在跳过 {skip}｜解析失败 {bad}")
    print(f"将写入消息 {total_msgs} 条\n")
    for mid, _, (session, ts, msgs) in todo[:3]:
        print(f"  #{mid} {session} {time.strftime('%m-%d %H:%M', time.localtime(ts))} "
              f"{len(msgs)} 条，首句：{msgs[0][1][:30]}")
    if not apply:
        print("\n（预演，未写入。加 --apply 才落盘）")
        return

    n = 0
    for mid, src, (session, ts, msgs) in todo:
        for i, (role, body) in enumerate(msgs):
            # 头只有一个时间点，往后每句 +30 秒，保证同一段对话里顺序可还原
            got = raw.append(session, role, body, ts=ts + i * 30, extra={"src": src})
            if got:
                n += 1
    print(f"\n已写入 {n} 条消息")
    print("统计：", raw.stats())


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
