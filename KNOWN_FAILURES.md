# 已知失败用例（公开版 0.3.0，2026-11-21）

在干净环境下跑 `cd memory-core && python -m unittest discover -s tests -p "test_*.py"`：
**161 个用例通过，8 个按平台跳过。**

> 想看当前口径（门一 30/30、独立投题 23/31、留出集 5 条首次 1/5），直接读 README 顶部的状态块。
> 这个文件只回答一件事：**还有哪些用例在什么条件下会红。**

## 依赖相关的跳过（不是失败）

| 情况 | 表现 | 怎么办 |
| --- | --- | --- |
| 没装 `langgraph` | `test_multi_framework_adapters` 整组 skip | `pip install "memory-agent[langgraph]"` |
| 没装 `mcp`，或装成了 2.x | 同上；MCP 适配器 import 时给出明确原因 | `pip install "memory-agent[mcp]"`（锁 1.x） |
| ffprobe 缺失 | 语音时长校验跳过 | 装 ffmpeg 或忽略 |

## 0.1.0 时期的那 17 个红（已不适用）

0.1.0 公开时记录过 10 failures + 7 errors，成因是缺本地夹具与资源路径。
0.2.0 起测试改用临时库与合成夹具，这一批已经清掉。

