# Memory Agent

给对话 agent 用的长期记忆层。核心不是「记得多」，而是**知道自己在哪件事上没有根据**。

一条原话进来，系统把它拆成条目、连上关系；一次提问出去，系统先判断「该不该查」，再给出四种状态之一，并把缺口写清楚。

> **当前状态（2026-11-26，四道门口径对齐）**
> 冻结回放：门一 30/30、独立投题 23/31（沿用 2026-11-21 冻结实跑基线）。真机连跑会漂，评测一律报冻结口径并附快照哈希。
> 留出集已建 5 条，首次 1/5（3 条是标注错、2 条暴露真漏检）；**不做回填、不改标**。
> 门三（引用清单冻结与发件前引用核验）已收进核心，QQ 桥接与独立语音走同一份 `check_reply`；MCP / HTTP / LangGraph 返回 `allowed_raw_ids`，投递由调用方负责。
> 门四（主动重提出手预算）已从门三拆出并切至 `POST /memory/gates`（`gate=4`）与 `memory_gate4_log`，旧 `gate=3` 预算入口退役。

## 四态

| 状态 | 含义 |
| --- | --- |
| `found` | 范围内查到可作依据的原话，能逐字引用 |
| `suspected` | 查到沾边材料，但缺东西（必须列出缺什么） |
| `absent` | **查过**，范围内没有 |
| `not_checked` | **没查**（不需要查，或不是回忆类问题） |

`absent` 和 `not_checked` 混用是最常见也最伤的错：一个说「没有」，一个说「我没找」。本系统把这两件事分开，并把「没查到什么」写进回答里。

## 四道门

1. **门一 判定**：该不该查 → 查到什么 → 四态之一。
2. **门二 来源资格**：同一条原话，本人说的、我转述的、别人说的，能说的话完全不同（来源分类 + 逐字重合核对）。
3. **门三 引用清单**：覆盖**本轮清单冻结**与**发件前引用核验**。回复中的引文必须属于许可清单；许可来自本轮实际注入的原话与同一会话近期符合资格的原话。
4. **门四 主动重提出手预算**：专管主动重提旧事的**生成前准入**。要求同作用域内存在与当前文本对象和动作逐字匹配的未消解成本记录（`错过` / `被拒` / `改口` / `认错`）及对应用户原话、新上下文晚于原话且不超前当前时间 5 秒、附非空且至多 300 字符的 `why_now`；按 `(scope, request_id)` 去重，按 `(bot_id, persona)` 在上海自然周（周一 00:00 起）内跨作用域累计放行上限 3 次，放行写日志（`memory_gate4_log`）即入账。

