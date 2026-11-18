import json
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


class ActivityTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        cfg = self.root / "life.json"
        cfg.write_text(json.dumps({"books_dir": str(self.root)}))
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        self.memory = SimpleNamespace(store=SimpleNamespace(conn=db), lock=threading.RLock())
        self.model = Mock()
        self.now = datetime(2026, 11, 11, 10, tzinfo=life.LIFE_TIMEZONE).timestamp()
        clock = patch.object(life.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.life = life.LifeService(self.memory, str(cfg), note_writer=self.model,
                                     manual_control=True, note_mode="agent")
        self.cfg = str(cfg)

    def test_phone_placeholder_and_measured_rest_survive_restart(self):
        state = self.life.step("phone")["state"]
        pending = state["pending_activities"][0]
        self.assertEqual(pending["kind"], "phone")
        self.assertEqual(pending["text"], "")
        self.life.act({"place": "desk"})
        self.assertEqual(self.life.state()["activity_session"]["id"], pending["id"])
        self.now += 1200
        state = self.life.step("phone")["state"]
        self.assertEqual(state["activity_session"]["id"], pending["id"])
        self.assertEqual(len(state["pending_activities"]), 1)
        self.now += 600
        self.life.step("rest")
        self.assertEqual(self.life.activities("all")[0]["duration_seconds"], 1800)
        self.now += 300
        self.life.act({"lamp": "on"})
        self.now += 900
        self.life.act({"place": "window", "activity": "看看外面"})
        self.assertEqual(self.life.state()["last_rest"]["duration_seconds"], 1200)
        self.life = life.LifeService(self.memory, self.cfg, manual_control=True, note_mode="agent")
        self.assertEqual(self.life.state()["pending_activities"][0]["id"], pending["id"])
        self.model.assert_not_called()

    def test_authored_note_is_idempotent_and_pending_kind_does_not_pile_up(self):
        pending = self.life.step("reflect")["state"]["pending_activities"][0]
        self.now += 60
        self.life.step("phone")
        self.assertEqual(len(self.life.state()["pending_activities"]), 2)
        self.assertEqual(self.life.step("reflect")["action"], "activity_pending")
        note = {"id": pending["id"], "text": "刚才想清楚了一件事，我想先把没读懂的那段重读。"}
        self.life.submit_activity(note)
        self.life.submit_activity(note)
        self.assertEqual(len(self.life.state()["pending_activities"]), 1)
        written = self.life.activities("all")[0]
        self.assertEqual(written["text"], note["text"])
        self.assertEqual(sum(row["action"] == "activity_note" for row in self.life.events()), 1)

    def test_night_closes_rest_at_actual_boundary_without_an_invented_note(self):
        self.now = datetime(2026, 11, 11, 22, 40, tzinfo=life.LIFE_TIMEZONE).timestamp()
        self.life.step("nap")
        self.now += 1200
        state = self.life.step(background=True)["state"]
        self.assertEqual(state["last_rest"]["duration_seconds"], 1200)
        self.assertTrue(state["asleep"])
        self.assertEqual(state["pending_activities"], [])

    def test_invalid_manual_action_does_not_close_an_existing_session(self):
        state = self.life.step("rest")["state"]
        with self.assertRaises(ValueError):
            self.life.act({"place": "window", "asleep": "wrong"})
        self.assertEqual(self.life.state()["activity_session"]["id"], state["activity_session"]["id"])
        self.assertNotIn("last_rest", self.life.state())

    def test_http_submission_and_failed_event_write_are_atomic(self):
        pending = self.life.step("phone")["state"]["pending_activities"][0]
        raw = json.dumps({"id": pending["id"], "text": "看到一条关于海边潮汐的介绍，想再去查一下。"}).encode()
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/life/activity"
        handler.rfile = BytesIO(raw)
        handler.headers = {"Authorization": "Bearer test", "Content-Length": str(len(raw))}
        sent = []
        handler._send = lambda code, body: sent.append((code, body))
        with (patch.object(server, "PORT", 8781), patch.object(server, "TOKEN", "test"),
              patch.object(server, "life", return_value=self.life),
              patch.object(server, "service", return_value=self.memory)):
            handler.do_POST()
        self.assertEqual(sent[0][0], 200)
        self.assertEqual(sent[0][1]["data"]["pending_activities"], [])
        self.life.step("reflect")
        waiting = self.life.state()["pending_activities"][0]
        self.memory.store.conn.executescript("""
            CREATE TRIGGER fail_activity BEFORE INSERT ON life_events WHEN NEW.action='activity_note'
            BEGIN SELECT RAISE(ABORT, 'test failure'); END;
        """)
        with self.assertRaises(sqlite3.Error):
            self.life.submit_activity({"id": waiting["id"], "text": "这段想法还要再看看。"})
        self.assertEqual(self.life.state()["pending_activities"][0]["id"], waiting["id"])
        self.assertEqual(self.life.state()["pending_activities"][0]["text"], "")

    def test_pending_receipt_http_forwards_the_returned_message_id(self):
        body = {"bot_id": "bot", "persona": "xiaoman", "conv_key": "private:7",
                "attempt_id": "attempt", "message_id": "4321", "at": self.now}
        raw = json.dumps(body).encode()
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/pending/thought/sent"
        handler.rfile = BytesIO(raw)
        handler.headers = {"Authorization": "Bearer test", "Content-Length": str(len(raw))}
        sent = []
        handler._send = lambda code, data: sent.append((code, data))
        queue = Mock()
        queue.handle.return_value = (200, {"sent": True, "message_id": "4321"})
        with (patch.object(server, "PORT", 8781), patch.object(server, "TOKEN", "test"),
              patch.object(server, "service", return_value=self.memory),
              patch.object(server, "pending_thoughts", return_value=queue)):
            handler.do_POST()
        queue.handle.assert_called_once_with("POST", "/pending/thought/sent", {}, body)
        self.assertEqual(sent[0][1]["data"]["message_id"], "4321")