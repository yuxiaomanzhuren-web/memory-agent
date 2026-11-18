# -*- coding: utf-8 -*-
import sys
import tempfile
import os
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.compress import ContextCompressor
from memory_core.server import MemoryService


# 公开版不带本地数据（表情/亲和度资源、压缩夹具、真库样本、eval 数据目录）。
# 这几条在完整部署上是真的能跑的，所以在公开仓用开关显式跳过，而不是删掉或造假数据。
LOCAL_DATA = os.environ.get("MEMORY_AGENT_LOCAL_TESTS") == "1"
skip_local = unittest.skipUnless(
    LOCAL_DATA, "需要本地数据/运行期配置；在完整部署上设 MEMORY_AGENT_LOCAL_TESTS=1 再跑")

class TestCompress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryService(str(Path(self.tmp.name) / "brain.db"), embed=False)
        self.mem.summarize = lambda text: "之前聊过工作和晚饭。"
        self.c = ContextCompressor(self.mem, budget_chars=2000, keep_recent=4)

    def tearDown(self):
        self.mem.store.close()
        self.tmp.cleanup()

    @skip_local
    def test_rollup(self):
        msgs = [{"role": "system", "content": "你是助手"}]
        for i in range(80):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}条很长的对话内容" * 4})
        out = self.c.compress(msgs, "u1:x")
        self.assertLess(len(out), len(msgs))
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("之前对话摘要", out[1]["content"])
        self.assertEqual(out[-1]["content"], msgs[-1]["content"])
        hits = self.mem.recall({"query": "之前聊过", "k": 3})["hits"]
        self.assertTrue(any("对话摘要" in h["text"] or "工作" in h["text"] for h in hits))

    def test_short_passthrough(self):
        msgs = [{"role": "user", "content": "短"}]
        self.assertEqual(self.c.compress(msgs, "u1"), msgs)


if __name__ == "__main__":
    unittest.main()
