# Changelog

## 0.4.0 — 2026-11-26

按 MEMO-7 方案 B 决策，将原仓内门三术语与代码预算接口正式拆分为四道门并同步文档：

- **门三与门四职责拆分**：门三统一定义为「引用清单冻结 + 发件前引用核验」（`memory_core.citation.check_reply`、`POST /citation/check`、`adapters/citation.py` `guard`）；主动重提出手预算独立为**门四**（`MemoryGates.four()`、`POST /memory/gates` `gate=4`）。
- **配对切换与旧入口退役**：托管核心 8781 退役旧 `gate=3` 预算入口（授权请求返回 HTTP 400 且零预算记录），自有 QQ 桥的群聊主动开话题与 V2 主动唤醒同步切至 `gate=4`；`memory_gates_smoke.py` 与 `memory_temporal_smoke.py` 输出键统一为 `gate4`，不留旧键或入口别名。
- **门四审计与历史复制**：新增 `memory_gate4_log` 与 `memory_gate4_migration` 表；8781 启动时在绑定端口后、接流就绪前于单事务内全量复制 `memory_gate3_log`（含允许与拒绝记录及去重键）并双向校验，支持幂等重跑，`POST /memory/gates/audit` 统一查询新表。
- **文档对齐**：同步 `README.md` 现行术语、四道门定义、双路径架构图、HTTP 路由接入与审计说明。

## 0.3.1 — 2026-11-22

- 与 0.3.0 代码完全一致，仅把版本号前进一格：0.3.0 的 Release 指向 `49621c1`，落后最新提交一个（缺 `99e6e5b` 的 mcp 主版本守卫）。正常安装 wheel 不受影响，但下载 Release 源码自建环境的人若预装 mcp 2.x，会看到 3 条误报的红。**不改已发布的标签**，用一个新版本把这件事说清楚。

## 0.3.0 — 2026-11-21

- 仓库整理：`dsh/`、`life/`、`console-v2/` 与两个根目录脚本移到 `examples/legacy/`（附说明，说明它们不是库的一部分）；根目录只剩库、文档与示例。

门三从「只在 QQ 桥接里」变成通用能力，同时把多框架接入补齐。

- **引用校验收进核心**：`memory_core.citation.check_reply(reply, snapshot)` 是纯函数，不碰网络、不碰 QQ；桥接里原来那份判定删掉，改成调核心，一份规则。
- **新增 `POST /citation/check`**：托管核心与独立包都有，入参 `{reply, snapshot}`，返回 `allowed` / `violations` / `stripped`。
- **宿主用 `guard`**：LangGraph、函数工具示例都演示「先拦、改写、再核验，通过才发」；Claude Code 原生 hook 只做注入与记录，发送前硬拦截由掌握投递的 SDK 宿主执行。
- **多框架适配**：`adapters/langgraph.py`（`MemoryStore`，`search(...).as_dict()` 拿四态与 `allowed_raw_ids`）、`adapters/tools.py`（OpenAI 风格三件套）、`adapters/claude_code.py`（`memory-hook`）。
- **修掉留出集暴露的两条漏检**：「你记得…」「…记得嘛」这类句式现在会去检索；无可查对象的「你还记得吗」仍是 `not_checked`。
- **修掉 C1 误剥**：`我上一句说错了` 不再被当成引用；越界引用仍剥。
- 口径变更：问「我是不是说过要做某动作」时，开放的疑问对象是待填项，原话能确认「说过这个动作」即 `found`，但必须标 `meta_only`，不替具体对象背书。

## 0.2.0 — 2026-11-20

- **C1 引用硬拦截真正生效**：回复里带引号的片段必须在注入清单里，找不到就剥掉并留 `citation_rejected` 审计。
- **门一旧事组修复**：「清理这个吧」能支撑「说过清理」；介词残片（「为」）与问句外壳不再当成对象缺口；「什么时候」纳入时间检索。
- **评测口径固定**：所有分数只在冻结快照 + 冻结时钟上报告，并附快照哈希；真机连跑会漂，不混报。

## 0.1.0 — 2026-11-18

首个公开版本。范围只到「四态记忆核心 + 接入」：

- 四层结构：原文层 → 条目层 → 连线层 → 判定层。
- 门一四态：`found` / `suspected` / `absent` / `not_checked`，缺口必须写清。
- 门二来源资格：`user` / `assistant` / `other:<id>`，缺 `speaker` 直接 400。
- 门三引用清单：要引的必须在注入清单里。
- 接入：MCP stdio（`remember` / `recall` / `judge`）与本机 HTTP `/ingest`。
- 可审计：判定 / 归档 / 删除 / 提炼四类自动记账；删除无审计不许删。

### 已验证

- 门一题集 30/30（其中 5 条修订过期望值）。
- 固定回归集 10/11（回归集，非盲测）。
- 独立投题 22/31（echo 9/9、当下 10/15、旧事 3/7）。
- 真实盲测样本 0，留出集 0。

### 已知问题

见 `KNOWN_FAILURES.md`：公开环境下 142 个用例中 10 failures + 7 errors，原因逐条写明。
