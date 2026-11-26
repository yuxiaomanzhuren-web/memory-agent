from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from memory_core import server
from memory_core.memory_evidence import scope_key
from memory_core.memory_gates import MemoryGates
from memory_core.memory_gates_smoke import run_smoke as gates_smoke
from memory_core.memory_temporal_smoke import fixture as temporal_fixture, run_smoke as temporal_smoke
from memory_core.turn_recall import SHANGHAI


MONDAY = datetime(2026, 11, 23, tzinfo=SHANGHAI).timestamp()
WEEK = 7 * 86400
SCOPE = dict(bot_id='111', persona='gate-fixture', conv_key='private:222')
OTHER_CONVERSATION = dict(SCOPE, conv_key='group:333')
OTHER_OWNER = dict(OTHER_CONVERSATION, persona='other-fixture')
COLUMNS = ('id, owner, scope, request_id, week_start, at, allowed, reason_code, evidence')
LEGACY_SCHEMA = '''CREATE TABLE memory_gate3_log(
 id INTEGER PRIMARY KEY,owner TEXT NOT NULL,scope TEXT NOT NULL,request_id TEXT NOT NULL,
 week_start REAL NOT NULL,at REAL NOT NULL,allowed INTEGER NOT NULL,
 reason_code TEXT NOT NULL,evidence TEXT NOT NULL,UNIQUE(scope,request_id))'''


def owner(scope):
    return json.dumps([scope['bot_id'], scope['persona']], ensure_ascii=False)


def history_row(id_, scope, request_id, week, allowed):
    return (id_, owner(scope), scope_key(scope), request_id, week, week + 3, allowed,
            'allowed' if allowed else 'weekly_limit', '{ "quote": "旧证据" }')


