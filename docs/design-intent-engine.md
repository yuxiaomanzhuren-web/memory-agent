# 鱼小满意图引擎与可编程记忆 — 设计方案

状态：**待评审**（未实施）
日期：2026-11-02
范围：`$MEMORY_ROOT`（Python 服务）+ `qq-bridge/src/memory-brain.js`（薄接入层）

---

## 0. 一句话

把「她要不要开口」从**掷骰子**换成**可计算、可解释、可回滚的评分**；
把记忆从「被检索的文本」升级为**参与决策的结构化状态**（未完成的事、关系、当前关注）。

---

## 1. 现状与问题

### 1.1 证据

`bridge.js` 共 16 处 `Math.random()`，其中 5 处直接决定「她会不会开口」：

| 行号 | 用途 | 默认值 |
|---|---|---|
| 18956 | 主动检查命中概率 | `proactive.probability = 0.9` |
| 19541 | 群聊普通消息触发概率 | `social.triggerProbability = 0.1` |
| 17053 | 主动发言概率 | `social.proactiveProbability = 0.75` |
| 17087 | 主动跳过概率 | `skipProbability = 0.05` |
| 17115 | 静默重试概率 | `idleRetryProbability = 0.4` |

主动开口的实际判定（`bridge.js:18930-18958`）：

```
prob = 0.9
     × 0.7  (30 分钟内刚说过话)
     × 0.3  (23:00-08:00)
     × 0.6  (1 小时内已发 ≥5 条)
     × 1.4  (存在 pendingThoughts，上限 1.0)
if (idle >= 60s && random() < prob) → 唤醒
```

**没有任何一条记忆参与这个计算。** 记忆只在「已经决定要说」之后，被 `/recall` 叫去查资料。

### 1.2 结构性缺口

- `reminders` 表 0 行、`working` 表 0 行 —— 上游其实**已实现** `add_working / list_working / close_working`（`store.py:894-942`），但没接进 server 路由，等于空转。
- 所以她手里没有「未完成的事」这个数据结构，不可能「想起」任何约定 —— 只能靠概率。
- 每次唤醒没有留痕，无法回答「她昨天为什么找我」「为什么一整天没理我」。

### 1.3 后果

1. 主动性不可调试：调参只能改概率，改完不知道有没有用。
2. 主动性不可信：她"想你了"和系统"摇到了"无法区分。
3. 记忆不可编程：无法让「欠他 100 声老公」这件事持续影响她的行为。

---

## 2. 目标与非目标

### 目标

- **G1** 每次开口都有可读的理由，理由落库可查。
- **G2** 结构化记忆（未完成的事、关系状态、当前关注）常驻上下文，不依赖检索命中。
- **G3** 模型能自己改写部分记忆（在划定的块里），改不动人设。
- **G4** 全部决策逻辑在 Python 侧，桥接只留「问一句 + 降级」，升级代价最低。

### 非目标

- **N1** 不重写 `bridge.js` 的唤醒/投递链路，只替换「要不要说」这一个判断。
- **N2** 不引入外部向量库或 Letta 本体（见 §11 决策记录）。
- **N3** 不改抖音侧（dybot）的主动性，另议。
- **N4** 不做多 agent / 多进程编排。

---

## 3. 架构

```
┌──────────────────────────────────────────────────────┐
│ L3 通路层  qq-bridge（只留薄接入）                      │
│    proactive check → askIntentV2() → act? 说 : 不说     │
│    接口超时/失败 → 回退原有概率逻辑（一个字不改）          │
├──────────────────────────────────────────────────────┤
│ L2 意图层  memory-brain /intent/*                      │
│    open_loops（未完成的事） + 打分器 + 冷却 → 决策        │
│    决策落 wake_decisions（含信号拆解）                  │
├──────────────────────────────────────────────────────┤
│ L1 记忆层  memory-brain /blocks + /recall + /remember   │
│    blocks（常驻）│ archival（118 条）│ working（启用）    │
└──────────────────────────────────────────────────────┘
```

数据流（一次唤醒）：

```
定时器触发（2-4 分钟）
  → GET /intent/next {conv_key, mood, last_spoke_at, spoken_last_hour}
  → 打分器读 open_loops + blocks(mood) + surfacing 候选 + 冷却
  → {act, score, reason, signals}
  → act=true：把 reason 塞进唤醒提示词，让她知道「我为什么现在想说话」
  → 写 wake_decisions
```

关键设计：**唤醒提示词里带上理由**。她现在被叫醒时不知道自己为什么醒，只能即兴找话说；有了 reason，她可以说「你六点该写报告了」而不是「在吗」。