门三由 core 统一核验（`check_reply` / `POST /citation/check`）。QQ 的对话文字和独立语音工具在发送或合成前调用它，端点不可用时停发草稿。LangGraph 和函数工具的宿主在发件前调用 `guard`。Claude Code 原生 Hook 负责注入与记录，Claude Agent SDK 由宿主在校验后投递。[Claude Code Hook 参考](https://code.claude.com/docs/en/hooks#stop)

门四由托管核心 8781 的 `POST /memory/gates`（`gate=4`）提供，旧 `gate=3` 预算入口已退役（授权后返回 HTTP 400 且不写预算记录）。自有 QQ 桥的群聊主动开话题与 V2 主动唤醒在生成前调用门四，放行后仍经门三做发件前引用核验；被动回复路径不经过门四。

铁律：上层不许越级编造。门不能编条目，条目不能编原文。

## 架构

```
① 原文层   每条消息原样存（带时间 / 说话人 / 来源标签）
     ↓ 提炼
② 条目层   一句话一条（谁说的、何时、哪个范围、什么资格）
     ↓ 连线
③ 连线层   同话题 / 改口 / 同源
     ↓ 被查询 / 主动触发
④ 判定层   门一（四态）与门二（来源资格）决定「能不能说、能怎么说」
           主动重提路径在生成前经门四（出手预算，gate=4）准入并记账
     ↓ 回复发出前（QQ 桥接 / 独立语音 / 宿主 guard）
   门三（引用清单）按本轮冻结清单核对「能引哪条」，越界拦截或停发
```

## 接入

各入口共用记忆核。独立接入包随附 LangGraph、Claude Code 和通用工具的可运行样例。

### MCP（stdio）

```bash
pip install memory_agent-0.4.0-py3-none-any.whl   # 装完包名是 memory_core
```

启动前给四样环境变量：

```
MEMORY_AGENT_SOURCE_ID=my-agent
MEMORY_AGENT_OWNER_ID=user-1
MEMORY_AGENT_CONVERSATION_ID=conv-1
MEMORY_AGENT_DATA_DIR=/path/to/data
```

工具：`remember` / `recall` / `judge`。

### 本机 HTTP

设置 `MEMORY_AGENT_HTTP_PORT`、`MEMORY_AGENT_HTTP_TOKEN` 后运行 `memory-agent-ingest`，请求头用 `Authorization: Bearer <你的口令>`。写入原话用 `POST /ingest`，通用后端用 `POST /add`、`/search`、`/get`、`/delete`。MCP 如需连接同一档案，设置 `MEMORY_AGENT_MCP_USE_HTTP=1`。

### LangGraph、Claude Code 与函数工具

- LangGraph 从 `memory_core.adapters.langgraph` 导入 `MemoryStore`，调用 `graph.compile(store=MemoryStore())`。用 `search(...).as_dict()` 读取四态、证据和 `allowed_raw_ids`。
- Claude Code 用 `memory-hook` 接入 `SessionStart`、`UserPromptSubmit`、`Stop`，用于注入与记录。HTTP 后端按 Claude 会话 UUID 绑定。
- OpenAI 风格工具使用 `memory_core.adapters.tools.openai_tools()`，工具执行通过 `call_tool` 调用核心。

查询失败显式返回 `memory_unavailable`。派生条目沿用核心提炼与审计。

### 接入时最容易卡住的三处（所有 HTTP 接入都适用，不只 LangGraph）

1. **口令短了不行**：`MEMORY_AGENT_HTTP_TOKEN` 必须 32–256 个可打印 ASCII 字符，短于 32 会直接报 `http_token_required`。
2. **会话 ID 不能带冒号**：`MEMORY_AGENT_CONVERSATION_ID` 只接受字母数字一类标识（例如 `100012345`），写 `private:100012345` 会报 `invalid_conversation`。
3. **两端 scope 必须一致**：客户端与服务端的 `MEMORY_AGENT_SOURCE_ID` / `OWNER_ID` / `CONVERSATION_ID` 三个变量必须完全相同，不一致会报 `scope_forbidden`——这是范围隔离在起作用，不是 bug。

另：`/ingest` 属于独立接入包自带的 HTTP 入口（`memory-agent-ingest`），不是主服务 8781 的路由；直接打 8781 的 `/ingest` 会得到 `not found`。主服务 8781 的门控与引用路由包括：`POST /citation/check`（门三发件前核验，Bearer 鉴权）、`POST /memory/gates`（`gate=1` 门一、`gate=2` 门二、`gate=4` 门四主动预算、`inspect` 组合查证；均需 Bearer + 范围凭据，旧 `gate=3` 返回 HTTP 400）、`POST /memory/gates/cost`（登记成本记录）与 `POST /memory/gates/audit`（查询同作用域最近 20 条门四预算审计）。

## 写入必须带什么

一条消息喂进来，**四样必填，缺一样就拒收**：

| 字段 | 说明 |
| --- | --- |
| `speaker` | `user` / `assistant` / `other:<id>` |
| `at` | 带时区的时间戳 |
| `conversation` | 会话标识（source / owner / conversation 三项）|
| `source_kind` | `direct` / `quote` / `forward` / `unknown` |

缺 `speaker` 返回 `400 missing_speaker`。**不会**替你默认成 `user`——默认成 user 等于替用户认领一句话，这正是本系统要消掉的那个错。

## 只读查证与唤醒浮起

- `TurnRecall.recall(data)` 默认只读。数据快照、范围、查询与时钟相同时，按匹配分、重要度、原话时间、ID 排序，最多返回三条。该路径支持已用 SQLite `mode=ro` 打开的数据库，来源诊断保留在结果中，来源 trace 文件不写入。
- 托管的 `/recall_for_turn` 显式启用唤醒浮起。普通话题命中会记录到 `memory_surface_log`，同一会话内两小时内浮起过的条目会跳过。结果会随浮起记录与时间变化。日期、逐字原话和明确回忆查询沿用查证路径。
- 按预批原文 ID 导出本机隔离副本的登记、鉴权和复跑方法见 [受管只读快照导出](docs/managed-snapshot.md)。
- 浮起表与 `idx_surface_log` 在可写存储初始化时创建。浮起记录保存范围、条目 ID、时间和选中理由。MCP 的 `recall` 还会执行门一，并按约定写判定账本。离线只读评测使用核心查证路径。

## 可审计

四类自动记账：判定 / 归档 / 删除 / 提炼（放弃、降级）。两条硬规定：

1. 写账本失败**不能**让查询失败。
2. 删除没留审计就**不许**删。

门四主动重提预算决策单独写入 SQLite 表 `memory_gate4_log`（按 `(scope, request_id)` 去重，放行即入账；`POST /memory/gates/audit` 返回同作用域最近 20 条记录）。从旧版升级时，核心在绑定端口后、接流就绪前于单个事务内将 `memory_gate3_log` 全量历史（含允许与拒绝记录）复制到 `memory_gate4_log` 并双向校验，写入 `memory_gate4_migration(version=1)` 标记；迁移可幂等重跑，旧表保留为历史来源。

## 怎么跑测试

所有测试都在 `memory-core/tests/`：

```bash
cd memory-core && python -m unittest discover -s tests -p "test_*.py"
```

161 个用例通过，8 个按平台跳过。`examples/legacy/` 里那份 `tests-astrbot/` 属于本机应用，不在此列。

## 现状（诚实版）

（2026-11-21 核过一遍代码与实跑，数字按当天为准）

- 门一题集：**冻结回放 30/30**；真机连跑会漂——同日实测 **29/30**（`我是不是说过不用管时间` 因数据增长从 `absent` 变 `suspected`）。**评测一律报冻结回放口径，并标快照哈希**。
- 固定回归集 10/11 —— 是回归集，不是盲测。
- 独立投题 **23/31**（echo 9/9、当下 8/15、旧事 6/7）。历史最好 24/31 出现在另一版标签下，不可与当前混报。
- 冷启动基线：全 `not_checked` 8 / 全 `absent` 11 / 全 `found` 7。
- **留出集已建 5 条，首次跑 1/5**：其中 3 条是我的标注错（助手原话只能算线索），2 条暴露真漏检（「你记得…」句式没触发检索）。留出集不做回填、不改标。
- 旧事召回仍是短板；改口样本 n=1，不足以报比率。
- 门三（引用清单冻结与发件前引用核验）的硬拦截在掌握投递的宿主接入后生效。MCP 和 HTTP 的记忆响应返回 `allowed_raw_ids` 与 `citation_policy`。宿主用本轮实际注入的出处调用 `/citation/check`，端点故障时停发草稿。LangGraph 与函数工具样例包含重新校验后的改写。
- 门四（主动重提出手预算）通过 `POST /memory/gates`（`gate=4`）与自有 QQ 桥配对切换，旧 `gate=3` 已退役返回 HTTP 400；两份冒烟（`memory_gates_smoke.py`、`memory_temporal_smoke.py`）输出键统一为 `gate4`。

## 许可

见 [LICENSE](LICENSE)。