class Gate4Tests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(temp) / 'isolated.sqlite'
        self.memory = SimpleNamespace(store=SimpleNamespace(path=self.db))
        self.now = MONDAY + 60
        self.gates = MemoryGates(self.memory, clock=lambda: self.now)

    def query(self, sql, args=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute(sql, args).fetchall()

    def seed(self):
        self.query(LEGACY_SCHEMA)
        rows = [history_row(i, SCOPE, f'old-{i}', MONDAY - WEEK, int(i in (1, 2, 3)))
                for i in range(1, 24)]
        rows += [history_row(31, SCOPE, 'allowed-current', MONDAY, 1),
                 history_row(45, OTHER_CONVERSATION, 'other-conversation', MONDAY, 1),
                 history_row(52, SCOPE, 'denied-current', MONDAY, 0),
                 history_row(60, OTHER_OWNER, 'same-owner-name-is-scoped', MONDAY, 1)]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany('INSERT INTO memory_gate3_log VALUES(?,?,?,?,?,?,?,?,?)', rows)
        return rows

    def schema(self):
        return self.query("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")

    def decision(self, request_id, scope=SCOPE, **details):
        return self.gates.four(dict(scope, request_id=request_id, text='手机换电池重新预约',
                                    context_ts=self.now, why_now='当前再次讨论这件事') | details)

    def evidence(self):
        old = MONDAY - WEEK - 1
        quote = '手机换电池的预约错过了'
        ref = dict(raw_id='synthetic', role='user', quote=quote, raw_ts=old)
        self.enterContext(patch.object(self.gates, '_load', return_value=(
            [dict(id=1, meta=json.dumps(dict(object='手机', action='换电池')))], {})))
        self.enterContext(patch.object(self.gates, '_refs', return_value=[ref]))
        scope_keys = [scope_key(scope) for scope in (SCOPE, OTHER_CONVERSATION, OTHER_OWNER)]
        for key in scope_keys:
            self.query('INSERT INTO memory_gate_costs VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, 1, '错过', 'synthetic', quote, 'test', 'operator', None, None))
        return old

    def test_upgrade_preserves_history_audit_dedup_and_weekly_limits(self):
        old_rows = self.seed()
        audit_sql = 'WHERE scope=? ORDER BY id DESC LIMIT 20'
        before_audit = self.query(f'SELECT {COLUMNS} FROM memory_gate3_log {audit_sql}', (scope_key(SCOPE),))
        use_sql = 'WHERE allowed=1 GROUP BY owner,week_start ORDER BY owner,week_start'
        before_use = self.query(f'SELECT owner,week_start,count(*) FROM memory_gate3_log {use_sql}')
        self.gates.migrate_budget()
        self.gates.migrate_budget()
        self.assertEqual(self.query(f'SELECT {COLUMNS} FROM memory_gate4_log ORDER BY id'), old_rows)
        self.assertEqual([tuple(row.values()) for row in self.gates.audit(SCOPE)['decisions']], before_audit)
        self.assertEqual(self.query(f'SELECT owner,week_start,count(*) FROM memory_gate4_log {use_sql}'), before_use)
        old = self.evidence()
        for request_id in ('old-1', 'denied-current', 'old-23', 'allowed-current'):
            self.assertEqual(self.decision(request_id)['reason_code'], 'duplicate_request')
        third = self.decision('third-allow', OTHER_CONVERSATION)
        self.assertEqual((third['allowed'], third['used_this_week']), (True, 3))
        self.assertIn('[门四允许]', third['rendered'])
        self.assertEqual(self.decision('fourth-denied')['reason_code'], 'weekly_limit')
        self.assertEqual(self.decision('separate-owner', OTHER_OWNER)['used_this_week'], 2)
        self.now = MONDAY - 0.001
        self.assertEqual(self.decision('last-moment')['reason_code'], 'weekly_limit')
        self.now = MONDAY + WEEK
        self.assertEqual(self.decision('first-moment')['used_this_week'], 1)
        self.assertEqual(self.decision('old-timestamp', context_ts=old)['reason_code'], 'missing_new_context_or_source')
        self.assertEqual(self.decision('missing-why', why_now='')['reason_code'], 'missing_why_now')
        self.assertEqual(self.decision('long-why', why_now='x' * 301)['reason_code'], 'missing_why_now')
        self.assertEqual(self.decision('wrong-action', text='手机重新预约')['reason_code'], 'no_same_object_action_cost')
        with self.assertRaisesRegex(ValueError, 'invalid current context timestamp'):
            self.decision('future', context_ts=self.now + 5.001)
        final = self.query(f'SELECT {COLUMNS} FROM memory_gate4_log ORDER BY id')
        self.gates.migrate_budget()
        self.assertEqual(self.query(f'SELECT {COLUMNS} FROM memory_gate4_log ORDER BY id'), final)
        self.assertEqual(self.query(f'SELECT {COLUMNS} FROM memory_gate3_log ORDER BY id'), old_rows)

    def test_migration_rolls_back_after_table_creation_and_after_copy(self):
        previous = self.seed()
        for prefix in ('INSERT INTO memory_gate4_log', 'INSERT INTO memory_gate4_migration'):
            with self.subTest(prefix=prefix):
                class InterruptedConnection(sqlite3.Connection):
                    def execute(self, sql, parameters=()):
                        if sql.lstrip().startswith(prefix):
                            raise sqlite3.OperationalError('injected migration interruption')
                        return super().execute(sql, parameters)

                connect = sqlite3.connect
                with patch('sqlite3.connect', side_effect=lambda *args, **kwargs:
                           connect(*args, **kwargs, factory=InterruptedConnection)):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'injected migration interruption'):
                        self.gates.migrate_budget()
                self.assertEqual(self.schema(), [('memory_gate3_log',)])
                self.assertEqual(self.query(f'SELECT {COLUMNS} FROM memory_gate3_log ORDER BY id'), previous)
        self.gates.migrate_budget()
        self.assertEqual(self.query(f'SELECT {COLUMNS} FROM memory_gate4_log ORDER BY id'), previous)

    def test_unmarked_target_must_match_legacy_and_marked_target_must_have_schema(self):
        previous = self.seed()
        self.query(LEGACY_SCHEMA.replace('memory_gate3_log', 'memory_gate4_log'))
        self.query('INSERT INTO memory_gate4_log SELECT * FROM memory_gate3_log')
        self.query('UPDATE memory_gate4_log SET evidence=? WHERE id=1', ('mismatch',))
        with self.assertRaisesRegex(RuntimeError, 'history'):
            self.gates.migrate_budget()
        self.assertNotIn(('memory_gate4_migration',), self.schema())
        self.query('UPDATE memory_gate4_log SET evidence=? WHERE id=1', (previous[0][-1],))
        self.gates.migrate_budget()
        self.query('DROP TABLE memory_gate4_log')
        with self.assertRaisesRegex(RuntimeError, 'schema'):
            self.gates.migrate_budget()
        self.assertNotIn(('memory_gate4_log',), self.schema())

    def test_last_slot_and_same_request_are_serialized(self):
        self.seed()
        self.gates.migrate_budget()
        self.evidence()
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.decision, 'race-a', SCOPE)
            second = pool.submit(self.decision, 'race-b', OTHER_CONVERSATION)
            retry = pool.submit(self.decision, 'race-a', SCOPE)
        self.assertCountEqual([run.result()['reason_code'] for run in (first, second, retry)],
                              ['allowed', 'weekly_limit', 'duplicate_request'])

    def test_startup_migrates_only_after_bind_and_before_readiness(self):
        seeded = self.seed()
        control = self.db.with_suffix('.control')
        ready = control.with_suffix('.ready')
        http = Mock()
        http.__enter__ = Mock(return_value=http)
        http.__exit__ = Mock(return_value=False)

        def bound(*args):
            self.assertEqual(self.schema(), [('memory_gate3_log',)])
            return http

        def serving():
            self.assertTrue(ready.is_file())
            self.assertEqual(self.query(f'SELECT {COLUMNS} FROM memory_gate4_log ORDER BY id'), seeded)

        http.serve_forever.side_effect = serving
        with (patch.dict(os.environ, {'MEMORY_8781_CONTROL': str(control),
                                      'MEMORY_PIPELINE_AUTO': '0', 'LIFE_AUTO': '0'}),
              patch('memory_core.startup_check.check'), patch.object(server, 'PORT', 8781),
              patch.object(server, 'DB', str(self.db)), patch.object(server, '_slot_environment', return_value={}),
              patch.object(server, 'service', return_value=self.memory),
              patch.object(server, 'ThreadingHTTPServer', side_effect=OSError('fixture bind failed')) as listener,
              patch.object(server.threading, 'Thread') as worker):
            with self.assertRaisesRegex(OSError, 'fixture bind failed'):
                server.main()
            self.assertEqual(self.schema(), [('memory_gate3_log',)])
            self.assertFalse(ready.exists())
            listener.side_effect = bound
            with patch.object(MemoryGates, 'migrate_budget', side_effect=RuntimeError('fixture migration failed')):
                with self.assertRaisesRegex(RuntimeError, 'fixture migration failed'):
                    server.main()
            self.assertFalse(ready.exists())
            http.serve_forever.assert_not_called()
            worker.assert_not_called()
            server.main()
            http.serve_forever.assert_called_once()

    def test_transition_still_rejects_an_unqualified_source_ref(self):
        with temporal_fixture() as fixture:
            older, newer = fixture['proposal']['source_refs']
            newer = {key: value for key, value in newer.items() if key not in ('stance', 'support_scope')}
            with self.assertRaisesRegex(ValueError, 'pair does not jointly license'):
                fixture['facts'].supersede(dict(fixture['proposal'], source_refs=[older, newer]))

    def test_http_routing_authentication_and_smoke_output(self):
        root = self.db.parent
        memory = server.MemoryService(str(self.db), embed=False)
        self.addCleanup(memory.store.close)
        credential = 'fixture-credential-0123456789abcdef'
        creds = root / 'scopes.json'
        creds.write_text(json.dumps({'version': 1, 'credentials': [
            {'sha256': hashlib.sha256(credential.encode()).hexdigest(), 'scopes': [SCOPE]}]}), encoding='utf-8')

        def request(gate, scope=SCOPE, bearer=True, scoped=True):
            body = json.dumps(dict(scope, gate=gate, text='这是一条合成输入',
                                   request_id='http-input', context_ts=0)).encode()
            handler = server.Handler.__new__(server.Handler)
            handler.path, handler.rfile = '/memory/gates', BytesIO(body)
            handler.headers = {'Content-Length': str(len(body))}
            if bearer:
                handler.headers['Authorization'] = 'Bearer fixture'
            if scoped:
                handler.headers['X-Memory-Scope-Credential'] = credential
            sent = []
            handler._send = lambda code, data: sent.append((code, data))
            handler.do_POST()
            return sent[0]

        with (patch.dict(os.environ, {'MEMORY_SCOPE_CREDENTIALS_FILE': str(creds)}),
              patch.object(server, 'PORT', 8781), patch.object(server, 'TOKEN', 'fixture'),
              patch.object(server, 'PIPELINE', None), patch.object(server, 'service', return_value=memory)):
            self.assertEqual(request(4, bearer=False)[0], 401)
            self.assertEqual(request(4, scoped=False)[0], 403)
            self.assertEqual(request(4, OTHER_CONVERSATION)[0], 403)
            self.assertEqual(request(3)[0], 400)
            self.assertEqual(MemoryGates(memory).audit(SCOPE)['decisions'], [])
            code, result = request(4)
            self.assertEqual(code, 200)
            self.assertEqual(result['data']['reason_code'], 'no_same_object_action_cost')
            for gate in (1, 2, 'inspect'):
                self.assertEqual(request(gate)[0], 200)
        for smoke in (gates_smoke, temporal_smoke):
            result = smoke()
            self.assertIn('gate4', result)
            self.assertNotIn('gate3', result)


if __name__ == '__main__':
    unittest.main()