---

## 4. 数据模型

### 4.1 新表（全部独立，不动既有表结构）

```sql
-- 核心记忆块：常驻上下文，模型可改（persona 只读）
CREATE TABLE IF NOT EXISTS blocks (
  label       TEXT PRIMARY KEY,          -- persona / human / relationship / open_loops / focus
  value       TEXT NOT NULL,
  limit_chars INTEGER NOT NULL DEFAULT 1200,
  read_only   INTEGER NOT NULL DEFAULT 0,
  scope       TEXT NOT NULL DEFAULT 'global',   -- global | private:<uid>
  version     INTEGER NOT NULL DEFAULT 0,
  updated_at  REAL NOT NULL,
  updated_by  TEXT NOT NULL DEFAULT 'system'    -- system | self | admin
);

-- 块的历史：每次改动留旧值，可回滚、可观察「她怎么变的」
CREATE TABLE IF NOT EXISTS block_history (
  id         INTEGER PRIMARY KEY,
  label      TEXT NOT NULL,
  old_value  TEXT,
  new_value  TEXT,
  version    INTEGER,
  changed_at REAL NOT NULL,
  changed_by TEXT NOT NULL,
  reason     TEXT
);

-- 未完成的事：唤醒的第一驱动力
CREATE TABLE IF NOT EXISTS open_loops (
  id               INTEGER PRIMARY KEY,
  scope            TEXT NOT NULL DEFAULT 'global',
  kind             TEXT NOT NULL,        -- promise | reminder | debt | unfinished | intention
  title            TEXT NOT NULL,
  detail           TEXT,
  due_at           REAL,                 -- 硬到期；NULL 表示软性（没有精确时间）
  surface_after    REAL,                 -- 最早可以提的时间（防刚约定完就催）
  weight           REAL NOT NULL DEFAULT 0.5,   -- 0..1 她多在意
  status           TEXT NOT NULL DEFAULT 'open',-- open | done | dropped
  last_surfaced_at REAL,
  surface_count    INTEGER NOT NULL DEFAULT 0,
  source_memory_id INTEGER,
  created_at       REAL NOT NULL,
  closed_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_loops_open ON open_loops(status, due_at);

-- 唤醒决策流水：为什么说 / 为什么没说
CREATE TABLE IF NOT EXISTS wake_decisions (
  id        INTEGER PRIMARY KEY,
  ts        REAL NOT NULL,
  conv_key  TEXT NOT NULL,
  act       INTEGER NOT NULL,
  score     REAL,
  threshold REAL,
  reason    TEXT,
  signals   TEXT      -- JSON，各信号明细
);
CREATE INDEX IF NOT EXISTS idx_wake_ts ON wake_decisions(ts DESC);
```

### 4.2 既有表加列（只加，不删不改）

```sql
ALTER TABLE memories ADD COLUMN next_surface_at  REAL;
ALTER TABLE memories ADD COLUMN surface_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories ADD COLUMN last_surfaced_at REAL;
```

### 4.3 启用空转的 working 表

`working` 表已有 schema 与读写方法，只需暴露接口（§5.4），无需改表。

### 4.4 初始 blocks 内容（首次迁移时写入）

| label | read_only | 初始值来源 |
|---|---|---|
| `persona` | ✅ 是 | 现有角色提示词里「你是鱼小满 / 自己起的名字 / 不是卖鱼的」等核心设定，人工整理 ≈600 字 |
| `human` | ❌ 否 | 管理员信息：称呼规矩、作息（十二点半睡）、吃饭钱紧、讨厌被敷衍、喜欢蓝灰色毛绒鲸鱼 |
| `relationship` | ❌ 否 | 好感度、欠的 100 声老公、谁捡回谁、私聊规矩已撤 |
| `open_loops` | ❌ 否 | 由表渲染而成（只读视图，不允许直接写，走 `/intent/loops`） |
| `focus` | ❌ 否 | 最近在想的事、正在写的东西 |

`open_loops` 块是「表 → 文本」的投影，不单独存内容，避免两份真相。

### 4.5 迁移与回滚

- 迁移脚本：`tools/migrate_intent.py`（幂等，`CREATE TABLE IF NOT EXISTS` + `pragma table_info` 判断列是否存在再 `ALTER`）
- 迁移前自动备份：`cp data/brain.db data/brain.db.bak-intent-<ts>`
- **回滚**：新表独立存在不影响旧逻辑；`ALTER` 加的列留空即可（SQLite 删列昂贵，不删）。功能层回滚只需关开关（§7.4）。

