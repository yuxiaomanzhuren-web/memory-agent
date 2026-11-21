import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from memory_core import life, server


class TodayLifeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.book = self.root / "A.txt"
        self.book.write_text("窗外的雨终于停了" + "正文" * 6000, encoding="utf-8")
        self.cfg = self.root / "life.json"
        self.cfg.write_text(json.dumps({
            "books_dir": str(self.root), "interval_min": 1,
            "weights": {"book": 1, "idle": 0, "rest": 0, "reflect": 0, "phone": 0},
        }), encoding="utf-8")
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.memory = SimpleNamespace(store=SimpleNamespace(conn=self.db), lock=threading.RLock())
        self.writer = Mock()
        self.now = self.at(11, 10)
        clock = patch.object(life.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        window = patch.dict(os.environ, {"MEMORY_NIGHT_WINDOW": ""})
        window.start()
        self.addCleanup(window.stop)
        self.service = self.new()

    def at(self, day, hour, minute=0, second=0):
        return datetime(2026, 11, day, hour, minute, second, tzinfo=life.LIFE_TIMEZONE).timestamp()

    def new(self):
        return life.LifeService(self.memory, str(self.cfg), manual_control=True,
                                note_mode="agent", note_writer=self.writer)

    def step(self, force="book", background=False):
        with self.memory.lock:
            return self.service.step(force, background=background)

    def finish(self):
        for i in range(3):
            self.now += 61
            out = self.step()
        return out

    def test_manual_hold_expires_and_background_advances(self):
        state = self.service.act({"place": "desk"})
        hold = state["manual_hold_until"]
        self.assertEqual(self.step(None, True)["action"], "skip")
        self.now = hold + 1
        self.assertEqual(self.step(None, True)["action"], "book")
        self.now += 61
        self.assertEqual(self.step(None, True)["state"]["focus"]["done"], 1500)
        self.service.act({"place": "window"})
        self.now += 1
        state = self.service.act({"place": "desk"})
        self.assertEqual(state["place"], "desk")
        self.assertEqual(state["manual_hold_until"], self.now + 60)

    def test_pending_survives_restart_and_blocks_another_segment(self):
        out = self.finish()
        pending = out["state"]["pending_note"]
        self.assertEqual(pending["id"], out["event_id"])
        self.assertEqual((pending["start"], pending["end"]), (0, 3000))
        self.assertNotIn("body", pending)
        self.writer.assert_not_called()
        self.service = self.new()
        self.now += 2 * 86400
        self.assertTrue(self.service.state()["pending_note"]["overdue"])
        self.assertEqual(self.step()["action"], "note_pending")
        self.service.act({"book": {"book_id": pending["book_id"], "cursor": 6000}})
        self.assertEqual(self.step()["action"], "note_pending")
        self.assertEqual(self.service.state()["pending_note"]["id"], pending["id"])
        source = self.service.note_source(pending["id"])
        self.assertTrue(source["body"].startswith("窗外的雨终于停了"))
        note = {"id": pending["id"], "text": "窗外的雨停下来以后这个场景突然安静让我想再读一遍",
                "tag": "有意思", "quote": "窗外的雨终于停了"}
        self.service.submit_note(note)
        self.assertNotIn("pending_note", self.service.state())
        self.assertEqual(self.service.state()["last_read_note"]["text"], note["text"])
        self.service.submit_note(note)
        self.assertEqual(len(self.service.bookshelf()["books"][0]["read_notes"]), 1)

    def test_changed_source_requires_explicit_skip(self):
        pending = self.finish()["state"]["pending_note"]
        self.book.write_text("正文已被换掉", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.note_source(pending["id"])
        self.service.submit_note({"id": pending["id"], "op": "skip", "reason": "原文已被换掉"})
        self.assertNotIn("pending_note", self.service.state())

    def test_process_night_window_has_minute_precision_and_wakes_loop_at_boundary(self):
        self.now = self.at(11, 10, 29, 40)
        with patch.dict(os.environ, {"MEMORY_NIGHT_WINDOW": "10:30-10:32"}):
            self.service = self.new()
        self.assertAlmostEqual(self.service.next_tick_seconds(), 20)
        self.now = self.at(11, 10, 30)
        state = self.step(None, True)["state"]
        self.assertTrue(state["asleep"])
        self.assertEqual(state["rest_until"], self.at(11, 10, 32))
        self.now = self.at(11, 10, 32)
        self.assertFalse(self.step(None, True)["state"]["asleep"])
        self.assertNotIn("rest_until", self.service.state())
        self.service = self.new()
        self.assertEqual(self.service.state()["night_window"], "23:00-08:00")

    def test_invalid_window_fails_with_an_explanation(self):
        for window in ("25:00-08:00", "10:00-10:00", "8:00-9:00"):
            with patch.dict(os.environ, {"MEMORY_NIGHT_WINDOW": window}):
                with self.assertRaisesRegex(ValueError, "MEMORY_NIGHT_WINDOW"):
                    self.new()

    def test_cross_midnight_window_and_note_http_routes(self):
        with patch.dict(os.environ, {"MEMORY_NIGHT_WINDOW": "23:59-00:01"}):
            reader = self.new()
        self.now = self.at(11, 23, 59)
        self.assertTrue(reader.step(background=True)["state"]["asleep"])
        self.now = self.at(12, 0, 1)
        self.assertFalse(reader.step(background=True)["state"]["asleep"])
        self.now = self.at(12, 10)
        pending = self.finish()["state"]["pending_note"]
        def request(method, path, data=None):
            raw = json.dumps(data).encode()
            handler = server.Handler.__new__(server.Handler)
            handler.path = path
            handler.rfile = BytesIO(raw)
            handler.headers = {"Content-Length": str(len(raw)), "Authorization": "Bearer test"}
            sent = []
            handler._send = lambda code, payload: sent.append((code, payload))
            with (patch.object(server, "PORT", 8781), patch.object(server, "TOKEN", "test"),
                  patch.object(server, "life", return_value=self.service),
                  patch.object(server, "service", return_value=self.memory)):
                getattr(handler, "do_" + method)()
            return sent[0]
        status, payload = request("GET", f"/life/note?id={pending['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["body"].startswith("窗外的雨终于停了"))
        status, payload = request("POST", "/life/note",
                                  {"id": pending["id"], "op": "skip", "reason": "这次先不写"})
        self.assertEqual(status, 200)
        self.assertNotIn("pending_note", payload["data"])

    def test_background_writes_one_yesterday_line_to_notebook(self):
        from memory_core.life_daily import write_yesterday
        from memory_core.notebook import Notebook
        from memory_core.server import MemoryService

        memory = MemoryService(str(self.root / "daily.db"), embed=False)
        self.addCleanup(memory.store.close)
        reader = life.LifeService(memory, str(self.cfg), manual_control=True, note_mode="agent",
                                  daily_writer=lambda now: write_yesterday(memory, now))
        memory.store.set_block("self", "原来的自我认识")
        reader._save_events(reader._state(), self.at(10, 18), [
            ("book", "读完这段《A》，书签到第 3000 字（本段 3000 字）。")])
        reader.step(background=True)
        first = memory.store.get_block("days")
        self.assertIn("2026-11-10", first["body"])
        self.assertIn("3000", first["body"])
        reader.step(background=True)
        self.assertEqual(memory.store.get_block("days")["version"], first["version"])
        self.assertEqual(memory.store.get_block("self")["body"], "原来的自我认识")
        self.assertIn("自动日记", Notebook(memory.store).render())
