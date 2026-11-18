import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from memory_core import life

try:
    from memory_core.life_slots import LifeSlots, LocalSlotModel
except ModuleNotFoundError as exc:
    if exc.name != "cryptography":
        raise
    raise unittest.SkipTest("life memory slots require cryptography") from exc


class KeyVault:
    def __init__(self):
        self.keys = {}
        self.block_delete = False

    def put(self, slot_id, key):
        self.keys[slot_id] = key

    def get(self, slot_id):
        return self.keys.get(slot_id)

    def delete(self, slot_id):
        if self.block_delete:
            raise OSError("blocked")
        self.keys.pop(slot_id, None)


class LifeSlotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "brain.db"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL").fetchone()
        self.vault = KeyVault()
        self.drafts = []
        self.decisions = []
        self.comparisons = []
        self.slots = LifeSlots(self.path, self.vault, self.model)

    def model(self, stage, payload):
        if stage == "draft":
            return {"text": self.drafts.pop(0)}
        self.comparisons.append((stage, payload))
        return self.decisions.pop(0)

    def review(self, event_id, text, decision):
        self.drafts.append(text)
        self.decisions.append(decision)
        return self.slots.consider(event_id, {"title": "A", "body": "source paragraph"})

    def seed(self):
        for event, text in [(101, "UNIQUE OLD BODY: I'll start from the short chapter next time."),
                            (102, "Keep a note that the difficult dialogue is worth rereading.")]:
            self.assertEqual(self.review(event, text, {"action": "keep", "reason": "specific_value"}), "keep")
        return self.slots.status()["slots"][0]["id"]

    def test_two_declines_then_consequential_replacement_survives_old_database_restore(self):
        first = self.seed()
        backup = self.path.with_name("before.db")
        with closing(sqlite3.connect(self.path)) as src, closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
        for event in (103, 104):
            decision = {"action": "decline", "reason": "old_still_useful"}
            self.assertEqual(self.review(event, "This repeats an observation already in the slots.", decision), "decline")
        self.assertEqual([row["id"] for row in self.slots.status()["slots"]][0], first)
        proposal = "Tomorrow I will open B before A because I finally know what to check."
        decision = {"action": "replace", "retire_id": first, "reason": "changes_next_action"}
        self.assertEqual(self.review(105, proposal, decision), "replace")
        stage, comparison = self.comparisons[-1]
        self.assertEqual(stage, "decide")
        self.assertEqual(comparison["new"], proposal)
        self.assertEqual(comparison["current"][0]["id"], first)
        self.assertIn("UNIQUE OLD BODY", comparison["current"][0]["text"])
        reopened = LifeSlots(self.path, self.vault, self.model)
        self.assertEqual(len(reopened.context()), 2)
        self.assertIn(proposal, [row["text"] for row in reopened.context()])
        self.assertEqual(reopened.status()["recent"][0]["retirement"], "complete")
        self.assertEqual([row["action"] for row in reopened.status()["recent"][:3]],
                         ["replace", "decline", "decline"])
        new_slot = reopened.status()["recent"][0]["kept_id"]
        self.decisions.append({"book_id": "B", "used_slot_id": new_slot})
        self.assertEqual(reopened.choose_book([{"id": "A"}, {"id": "B"}], None)["book_id"], "B")
        used = self.comparisons[-1][1]["life_memories"]
        self.assertEqual([row["id"] for row in used][-1], new_slot)
        self.assertNotIn(first, [row["id"] for row in used])
        reopened.record_regret(105, "old_would_help_now")
        self.assertEqual(reopened.status()["regrets"][0]["event_id"], 105)
        self.assertIsNone(self.vault.get(first))
        restored = LifeSlots(backup, self.vault, self.model)
        self.assertNotIn(first, [row["id"] for row in restored.context()])
        self.assertEqual(len(restored.context()), 1)
        for file in self.path.parent.glob("*.db*"):
            self.assertNotIn(b"UNIQUE OLD BODY", file.read_bytes())

    def test_failed_key_deletion_is_pending_and_recovers_on_start(self):
        first = self.seed()
        self.vault.block_delete = True
        result = self.review(103, "This is a new concrete point that changes the next step.",
                             {"action": "replace", "retire_id": first, "reason": "changes_next_action"})
        self.assertEqual(result, "pending")
        self.assertEqual(self.slots.status()["recent"][0]["retirement"], "pending")
        self.assertIsNotNone(self.vault.get(first))
        self.vault.block_delete = False
        reopened = LifeSlots(self.path, self.vault, self.model)
        self.assertIsNone(self.vault.get(first))
        self.assertEqual(reopened.status()["recent"][0]["retirement"], "complete")

    def test_no_unauthorized_replacement_and_receipt_idempotency(self):
        first = self.seed()
        self.assertEqual(self.review(103, "This candidate is valid but the response is not.",
                                     {"action": "replace", "retire_id": "absent", "reason": "changes_next_action"}),
                         "invalid")
        self.assertIsNotNone(self.vault.get(first))
        self.assertEqual(self.slots.consider(102, {"title": "A", "body": "already processed"}), "duplicate")
        self.assertEqual(len(self.slots.context()), 2)

    def test_failed_insert_does_not_retire_old_memory_or_leak_new_key(self):
        first = self.seed()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""
                CREATE TRIGGER fail_slot BEFORE INSERT ON life_slot_entries WHEN NEW.event_id=103
                BEGIN SELECT RAISE(ABORT, 'test failure'); END
            """)
        self.assertEqual(self.review(103, "This candidate fails in the main database transaction.",
                                     {"action": "replace", "retire_id": first, "reason": "changes_next_action"}),
                         "failed")
        self.assertEqual(len(self.vault.keys), 2)
        self.assertIsNotNone(self.vault.get(first))
        self.assertEqual(len(self.slots.context()), 2)
        self.assertEqual(self.slots.status()["recent"][0]["event_id"], 102)

    def test_local_endpoint_cannot_be_replaced_by_a_remote_service(self):
        LocalSlotModel("http://127.0.0.1:8080/v1/chat/completions", "local")
        for endpoint in ("https://example.com/v1/chat/completions",
                         "http://127.0.0.1:8080@invalid.example/v1/chat/completions",
                         "http://127.0.0.1:8080/v1/chat/completions?session=save"):
            with self.assertRaises(ValueError):
                LocalSlotModel(endpoint, "local")

    def test_real_life_completion_uses_slots_without_saving_a_second_note(self):
        root = self.path.parent
        (root / "A.txt").write_text("a" * 6000)
        cfg = root / "life.json"
        cfg.write_text(json.dumps({"books_dir": str(root), "interval_min": 1}))
        proposal = "A specific personal reason to change how I read next time."
        self.drafts.append(proposal)
        self.decisions.append({"action": "keep", "reason": "specific_value"})
        normal_writer = Mock()
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            memory = SimpleNamespace(store=SimpleNamespace(conn=db))
            service = life.LifeService(memory, str(cfg), note_writer=normal_writer,
                                       manual_control=True, memory_slots=self.slots)
            start = datetime(2026, 11, 10, 11, tzinfo=life.LIFE_TIMEZONE).timestamp()
            with (patch.object(life.time, "time", return_value=start) as clock,
                  patch.object(life.threading, "Thread") as thread):
                for i in range(3):
                    clock.return_value = start + i * 61
                    out = service.step("book")
                job = thread.call_args.kwargs
                job["target"](*job["args"])
            self.assertEqual(self.slots.context()[0]["event_id"], out["event_id"])
            self.assertEqual(self.slots.context()[0]["text"], proposal)
            self.assertEqual(service.bookshelf()["books"][0]["read_notes"], [])
            self.assertNotIn(proposal, json.dumps(service.state(), ensure_ascii=False))
            normal_writer.assert_not_called()
