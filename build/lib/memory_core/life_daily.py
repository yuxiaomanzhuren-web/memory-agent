"""Deterministic daily notebook recap; call under memory.lock on background ticks.

Only yesterday in Asia/Shanghai (UTC+8) is considered. A date receipt in meta,
the days block and its version share one transaction; even empty days receive a
receipt. No model, memory recall, or mutable current life state is consulted.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone


LIFE_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
RECEIPT_PREFIX = "life_daily:"


def _fact(event) -> tuple[int, str] | None:
    text = " ".join(str(event["text"] or "").split())
    if event["action"] == "activity_note" and event["source"] in ("agent", "her"):
        return 0, text[:120]
    title = re.search(r"《([^》]+)》", text)
    if not title:
        return None
    book = title[1]
    if len(book) > 40:
        book = book[:40] + "…"
    book = f"《{book}》"
    action = event["action"]
    if action == "read_note" and event["source"] in ("agent", "her"):
        # Record that a note was authored, without inventing or adopting its feelings.
        return 0, f"为{book}留下阅读感想"
    if action == "bookshelf":
        if re.match(r"撤销《[^》]+》的暂缓", text):
            return 1, f"撤销{book}的暂缓"
        if re.match(r"暂缓《[^》]+》", text):
            return 1, f"暂缓{book}"
        if re.match(r"选了《[^》]+》", text):
            return 1, f"选了{book}"
    if action == "book":
        segment = re.fullmatch(
            r"读完这段《[^》]+》，书签到第 (\d+) 字（本段 (\d+) 字）。", text)
        if segment and int(segment[1]) >= int(segment[2]) > 0:
            return 2, f"读完{book}一段，书签到第 {segment[1]} 字"
        if re.fullmatch(r"读完《[^》]+》[。.!！]?", text):
            return 2, f"读完{book}"
    return None


def write_yesterday(memory, now: float) -> dict | None:
    """Append at most two observed facts, returning the new receipt or None.

    Caller owns memory.lock. The Store lock also spans the transaction so another
    connection user or the transaction watchdog cannot split the atomic write.
    An existing transaction is rejected without committing or rolling it back.
    Failures propagate after rollback and can be retried on the next tick.
    """
    today = datetime.fromtimestamp(now, LIFE_TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    day = yesterday.date().isoformat()
    key = RECEIPT_PREFIX + day
    store = memory.store
    conn = store.conn
    with store._lock:
        if conn.in_transaction:
            raise RuntimeError("daily recap requires no active transaction")
        if conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
            return None
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Also serialize a second process using the same SQLite database.
            if conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
                conn.rollback()
                return None
            events = conn.execute(
                "SELECT action,text,source FROM life_events "
                "WHERE created_at>=? AND created_at<? ORDER BY created_at DESC,id DESC",
                (yesterday.timestamp(), today.timestamp()),
            ).fetchall()
            candidates = [fact for event in events if (fact := _fact(event)) is not None]
            facts = []
            for _, fact in sorted(candidates, key=lambda candidate: candidate[0]):
                if fact not in facts:
                    facts.append(fact)
                if len(facts) == 2:
                    break
            line = f"{day}：{'；'.join(facts)}。" if facts else ""
            receipt = {"date": day, "facts": facts, "line": line}
            if line:
                result = store.set_block(
                    "days", line, op="append", note=f"daily recap {day}",
                    by="auto", now=now, commit=False)
                if not result.get("ok"):
                    raise RuntimeError(f"daily notebook write failed: {result}")
                receipt["version"] = result["version"]
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?)",
                (key, json.dumps(receipt, ensure_ascii=False)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return receipt if line else None