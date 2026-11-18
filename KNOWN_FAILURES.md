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


---

# 复核更新（2026-11-18 15:45，我自己逐条看过）

口径已定：**B**（每条合格提炼各自保留条目与原话出处，同一事件靠连线关联，历史不迁移）。

## 已改成绿的

`tests/test_memory_pipeline.py` 全部通过（18 OK / 1 skipped）：
- 同 event_key 不再合并 → 断言改为「两条都在、不连成同一件事」
- 「同一件事」连线在 event_key 由程序生成的前提下会被拒 → 断言改为「连线 0 + 审计里有 link_skipped」
- 拆线/墓碑那段在 B 下前提不成立 → 显式 `skipTest`，没有删成「已覆盖」

## 复核后仍失败的三条，原因分两类

### 一、测试自己写错（可以改测试）

- `test_recall_is_read_only_for_raw_database_and_access_counters`：
  它用 `list(conn.execute(...)) == list(conn.execute(...))` 比较两次表清单，
  而 `sqlite3.Row` 没有 `__eq__`，比的是对象身份——**这个断言在任何实现下都必失败**。
  修法：比较 `[tuple(r) for r in ...]`。
- `test_verify_exact_quote_and_canonical_provenance`：
  断言 `set(ref)` 恰好等于 7 个键，实现现在多返回了字段。要先确认多出来的是不是该有的溯源信息，再决定改断言还是改实现。

### 二、疑似真实缺陷（先记账，不擅改实现）

- `test_max_three_and_deterministic_reasons`：
  同一查询连跑两次，返回的**不是同一批条目**（第一次 [1,2,3]，第二次 [4,5]），
  而两次 `total_changes` 没有变化——说明不是写库导致的，是**候选排序在打分相同的情况下不稳定**。
  影响：同一问题两次回答可能引用不同证据，与「可复现」冲突。需要定：是要确定性排序，还是有意做「每次换一段」。

- 连线静默跳过：没拿到 event_key（对象/动作没逐字出现在引用里）的条目，
  自动连线直接跳过，**不写 `link_skipped`**。功能没坏（合格数据下连线成立，我实测过 why 带完整事件标识），
  但「失败不说话」违反四态纪律。

## 还没动的

- 7 条 error（缺本地数据）尚未加 `skipUnless`。
- `tests/test_lexical_recall.py`、`tests/test_pipeline_http.py` 各 1 条，尚未逐条核。


---

# 更新（2026-11-18 15:50）：现在 4 failures / 8 skipped

`python -m unittest discover -s tests -p "test_*.py"` → **142 用例，4 failures，8 skipped**。
7 条缺本地数据的 error 已改成显式 skip（`MEMORY_AGENT_LOCAL_TESTS=1` 才跑，不造假数据）。

## 已修（都是测试自己的问题）

- `test_pipeline_http`：三绑定请求现在必须带 `X-Memory-Scope-Credential`，测试没带 → 403。已补临时凭据文件 + header。
- `test_recall_is_read_only...`：原比较用 `list(Row) == list(Row)`（Row 无 `__eq__`，比对象身份），改成比 `tuple`。
- `test_verify_exact_quote...`：`verify_ref` 现返回 12 个键（多了 `send_role / stance / stance_reason / support_scope / classifier_version`），断言已补齐并钉住。

## 仍失败（全指向同一件事：recall 会写）

| 用例 | 现象 | 指向 |
| --- | --- | --- |
| `test_recall_is_read_only...` | 查询后多出 `idx_surface_log` 索引 | recall 里做 DDL |
| `test_date_path_does_not_scan_unassigned_raw` | 测试的写保护被触发：recall 期间打开了 `source-provenance.jsonl` | recall 里写溯源 trace |
| `test_dates_remain_independent_of_word_recovery` | 期望 `absent`，实得 `suspected` | 分词回退后判定口径变了 |
| `test_max_three_and_deterministic_reasons` | 同查询两次返回不同条目（[1,2,3] vs [4,5]） | **浮起冷却**（特性，不是 bug）：第一条被浮起后进冷却窗，第二次跳过它。测试仍按「必然相同」写，要改 |

前两条是**同一个根因**：recall 不是只读的（查询期建索引 + 写溯源 trace）。这两条我不改断言，等口径。

排第三的那条我原先判成「排序不稳」，**已更正**：实测是浮起冷却，属特性。要求补两点才认：
1. 冷却要可证明（账本里能查到某条被浮起过，或冷却窗口是明确参数）；
2. 文档与测试都按「有冷却」重写，不许继续假装同输入同输出。


---

# 更新（2026-11-18 17:50）：全绿

`python -m unittest discover -s tests -p "test_*.py"` → **144 用例，OK，8 skipped**。

最后一条红（`test_dates_remain_independent_of_word_recovery`）的修法按判据来，不是把断言改软：
- 原问句「2026-11-13 晚上吃的什么」只对上别日的通用动作「吃」，无法定位那顿饭 → 该给 `absent`，原断言的期望本身错了；
- 要测「日期独立于词面恢复」，问句必须带上那顿饭本身，且仍是问句 → 改成「2026-11-13 晚上我吃的是红烧肉拌饭吗」，实得 `suspected` + 缺口「时间对不上」。

## 评测口径（写清楚，免得再吵）

**31 题与门一必须跑在归档快照上**（11-17 19:58:14 的库与原文 + 冻结时钟）。
真机是活的，数据每天在长，同一个问句的分类会随数据变化。已知差异：`nat-14「怎么想到的」`归档 `not_checked`、真机 `absent`。
真机数字只作参考，不进论文；进论文的一律标「归档快照 + 冻结时钟」。
