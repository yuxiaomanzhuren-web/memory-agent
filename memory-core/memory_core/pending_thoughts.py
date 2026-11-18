"""Scoped SQLite pending queue. Call handle under MemoryService.lock.

No sending or background work. An unresolved reservation never expires: a caller
must prove no_send or supply the real receipt. Replayed claims never authorize send.
"""
from __future__ import annotations

import math
import re
import secrets
import sqlite3
import time
import threading
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
SCOPE = ("bot_id", "persona", "conv_key")


class Rejected(Exception):
    def __init__(self, code, error):
        self.code, self.error = code, error


class PendingThoughts:
    def __init__(self, memory):
        self.conn = memory.store.conn
        self.store_lock = getattr(memory.store, "_lock", threading.RLock())
        with self.store_lock:
            self._initialize()

    def _initialize(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS pending_thoughts (
                id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, persona TEXT NOT NULL,
                conv_key TEXT NOT NULL, content TEXT NOT NULL, topic TEXT NOT NULL,
                motive TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'ready', defer_count INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0, defer_reason TEXT NOT NULL DEFAULT '',
                attempt_id TEXT
            );
            CREATE INDEX IF NOT EXISTS pending_scope ON pending_thoughts(bot_id,persona,conv_key,state);
            CREATE TABLE IF NOT EXISTS pending_attempts (
                attempt_id TEXT PRIMARY KEY, pending_id TEXT NOT NULL,
                bot_id TEXT NOT NULL, persona TEXT NOT NULL, conv_key TEXT NOT NULL,
                topic TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL,
                reserved_at REAL NOT NULL, quota_day TEXT NOT NULL,
                message_id TEXT, sent_at REAL,
                UNIQUE(bot_id,persona,request_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS pending_owner_outstanding
                ON pending_attempts(bot_id,persona) WHERE state IN ('reserved','unknown');
            CREATE UNIQUE INDEX IF NOT EXISTS pending_receipt
                ON pending_attempts(bot_id,message_id) WHERE message_id IS NOT NULL;
        """)
        self.conn.commit()

    def _rows(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        names = [col[0] for col in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    @staticmethod
    def _text(data, key, limit=256, required=True):
        value = data.get(key, "")
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise Rejected(400, f"invalid_{key}")
        value = str(value).strip()
        if (required and not value) or len(value) > limit:
            raise Rejected(400, f"invalid_{key}")
        return value

    def handle(self, method, path, query, data):
        """Return (HTTP status, direct payload); server supplies status/data wrapper."""
        with self.store_lock:
            return self._locked_handle(method, path, query, data)

    def _locked_handle(self, method, path, query, data):
        try:
            values = query if method == "GET" else data
            if not isinstance(values, dict):
                raise Rejected(400, "invalid_body")
            scope = tuple(self._text(values, key) for key in SCOPE)
            if not re.fullmatch(r"[1-9]\d*", scope[0]) or not re.fullmatch(r"(?:private|group):[1-9]\d*", scope[2]):
                raise Rejected(400, "invalid_scope")
            # Serialized both by service.lock and SQLite, including independent processes.
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._handle(method, path.rstrip("/"), values, scope, time.time())
                self.conn.commit()
                return result
            except Exception:
                self.conn.rollback()
                raise
        except Rejected as exc:
            return exc.code, {"error": exc.error}
        except (ValueError, TypeError, OverflowError):
            return 400, {"error": "invalid_value"}
        except sqlite3.IntegrityError:
            return 409, {"error": "reservation_or_receipt_conflict"}

    def _handle(self, method, path, data, scope, now):
        if path == "/pending" and method == "POST":
            content = self._text(data, "content", 200)
            if len(str(data.get("content", "")).splitlines()) != 1:
                raise Rejected(400, "content_requires_one_line")
            topic = self._text(data, "topic", 120)
            motive = self._text(data, "motive", 50, required=False)
            ttl = float(data.get("ttl_hours", 48))
            if not math.isfinite(ttl) or not 0 < ttl <= 720:
                raise Rejected(400, "invalid_ttl_hours")
            item = secrets.token_urlsafe(18)
            self.conn.execute("""INSERT INTO pending_thoughts
                (id,bot_id,persona,conv_key,content,topic,motive,created_at,expires_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (item, *scope, content, topic, motive, now, now + ttl * 3600))
            return 201, self._item(item, scope)
        if path == "/pending" and method == "GET":
            items = self._rows("""SELECT * FROM pending_thoughts
                WHERE bot_id=? AND persona=? AND conv_key=?
                  AND ((state IN ('ready','dormant') AND expires_at>?) OR state IN ('reserved','unknown'))
                ORDER BY priority DESC,created_at,id LIMIT 100""", (*scope, now))
            return 200, {"items": items}
        if method == "GET" and re.fullmatch(r"/pending/[A-Za-z0-9_-]+", path):
            item = self._item(path.rsplit("/", 1)[1], scope)
            return 200, {**item, "attempts": self._rows(
                "SELECT * FROM pending_attempts WHERE pending_id=? ORDER BY reserved_at", (item["id"],))}
        match = re.fullmatch(r"/pending/([A-Za-z0-9_-]+)/(claim|sent|failed|defer)", path)
        if method != "POST" or not match:
            raise Rejected(404, "pending_route_not_found")
        item_id, action = match.groups()
        item = self._item(item_id, scope)
        if action == "claim":
            return self._claim(item, data, scope, now)
        if action == "defer":
            if item["state"] not in ("ready", "dormant"):
                raise Rejected(409, "delivery_already_started")
            reason = self._text(data, "reason", required=False) or "not_now"
            # Only an explicit human/model choice counts; operational failures never do.
            blocked = reason.startswith(("system", "blocked")) or reason in (
                "cooldown", "daily_quota", "rate_limit", "permission", "service_unavailable", "network_error")
            deliberate = data.get("deliberate") is True and not blocked
            count = item["defer_count"] + int(deliberate)
            self.conn.execute("""UPDATE pending_thoughts SET defer_count=?,priority=?,state=?,defer_reason=? WHERE id=?""",
                              (count, -1 if count >= 2 else 0, "dormant" if count >= 3 else "ready", reason, item_id))
            return 200, self._item(item_id, scope)
        attempt_id = self._text(data, "attempt_id")
        attempts = self._rows("SELECT * FROM pending_attempts WHERE attempt_id=? AND pending_id=?", (attempt_id, item_id))
        if not attempts or attempts[0]["attempt_id"] != item["attempt_id"]:
            raise Rejected(409, "attempt_mismatch")
        attempt = attempts[0]
        if action == "sent":
            message = self._text(data, "message_id")
            if not re.fullmatch(r"-?[1-9]\d*", message):
                raise Rejected(400, "invalid_message_id")
            at = data.get("at")
            if isinstance(at, str) and not re.fullmatch(r"\d+(?:\.\d+)?", at):
                stamp = datetime.fromisoformat(at.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise Rejected(400, "at_requires_timezone")
                at = stamp.timestamp()
            at = float(at)
            if not math.isfinite(at) or at < attempt["reserved_at"] - 300 or at > now + 300:
                raise Rejected(400, "invalid_at")
            if attempt["state"] == "sent":
                if attempt["message_id"] != message or attempt["sent_at"] != at:
                    raise Rejected(409, "receipt_conflict")
            elif attempt["state"] in ("reserved", "unknown"):
                self.conn.execute("""UPDATE pending_attempts SET state='sent',message_id=?,sent_at=?,quota_day=?
                    WHERE attempt_id=?""", (message, at, self._day(at), attempt_id))
                self.conn.execute("UPDATE pending_thoughts SET state='sent' WHERE id=?", (item_id,))
            else:
                raise Rejected(409, "attempt_closed")
        else:
            outcome = data.get("outcome", "unknown")
            if outcome not in ("unknown", "no_send"):
                raise Rejected(400, "invalid_outcome")
            target = "released" if outcome == "no_send" else "unknown"
            if attempt["state"] not in ("reserved", "unknown", target):
                raise Rejected(409, "attempt_closed")
            self.conn.execute("UPDATE pending_attempts SET state=? WHERE attempt_id=?", (target, attempt_id))
            self.conn.execute("UPDATE pending_thoughts SET state=? WHERE id=?",
                              ("ready" if outcome == "no_send" else "unknown", item_id))
        return 200, self._receipt(self._rows("SELECT * FROM pending_attempts WHERE attempt_id=?", (attempt_id,))[0], item)

    def _item(self, item, scope):
        rows = self._rows("SELECT * FROM pending_thoughts WHERE id=? AND bot_id=? AND persona=? AND conv_key=?", (item, *scope))
        if not rows:
            raise Rejected(404, "pending_not_found")
        return rows[0]

    @staticmethod
    def _day(at):
        return datetime.fromtimestamp(at, SHANGHAI).date().isoformat()

    @staticmethod
    def _receipt(attempt, item, send_allowed=False):
        return {"id": item["id"], "attempt_id": attempt["attempt_id"], "state": attempt["state"],
                "send_allowed": send_allowed, "content": item["content"],
                "message_id": attempt["message_id"], "at": attempt["sent_at"]}

    def _claim(self, item, data, scope, now):
        request = self._text(data, "request_id")
        owner = scope[:2]
        old = self._rows("SELECT * FROM pending_attempts WHERE bot_id=? AND persona=? AND request_id=?", (*owner, request))
        if old:
            if old[0]["pending_id"] != item["id"] or old[0]["conv_key"] != scope[2]:
                raise Rejected(409, "request_id_conflict")
            return 200, self._receipt(old[0], item)
        if item["state"] != "ready" or item["expires_at"] <= now:
            raise Rejected(409, "pending_not_ready")
        history = self._rows("SELECT * FROM pending_attempts WHERE bot_id=? AND persona=? AND state!='released'", owner)
        if any(a["state"] in ("reserved", "unknown") for a in history):
            raise Rejected(409, "outstanding_delivery")
        if sum(a["quota_day"] == self._day(now) for a in history) >= 2:
            raise Rejected(429, "daily_quota")
        for attempt in history:
            last = max(attempt["reserved_at"], attempt["sent_at"] or 0)
            if now - last < 2 * 3600:
                raise Rejected(429, "global_cooldown")
            if attempt["topic"] == item["topic"] and now - last < 6 * 3600:
                raise Rejected(429, "topic_cooldown")
        attempt_id = secrets.token_urlsafe(24)
        self.conn.execute("""INSERT INTO pending_attempts
            (attempt_id,pending_id,bot_id,persona,conv_key,topic,request_id,state,reserved_at,quota_day)
            VALUES (?,?,?,?,?,?,?,'reserved',?,?)""", (attempt_id, item["id"], *scope, item["topic"], request, now, self._day(now)))
        self.conn.execute("UPDATE pending_thoughts SET state='reserved',attempt_id=? WHERE id=?", (attempt_id, item["id"]))
        attempt = self._rows("SELECT * FROM pending_attempts WHERE attempt_id=?", (attempt_id,))[0]
        return 200, self._receipt(attempt, item, send_allowed=True)