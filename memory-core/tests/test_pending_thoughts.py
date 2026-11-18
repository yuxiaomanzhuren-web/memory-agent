"""Scratch tests: python -m unittest discover -s .pending-activity-work -p tests_pending.py -v"""
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memory_core import pending_thoughts as module


SCOPE = dict(bot_id="10001", persona="role-a", conv_key="private:20002")


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.now = datetime(2026, 11, 11, 10, tzinfo=timezone(timedelta(hours=8))).timestamp()
        clock = patch.object(module.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.q = module.PendingThoughts(SimpleNamespace(store=SimpleNamespace(conn=self.db)))

    def call(self, method, path="/pending", **data):
        values = {**SCOPE, **data}
        return self.q.handle(method, path, values if method == "GET" else {}, values if method != "GET" else {})

    def create(self, **data):
        code, out = self.call("POST", content="hello", topic="topic", motive="share", **data)
        self.assertEqual(code, 201, out)
        return out["id"]

    def claim(self, item, request="request-1", **data):
        return self.call("POST", f"/pending/{item}/claim", request_id=request, **data)

    def sent(self, item, attempt, message="-998", **data):
        return self.call("POST", f"/pending/{item}/sent", attempt_id=attempt,
                         message_id=message, at=self.now, **data)

    def test_create_scope_ttl_and_restart(self):
        item = self.create()
        out = self.call("GET")[1]["items"]
        self.assertEqual(out[0]["expires_at"] - out[0]["created_at"], 48 * 3600)
        self.assertEqual(self.call("GET", persona="other")[1]["items"], [])
        self.q = module.PendingThoughts(SimpleNamespace(store=SimpleNamespace(conn=self.db)))
        self.assertEqual(self.call("GET")[1]["items"][0]["id"], item)
        self.now += 48 * 3600
        self.assertEqual(self.claim(item)[0], 409)

    def test_required_scope_and_input(self):
        for field in SCOPE:
            code, out = self.q.handle("POST", "/pending", {}, {**SCOPE, field: "", "content": "hi"})
            self.assertEqual(code, 400, out)
        self.assertEqual(self.call("POST", content="hi", ttl_hours=float("nan"))[0], 400)
        self.assertEqual(self.call("POST", content="hi", conv_key="fake")[0], 400)

    def test_claim_idempotency_scope_and_request_binding(self):
        one, two = self.create(), self.create()
        code, out = self.claim(one)
        self.assertEqual(code, 200, out)
        self.assertEqual(out["content"], "hello")
        self.assertTrue(out["send_allowed"])
        repeated = self.claim(one)[1]
        self.assertEqual(repeated["attempt_id"], out["attempt_id"])
        self.assertFalse(repeated["send_allowed"])
        self.assertEqual(self.claim(two)[0], 409)
        self.assertEqual(self.claim(one, persona="other")[0], 404)

    def test_sent_binding_idempotency_and_conflict(self):
        item = self.create()
        attempt = self.claim(item)[1]["attempt_id"]
        self.assertEqual(self.sent(item, "fake")[0], 409)
        self.assertEqual(self.sent(item, attempt, message="0")[0], 400)
        self.assertEqual(self.sent(item, attempt, persona="other")[0], 404)
        self.assertEqual(self.sent(item, attempt)[0], 200)
        self.assertEqual(self.sent(item, attempt)[0], 200)
        self.assertEqual(self.sent(item, attempt, message="999")[0], 409)
        self.assertEqual(self.claim(item)[1]["message_id"], "-998")

    def test_cooldowns_and_daily_quota_across_conversations(self):
        item = self.create()
        attempt = self.claim(item)[1]["attempt_id"]
        self.sent(item, attempt)
        second = self.create(conv_key="group:30003")
        self.assertEqual(self.claim(second, "r2", conv_key="group:30003")[0], 429)
        self.now += 2 * 3600
        self.assertEqual(self.claim(second, "r2", conv_key="group:30003")[0], 429)
        self.now += 4 * 3600
        out = self.claim(second, "r2", conv_key="group:30003")[1]
        self.assertIn("attempt_id", out)
        self.assertEqual(self.sent(second, out["attempt_id"], message="999", conv_key="group:30003")[0], 200)
        self.now += 6 * 3600
        third = self.create()
        self.assertEqual(self.claim(third, "r3")[1]["error"], "daily_quota")
        self.now += 24 * 3600
        self.assertEqual(self.claim(third, "r3")[0], 200)

    def test_outstanding_unknown_crosses_day_and_expiry(self):
        item = self.create()
        attempt = self.claim(item)[1]["attempt_id"]
        self.call("POST", f"/pending/{item}/failed", attempt_id=attempt, outcome="unknown")
        self.now += 4 * 86400
        other = self.create(conv_key="group:30003")
        self.assertEqual(self.claim(other, "r2", conv_key="group:30003")[1]["error"], "outstanding_delivery")
        self.assertFalse(self.claim(item)[1]["send_allowed"])
        self.assertEqual(self.sent(item, attempt)[0], 200)
        self.assertEqual(self.claim(other, "r2", conv_key="group:30003")[0], 429)

    def test_explicit_no_send_releases_but_old_request_never_resends(self):
        item = self.create()
        attempt = self.claim(item)[1]["attempt_id"]
        self.assertEqual(self.call("POST", f"/pending/{item}/failed", attempt_id=attempt, outcome="no_send")[0], 200)
        self.assertFalse(self.claim(item)[1]["send_allowed"])
        code, out = self.claim(item, "new-request")
        self.assertEqual(code, 200, out)
        self.assertNotEqual(out["attempt_id"], attempt)
        self.assertEqual(self.sent(item, attempt)[0], 409)

    def test_defer_deliberate_and_system_block(self):
        item = self.create()
        for reason in ["system_busy", "blocked_permission", "cooldown"]:
            out = self.call("POST", f"/pending/{item}/defer", deliberate=True, reason=reason)[1]
            self.assertEqual(out["defer_count"], 0)
        for count in range(1, 4):
            out = self.call("POST", f"/pending/{item}/defer", deliberate=True, reason="not_now")[1]
            self.assertEqual(out["defer_count"], count)
        self.assertEqual(out["state"], "dormant")
        self.assertEqual(self.claim(item)[0], 409)

    def test_two_deferrals_deprioritize(self):
        first, second = self.create(), self.create()
        for _ in range(2):
            out = self.call("POST", f"/pending/{first}/defer", deliberate=True, reason="not_now")[1]
        self.assertEqual(out["priority"], -1)
        self.assertEqual(self.call("GET")[1]["items"][0]["id"], second)

    def test_short_single_line_and_stable_topic(self):
        for data in [{"content": "x" * 201}, {"content": "line one\nline two"},
                     {"topic": ""}, {"topic": "x" * 121}, {"motive": "x" * 51}]:
            values = {"content": "hello", "topic": "stable", **data}
            self.assertEqual(self.call("POST", **values)[0], 400, values)
        self.assertEqual(self.call("POST", content="x" * 200, topic="x" * 120, motive="x" * 50)[0], 201)

    def test_scoped_audit_includes_closed_attempt_receipt(self):
        item = self.create()
        attempt = self.claim(item)[1]["attempt_id"]
        self.sent(item, attempt)
        self.assertEqual(self.call("GET")[1]["items"], [])
        code, audit = self.call("GET", f"/pending/{item}")
        self.assertEqual(code, 200)
        self.assertEqual(audit["attempts"][0]["message_id"], "-998")
        self.assertEqual(self.call("GET", f"/pending/{item}", persona="other")[0], 404)

    def test_store_lock_held_until_transaction_commit(self):
        class Lock:
            held = False
            def __enter__(self): self.held = True
            def __exit__(self, *_): self.held = False
        lock = Lock()
        self.q.store_lock = lock
        original = self.q._handle
        def checked(*args):
            self.assertTrue(lock.held)
            return original(*args)
        self.q._handle = checked
        commits = []
        self.db.set_trace_callback(lambda sql: commits.append(lock.held) if sql == "COMMIT" else None)
        self.create()
        self.db.set_trace_callback(None)
        self.assertEqual(commits, [True])
        self.assertFalse(lock.held)

    def test_asia_shanghai_midnight_rollover(self):
        self.now = datetime(2026, 11, 11, 23, 30, tzinfo=module.SHANGHAI).timestamp()
        item = self.create()
        attempt = self.claim(item)[1]["attempt_id"]
        self.assertEqual(self.sent(item, attempt)[0], 200)
        self.now += 7 * 3600
        next_item = self.create()
        next_attempt = self.claim(next_item, "next")[1]["attempt_id"]
        self.assertEqual(self.sent(next_item, next_attempt, message="123")[0], 200)
        self.assertEqual(self.db.execute("SELECT quota_day FROM pending_attempts WHERE attempt_id=?", (next_attempt,)).fetchone()[0], "2026-11-12")

    def test_parallel_connections_one_owner_winner(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = folder + "/queue.db"
            db = sqlite3.connect(path)
            q = module.PendingThoughts(SimpleNamespace(store=SimpleNamespace(conn=db)))
            items = [q.handle("POST", "/pending", {}, {**SCOPE, "content": "x", "topic": "stable"})[1]["id"] for _ in range(2)]
            db.close()
            barrier = threading.Barrier(2)
            results = []
            def run(i):
                conn = sqlite3.connect(path, timeout=5)
                service = module.PendingThoughts(SimpleNamespace(store=SimpleNamespace(conn=conn)))
                barrier.wait()
                results.append(service.handle("POST", f"/pending/{items[i]}/claim", {}, {**SCOPE, "request_id": str(i)})[0])
                conn.close()
            threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(sorted(results), [200, 409])


if __name__ == "__main__":
    unittest.main()