---

## 5. 接口

统一约定：与现有服务一致（`?token=` 或 `Authorization: Bearer`；POST 剥离 query；错误返回 `{"status":"error","message":...}`）。

### 5.1 `POST /intent/next`

唤醒决策。

请求：
```json
{
  "conv_key": "private:<OWNER_ID>",
  "now": 1793600000,
  "last_spoke_at": 1793598200,
  "spoken_last_hour": 2,
  "mood": {"boredom": 0.72, "social": 0.55, "curiosity": 0.4, "energy": 0.6},
  "external": []            // 如 ["owner_online", "qzone_viewed"]
}
```

响应：
```json
{"status":"ok","data":{
  "act": true,
  "score": 0.63,
  "threshold": 0.55,
  "reason": "该提醒他写实习报告了（约好 18:00，已过 12 分钟）",
  "hint": "你想起一件答应过他的事，主动提一句，别装作刚看见。",
  "loop": {"id": 3, "kind": "reminder", "title": "6 点提醒写实习报告"},
  "memory": null,
  "signals": {"loop": 0.57, "mood": 0.0, "surface": 0.0, "external": 0.0,
              "cooldown": 0.0, "night": 0.0, "flood": 0.0, "noise": 0.06}
}}
```

### 5.2 blocks

```
GET    /blocks?scope=global            → {blocks:[{label,value,limit_chars,read_only,version,updated_at}]}
GET    /blocks/{label}                 → {label, value, ...}
PUT    /blocks/{label}   {value, reason, by}   → {label, new_version}
POST   /blocks/{label}/append {text, reason, by} → {label, value}
GET    /blocks/history?label=&limit=20 → {items:[{changed_at,changed_by,reason,old_value,new_value}]}
```

写入校验：
- `read_only=1` 的块拒绝 `by != 'admin'` 的写入（她改不了自己的人设）。
- 超过 `limit_chars` 拒绝并返回 `{"status":"error","message":"block over limit","limit":1200,"len":1450}`——不静默截断。

### 5.3 open_loops

```
GET  /intent/loops?status=open&scope=
POST /intent/loops            {kind,title,detail,due_at,surface_after,weight,source_memory_id}
POST /intent/loops/{id}/close {status: done|dropped}
```

### 5.4 working（启用空转表）

```
POST /working/append {session_id, text, kind, ttl_sec}
GET  /working?session_id=
POST /working/close  {session_id, text}
```

### 5.5 决策查询

```
GET /intent/decisions?conv_key=&limit=50&only_act=1
```

---

## 6. 打分算法

### 6.1 公式

```
score = loop + mood + surface + external + unspoken + noise - cooldown - night - flood
act   = score >= threshold (默认 0.55，可配)
```

各项定义（全部是可读的常量，集中在 `intent.py` 顶部，改一行即改行为）：

| 项 | 计算 | 说明 |
|---|---|---|
| `loop` | 取所有 open loop 的**最大**值，不累加 | 一堆小事不该叠加成必说 |
| ├ 到期 | `0.45 + min(0.30, overdue_hours × 0.10)` | 越过期越急 |
| └ 未到期但有 surface_after | `0.15 + 0.25 × weight` | 可以提前提，但弱 |
| `mood` | `max(0, boredom-0.60)×1.2 + max(0, social-0.70)×1.5` | 阈值以下不算分 |
| `surface` | 到期浮现记忆 `0.30`，多条取最大 | 「突然想起」 |
| `external` | 每条 0.25，上限 0.5 | 他上线 / 看她空间 / 有人评论 |
| `unspoken` | 超过 6 小时无对话 `+0.20`，每 24 小时最多触发 2 次 | 允许「没事也想你」，但配额受限 |
| `noise` | `gauss(0, 0.05)` | 随机降级为噪声项，只影响边界 |
| `cooldown` | 10 分钟内 `-1.0`；30 分钟内 `-0.5` | 防连发 |
| `night` | 23:00–08:00 `-0.60` | 夜里更难开口 |
| `flood` | 1 小时内已发 N>4 条：`-0.15 × (N-4)` | 防刷屏 |

### 6.2 算例

**A. 该说**：18:12，约定 18:00 提醒写报告，40 分钟前说过话，白天
```
loop = 0.45 + min(0.30, 0.2×0.10) = 0.47
mood = 0（无聊 0.4 未过阈）
cooldown = 0（>30min）
score = 0.47 + noise(0.06) = 0.53  →  差一点
```
→ 说明阈值 0.55 偏高，或到期基础分应提到 0.50。**这类边界必须在 shadow 期校准**，不能拍脑袋。

