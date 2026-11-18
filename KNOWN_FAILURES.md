# 已知失败用例（公开版 0.1.0，2026-11-18）

我在干净环境下跑 `python -m unittest discover -s tests -p "test_*.py"`：
**142 个用例，10 failures + 7 errors = 17 个不绿。**

原因分两类，我按类写清，方便你判断要不要重跑：

## A. 缺少本地运行环境（error，7 个）

| 用例 | 缺什么 |
| --- | --- |
| `test_agent.TestAgent.test_emotion_and_affinity` | 需要表情/亲和度资源路径，公开版未带 |
| `test_agent.TestAgent.test_style_limit` | 同上 |
| `test_compress.TestCompress.test_rollup` | 需要已压缩的会话夹具 |
| `test_core.TestCore.test_remember_recall` | 需要完整引擎初始化（依赖运行期配置）|
| `test_core.TestCore.test_visibility` | 同上 |
| `test_eval.TestEval.test_hit` | 需要本地评测数据目录 `eval/` 和真库样本 |
| `test_eval.TestEval.test_negative` | 同上 |

这些不是逻辑错，是**公开副本里没有那几份数据和配置**。

## B. 断言与实现不同步（failure，10 个）

集中在三块：

1. `test_memory_pipeline`（4 条）——连线门槛从「同一本书」改成「同一条原话事件」那次改动，测试还按旧口径断言。
2. `test_turn_recall`（4 条）——召回预算（最多 3 条）与日期路径「不扫未归属原文」这两条规则改过，测试没跟上。
3. `test_lexical_recall` / `test_pipeline_http`（2 条）——分词回退与「无相关证据就返回空」的行为调整。

**性质**：这是我在生产版上边改边跑留下的债，公开版把债一起打包了。

## 我打算怎么处理（按你点头再动）

1. 先修 B 类：把 10 条断言对齐现有实现——这是纯测试工作，不动生产代码。
2. A 类不修：在测试文件里加 `unittest.skipUnless`，并在 README 写明「这 7 条需要本地数据，公开版默认跳过」。
3. 修完在 README 里贴一行可复制的命令和真实输出，不放「全绿」这种没有出处的说法。

## 现在你可以怎么验

```bash
cd memory-core
python -m unittest discover -s tests -p "test_*.py"
```

预期：`Ran 142 tests ... FAILED (failures=10, errors=7)`。这个数字我写在这里，改完会更新。
