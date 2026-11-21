# Memory Agent

给对话 agent 用的长期记忆层。核心不是「记得多」，而是**知道自己在哪件事上没有根据**。

一条原话进来，系统把它拆成条目、连上关系；一次提问出去，系统先判断「该不该查」，再给出四种状态之一，并把缺口写清楚。

> **当前状态（2026-11-21，一句话版）**
> 冻结回放：门一 30/30、独立投题 23/31。真机连跑会漂，评测一律报冻结口径并附快照哈希。
> 留出集已建 5 条，首次 1/5（3 条是标注错、2 条暴露真漏检）；**不做回填、不改标**。
> 门三（引用硬拦截）已收进核心，QQ 桥接与独立语音走同一份 `check_reply`；MCP / HTTP / LangGraph 只给 `allowed_raw_ids`，投递由调用方负责。

## 四态

| 状态 | 含义 |
| --- | --- |
| `found` | 范围内查到可作依据的原话，能逐字引用 |
| `suspected` | 查到沾边材料，但缺东西（必须列出缺什么） |
| `absent` | **查过**，范围内没有 |
| `not_checked` | **没查**（不需要查，或不是回忆类问题） |

`absent` 和 `not_checked` 混用是最常见也最伤的错：一个说「没有」，一个说「我没找」。本系统把这两件事分开，并把「没查到什么」写进回答里。

## 三道门

1. **门一 判定**：该不该查 → 查到什么 → 四态之一。
2. **门二 来源资格**：同一条原话，本人说的、我转述的、别人说的，能说的话完全不同。
3. **门三 引用清单**逐条核对引文。许可来自本轮实际注入的原话与同一会话近期符合资格的原话。

门三由 core 统一核验。QQ 的对话文字和语音工具在发送或合成前调用它。LangGraph 和函数工具的宿主在发件前调用 `guard`。Claude Code 原生 Hook 负责注入与记录，Claude Agent SDK 由宿主在校验后投递。 [Claude Code Hook 参考](https://code.claude.com/docs/en/hooks#stop)

铁律：上层不许越级编造。门不能编条目，条目不能编原文。

## 架构

```
① 原文层   每条消息原样存（带时间 / 说话人 / 来源标签）
     ↓ 提炼
② 条目层   一句话一条（谁说的、何时、哪个范围、什么资格）
     ↓ 连线
③ 连线层   同话题 / 改口 / 同源
     ↓ 被查询
④ 判定层   四态与来源资格决定「能不能说、能怎么说」
     ↓ QQ 桥接发出回复前
   引用清单门核对「能引哪条」
```

## 接入

各入口共用记忆核。独立接入包随附 LangGraph、Claude Code 和通用工具的可运行样例。

### MCP（stdio）

```bash
pip install memory_agent-0.3.0-py3-none-any.whl
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

- LangGraph 从 `memory_agent.adapters.langgraph` 导入 `MemoryStore`，调用 `graph.compile(store=MemoryStore())`。用 `search(...).as_dict()` 读取四态、证据和 `allowed_raw_ids`。
- Claude Code 用 `memory-hook` 接入 `SessionStart`、`UserPromptSubmit`、`Stop`，用于注入与记录。HTTP 后端按 Claude 会话 UUID 绑定。
- OpenAI 风格工具使用 `memory_agent.adapters.tools.openai_tools()`，工具执行通过 `call_tool` 调用核心。

查询失败显式返回 `memory_unavailable`。派生条目沿用核心提炼与审计。

### 接入时最容易卡住的三处（所有 HTTP 接入都适用，不只 LangGraph）

1. **口令短了不行**：`MEMORY_AGENT_HTTP_TOKEN` 必须 32–256 个可打印 ASCII 字符，短于 32 会直接报 `http_token_required`。
2. **会话 ID 不能带冒号**：`MEMORY_AGENT_CONVERSATION_ID` 只接受字母数字一类标识（例如 `3431816247`），写 `private:3431816247` 会报 `invalid_conversation`。
3. **两端 scope 必须一致**：客户端与服务端的 `MEMORY_AGENT_SOURCE_ID` / `OWNER_ID` / `CONVERSATION_ID` 三个变量必须完全相同，不一致会报 `scope_forbidden`——这是范围隔离在起作用，不是 bug。

另：`/ingest` 属于独立接入包自带的 HTTP 入口（`memory-agent-ingest`），不是主服务 8781 的路由；直接打 8781 的 `/ingest` 会得到 `not found`。

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
- 浮起表与 `idx_surface_log` 在可写存储初始化时创建。浮起记录保存范围、条目 ID、时间和选中理由。MCP 的 `recall` 还会执行门一，并按约定写判定账本。离线只读评测使用核心查证路径。

## 可审计

四类自动记账：判定 / 归档 / 删除 / 提炼（放弃、降级）。两条硬规定：

1. 写账本失败**不能**让查询失败。
2. 删除没留审计就**不许**删。

## 现状（诚实版）

（2026-11-21 核过一遍代码与实跑，数字按当天为准）

- 门一题集：**冻结回放 30/30**；真机连跑会漂——同日实测 **29/30**（`我是不是说过不用管时间` 因数据增长从 `absent` 变 `suspected`）。**评测一律报冻结回放口径，并标快照哈希**。
- 固定回归集 10/11 —— 是回归集，不是盲测。
- 独立投题 **23/31**（echo 9/9、当下 8/15、旧事 6/7）。历史最好 24/31 出现在另一版标签下，不可与当前混报。
- 冷启动基线：全 `not_checked` 8 / 全 `absent` 11 / 全 `found` 7。
- **留出集已建 5 条，首次跑 1/5**：其中 3 条是我的标注错（助手原话只能算线索），2 条暴露真漏检（「你记得…」句式没触发检索）。留出集不做回填、不改标。
- 旧事召回仍是短板；改口样本 n=1，不足以报比率。
- 门三的硬拦截在掌握投递的宿主接入后生效。MCP 和 HTTP 的记忆响应返回 `allowed_raw_ids` 与 `citation_policy`。宿主用本轮实际注入的出处调用 `/citation/check`，端点故障时停发草稿。LangGraph 与函数工具样例包含重新校验后的改写。

## 许可

见 [LICENSE](LICENSE)。