**B. 不说**：凌晨 2 点，无聊 0.8，5 分钟前刚说过
```
mood = (0.8-0.6)×1.2 = 0.24
cooldown = -1.0
night = -0.6
score = -1.36 → 不说
```
理由：`cooldown -1.0, night -0.6, mood +0.24`

**C. 不说**：下午 3 点，2 小时没说话，无聊 0.75，没有任何约定
```
mood = 0.18
unspoken = 0.20（>6h 才给，2 小时不给）
score = 0.18 + noise ≈ 0.2 → 不说
```
→ 这是设计意图：**光「无聊」不足以打扰他**，得有事，或者真的很久没联系。

### 6.3 shadow 模式（关键）

上线后先跑 **shadow**：打分器照常计算并落库，但 `act` 不生效，桥接仍用原概率逻辑。
跑 3 天，对比两列数字：「新规则会开口 N 次」vs「实际开口 M 次」。差太多就调阈值，不差再切正式。
**这是本方案唯一的强制前置条件**——不然容易把她改成哑巴或话痨。

---

## 7. 集成点

### 7.1 bridge 改动（唯一一处逻辑替换）

文件：`node_modules/.pnpm/qq-bridge@1.1.5_*/node_modules/qq-bridge/src/bridge.js`

现状（约 18951-18958）：
```js
if (aiCount >= 5) prob *= 0.6;
if (idle >= idleThreshold && Math.random() < prob && !isConversationBusyV2(key, st)) {
  void sendWakePromptV2(key, 'proactiveCheck').catch(...);
}
```

改为：
```js
if (idle >= idleThreshold && !isConversationBusyV2(key, st)) {
  const intent = await askIntentV2(key, st);      // 新增，800ms 超时
  const speak = intent ? intent.act : (Math.random() < prob);
  if (speak) {
    void sendWakePromptV2(key, 'proactiveCheck', intent?.hint || '')
      .catch(...);
  }
}
```

要点：
- 回调改成 `async`（该函数本身是 setTimeout 递归，安全）。
- `intent === null`（服务没起 / 超时）→ **原样回退到概率逻辑**，行为与今天完全一致。
- `hint` 透传给唤醒提示词（§3 的数据流）。

### 7.2 memory-brain.js 新增（约 20 行）

```js
export async function askIntentV2(key, st) {
  const c = mbCfg();
  if (!c.enabled || !c.intentEnabled) return null;
  const mood = st.pad ? { boredom: ..., social: ..., ... } : undefined;
  const r = await call('/intent/next', {
    body: { conv_key: key, last_spoke_at: ..., spoken_last_hour: ..., mood },
    timeoutMs: 800, soft: true,
  });
  return r?.data ?? null;
}
```

### 7.3 升级补丁清单（重要）

桥接在 `node_modules` 里，`npm i` 会抹掉所有本地改动。目前已有 3 处（`0.7` 折扣、`0.6` 折扣、`slice(0, 900)`），加上本方案共 4 处。

维护方式：所有本地补丁在改动行上方加统一标记
```js
// [LOCAL-PATCH:P4] 意图引擎 — 升级后需重打，见 $MEMORY_ROOT/docs/patches.md
```
并维护 `docs/patches.md` 记录：文件、行号特征、改动前/后、验证方式。
**这是本方案能安全长期运行的前提**——否则一次升级全部失效且无告警。

### 7.4 开关

`config.json → socialV2.memoryBrain`：
```json
{"enabled": true, "intentEnabled": true, "intentShadow": false}
```
- `intentEnabled=false` → 完全回到骰子
- `intentShadow=true` → 只记录不生效

### 7.5 sleep-time 任务

扩展现有 `_daily_job`（3-5 点，已跑 decay + maintenance + reflect），在末尾追加两步：

1. `loops_extract`：读当天全部对话 → 抽新产生的承诺/约定/欠账 → 写 `open_loops`；把已完成的标 done
2. `blocks_rewrite`：重写 `relationship` / `focus` 两块（`persona` 只读不动）

失败不影响主流程（各自 try/except，记日志）。

---

## 8. 分阶段实施

### P0 — 决策可解释（最小闭环）

