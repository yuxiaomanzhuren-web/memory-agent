# -*- coding: utf-8 -*-
import sys
import tempfile
import os
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.agent import AgentService
from memory_core.server import MemoryService


# 公开版不带本地数据（表情/亲和度资源、压缩夹具、真库样本、eval 数据目录）。
# 这几条在完整部署上是真的能跑的，所以在公开仓用开关显式跳过，而不是删掉或造假数据。
LOCAL_DATA = os.environ.get("MEMORY_AGENT_LOCAL_TESTS") == "1"
skip_local = unittest.skipUnless(
    LOCAL_DATA, "需要本地数据/运行期配置；在完整部署上设 MEMORY_AGENT_LOCAL_TESTS=1 再跑")

class TestAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryService(str(Path(self.tmp.name) / "brain.db"), embed=False)
        self.agent = AgentService(self.mem)

    def tearDown(self):
        self.mem.store.close()
        self.tmp.cleanup()

    @skip_local
    def test_emotion_and_affinity(self):
        old = self.mem.chat
        seen = {}

        def fake(data):
            seen["system"] = data["messages"][0]["content"]
            return {"choices": [{"message": {"content": "好呀"}}]}

        self.mem.chat = fake
        try:
            out = self.agent.chat({"message": "今天好开心", "user_id": "u1"})
        finally:
            self.mem.chat = old
        self.assertIn("开心", out["state"]["label"])
        self.assertGreater(out["state"]["affinity"], 50)
        self.assertIn("主要情绪：开心", seen["system"])

    @skip_local
    def test_style_limit(self):
        old = self.mem.chat
        self.mem.chat = lambda data: {"choices": [{"message": {"content": "第一行\n第二行\n第三行\n第四行"}}]}
        try:
            out = self.agent.chat({"message": "在吗", "user_id": "u1"})
        finally:
            self.mem.chat = old
        self.assertLessEqual(len(out["reply"].split("\n")), 3)


if __name__ == "__main__":
    unittest.main()
