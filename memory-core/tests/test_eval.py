# -*- coding: utf-8 -*-
import sys
import tempfile
import os
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.eval import EvalService
from memory_core.server import MemoryService


# 公开版不带本地数据（表情/亲和度资源、压缩夹具、真库样本、eval 数据目录）。
# 这几条在完整部署上是真的能跑的，所以在公开仓用开关显式跳过，而不是删掉或造假数据。
LOCAL_DATA = os.environ.get("MEMORY_AGENT_LOCAL_TESTS") == "1"
skip_local = unittest.skipUnless(
    LOCAL_DATA, "需要本地数据/运行期配置；在完整部署上设 MEMORY_AGENT_LOCAL_TESTS=1 再跑")

class TestEval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryService(str(Path(self.tmp.name) / "brain.db"), embed=False)
        self.eval = EvalService(self.mem, str(Path(self.tmp.name) / "q.jsonl"))

    def tearDown(self):
        self.mem.store.close()
        self.tmp.cleanup()

    @skip_local
    def test_hit(self):
        self.mem.remember({"text": "用户喜欢蓝色的毛绒鲸鱼", "importance": 0.8})
        self.eval.add_query({"query": "用户喜欢什么玩具", "expect_any": ["鲸鱼"]})
        out = self.eval.run()
        self.assertEqual(out["metrics"]["recall@1"], 1.0)
        self.assertEqual(out["results"][0]["rank"], 1)

    @skip_local
    def test_negative(self):
        self.eval.add_query({"query": "完全没提过的话题", "expect_any": []})
        out = self.eval.run()
        self.assertEqual(out["metrics"]["negatives"], 1)


if __name__ == "__main__":
    unittest.main()
