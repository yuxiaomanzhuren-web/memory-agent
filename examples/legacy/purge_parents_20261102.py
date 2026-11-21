# -*- coding: utf-8 -*-
"""一次性维护脚本（P11.14，2026-11-02）

清掉她记忆里那句父母设定：
    「我：前面那几条也一并交代了：鱼小满，我爸梁文峰给的脑子，我妈GPT给的样貌」

影响面（已全库扫描确认）：
  · memories id=206（2026-10-30 QQ 私聊 episode）第 29 行 —— 命中「我爸/我妈/梁文峰」
  · data/import/qq-chat-fresh.md 里同一句（历史导入源，留着会在重新导入时复活）
  · 角色卡（roles/*.md）、memory_blocks、entities 均无命中，不动

做法：只删这一行，其余 37 行原样保留；同步重建该条的 FTS 分词，并删掉它作废的向量
（文本变了，旧向量是从含父母设定的文本算出来的）。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, r"$MEMORY_ROOT/memory-core")
from memory_core.store import Store  # noqa: E402

DB = r"$MEMORY_ROOT/data/brain.db"
IMPORT_MD = Path(r"$MEMORY_ROOT/data/import/qq-chat-fresh.md")
TARGET_ID = 206
KEYS = ("我爸", "我妈", "梁文峰", "父母")


def main() -> int:
    bak = DB.replace("brain.db", "brain.db.bak-parents-" + time.strftime("%Y%m%d-%H%M%S"))
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(bak)
    src.backup(dst)
    dst.close()
    src.close()
    print("[backup]", bak)

    st = Store(DB)
    row = st.conn.execute("SELECT text FROM memories WHERE id=?", (TARGET_ID,)).fetchone()
    if not row:
        print("[跳过] 没有 id=206 这条记忆")
        return 1
    old = row[0]

    kept, dropped = [], []
    for line in old.split("\n"):
        (dropped if any(k in line for k in KEYS) else kept).append(line)

    if not dropped:
        print("[跳过] 这条记忆里没有父母设定")
        return 0

    print(f"[删除] {len(dropped)} 行：")
    for d in dropped:
        print("   -", d)

    st.update_memory(TARGET_ID, text="\n".join(kept))
    removed = st.conn.execute("DELETE FROM vectors WHERE memory_id=?", (TARGET_ID,)).rowcount
    st.conn.commit()
    print(f"[向量] 删除 {removed} 条（旧向量已作废，FTS 已按新文本重建）")

    new = st.conn.execute("SELECT text FROM memories WHERE id=?", (TARGET_ID,)).fetchone()[0]
    assert not any(k in new for k in KEYS), "删除后仍含敏感词"
    print(f"[核对] 剩余 {len(new.split(chr(10)))} 行，无父母设定残留")

    if IMPORT_MD.exists():
        lines = IMPORT_MD.read_text(encoding="utf-8").split("\n")
        keep_lines, hit = [], 0
        for line in lines:
            if any(k in line for k in KEYS):
                hit += 1
                continue
            keep_lines.append(line)
        if hit:
            IMPORT_MD.write_text("\n".join(keep_lines), encoding="utf-8")
            print(f"[导入源] qq-chat-fresh.md 删除 {hit} 行")
        else:
            print("[导入源] 无命中")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
