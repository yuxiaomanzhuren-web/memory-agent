# -*- coding: utf-8 -*-
"""通用生活层：状态、动作、事件 outbox。不绑定任何平台。"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# 位置：房间只读这五个字段（place / activity / mood / last_moved_at / updated_at）。
# 规则来自 codex：事件只是「候选理由」，当前活动决定能不能切；手上的事没做完，默认先做完。
PLACE_OF = {
    "rest": "bed",
    "nap": "bed",
    "book": "shelf",
    "phone": "desk",
    "reflect": "window",   # 坐着想事，习惯往窗边走
    "study": "desk",
    "idle": "desk",
}
PLACE_HOLD_SEC = 1500      # 换位置后至少待 25 分钟，别一分钟挪三次
AWAY_AFTER_SEC = 5400      # 一个半小时没动静，算不在屋里
READ_STEP_CHARS = 1500
READ_SESSION_CHARS = 3000
BOOK_DEFER_SEC = 7 * 86400
BOOK_MODEL_PICKS_PER_DAY = 2
READ_NOTE_CALLS_PER_DAY = 4
READ_NOTES_PER_BOOK_PER_DAY = 2
READ_NOTE_TAGS = ("有意思", "没意思", "没看懂")
LIFE_TIMEZONE = timezone(timedelta(hours=8))
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 8
MANUAL_FIELDS = ("place", "activity", "lamp", "book", "asleep")
MANUAL_PLACES = {"bed": "床边", "desk": "桌边", "shelf": "书架", "window": "窗边", "away": "离屋"}
TIMED_ACTIVITIES = ("phone", "reflect", "rest", "nap")

DEFAULTS = {
    "enabled": True,
    "interval_min": 20,
    "books_dir": "./books",
    "topics": ["最近在学的东西", "一个一直没弄懂的问题", "今天看到的一个现象"],
    "weights": {"idle": 1.0, "rest": 1.0, "reflect": 1.0, "book": 1.4, "phone": 1.2, "nap": 0.5},
}


class LifeService:
    def __init__(self, memory, config_path: str | None = None, *,
                 note_writer: Callable[[str, str], dict | None] | None = None,
                 book_chooser: Callable[[list[dict]], dict | None] | None = None,
                 manual_control: bool = False, memory_slots=None, note_mode: str = "model",
                 daily_writer: Callable[[float], None] | None = None):
        self.memory = memory
        self.note_writer = note_writer
        self.book_chooser = book_chooser
        self.manual_control = manual_control
        self.memory_slots = memory_slots
        self.note_mode = note_mode
        self.daily_writer = daily_writer
        window = os.environ.get("MEMORY_NIGHT_WINDOW") or f"{NIGHT_START_HOUR:02d}:00-{NIGHT_END_HOUR:02d}:00"
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", window):
            raise ValueError("MEMORY_NIGHT_WINDOW 必须是 HH:MM-HH:MM")
        self.night_start, self.night_end = (
            int(part[:2]) * 60 + int(part[3:]) for part in window.split("-"))
        if self.night_start == self.night_end:
            raise ValueError("MEMORY_NIGHT_WINDOW 的起止时刻必须不同")
        self.night_window = window
        self._book_pick_pending = False
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path and Path(config_path).exists() else json.loads(json.dumps(DEFAULTS))
        for k, v in DEFAULTS.items():
            self.cfg.setdefault(k, v)
        self.conn = memory.store.conn
        self.conn.executescript(
            """CREATE TABLE IF NOT EXISTS life_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
               CREATE TABLE IF NOT EXISTS life_events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   action TEXT NOT NULL,
                   text TEXT NOT NULL,
                   created_at REAL NOT NULL,
                   delivered INTEGER NOT NULL DEFAULT 0,
                   source TEXT NOT NULL DEFAULT 'system'
               );
               CREATE TABLE IF NOT EXISTS life_activity_log (
                   id TEXT PRIMARY KEY,
                   kind TEXT NOT NULL,
                   started_at REAL NOT NULL,
                   ended_at REAL,
                   source TEXT NOT NULL,
                   note_status TEXT NOT NULL,
                   text TEXT NOT NULL DEFAULT '',
                   noted_at REAL,
                   skip_reason TEXT NOT NULL DEFAULT ''
               );"""
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(life_events)")}
        if "source" not in columns:
            self.conn.execute("ALTER TABLE life_events ADD COLUMN source TEXT NOT NULL DEFAULT 'system'")
        self.conn.commit()

    def _get(self, key: str, default: dict) -> dict:
        row = self.conn.execute("SELECT value FROM life_state WHERE key=?", (key,)).fetchone()
        if not row:
            return dict(default)
        try:
            return json.loads(row[0])
        except Exception:
            return dict(default)

    def _put(self, key: str, value: dict, *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO life_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        if commit:
            self.conn.commit()

    def state(self) -> dict:
        return self._snapshot(self._state())

    def _snapshot(self, s: dict) -> dict:
        focus = s.get("focus")
        public = dict(s)
        public["pending_activities"] = self.activities("pending")
        if session := s.get("activity_session"):
            public["activity_session"] = {**session, "elapsed_seconds": max(0, time.time() - session["started_at"])}
        last_rest = self.conn.execute(
            "SELECT * FROM life_activity_log WHERE kind IN ('rest','nap') AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC,rowid DESC LIMIT 1").fetchone()
        if last_rest is not None:
            public["last_rest"] = self._activity_row(last_rest)
        last_note = self.conn.execute(
            "SELECT * FROM life_activity_log WHERE note_status='written' ORDER BY noted_at DESC,rowid DESC LIMIT 1"
        ).fetchone()
        if last_note is not None:
            public["last_activity_note"] = self._activity_row(last_note)
        public.pop("book_records", None)
        public.pop("read_note_budget", None)
        public.pop("book_pick_budget", None)
        if pending := s.get("pending_note"):
            public["pending_note"] = {**pending, "overdue": time.time() - pending["completed_at"] >= 86400}
        if ref := public.pop("last_read_note_ref", None):
            record = s.get("book_records", {}).get(ref["book_id"], {})
            public["last_read_note"] = next(
                (note for note in reversed(record.get("read_notes", [])) if note["id"] == ref["id"]), None)
        rest = public.pop("night_rest", None)
        manual_asleep = public.pop("manual_asleep", None)
        public["asleep"] = bool(rest and s["place"] == "bed") if manual_asleep is None else manual_asleep
        if rest:
            public["rest_until"] = rest["until"]
        public["focus"] = None if not focus else {
            "title": f"读《{focus['title']}》",
            "book_id": focus.get("book_id"),
            "status": focus["status"],
            "started_at": focus["started_at"],
            "interruptions": focus["interruptions"],
            "done": focus["cursor"] - focus["start"],
            "total": focus["target"] - focus["start"],
            "cursor": focus["cursor"],
            "excerpt": focus.get("excerpt", ""),
        }
        if focus and focus["status"] == "active":
            if manual_asleep:
                public["focus"].update(status="paused", pause_reason="manual_sleep")
            elif rest and manual_asleep is None:
                public["focus"].update(status="paused", pause_reason="night")
        if focus and "source" in focus:
            public["focus"]["source"] = focus["source"]
        if focus and "last_interrupt" in focus:
            public["focus"]["last_interrupt"] = focus["last_interrupt"]
        return public

    def _state(self) -> dict:
        now = time.time()
        s = self._get("state", {"energy": 0.7, "boredom": 0.5, "social": 0.5, "curiosity": 0.5,
                                "play": 0.5, "rest": 0.7, "last": now, "book_offset": 0,
                                "place": "desk", "place_since": now, "place_reason": "一开始就在桌边",
                                "activity": "坐着", "activity_since": now})
        s.setdefault("place", "desk")
        s.setdefault("place_since", now)
        s.setdefault("place_reason", "一开始就在桌边")
        s.setdefault("activity", "坐着")
        s.setdefault("activity_since", now)
        s.setdefault("lamp", "auto")
        s["control_mode"] = "manual_priority" if self.manual_control else "auto"
        s["night_window"] = self.night_window
        # 长时间没动静 = 真的不在屋里，不是暂停
        if (not self.manual_control and not s.get("night_rest")
                and now - float(s.get("last") or now) > AWAY_AFTER_SEC and s["place"] != "away"):
            s["place"] = "away"
            s["place_since"] = float(s.get("last") or now)
            s["place_reason"] = "很久没动静，出门了"
            s["activity"] = "不在"
            s["activity_since"] = s["place_since"]
        s["mood"] = {"valence": round(1.0 - float(s.get("boredom", 0.5)), 2),
                     "arousal": round(float(s.get("energy", 0.7)), 2)}
        s["last_moved_at"] = float(s.get("place_since") or now)
        s["updated_at"] = float(s.get("last") or now)
        return s

    def act(self, data: dict) -> dict:
        if not isinstance(data, dict) or not data or set(data) - set(MANUAL_FIELDS):
            raise ValueError("请提供 place、activity、lamp、book 或 asleep 中至少一项")
        now, s = time.time(), self._state()
        previous_activity = (s["place"], s["activity"], s.get("manual_asleep"))
        night = self._apply_night(s, now)
        entries = [night] if night is not None and night[0] != "skip" else []
        changes = []
        if "place" in data:
            place = data["place"]
            if not isinstance(place, str) or place not in MANUAL_PLACES:
                raise ValueError("place 必须是 bed、desk、shelf、window 或 away")
            if place != s["place"]:
                s.update(place=place, place_since=now, place_reason="手动移动")
            changes.append("位置 " + MANUAL_PLACES[place])
        if "activity" in data:
            activity = data["activity"]
            if not isinstance(activity, str) or not 1 <= len(activity.strip()) <= 120 or len(activity.strip().splitlines()) != 1:
                raise ValueError("activity 必须是一行 1 到 120 字的文字")
            s.update(activity=activity.strip(), activity_since=now)
            changes.append("活动 " + s["activity"])
        if "lamp" in data:
            lamp = data["lamp"]
            if lamp not in ("on", "off", "auto"):
                raise ValueError("lamp 必须是 on、off 或 auto")
            s["lamp"] = lamp
            changes.append("台灯 " + lamp)
        if "book" in data:
            changes.append(self._act_book(s, data["book"], now))
        if "asleep" in data:
            asleep = data["asleep"]
            if not isinstance(asleep, bool):
                raise ValueError("asleep 必须是 true 或 false")
            s["manual_asleep"] = asleep
            changes.append("睡着了" if asleep else "醒着")
        current_activity = (s["place"], s["activity"], s.get("manual_asleep"))
        if "book" in data or current_activity != previous_activity:
            s.pop("activity_session", None)
        if rest := s.get("night_rest"):
            if "place" in data:
                rest["before"].update(place=s["place"], place_reason=s["place_reason"])
            if "activity" in data:
                rest["before"]["activity"] = s["activity"]

        hold_until = now + self.interval_seconds()
        if focus := s.get("focus"):
            focus["next_step_at"] = max(focus["next_step_at"], hold_until)
        s.update(last=now, last_step_at=now, updated_at=now, last_moved_at=s["place_since"],
                 manual_hold_until=hold_until,
                 last_manual_act={"at": now, "source": "agent", "fields": list(data)})
        entries.append(("act", "手动动作 · " + "；".join(changes), "agent"))
        self._save_events(s, now, entries)
        return self._snapshot(s)

    def _act_book(self, s: dict, book: dict | None, now: float) -> str:
        if book is None:
            s["focus"] = None
            return "合上书，保留书签"
        if not isinstance(book, dict) or not book or set(book) - {"book_id", "cursor"}:
            raise ValueError("book 需要 book_id、cursor，或传 null 合上书")
        focus = s.get("focus") or {}
        book_id = book.get("book_id", focus.get("book_id"))
        books = self._catalog()
        if not isinstance(book_id, str) or book_id not in books:
            raise ValueError("找不到这本书，请使用书架返回的 book_id")
        path, body = books[book_id]
        record = self._book_records(s, books).setdefault(book_id, {})
        cursor = book.get("cursor", record.get("cursor", 0))
        if type(cursor) is not int or not 0 <= cursor <= len(body):
            raise ValueError("cursor 必须是从 0 到正文长度的整数字符位置")
        record.update(cursor=cursor, title=path.stem)
        s.update(book_title=path.stem, book_offset=cursor)
        if cursor == len(body):
            s["focus"] = None
        elif focus.get("book_id") == book_id and focus["start"] <= cursor < focus["target"]:
            if cursor != focus["cursor"]:
                focus["excerpt"] = ""
            focus.update(cursor=cursor, source="agent")
        else:
            s["focus"] = {
                "book": path.name, "book_id": book_id, "title": path.stem, "status": "active",
                "source": "agent", "started_at": now, "interruptions": 0,
                "start": cursor, "cursor": cursor, "target": min(len(body), cursor + READ_SESSION_CHARS),
                "next_step_at": now + self.interval_seconds(),
            }
        return f"《{path.stem}》书签第 {cursor} 字"

    def _move(self, s: dict, action: str, text: str, force: bool = False, background: bool = False) -> None:
        """事件只是候选：能不能换位置，由当前活动做没做完决定。"""
        target = PLACE_OF.get(action)
        now = time.time()
        if not target:
            return
        cur = s.get("place") or "desk"
        if text:
            s["activity"] = text[:40]
            s["activity_since"] = now
        if cur == "away":
            s["place"] = target
            s["place_since"] = now
            s["place_reason"] = "生活安排接上了，回来了" if background else "有人叫我，回来了"
            s["activity_since"] = now
            return
        if target == cur:
            return
        # 惯性：刚换过位置、手上还压着事，就先不切（除非强推）
        if not force and now - float(s.get("place_since") or 0) < PLACE_HOLD_SEC:
            return
        s["place"] = target
        s["place_since"] = now
        s["place_reason"] = text[:40] or action
        s["activity"] = text[:40] or action
        s["activity_since"] = now

    def _recover(self, s: dict) -> None:
        # 恢复速率要压得住消耗：实测 45 分钟一步、每次 rest/reflect/phone 都在扣，
        # 旧速率（无聊 +0.04/h、好奇 +0.02/h）入不敷出，两个值长期趴 0。
        now = time.time()
        hours = max(0.0, (now - float(s.get("last") or now)) / 3600.0)
        s["energy"] = min(1.0, float(s.get("energy", 0.7)) + hours * 0.03)
        s["boredom"] = min(1.0, float(s.get("boredom", 0.5)) + hours * 0.07)
        s["social"] = min(1.0, float(s.get("social", 0.5)) + hours * 0.02)
        s["curiosity"] = min(1.0, float(s.get("curiosity", 0.5)) + hours * 0.06)
        s["last"] = now

    @staticmethod
    def _load_book(path: Path) -> tuple[str, str]:
        raw = path.read_bytes()
        return hashlib.sha256(raw).hexdigest(), raw.decode("utf-8-sig", errors="ignore")

    def _catalog(self) -> dict[str, tuple[Path, str]]:
        folder = Path(str(self.cfg.get("books_dir") or "./books"))
        books = {}
        for path in sorted(folder.glob("*.txt")):
            try:
                book_id, text = self._load_book(path)
            except OSError:
                continue
            if text.strip():
                books.setdefault(book_id, (path, text))
        return books

    @staticmethod
    def _book_records(s: dict, books: dict) -> dict:
        if "book_records" not in s:
            s["book_records"] = records = {}
            # 第一回合保留旧版按书名存的书签。之后只按正文标识查找。
            title = s.get("book_title")
            matches = [book_id for book_id, (path, _) in books.items() if path.stem == title]
            if len(matches) == 1:
                records[matches[0]] = {"cursor": max(0, int(s.get("book_offset") or 0))}
        return s["book_records"]

    def _book_options(self, s: dict, books: dict, now: float) -> tuple[list[str], list[dict]]:
        records = self._book_records(s, books)
        ready, skipped = [], []
        for book_id, (path, text) in books.items():
            record = records.get(book_id, {})
            defer = record.get("defer") or {}
            if int(record.get("cursor", 0)) >= len(text):
                skipped.append({"id": book_id, "title": path.stem, "rule": "finished", "reason": "已经读到书尾"})
            elif float(defer.get("until", 0)) > now:
                skipped.append({"id": book_id, "title": path.stem, "rule": "deferred", "reason": defer["reason"]})
            else:
                ready.append(book_id)

        # 到期后可以再读。若已有未暂缓的替代书，先兑现一次「下次换一本」。
        preferred = [book_id for book_id in ready if not (records.get(book_id, {}).get("defer") or {}).get("skip_next")]
        if preferred:
            for book_id in ready:
                if book_id not in preferred:
                    skipped.append({"id": book_id, "title": books[book_id][0].stem, "rule": "prefer_other",
                                    "reason": records[book_id]["defer"]["reason"]})
            ready = preferred
        return ready, skipped

    def bookshelf(self) -> dict:
        s, books = self._state(), self._catalog()
        ready, skipped = self._book_options(s, books, time.time())
        records = self._book_records(s, books)
        blocked = {item["id"]: item for item in skipped}
        next_steps = {
            "ready": "下次从原书签继续。",
            "deferred": "暂缓到期后再考虑，从原书签继续。",
            "prefer_other": "这次先读别的书，之后可从原书签继续。",
            "finished": "归档，不参加下一次选书。",
        }
        rows = []
        for book_id, (path, _) in books.items():
            record = records.get(book_id, {})
            block = blocked.get(book_id, {})
            status = block.get("rule", "ready")
            reason = block.get("reason", "")
            notes = record.get("read_notes", [])
            note_ids = (record.get("defer") or {}).get("note_ids", []) if status in ("deferred", "prefer_other") else []
            rows.append({
                "id": book_id, "title": path.stem, "cursor": record.get("cursor", 0),
                "status": status, "reason": reason,
                "deferred_until": (record.get("defer") or {}).get("until"),
                "read_notes": notes, "defer_note_ids": self._defer_note_ids(notes),
                "card": {"book": path.stem, "conclusion": reason or "可继续读",
                         "next": next_steps[status], "note_ids": note_ids},
            })
        return {"books": rows,
                "next": ready[0] if ready else None,
                "last_choice": s.get("book_choice")}

    @staticmethod
    def _defer_note_ids(notes: list[dict]) -> list[int]:
        recent = notes[-5:]
        negative = [note["id"] for note in recent if note["tag"] in ("没意思", "没看懂")]
        last_two = [note["id"] for note in recent[-2:]]
        if len(last_two) == 2 and negative[-2:] == last_two:
            return last_two
        return negative[-3:] if len(negative) >= 3 else []

    def update_bookshelf(self, op: str, book_id: str, reason: str) -> dict:
        s, books, now = self._state(), self._catalog(), time.time()
        result = self._apply_bookshelf_update(s, books, now, op, book_id, reason)
        if result["changed"]:
            self._save_events(s, now, [("bookshelf", result["note"])])
        return result

    def organize_bookshelf(self, decisions: list[dict]) -> dict:
        if not isinstance(decisions, list) or not decisions:
            raise ValueError("整理书架需要非空的 decisions 列表")
        s, books, now = self._state(), self._catalog(), time.time()
        records = self._book_records(s, books)
        seen, results, events = set(), [], []
        for item in decisions:
            if not isinstance(item, dict):
                raise ValueError("每条整理判断必须是对象")
            book_id, decision = item.get("book_id"), item.get("decision")
            if not isinstance(book_id, str) or book_id not in books or book_id in seen:
                raise ValueError("整理判断需要书架上存在且不重复的 book_id")
            if decision not in ("defer", "continue", "finished"):
                raise ValueError("decision 必须是 defer、continue 或 finished")
            seen.add(book_id)
            path, text = books[book_id]
            finished = int(records.get(book_id, {}).get("cursor", 0)) >= len(text)
            if decision == "finished":
                if not finished:
                    raise ValueError(f"《{path.stem}》还没读到书尾")
                results.append({"changed": False, "book_id": book_id})
                continue
            if finished:
                raise ValueError(f"《{path.stem}》已读完")
            reason = item.get("reason")
            if not isinstance(reason, str):
                raise ValueError("暂缓或继续需要填写理由")
            note_ids = item.get("note_ids", [])
            notes = records.get(book_id, {}).get("read_notes", [])
            if (not isinstance(note_ids, list) or len(note_ids) > 3
                    or any(type(note_id) is not int for note_id in note_ids)
                    or len(set(note_ids)) != len(note_ids)
                    or not set(note_ids) <= {note["id"] for note in notes}):
                raise ValueError(f"《{path.stem}》的笔记 ID 无效")
            if note_ids and decision == "defer":
                evidence = self._defer_note_ids(notes)
                if not evidence or not set(evidence) <= set(note_ids):
                    raise ValueError(f"《{path.stem}》的笔记还不足以支持暂缓")
            op = "undo" if decision == "continue" else "defer"
            result = self._apply_bookshelf_update(s, books, now, op, book_id, reason, note_ids)
            results.append(result)
            if result["changed"]:
                events.append(("bookshelf", result["note"]))
        if events:
            self._save_events(s, now, events)
        return {"changed": bool(events), "results": results}

    def _apply_bookshelf_update(self, s: dict, books: dict, now: float,
                                op: str, book_id: str, reason: str, note_ids: list[int] | None = None) -> dict:
        reason = " ".join(reason.split())
        if op not in ("defer", "undo") or not reason or len(reason) > 160:
            raise ValueError("请指定 defer 或 undo，以及 1 到 160 字的理由")
        records = self._book_records(s, books)
        focus = s.get("focus") or {}
        if book_id not in books and book_id not in records and book_id != focus.get("book_id"):
            raise ValueError("找不到这本书的标识")
        if not book_id:
            raise ValueError("需要提供书的标识")
        record = records.setdefault(book_id, {})
        title = books[book_id][0].stem if book_id in books else (
            focus["title"] if focus.get("book_id") == book_id else record.get("title", "原书"))
        old = record.get("defer")
        note_ids = note_ids or []
        if op == "undo":
            if not old:
                return {"changed": False, "book_id": book_id}
            del record["defer"]
            note = f"撤销《{title}》的暂缓。改主意的原因：{reason}；原来暂缓的原因：{old['reason']}。"
        else:
            if old and old["reason"] == reason and old["until"] > now:
                if not note_ids or old.get("note_ids", []) == note_ids:
                    return {"changed": False, "book_id": book_id, "until": old["until"]}
            else:
                record["title"] = title
                record["defer"] = {"reason": reason, "until": now + BOOK_DEFER_SEC, "skip_next": True}
            if note_ids:
                record["defer"]["note_ids"] = note_ids
            if focus and (focus.get("book_id") == book_id or
                          (not focus.get("book_id") and book_id in books and focus.get("book") == books[book_id][0].name)):
                record["cursor"] = focus["cursor"]
                s["focus"] = None
                activity = f"暂缓《{title}》，书签已保留。"
                if rest := s.get("night_rest"):
                    rest["before"]["activity"] = activity
                else:
                    s.update(activity=activity, activity_since=now)
            note = f"暂缓《{title}》：{reason}。保留原书和书签。"
        if note_ids:
            note += "引用阅读笔记 " + "、".join(f"#{note_id}" for note_id in note_ids) + "。"
        return {"changed": True, "book_id": book_id, "cursor": record.get("cursor", 0),
                "until": (record.get("defer") or {}).get("until"), "note": note}

    def _save_events(self, s: dict, now: float, entries: list[tuple[str, str] | tuple[str, str, str]]) -> int | None:
        first_id = None
        # 同一连接的状态和解释记录一起提交，避免一次选择只有结果、没有理由。
        with getattr(self.memory.store, "_lock", nullcontext()):
            try:
                previous = self._get("state", {}).get("activity_session")
                current = s.get("activity_session")
                if previous and (current is None or previous["id"] != current["id"]):
                    self.conn.execute(
                        "UPDATE life_activity_log SET ended_at=? WHERE id=? AND ended_at IS NULL",
                        (max(now, previous["started_at"]), previous["id"]))
                if current and (previous is None or previous["id"] != current["id"]):
                    self.conn.execute(
                        "INSERT INTO life_activity_log(id,kind,started_at,source,note_status) VALUES(?,?,?,?,?)",
                        (current["id"], current["kind"], current["started_at"], current["source"],
                         "pending" if current["kind"] in ("phone", "reflect") else "none"))
                for action, text, *source in entries:
                    cur = self.conn.execute(
                        "INSERT INTO life_events(action,text,created_at,source) VALUES(?,?,?,?)",
                        (action, text, now, source[0] if source else "system"))
                    if first_id is None:
                        first_id = int(cur.lastrowid)
                pending = s.get("pending_note")
                if pending is not None and "id" not in pending and first_id is not None:
                    pending["id"] = first_id
                self._put("state", s, commit=False)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return first_id

    @staticmethod
    def _activity_row(row) -> dict:
        end = row["ended_at"]
        return {
            "id": row["id"], "kind": row["kind"], "started_at": row["started_at"], "ended_at": end,
            "duration_seconds": None if end is None else max(0, end - row["started_at"]),
            "source": row["source"], "status": row["note_status"], "text": row["text"],
            "noted_at": row["noted_at"], "skip_reason": row["skip_reason"],
        }

    def activities(self, status: str = "pending") -> list[dict]:
        if status not in ("pending", "all"):
            raise ValueError("status 必须是 pending 或 all")
        clause = "WHERE note_status='pending'" if status == "pending" else ""
        rows = self.conn.execute(
            f"SELECT * FROM life_activity_log {clause} ORDER BY started_at DESC,rowid DESC LIMIT 100").fetchall()
        return [self._activity_row(row) for row in reversed(rows)]

    def submit_activity(self, data: dict) -> dict:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ValueError("需要活动记录的 id")
        row = self.conn.execute("SELECT * FROM life_activity_log WHERE id=?", (data["id"],)).fetchone()
        if row is None or row["kind"] not in ("phone", "reflect"):
            raise ValueError("找不到需要补写的活动")
        op = data.get("op", "write")
        field = "text" if op == "write" else "reason"
        value = data.get(field)
        if op not in ("write", "skip") or not isinstance(value, str) or not 1 <= len(value.strip()) <= 160:
            raise ValueError("write 需要一行文字，skip 需要理由，均为 1 到 160 字")
        value = value.strip()
        if len(value.splitlines()) != 1:
            raise ValueError("请写成一行")
        expected = "written" if op == "write" else "skipped"
        saved = row["text"] if op == "write" else row["skip_reason"]
        if row["note_status"] == expected and saved == value:
            return self.state()
        if row["note_status"] != "pending":
            raise ValueError("这一条已经处理过，重复请求不能覆盖它")
        now, s = time.time(), self._state()
        text = value if op == "write" else ""
        reason = value if op == "skip" else ""
        label = "刷手机时" if row["kind"] == "phone" else "停下来想事时"
        action = "activity_note" if op == "write" else "activity_skip"
        description = f"{label}记下：{text}" if op == "write" else f"{label}没有留话，原因是{reason}。"
        with getattr(self.memory.store, "_lock", nullcontext()):
            try:
                self.conn.execute(
                    "UPDATE life_activity_log SET note_status=?,text=?,noted_at=?,skip_reason=? WHERE id=?",
                    (expected, text, now, reason, row["id"]))
                self._save_events(s, now, [(action, description, "agent")])
            except Exception:
                self.conn.rollback()
                raise
        return self._snapshot(s)

    def _current_book(self, focus: dict) -> tuple[Path, str, str] | None:
        folder = Path(str(self.cfg.get("books_dir") or "./books"))
        path = folder / Path(focus["book"]).name
        try:
            book_id, text = self._load_book(path)
            if focus.get("book_id") in (None, book_id):
                return path, text, book_id
        except OSError:
            pass
        saved_id = focus.get("book_id")
        if saved_id and (book := self._catalog().get(saved_id)):
            return (*book, saved_id)
        return None

    def _recent_life_text(self, text: str, minutes: float = 1440) -> bool:
        """同一句生活记录在窗口内只入库一次。
        原来是 90 分钟：她一天发好几次呆，同一句「坐着想了一会儿」被记了 10 遍。
        改成 24 小时——同类小动作一天最多留一条，生活感还在，库不刷屏。"""
        try:
            row = self.conn.execute(
                "SELECT 1 FROM memories WHERE status='active' AND source='life' AND text=? AND created_at>? LIMIT 1",
                (text, time.time() - minutes * 60),
            ).fetchone()
            return bool(row)
        except Exception:
            return False

    def _choose(self, s: dict, has_books: bool) -> str:
        hour = time.localtime().tm_hour
        if s.get("energy", 1) < 0.25:
            return "nap" if (12 <= hour < 14) else "rest"
        if 12 <= hour < 14 and random.random() < 0.25:
            return "nap"
        if s.get("boredom", 0) > 0.7 and random.random() < 0.45:
            return "phone"
        if has_books and s.get("curiosity", 0) > 0.65 and random.random() < 0.5:
            return "book"
        choices = [a for a in ("idle", "rest", "reflect", "book", "phone")
                   if self.cfg["weights"].get(a, 1) > 0 and (a != "book" or has_books)]
        if not choices:
            return "idle"
        weights = [float(self.cfg["weights"].get(a, 1)) for a in choices]
        return random.choices(choices, weights=weights, k=1)[0]

    def interval_seconds(self) -> int:
        return max(60, int(float(self.cfg.get("interval_min") or 20) * 60))

    def next_tick_seconds(self) -> float:
        now = time.time()
        local = datetime.fromtimestamp(now, LIFE_TIMEZONE)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        boundaries = [(midnight + timedelta(days=day, minutes=minute)).timestamp()
                      for day in (0, 1) for minute in (self.night_start, self.night_end, 0)]
        s = self._state()
        boundaries.extend((s.get("manual_hold_until", 0), (s.get("focus") or {}).get("next_step_at", 0)))
        future = [at - now for at in boundaries if at > now]
        return max(0.1, min([self.interval_seconds(), *future]))

    def _queue_book_pick(self, s: dict, now: float, books: dict, ready: list[str],
                         force: str | None, background: bool) -> str | None:
        if self._book_pick_pending:
            return None
        chooser = self.book_chooser
        if chooser is None:
            return "选书模型未启用，先按书架顺序选。"
        day = datetime.fromtimestamp(now, LIFE_TIMEZONE).date().isoformat()
        budget = s.get("book_pick_budget", {})
        attempts = budget.get("attempts", 0) if budget.get("day") == day else 0
        if attempts >= BOOK_MODEL_PICKS_PER_DAY:
            return "今天的模型选书次数已用完，先按书架顺序选。"
        records = self._book_records(s, books)
        candidates = []
        for book_id in ready:
            path, body = books[book_id]
            record = records.get(book_id, {})
            cursor = max(0, int(record.get("cursor", 0)))
            notes = record.get("read_notes", [])
            candidates.append({
                "id": book_id, "title": path.stem, "cursor": cursor,
                "excerpt": " ".join(body[cursor:cursor + 120].split()),
                "last_note": {key: notes[-1][key] for key in ("text", "tag")} if notes else None,
            })
        s["book_pick_budget"] = {"day": day, "attempts": attempts + 1}
        self._save_events(s, now, [])
        self._book_pick_pending = True
        try:
            threading.Thread(target=self._finish_book_pick, daemon=True,
                             args=(chooser, candidates, s.get("last_step_at"), now, force, background)).start()
        except RuntimeError:
            self._book_pick_pending = False
            return "选书后台任务没有启动，先按书架顺序选。"
        return None

    def _finish_book_pick(self, chooser: Callable, candidates: list[dict], last_step_at: float | None,
                          requested_at: float, force: str | None, background: bool) -> None:
        try:
            draft = chooser(candidates)
        except Exception:
            draft = None
        pick = {"requested_at": requested_at, "reason": "模型没有给出有效选项，先按书架顺序选。"}
        if isinstance(draft, dict):
            book_id, reason = draft.get("book_id"), draft.get("reason")
            if isinstance(reason, str):
                reason = " ".join(reason.split())
                if 0 < len(reason) <= 80 and any(candidate["id"] == book_id for candidate in candidates):
                    pick.update(book_id=book_id, reason=reason)
        with self.memory.lock:
            self._book_pick_pending = False
            s = self._state()
            if not s.get("focus") and s.get("last_step_at") == last_step_at:
                self.step(force, background=background, _book_pick=pick)

    def _read_step(self, s: dict, now: float, books: dict, ready: list[str],
                   skipped: list[dict], pick: dict | None = None) -> tuple[str, dict | None]:
        focus = s.get("focus")
        if not focus:
            pick = pick or {}
            book_id = ready[0] if ready else None
            source, reason = "none", "书架上暂时没有合适的书。"
            if len(ready) == 1:
                source, reason = "single", "这次只有这一本符合条件。"
            elif len(ready) > 1:
                if pick.get("book_id") in ready:
                    book_id = pick["book_id"]
                    source, reason = "model", pick["reason"]
                else:
                    source = "fallback"
                    reason = ("模型选的书已不在候选，先按书架顺序选。" if pick.get("book_id")
                              else pick.get("reason", "先按书架顺序选。"))
            selected = books[book_id][0].stem if book_id else None
            s["book_choice"] = {"selected": selected, "selected_id": book_id, "source": source,
                                "reason": reason, "skipped": skipped, "at": now}
            if not ready:
                return "书架上暂时没有合适的书。", None
            records = self._book_records(s, books)
            for item in skipped:
                if item["rule"] in ("deferred", "prefer_other"):
                    records[item["id"]]["defer"]["skip_next"] = False
            book, text = books[book_id]
            cursor = int(records.get(book_id, {}).get("cursor", 0))
            # 只有这一本时到期可再读一次，避免永久挡在候选外。
            if defer := (records.get(book_id, {}).get("defer")):
                defer["skip_next"] = False
            s["focus"] = {
                "book": book.name, "book_id": book_id, "title": book.stem, "status": "active",
                "started_at": now, "interruptions": 0,
                "start": cursor, "cursor": cursor,
                "target": min(len(text), cursor + READ_SESSION_CHARS),
                "next_step_at": pick.get("requested_at", now) + self.interval_seconds(),
            }
            return f"开始读《{book.stem}》这一段。", None

        title = focus["title"]
        current = self._current_book(focus)
        if not current or len(current[1]) < focus["target"]:
            focus["status"] = "paused"
            focus["next_step_at"] = now + self.interval_seconds()
            return f"《{title}》暂时没读到，先保留书签。", None
        book, text, book_id = current
        focus.update(book=book.name, title=book.stem, book_id=book_id)
        title = book.stem
        if focus["status"] == "paused":
            focus["status"] = "active"
            focus["next_step_at"] = now + self.interval_seconds()
            return f"接着读《{title}》。", None
        if now < focus["next_step_at"]:
            return "", None

        start = focus["cursor"]
        focus["cursor"] = min(focus["target"], start + READ_STEP_CHARS)
        focus["excerpt"] = " ".join(text[start:focus["cursor"]].split())[:120]
        s["book_title"] = title
        s["book_offset"] = focus["cursor"]
        self._book_records(s, {}).setdefault(book_id, {})["cursor"] = focus["cursor"]
        done = focus["cursor"] - focus["start"]
        if focus["cursor"] < focus["target"]:
            focus["next_step_at"] = now + self.interval_seconds()
            return f"《{title}》这一段已读 {done} 字。", None
        result = f"读完这段《{title}》，书签到第 {focus['cursor']} 字（本段 {done} 字）。"
        s["focus"] = None
        s["last_completed"] = {"title": f"读《{title}》", "result": result, "completed_at": now,
                               "excerpt": focus["excerpt"]}
        return result, {"book_id": book_id, "file": book.name, "title": title, "start": focus["start"],
                        "end": focus["cursor"], "completed_at": now, "body": text[focus["start"]:focus["cursor"]]}

    def _reserve_read_note(self, s: dict, segment: dict) -> bool:
        if self.note_writer is None and self.memory_slots is None:
            return False
        day = datetime.fromtimestamp(segment["completed_at"], LIFE_TIMEZONE).date().isoformat()
        notes = s["book_records"][segment["book_id"]].get("read_notes", [])
        budget = s.get("read_note_budget", {})
        attempts = budget.get("attempts", 0) if budget.get("day") == day else 0
        if attempts >= READ_NOTE_CALLS_PER_DAY or self._notes_on_day(notes, day) >= READ_NOTES_PER_BOOK_PER_DAY:
            return False
        s["read_note_budget"] = {"day": day, "attempts": attempts + 1}
        segment["day"] = day
        return True

    @staticmethod
    def _notes_on_day(notes: list[dict], day: str) -> int:
        return sum(note["day"] == day for note in notes)

    def note_source(self, note_id: int) -> dict:
        pending = self._state().get("pending_note")
        if type(note_id) is not int or not pending or pending["id"] != note_id:
            raise ValueError("这条待写感想已处理或不是当前那一段")
        book = self._catalog().get(pending["book_id"])
        if book is None or len(book[1]) < pending["end"]:
            raise ValueError("原文已变更或不可读，请恢复原文或明确跳过这一段")
        return {**pending, "body": book[1][pending["start"]:pending["end"]]}

    def submit_note(self, data: dict) -> dict:
        if not isinstance(data, dict) or type(data.get("id")) is not int:
            raise ValueError("需要待写感想的整数 id")
        data = dict(data)
        for field in ("text", "quote"):
            if isinstance(data.get(field), str):
                data[field] = data[field].strip()
        s, now = self._state(), time.time()
        note_id, op = data["id"], data.get("op", "write")
        if op not in ("write", "skip"):
            raise ValueError("op 必须是 write 或 skip")
        pending = s.get("pending_note")
        if not pending or pending["id"] != note_id:
            for record in s.get("book_records", {}).values():
                for note in record.get("read_notes", []):
                    if note["id"] == note_id and op == "write" and all(
                            note.get(key) == data.get(key) for key in ("text", "tag", "quote")):
                        return self._snapshot(s)
            if op == "skip" and s.get("last_note_resolution") == {"id": note_id, "op": "skip"}:
                return self._snapshot(s)
            raise ValueError("这条待写感想已处理或不是当前那一段")
        if op == "skip":
            reason = data.get("reason")
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 160:
                raise ValueError("跳过时需要 1 到 160 字的理由")
            entries = [("note_skip", f"跳过《{pending['title']}》这一段的感想，原因是{reason.strip()}。", "agent")]
        else:
            text, tag, quote = data.get("text"), data.get("tag"), data.get("quote")
            if not all(isinstance(value, str) for value in (text, tag, quote)):
                raise ValueError("需要 text、tag 和 quote")
            text, quote = text.strip(), quote.strip()
            body = self.note_source(note_id)["body"]
            if (not 20 <= len(text) <= 40 or len(text.splitlines()) != 1 or tag not in READ_NOTE_TAGS
                    or not 4 <= len(quote) <= 60 or quote not in body):
                raise ValueError("感想需一行 20 到 40 字、有效标记，以及本段逐字引用的 4 到 60 字原文")
            day = datetime.fromtimestamp(now, LIFE_TIMEZONE).date().isoformat()
            notes = s["book_records"][pending["book_id"]].setdefault("read_notes", [])
            if self._notes_on_day(notes, day) >= READ_NOTES_PER_BOOK_PER_DAY:
                raise ValueError("这本书今天已经存过两条感想，可以明天再写或明确跳过")
            notes.append({**pending, "day": day, "source": "agent", "text": text, "tag": tag, "quote": quote})
            s["last_read_note_ref"] = {"book_id": pending["book_id"], "id": note_id}
            entries = [("read_note", f"读《{pending['title']}》后留下感想：{text}", "agent")]
        del s["pending_note"]
        s["last_note_resolution"] = {"id": note_id, "op": op}
        self._save_events(s, now, entries)
        return self._snapshot(s)

    def _write_read_note(self, event_id: int, segment: dict) -> None:
        if self.memory_slots is not None:
            self.memory_slots.consider(event_id, segment)
            return
        writer = self.note_writer
        if writer is None:
            return
        body = segment.pop("body")
        try:
            draft = writer(segment["title"], body)
        except Exception:
            return
        if not isinstance(draft, dict):
            return
        text, tag, quote = draft.get("text"), draft.get("tag"), draft.get("quote")
        if not all(isinstance(value, str) for value in (text, tag, quote)):
            return
        text, quote = text.strip(), quote.strip()
        if (not 20 <= len(text) <= 40 or len(text.splitlines()) != 1 or tag not in READ_NOTE_TAGS
                or not 4 <= len(quote) <= 60 or quote not in body):
            return
        note = {**segment, "id": event_id, "text": text, "tag": tag, "quote": quote}
        with self.memory.lock:
            s = self._get("state", {})
            record = s.get("book_records", {}).get(note["book_id"])
            if record is None:
                return
            notes = record.setdefault("read_notes", [])
            if (any(item["id"] == event_id for item in notes)
                    or self._notes_on_day(notes, note["day"]) >= READ_NOTES_PER_BOOK_PER_DAY):
                return
            notes.append(note)
            notes.sort(key=lambda item: item["id"])
            if event_id > s.get("last_read_note_ref", {}).get("id", 0):
                s["last_read_note_ref"] = {"book_id": note["book_id"], "id": event_id}
            self._save_events(s, time.time(), [])

    def _apply_night(self, s: dict, now: float) -> tuple[str, str] | None:
        local = datetime.fromtimestamp(now, LIFE_TIMEZONE)
        minute = local.hour * 60 + local.minute
        rest = s.get("night_rest")
        crosses_midnight = self.night_start > self.night_end
        is_night = (minute >= self.night_start or minute < self.night_end) if crosses_midnight else (
            self.night_start <= minute < self.night_end)
        if is_night:
            wake_day = local + timedelta(days=1) if crosses_midnight and minute >= self.night_start else local
            until = wake_day.replace(hour=self.night_end // 60, minute=self.night_end % 60,
                                     second=0, microsecond=0).timestamp()
            if rest and rest["until"] == until:
                return "skip", ""
            before = rest["before"] if rest else {
                key: s[key] for key in ("place", "place_reason", "activity")
            }
            s["night_rest"] = {"started_at": now, "until": until, "before": before}
            s.pop("activity_session", None)
            s.pop("manual_asleep", None)
            action = "night_rest"
            if s["place"] == "away":
                text = "夜间休息，仍不在房间。"
            else:
                if s["place"] != "bed":
                    s.update(place="bed", place_since=now)
                s.update(activity="睡着了", activity_since=now, place_reason="夜间休息")
                text = "睡着了，夜间暂停推进。"
        elif rest:
            before = rest["before"]
            if s["place"] != before["place"]:
                s["place_since"] = now
            s.update(before, activity_since=now)
            del s["night_rest"]
            s.pop("manual_asleep", None)
            focus = s.get("focus")
            if focus and focus["status"] == "active":
                focus["next_step_at"] = now + self.interval_seconds()
            action = "night_wake"
            text = "夜间休息结束，手上的事保留原进度。"
        else:
            return None
        s.update(last=now, last_step_at=now, updated_at=now, last_moved_at=s["place_since"])
        return action, text

    def _night_step(self, s: dict, now: float) -> dict | None:
        change = self._apply_night(s, now)
        if change is None:
            return None
        action, text = change
        if action == "skip":
            return {"action": "skip", "text": "", "state": self._snapshot(s)}
        eid = self._save_events(s, now, [change])
        return {"action": action, "text": text, "event_id": eid, "state": self._snapshot(s)}

    def step(self, force: str | None = None, *, background: bool = False,
             _book_pick: dict | None = None) -> dict:
        s = self._state()
        now = time.time()
        if background and self.daily_writer is not None:
            try:
                self.daily_writer(now)
            except Exception as exc:
                print(f"[life] daily note failed ({type(exc).__name__})", flush=True)
        night = self._night_step(s, now)
        if night is not None:
            return night
        if not force and (now < s.get("manual_hold_until", 0) or s.get("manual_asleep")):
            return {"action": "skip", "text": "", "state": self._snapshot(s)}
        focus = s.get("focus")
        if force == "reply" and (not focus or focus["status"] != "active"):
            return {"action": "skip", "text": "", "state": self._snapshot(s)}
        if not force and (now - float(s.get("last_step_at") or 0) < min(90, self.interval_seconds()) or
                          (focus and now < focus["next_step_at"])):
            return {"action": "skip", "text": "", "state": self._snapshot(s)}
        books, ready, skipped = {}, [], []
        if not focus and (not force or force == "book"):
            books = self._catalog()
            ready, skipped = self._book_options(s, books, now)
        waiting_note = self.note_mode == "agent" and bool(s.get("pending_note"))
        action = force or ("book" if focus or _book_pick is not None else self._choose(s, bool(ready) and not waiting_note))
        if action == "book" and waiting_note:
            return {"action": "note_pending", "text": "", "state": self._snapshot(s)}
        active_kind = (s.get("activity_session") or {}).get("kind")
        if action in ("phone", "reflect") and active_kind != action:
            pending = self.conn.execute(
                "SELECT 1 FROM life_activity_log WHERE kind=? AND note_status='pending' LIMIT 1", (action,)
            ).fetchone()
            if pending:
                if force:
                    return {"action": "activity_pending", "text": "", "state": self._snapshot(s)}
                action = "idle"
        if (action == "book" and not force and s["place"] not in ("desk", "shelf", "away")
                and now - float(s.get("place_since") or now) < PLACE_HOLD_SEC):
            return {"action": "skip", "text": "", "state": self._snapshot(s)}
        if action == "book" and focus and focus["status"] == "active" and now < focus["next_step_at"]:
            return {"action": "skip", "text": "", "state": self._snapshot(s)}
        if action == "book" and not focus and len(ready) > 1 and _book_pick is None:
            fallback = self._queue_book_pick(s, now, books, ready, force, background)
            if fallback is None:
                return {"action": "book_pick_pending", "text": "", "state": self._snapshot(s)}
            _book_pick = {"reason": fallback}
        self._recover(s)
        interrupts = action == "reply" or (action in PLACE_OF and action not in ("book", "idle"))
        if focus and interrupts and focus["status"] == "active":
            focus["status"] = "paused"
            focus["interruptions"] += 1
            focus["last_interrupt"] = {"action": action, "at": now}
            focus["next_step_at"] = now + self.interval_seconds()
        text = ""
        completed = None
        if action == "reply":
            text = "先把书签留在这儿，去回一条消息。"
            s.update(activity=text, activity_since=now)
        elif action == "rest":
            s["energy"] = min(1.0, float(s.get("energy", 0.7)) + 0.12)
            s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.03)
            text = "躺下休息。"
        elif action == "nap":
            s["energy"] = min(1.0, float(s.get("energy", 0.7)) + 0.25)
            s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.05)
            text = "躺下眯一会儿。"
        elif action == "book":
            text, completed = self._read_step(s, now, books, ready, skipped, _book_pick)
            if completed:
                s["curiosity"] = max(0.0, float(s.get("curiosity", 0.5)) - 0.15)
                s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.10)
        elif action == "phone":
            s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.18)
            s["curiosity"] = min(1.0, float(s.get("curiosity", 0.5)) + 0.06)  # 刷到新东西反而勾起好奇
            s["energy"] = max(0.0, float(s.get("energy", 0.7)) - 0.02)
            text = "拿起手机看一会儿，看到什么待补写。"
        elif action == "reflect":
            s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.08)
            s["curiosity"] = min(1.0, float(s.get("curiosity", 0.5)) + 0.04)  # 想着想着就想查点什么
            text = "停下来想一会儿，想到什么待补写。"
        elif action == "study":
            topics = self.cfg.get("topics") or []
            topic = str(topics[int(time.time()) % len(topics)]) if topics else "一个没弄懂的问题"
            s["curiosity"] = max(0.0, float(s.get("curiosity", 0.5)) - 0.15)
            text = f"查了点「{topic}」的资料，先记个印象。"
        if action in TIMED_ACTIVITIES:
            if active_kind != action:
                s["activity_session"] = {
                    "id": uuid.uuid4().hex, "kind": action, "started_at": now,
                    "source": "agent" if force else "system",
                }
        else:
            s.pop("activity_session", None)
        s["last"] = now
        s["last_step_at"] = now
        s["updated_at"] = now
        self._move(s, action, text, force=bool(force), background=background)
        s["last_moved_at"] = s["place_since"]
        entries = [(action, text)] if text and action != "idle" else []
        if action == "book" and not focus:
            choice = s["book_choice"]
            if skipped or choice["source"] == "model":
                selected = f"选了《{choice['selected']}》" if choice["selected"] else "这次没有选书"
                reason = " " + choice["reason"] if choice["source"] == "model" else ""
                notes = "；".join(f"跳过《{item['title']}》：{item['reason']}" for item in skipped)
                entries.append(("bookshelf", f"{selected}。{reason}" + (f" {notes}。" if notes else "")))
        if completed is not None and self.note_mode == "agent":
            s["pending_note"] = {key: value for key, value in completed.items() if key != "body"}
        write_note = (self.note_mode == "model" and completed is not None and (focus or {}).get("source") != "agent"
                      and self._reserve_read_note(s, completed))
        eid = self._save_events(s, now, entries)
        if write_note and eid is not None:
            try:
                threading.Thread(target=self._write_read_note, args=(eid, completed), daemon=True).start()
            except RuntimeError:
                pass
        if text and action not in ("idle", "reply", *TIMED_ACTIVITIES) and (action != "book" or completed) and not self._recent_life_text(text):
            try:
                # importance 0.45→0.3：这是背景呼吸，不是值得回味的事，
                # 权重低于真实对话记忆，衰减时也先被归档。
                self.memory.remember({"text": text, "kind": "episode", "importance": 0.3,
                                      "visibility": "private", "source": "life"})
            except Exception:
                pass
        return {"action": action, "text": text, "event_id": eid, "state": self._snapshot(s)}

    def events(self, after: int = 0, include_delivered: bool = True) -> list[dict]:
        sql = "SELECT * FROM life_events WHERE id>?"
        args: list[Any] = [int(after)]
        if not include_delivered:
            sql += " AND delivered=0"
        rows = self.conn.execute(sql + " ORDER BY id ASC LIMIT 100", args).fetchall()
        return [{"id": int(r["id"]), "action": r["action"], "text": r["text"], "source": r["source"],
                 "created_at": float(r["created_at"]), "delivered": bool(r["delivered"])} for r in rows]

    def ack(self, event_id: int) -> bool:
        cur = self.conn.execute("UPDATE life_events SET delivered=1 WHERE id=?", (int(event_id),))
        self.conn.commit()
        return cur.rowcount > 0


def run_loop(life: LifeService, stop: threading.Event | None = None) -> None:
    stop = stop if stop is not None else threading.Event()
    interval = life.interval_seconds()
    while not stop.is_set():
        try:
            with life.memory.lock:
                if stop.is_set():
                    break
                life.step(background=True)
                interval = life.next_tick_seconds()
        except Exception:
            pass
        stop.wait(interval)
