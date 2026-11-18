import hashlib
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from memory_core import server


class PipelineHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.memory = server.MemoryService(str(Path(self.tmp.name) / 'brain.db'), embed=False)
        self.addCleanup(self.memory.store.close)
        self.scope = dict(bot_id='111', persona='xiaoman', conv_key='private:222')
        # 三绑定请求现在必须带范围凭据（scope_auth 的硬要求）：
        # 造一个临时凭据文件，把 sha256(token) 与这条 scope 绑上，测试才能走到业务逻辑。
        self.credential = 'test-scope-credential-0123456789abcdef'
        creds = {'version': 1, 'credentials': [
            {'sha256': hashlib.sha256(self.credential.encode()).hexdigest(), 'scopes': [self.scope]}]}
        cred_path = Path(self.tmp.name) / 'recall-scope-server.json'
        cred_path.write_text(json.dumps(creds), encoding='utf-8')
        patcher = patch.dict(os.environ, {'MEMORY_SCOPE_CREDENTIALS_FILE': str(cred_path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, body=None, method='POST'):
        raw = json.dumps(body or {}).encode()
        handler = server.Handler.__new__(server.Handler)
        handler.path, handler.rfile = path, BytesIO(raw)
        handler.headers = {'Authorization': 'Bearer test', 'Content-Length': str(len(raw)),
                           'X-Memory-Scope-Credential': self.credential}
        sent = []
        handler._send = lambda code, data: sent.append((code, data))
        with (patch.object(server, 'PORT', 8781), patch.object(server, 'TOKEN', 'test'),
              patch.object(server, 'PIPELINE', None, create=True),
              patch.object(server, 'service', return_value=self.memory)):
            getattr(handler, 'do_' + method)()
        return sent[0]

    def test_status_endpoint_exists_without_model_call(self):
        code, payload = self.request('/pipeline/status', method='GET')
        self.assertEqual(code, 200)
        self.assertIn('jobs', payload['data'])

    def test_recall_without_relevant_evidence_returns_empty_not_recent(self):
        self.memory.remember({'text': '他最喜欢宇宙飞船。', 'importance': 1})
        code, payload = self.request('/recall_for_turn', {**self.scope, 'text': '红楼梦暂缓的事呢'})
        self.assertEqual(code, 200)
        self.assertEqual(payload['data']['hits'], [])
        self.assertEqual(payload['data']['block'], '')

    def test_invalid_notice_scope_and_ordinary_review_report_are_bad_requests(self):
        code, payload = self.request('/pipeline/notice', {**self.scope, 'raw_ids': ['not-present']})
        self.assertEqual(code, 400)
        code, payload = self.request('/pipeline/reviews', {**self.scope, 'source_role': 'assistant',
                                    'source_kind': 'self_review', 'turn_id': '1', 'text': '收尾：本轮已回复。'})
        self.assertEqual(code, 400)