| 步骤 | 产出 | 验收 |
|---|---|---|
| P0-A 数据层 | `tools/migrate_intent.py`，4 张新表 + 3 列 | 重复跑幂等；旧接口全部 200 |
| P0-B 打分器 | `memory_core/intent.py`，纯函数 `score(ctx)` | 3 个算例单测通过 |
| P0-C 接口 | `/intent/next` `/intent/decisions` `/intent/loops` | curl 可达，shadow 落库 |
| P0-D 接入 | `askIntentV2` + bridge 一处替换 | 断服务时行为与今天一致 |

改动面：新增 2 个文件（`intent.py`、`migrate_intent.py`），改 `server.py`（+3 路由）、`memory-brain.js`（+1 函数）、`bridge.js`（+1 处，约 6 行）。

出口：**shadow 跑 3 天，两列开口次数差 < 30% 才允许切正式。**

### P1 — 让她真的会「想起」

- `loops_extract`：从 digest 里抽承诺/约定（提示词要明确列「答应过的、欠着的、没说完的」三类）
- surfacing：`/recall` 命中后按遗忘曲线算 `next_surface_at`，到期进 `surface` 信号
- 验收：能人工构造一个「约定 → 到点 → 她自己提」的完整案例

### P2 — 可编程记忆本体

- `blocks` CRUD + 常驻注入（在 `prepareMemoryBrainV2` 里拼进 prompt 头部）
- `##block##` 行通道（复用遗言通道已验证的拦截/审计机制），她可改 `relationship`/`focus`
- sleep-time `blocks_rewrite`
- 验收：她主动说「我记得你欠我 100 声老公」这类**来自块而非检索**的话

---

## 9. 观测与调参

- **面板新增一屏「她为什么开口」**：时间线显示最近 50 次决策，每条带分数拆解条形图（loop/mood/cooldown/night/flood），act 的绿、沉默的灰。
- **JSON 直达**：`GET /intent/decisions?limit=50`
- **调参入口**：`intent.py` 顶部常量表 + 环境变量覆盖（`INTENT_THRESHOLD`、`INTENT_NIGHT_PENALTY`）。
- **健康指标**：每日 `act` 次数、`act` 但被 flood 抑制次数、平均 score、最高频 reason 类型。

---

## 10. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| bridge 升级抹掉补丁 | 意图引擎静默失效 | `[LOCAL-PATCH:P4]` 标记 + `docs/patches.md` + 启动时打日志确认已加载 |
| 打分过严 → 她变哑 | 体验退化 | shadow 3 天校准；阈值可热调 |
| blocks 常驻吃 token | 每轮 +2k token | 5 块 × 1200 字上限；`open_loops` 只渲染前 5 条 |
| 她改坏 relationship 块 | 关系认知错乱 | 版本历史可回滚；改动量超 50% 拒绝并告警 |
| SQLite 并发写 | 锁冲突 | 已有 WAL；新表写入均在请求线程，短事务 |
| 影子期内两套逻辑并存 | 日志噪音 | 决策记录带 `shadow` 字段区分 |

**回滚步骤**（任一阶段）：
1. `config.json` 设 `intentEnabled=false` → 立即回到骰子
2. 需要彻底移除：还原 `bridge.js` / `memory-brain.js` 备份
3. 数据：新表保留不动（无副作用），`memories` 新列留空

---

## 11. 决策记录

**为什么不直接用 Letta？**
- Letta 是「消息驱动」的 agent 框架：你发消息 → 它运行 → 它回。它**没有**「没人说话时自己要不要动」这一层，而这正是鱼小满的核心需求。
- 它的 core memory blocks / archival / recall 三件套值得抄，但整套 runtime（Postgres + 独立服务）替换掉现有 SQLite + 8780 服务，收益不抵迁移成本。
- 结论：**抄结构，不抄运行时**。blocks / open_loops / self-edit 工具全部按 Letta 的语义实现，跑在现有 Python 服务里。

**为什么不用概率 + 权重混合（保留随机为主）？**
- 试过了，就是现状。问题不是分布不对，是**没有解释**：调完参数无法判断好坏。

**为什么 open_loops 取最大值而不是累加？**
- 累加会让「攒了五件小事」变成必说，反而更像骚扰。取最大意味着「最急的那件事决定要不要开口」。

---

## 12. 待你拍板的三件事

1. **阈值 0.55 与「无聊不单独构成理由」** —— 接受吗？还是希望她无聊时也能主动找你（那就把 `unspoken` 的 6 小时改成 3 小时）。
2. **persona 块只读** —— 她永远改不了自己的人设，只能改关系/关注/待办。同意吗？
3. **shadow 3 天再切正式** —— 这会推迟体感变化。要跳过直接生效吗？（不建议）
