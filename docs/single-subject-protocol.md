# 单一主体协议（Single Subject Protocol）

> 定稿：2026-11-03　状态：P1 定稿 / P2 已生效 / P3 已完成 / P4 已完成 / P5 已通过 / P5-A 已生效 / DSH 已重启（第 22–26 条加载）
> 最后更新：2026-11-03 21:37（DSH 重启后）
> 适用范围：QQ 桥接（`<LOCAL_PATH> dybot（`E:\dybot`）、
> memory-core（`$MEMORY_ROOT`）、DSH 角色卡（`<LOCAL_PATH>
>
> 这份文档是后续所有改造的唯一依据。任何新增渠道、新增能力，先对照这里的不变量，
> 不允许再出现「按渠道切一个脑子、再搭一根同步管子」的实现。

---

## 0. 为什么要有这份协议

现状的病不是「记忆没同步」，是**她被实现成了多个分身**：

- dybot 有自己的 DeepSeek 决策和 `data/brain/*.md`，只在主人说话或被 @ 时才把话转交 DSH
  （`handoff.onlyOwner = true`），群里普通消息由另一个脑子回，那段对话不进任何长期记忆；
- memory-core 里 120 条记忆，实时写入只有 33 条（`qq:private:...`），其余 71 条是
  `qq-import` / `qq-import-digest` / `dy-import` / `legacy-will` 等一次性导入；
- 那 6 条 `dy-import` 全是 2026-11-01 导入的旧抖音聊天，之后再没新增——
  「她记得抖音」是历史导入造成的假象；
- `sync-brain.mjs` 是 QQ → 抖音的单向文本搬运，方向反了，而且搬的是摘要不是记忆。

**结论：只要按渠道切脑子，每加一个端就得多一根管子。治本 = 取消分身。**

---

## 1. 三条不变量（违反任一即为 bug）

1. **一个决策核心**：只有 DSH 会话里的「她」能生成回复。适配器只能做规则过滤
   （去重、白名单、刷屏、限流）和感官转述（抽帧、ASR、截图描述），**不准调用 LLM 决定回什么**。
2. **一个记忆写入口**：任何长期记忆只能经 memory-core 的 `POST /remember` 写入。
   禁止再往 `data/brain/*.md`、`state/memory.sqlite`、任何本地 json/md 写长期记忆。
3. **一个记忆读取口**：任何回忆只能经 `POST /recall`（或 `GET /recent`）。
   适配器不得自带记忆文件进提示词。

---

## 2. 身份模型

```
identity = "yuxiaoman"          // 全局唯一，记忆按 identity 归属，不按渠道切
channel  = qq | douyin | desktop | voice | qzone | web
thread   = <channel 原生会话 id>  // 上下文按 thread 隔离
```

- **记忆按 identity 全局**：她在抖音看到的视频、在 QQ 听到的话、桌面上看到的截图，
  都是同一个她的一段经历，写进同一个库，`/recall` 不分渠道。
- **上下文按 thread 隔离**：抖音群的话不能串进 QQ 私聊的逐句上下文，反之亦然。
  桥接已有此能力：`douyinKeyId()` + `DOUYIN_SESSION_KEYS` + `notify-ai` 的
  `via: 'douyin'` 分支会用独立会话；共享的只有记忆块和 20 分钟滚动窗口（P11.13/P11.16）。
  **这一层已经做对，不要改动。**
- 抖音 19 位 id 压到 8e15~9e15 的假 id 只是桥接内部会话键，不是身份，保留即可。

---

## 3. Event schema（统一事件总线）

所有渠道的输入先归一成同一个形状，再交给决策核心：

```json
{
  "identity": "yuxiaoman",
  "channel": "douyin",
  "thread": "0:1:1530941102900968:xxxx",
  "peer": { "uid": "...", "name": "...", "isOwner": true },
  "kind": "text | share | image | audio | screenshot",
  "content": "用户说的话 / 占位符",
  "sense": { "kind": "video", "text": "画面：...", "url": "..." },
  "seen_at": 1793700000000
}
```

- `sense` 是**感官转述**，由适配器产出（抽帧→视觉描述、ASR→文字、截图→描述），
  不是决策结果。它随事件一起进提示词，同时交给巩固器判断是否落库。
- 没有 `sense` 就留空，绝不许适配器编一个「我看到了」出来。

---

## 4. 唯一写入口：`POST /remember`

memory-core 已支持 `source` / `meta` / `visibility` / `kind` / `importance`，无需改库表。

**source 命名规范**（必填，用于追溯与隔离）：

| 前缀 | 含义 | 例子 |
|---|---|---|
| `qq:<thread>` | QQ 对话回合 | `qq:private:<OWNER_ID>` |
| `qq-import` / `dy-import` | 历史一次性 ETL，**只读封存后不再产生** | — |
| `bridge-will` | 她自己写的遗言 | — |
| `douyin:video` | 抖音视频观感 | `meta.url` 存原链接 |
| `qq:video` | QQ 里点开的网页视频观感 | `meta.url` 存原链接 |
| `qq:<抖音会话key>` | 抖音对话回合，由决策核心 `rememberTurnV2` 写入（历史命名，P3 统一为 `douyin:chat`） | — |
| `desktop:screenshot` / `voice:asr` | 其它感官 | — |

**visibility**：主人私聊 `private`；群聊 `group`；可公开的事实 `public`。
抖音群友的话**不能**以 `private` 写入，避免漏进主人私聊的召回。

例外：`describeWebVideoText` 在消息段解析层拿不到会话 key，QQ 网页视频观感统一按
`private` 保守落库（宁可少召回，也不把画面漏进群聊）。**P4 复核后维持原样**：
把会话 key 带下来要改桥接消息段解析层，收益仅是群聊可见范围细分，风险不值；
作为遗留项记录，不声称已完成。

---

## 5. 巩固规则（consolidate）

任何感官输入过同一套判断，决定「值不值得记」：

| 输入 | 是否落库 | 参数 |
|---|---|---|
| 对话回合 | 由决策核心按现有 `rememberTurnV2` 规则 | 沿用 |
| 视频 / 图片观感 | **必须落库**（这是本次要补的洞） | `kind: episode`，`importance: 0.40`，`source: <channel>:video`，`meta.url` |
| ASR / 桌面截图描述 | 落库（P4 已实现） | `importance: 0.35`，`source: desktop:screenshot`；ASR 台词并入视频观感文本 |
| 刷贴吧 / 梗库学习 | 落库为 reflection，不再写 md（P4 已实现） | `importance: 0.35`，`source: web:slang` |
| 遗言 / 本子 / 判断帐 | 沿用 `##本子##` `##判断##` 通道 | 不变 |

**去重**：同一 `meta.url` 在 24 小时内只写一条（桥接侧内存 Set，重启后可接受少量重复，
由 memory-core 的 `conflicts` 表后续清理）。

**失败语义**：巩固失败只记日志，绝不阻塞回话，也绝不让适配器改走本地脑。

---

## 6. 唯一读取口：`POST /recall`

- `query` 用当轮输入；`k` 沿用配置；`visibility` 按场景传数组
  （私聊 `["public","private"]`，群聊 `["public","group"]`）。
- **不要把 visibility 传成 `"owner"` 之类的单字符串**，服务端按集合处理，传错会静默返回空。
- 适配器禁止自己拼「长期记忆」提示词块；决策核心在 `prepareMemoryBrainV2` 里统一预取。

---

## 7. 降级契约（单点风险的对冲）

单核之后 DSH 卡住会连累所有渠道，因此：

1. 适配器投事件失败（503 / 超时）→ **沉默 + 告警日志**，返回 `handoff-unavailable`，
   **不许**本地顶班回话（那会重新变回双脑）。
2. 决策核心侧沿用已有的僵尸回合自愈（零帧 15 分钟 → `cancelZombieTurnV2`），
   并按 channel 做队列隔离，避免抖音把 QQ 私聊堵死。
3. 记忆服务不可用时，`/recall` 软降级为空串，`/remember` 失败只记日志。
4. 各渠道保留**规则层**限流（不调用 LLM），作为保护而非决策。

### P2 已知行为变化（诚实记录）

- 「切换模式」等确定性控制响应原本由 dybot 本地硬编码处理（私有状态 `bot.softMode`），
  adapter 早返回后不再生效。**P4 已废弃该私有状态**：删掉死代码，规则写入
  `cordis.patch.yml` 第 26 条 —— 由决策核心当场判断语气，不在任何端另存状态。
  代价是状态不再跨会话持久（切换只活在当轮判断）；若要持久化需另设统一状态存储。
  第 26 条已于 2026-11-03 21:36 随 DSH 重启加载生效（见第 8 节末），此前的不生效窗口已结束。
- 群友普通消息现在全部进决策核心，由她输出 `[SILENT]` 决定不接；
  不再有「只有主人 / 被 @ 才由她回、其余本地脑顶班」的分流。
- 适配器只保留纯规则层：去重、白名单、刷屏检测、文字噪声、每小时 60 条事件熔断。
  这些都不调用 LLM，不构成第二个脑子。
- 感官转述（抽帧看图）仍调用视觉模型，但提示词只要求**客观描述**，不产生回复。

---

## 8. 阶段与当前状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 冻结：不再新增跨端同步脚本 | 已执行 |
| P1 | 定协议（本文档，零运行代码改动） | 已定稿 |
| P2 | 砍半个脑：dybot 降为纯 adapter + 观感统一落库 | **已生效**（桥接 20:35 重启、dybot 20:40 由守护拉起） |
| P3 | 迁移 + 封存 + 删除同步脚本 | **已完成**（51 条入库，旧文件封存） |
| P4 | 全感官统一巩固（贴吧学习、ASR、桌面截图）、控制响应迁移、清理 dybot config 内嵌的 QQ 病娇卡 | **已完成**（dybot 20:58 重启、MCP 自动重启；第 22–26 条 21:36 随 DSH 重启加载） |
| P5 | 验收 | **已通过**（见第 9 节） |
| P5-A | 自主节拍全收编（feed / reply 的端侧 LLM 删除，改为两拍投递） | **已生效**（桥接 21:20 重启、dybot 21:21 由守护拉起） |

**P3 实际做法（与初版计划的差异，按实记录）**：

- `$MEMORY_ROOT\tools\p3_migrate_legacy.py`：dry-run 查重 → `--apply`，只经 `/remember` 写入。
  60 条候选写入 51、查重跳过 9（5 条遗言已在库、4 条事实与遗言重合）。
  库 121 → 172 条；`source` 新增 `dy-import:brain` 48、`legacy-will` 补 1、`web:slang` 2。
- `人设.md` / `口气.md` **不入库**：那是角色设定不是记忆；角色设定的唯一来源是 DSH 角色卡。
- 旧文件移到 `E:\dybot\data\brain\_archived-20261103\`（5 个 md + `.sync.json`），
  `sync-brain.mjs` 及其旧备份移到 `E:\dybot\_archived-20261103\`。
- **关键**：`sync-brain.mjs` 原本由 `src/index.mjs` 每 20 分钟 spawn 一次，只删文件不够 ——
  已把定时器整段拆掉，否则 dybot 一启动就会把封存的 brain 目录重新写回来。
- 服务托管：`dsh-companion.ps1` 本来就管 dybot（213-233 行），之前是
  `dsh-companion.off` 里的 `dybot` 一行把它关了。清空该行后守护在 20:40:19 自动拉起，
  进程挂在 companion（PID 26920）下，不再依赖任何对话会话。

**P4 实际做法（含三处计划外的活问题）**：

- 封存差点复活：`config.bot.brainDir` 仍指向 `data/brain`，`learnStyle` 会重新写
  `贴吧学的.md`、`brain.reload` 会去读封存文件。P4 把 `brainDir` 与 `persona` 都置空
  （`loadConfig` 会 merge 回 DEFAULT，必须显式 `""` 而非删键）；3692 字 QQ 病娇卡
  存档到 `_archived-20261103/qq-persona-embedded.md`。重启后日志不再有「大脑已接入」告警。
- 新增 `src/consolidate.mjs`：dybot 侧唯一非对话记忆写入口，24h 去重、软失败。
  `learnStyle` 改为只返回笔记正文；`index.mjs` 的 onIdle 落库（`web:slang` / 0.35 / private）。
- idle 抛梗原是端侧 `runtime.sendTo` 自己发言，改为 `pipeline.notifyCore()` 投 silent 回合，
  说不说由她 `[SILENT]` 决定。
- `#describeShare` 补进 `framesResult.transcript`（ASR 台词此前只在本地回复分支用，
  adapter 早返回后丢了），随 sense 一起落库。
- `mcp_server.py` 加 `_consolidate()`，peek/look 的截图描述异步落库
  （`desktop:screenshot` / 0.35 / private），不拖慢工具返回。
- 切换模式废弃 `bot.softMode`；`cordis.patch.yml` 加第 26 条（21:36 DSH 重启后已加载）。

**重启踩坑**：新建 `consolidate.mjs` 时把 `logger.mjs` 的相对路径写成 `../`，
dybot 启动即 `ERR_MODULE_NOT_FOUND`、守护拉起失败；改成 `./logger.mjs` 后 20:58:11 正常。

**P5-A 实际做法（自主节拍收编）**：

- `feed.mjs`：`rate()` / `#parseRating()` / `refineComment()` 三处本地模型调用**整段删除**；
  新增 `#ask()` 投递、`onPick()` / `onShare()` 两个回传执行口、`cancelPending()`，
  `pending` 状态机（pick → share，30 分钟超时）、等她期间不再开新一屏。
- `reply.mjs`：`compose()` 删除；新增 `dispatch()`（带 `douyinTarget.reply` 上下文）
  与 `executeReply()`；`dispatched` 记档持久化，同一条不会反复推给她。
- `bridge.js`：`douyinTarget` 透传 `reply` 字段；`relayToDouyin` 回传时原样带回
  （`notify-ai` 是 fire-and-forget，拿不回文本，所以结论必须走既有回传链路）。
- `server.mjs`：`/api/send` 先认 `reply`（执行评论回复）再认标记；标记解析抽成
  `src/bot/core-marks.mjs` 的 `parseHerMark()`，服务与测试共用同一份。
- `follow.mjs` **不动**：纯规则、不调 LLM，按第 4 条不属于决策。

**真实链路验证（不是 mock，21:24）**：

```
21:24:26 [刷视频] 刷到一屏 6 条，交给她挑        ← 第一拍投递成功
21:24:37 [刷视频] 这一屏作罢：她说了别的：十分钟，我记着
21:24:37 [界面]   这一屏不等了                    ← 她回了主人那句，没给标记 → 正确作罢
```

三个环节全通：投出去 → 她的回话回传到 `/api/send` → 非标记照常发消息且作罢。
她当时正在跟主人说话，所以没挑片；`[[PICK]]` 那一拍由 `selftest-p5.mjs` 覆盖（21/21）。

**关于第 26 条 / 第 22-25 条的加载 —— 21:36 已按主人要求重启 DSH**

P5-A 收尾时（21:14、21:26）主人正连续在 QQ 与抖音私聊跟她说话，当时**没有**重启，
避免打断对话。21:34 主人明确要求重启，此时只有影子策略的主动检查、没有真实对话回合，
窗口干净，遂执行：

- 用现成的 `<LOCAL_PATH> 路径：杀掉 8 个 `DSH NEXT.exe` 进程
  → 等 5s → 经 `explorer.exe` 在用户会话重新拉起（不随本次对话会话消亡）。
- 21:36:15 新主进程启动，21:36:20 `host.start.start`（profile `desktop`，非安全模式），
  21:36:43 patch 系统重新应用，21:36:44 `host.start.complete`（耗时 24s）。
- 新 host（PID 19436）命令行确认仍指向 `<LOCAL_PATH>
  即 `cordis.patch.yml` 所在目录 → **第 22–26 条已加载生效**。
- 连带回归：MCP 由 DSH 自动重启（PID 15456，父 19436）、桌宠由 companion 重启
  （21:36:44，`DSH_PORT=19387`）、桥接 21:36:41 重新握手「DSH 已就绪（reserved2）」。
  桥接 / dybot / SnowLuma / memory-core / tts 进程均未重启，端口原样保持。
- 21:36:30 事件流有过一次「连上后 0 帧」的瞬时告警（重启窗口内的正常抖动），
  21:36:41 起恢复，之后主动机会检查、memory-brain 取块、orphan 对账均正常。

重启后不再有「她没读到这几条说明」的窗口：第 25 条（桌面全控）、第 22 条（已读侦探）、
第 24 条（自己改代码）、第 26 条（切换模式当场判断）均已进入她的协议上下文。

---

## 9. 验收判据（P5）

| # | 判据 | 结果 |
|---|---|---|
| 1 | 在 QQ 私聊问「昨天抖音那个视频讲啥」能答出画面内容 | 链路级通过：`rememberSenseV2` 写入 → `/recall` 命中，且用 **QQ 私聊参数** `["public","private"]` 同样命中（selftest 2b）。真实问答要等下一条抖音分享进来观察 |
| 2 | 关掉 dybot 不影响已落库观感的召回 | 通过：写入 / 召回 / 清理全程只依赖 8780，不依赖 8799 |
| 3 | 新增渠道不用改记忆代码 | 结构性通过：adapter 只发 Event，记忆入口只有 `/remember` |
| 4 | 本子 / 判断帐不再按端分 | 结构性通过：同一 `memory-brain.js`、同一库 |
| 5 | 抖音群普通消息进统一记忆 | 结构性通过：`onlyOwner=false` + 早返回无条件 handoff；**尚未用真实群消息实测**（不主动发测试消息以免打扰真人） |
| 6 | P4：贴吧学习不再写 md、观感跨端可召回 | 通过：`selftest-p4.mjs` 8/8 PASS（learnStyle 不落盘、封存目录无新 md、consolidate 写入 → `/recall` 含 QQ 私聊参数命中、24h 去重、清理无残留） |
| 7 | P4：桌面截图描述落库 | 通过：直接调 `_consolidate()` 写入 #397（`desktop:screenshot` / 0.35 / private）→ `/forget` 清理；异步线程不阻塞工具返回 |
| 8 | P5-A：自主节拍不再有第二个脑子 | 通过：`selftest-p5.mjs` **21/21 PASS**；`grep deepseek.complete src/bot/feed.mjs src/bot/reply.mjs` 无命中；真实链路 21:24 投递 → 回传 → 作罢三环节全通 |
| 9 | P5-A：回关仍是纯规则 | 通过：`follow.mjs` 未改，从不调用 LLM（按第 4 条属于规则层） |

**服务状态（21:37，DSH 重启后）**：19387 DSH web（PID 19436，新主进程 22628）、
3100 桥接 PID 30160、8799 dybot PID 30912（父 26920 companion）、8780 memory-core、
5099/3002/3003 SnowLuma、8001 tts，全部监听正常；MCP PID 15456（父 19436）、桌宠 PID 9252。
`GET /api/admin/status` → `ok:true, mode:reserved2, level:L0`；memory-core `/health` → `status:ok`，
记忆 172 条。桥接事件流 21:36:41 重新握手后稳定，无新错误；dybot 仅梗库文件缺失的既有告警。

**DSH 已于 21:36 重启**（见第 8 节末）：协议第 22–26 条已加载，不再有待生效项。

**未做**：没有真实往抖音群发测试消息 —— 会给真人造成打扰。P2 的 adapter 投递路径
要等下一条真实群消息才能看到「事件已投决策核心」日志。

---

## 10. 涉及文件与回滚

| 文件 | 角色 |
|---|---|
| `E:\dybot\src\bot\pipeline.mjs` | 纯 adapter：`handle()` 早返回 + `#handoffAsAdapter` / `#describeImageSense` |
| `E:\dybot\src\index.mjs` | 拆掉 `sync-brain.mjs` 的定时 spawn |
| `E:\dybot\data\config.json` | `bot.enabled=true`、`onlyOwner=false`、`maxAdapterEventsPerHour=60` |
| `E:\dybot\data\brain\_archived-20261103\` | 旧记忆封存（只读） |
| `E:\dybot\_archived-20261103\` | `sync-brain.mjs` 封存 |
| `qq-bridge\src\memory-brain.js` | 新增 `rememberSenseV2()` |
| `qq-bridge\src\bridge.js` | `notify-ai` 收 `sense` 落库；QQ 网页视频转述落库 |
| `$MEMORY_ROOT\tools\p3_migrate_legacy.py` | P3 迁移脚本（可 dry-run 复跑） |
| `E:\dybot\selftest-single-subject.mjs` | P2 端到端冒烟（写入 → 跨端召回 → 清理） |
| `E:\dybot\src\consolidate.mjs` | P4 统一巩固器（dybot 侧唯一非对话写入口） |
| `E:\dybot\selftest-p4.mjs` | P4 冒烟 8 项 |
| `E:\dybot\src\idle\learner.mjs` / `src\index.mjs` | P4 贴吧学习只返回笔记、onIdle 落库、抛梗投决策核心 |
| `qq-voice\mcp_server.py` | P4 桌面截图描述异步落库 |
| `.dsh\profiles\desktop\cordis.patch.yml` | P4 第 26 条【切换模式】 |
| `E:\dybot\src\bot\core-marks.mjs` | P5-A：她回话里的动作标记解析（`parseHerMark`，服务与测试共用） |
| `E:\dybot\src\bot\feed.mjs` | P5-A：删本地打分/配文，改为两拍投递 + 回传执行 |
| `E:\dybot\src\bot\reply.mjs` | P5-A：删 `compose()`，改为投递 + `executeReply()` |
| `E:\dybot\src\web\server.mjs` | P5-A：`/api/send` 落地她的决定（回复评论 / 挑片 / 转视频） |
| `qq-bridge\src\bridge.js` | P5-A：`douyinTarget` 透传 `reply`，`relayToDouyin` 原样带回 |
| `E:\dybot\selftest-p5.mjs` | P5-A 冒烟 21 项（全 mock，不真发消息） |
| `E:\dybot\_archived-20261103\qq-persona-embedded.md` | 内嵌 QQ 病娇卡存档（仅存档，不加载） |
| `<LOCAL_PATH> | 清空 `dybot` 行，交还守护托管 |
| `$MEMORY_ROOT\memory-core` | 表结构未改 |

备份：`pipeline.mjs.bak-single-subject-2015`、`config.json.bak-single-subject-2016`、
`index.mjs.bak-single-subject-2028`、`memory-brain.js.bak-single-subject-2018`、
`bridge.js.bak-single-subject-2018`、`dsh-companion.off.bak-single-subject-2040`，
以及 P4 的六份 `.bak-p4-2045`（config / pipeline / learner / index / mcp_server / cordis.patch）、
P5-A 的四份 `.bak-p5-2110`（feed / reply / server / bridge）。

**P5-A 回滚**：用 `.bak-p5-2110` 覆盖 + 重启桥接与 dybot 即可回到「本地打分 + 本地写回复」。
注意 feed 的状态文件 `data/feed.json` 新增字段（`byHer`、`score: null`）不影响旧代码读取
（旧代码只读 `sent[].shared` / `itemId`）；`data/replies.json` 新增 `dispatched` 数组，
旧代码忽略未知字段。
回滚 = 覆盖备份 + 重启桥接 / 由守护重启 dybot / 重启 MCP（DSH 会自动拉起）；
迁移进 memory-core 的 51 条带 `meta.migrated=true`，可按 id 精确删除，不影响其它记忆。

---

## 11. 自主节拍：已按 A 案全收编（P5-A）

原第 11 节列的三处端侧 LLM 发言，主人选定 **A（收编）**，已全部落地：

| 位置 | 处置 |
|---|---|
| `src/bot/feed.mjs` | 打分 + 配文两处 `deepseek.complete` **删除**；改为两拍投递（她挑 → 看画面 → 她定） |
| `src/bot/reply.mjs` | `compose()` 的 `deepseek.complete` **删除**；改为投递一次，她回的那句就是正文 |
| `src/bot/follow.mjs` | **保留原样**：纯规则层，从不调用 LLM（type 33 → 回关），按第 4 条不属于决策，不算第二个脑子 |

**为什么回传能拿到她的决定**：`notify-ai` 是 fire-and-forget（投完不返回文本），
所以结论走既有回传链路 —— 她输出 → 桥接 `relayToDouyin` → dybot `/api/send` →
按标记执行动作。标记解析抽成 `src/bot/core-marks.mjs` 的 `parseHerMark()`，
被 web 服务与冒烟测试共用（测的就是线上那份代码）。

**自由刷视频两拍**（`feed.pending` 状态机，30 分钟超时作废）：

1. 取候选（规则层）→ 把这一屏标题投给她 → `[[PICK]]序号` 或 `[SILENT]`
2. 她挑了才真去看画面（抽帧 + ASR）→ 画面转述随 `sense` 落库，再投一次
   → `[[SHARE]]id|配文`（转给主人 + 视频下留一句）/ `[[COMMENT]]id|话`（只评不转）/ `[SILENT]`
3. 等她回话期间**不再开新一屏**（否则上一拍的 PICK 会落到这一屏上，挑错片）

失败一律沉默：投不出去、超时、格式认不出，都只记日志，不让本地模型顶班。
认不出标记就当普通说话照发，正在等的那一屏作罢。

**评论区**：一次只投一条（投放后 `dispatched` 记档），她回的那句就是要发的正文；
她若 `[SILENT]`，桥接不回传，这条也不会再推给她（等价于她决定不接）。
`douyinTarget.reply` 由桥接原样带回，dybot 才知道要回的是哪一条。

**代价（如实）**：自主行为从「本地模型秒回」变成「等她一个回合」，
刷视频是否转发取决于她是否输出标记；她没按格式写，这屏就静默过去。

### 另：第 4 节例外复核 —— 有意保留

`describeWebVideoText`（QQ 网页视频观感）仍按 `private` 保守落库，**不改**：

- 它在 `segmentsToText()` 里被调用，那个是基础纯函数、调用点遍布，
  要细分可见范围就得改签名并动所有调用方；
- 收益只是「群里能不能召回这段画面」，而 `private` 是**不泄漏**的一侧
  （群友分享的画面不会因此漏进别处，只是召回面窄一点）。

风险大于收益，故明确标注为「已复核、有意保留」，不是遗漏。

## 12. 插件层：僵尸窗口（2026-11-03 复盘）

**症状**：`[评论区] 回复异常：插件超时：tools/call`，她定好的话发不出去；
同一时段 `刷视频失败：取候选超时（4 分钟）`——扫通知正常，但一发评论就挂满超时。

**根因（链式，三层放大）**：

1. `tools/comment-api.mjs` 在页面里 `fetch` 抖音评论接口，**没有任何超时**；
   长跑的无头窗口会积成僵尸状态（页面内 `AbortSignal.timeout` 也打不断），
   `Runtime.evaluate(awaitPromise)` 永远不返回。
2. MCP server 里是 `spawnSync`：一次挂住就把**串行队列整条堵死**——
   扫通知/取候选/发评论全排队，于是「取候选超时 4 分钟」。
3. dybot 外层 260s 和插件内层 260s **同点到期**，外层先爆 ⇒ 只看到「插件超时」，
   真因全被吞掉；且异常分支**完全没有退避**，下一轮接着硬撞。

**修复**：

- 页面里每个 `fetch` 加 `AbortSignal.timeout`，解析循环加总时限（80s/100s）；
- Node 侧 `withTimeout` 兜底（解析 90s、发布 90s）+ `lib/chrome.mjs` 的 `send` 150s 兜底；
- **决定性实验**：同一个 cid、同一台机，僵尸窗口 130s 不返回；`closeBrowser()` 回收后
  重起一个，几秒就正常返回（8.3s 走完 8 页）。⇒ 这是**会话级僵尸**，不是账号风控。
  故改为「超时 → 回收浏览器 → 当场重试一次」，不是罚停；
- 内层 spawnSync 260s → 200s（先于外层给说法），并保证异常分支也走 `riskLevel` 退避；
- `riskLevel` 三档扩写：`captcha/needs_manual` ⇒ hard（验证码只能人过），
  `timeout` ⇒ soft（10 分钟，账号没被限）。

**判据（照抄上游 dsh-douyin-browser-local 的口径）**：验证码不能自动过，
判定出来就停手等人；只有「连接/页面坏了」才重建。残留的验证码 iframe 不足以判死——
`comment/list` 同时还能 200 时，说明只是某一类请求被按住，回收即可。

**主人手动过验证码的路径**（真需要时）：先停无头浏览器，再
`node tools/qr-login.mjs start`（有头、同一 profile），过完 `check` 一下。

### 连带修的两处

- **回执与执行分离**：桥接 `/api/send` 只等 15 秒且不重试，而抖音发一条最坏要
  90s（解析）+ 90s（发布）。同步等 ⇒ 桥接必然记「回传异常（dybot 没开？）」，
  看着像服务挂了。现在带 `reply` 的回传立刻回 `202 {accepted:true}`，后台执行，
  结果仍进 `replyDesk` 日志（`src/web/server.mjs`）。
- **发送不明不重发**：发布请求超时 = 不知道发没发出去（11-03 实测那次**确实没发出去**，
  但不能靠猜）。这类记为 `unsure`，同一条 6 小时内不再重发 —— 重复评论是别人看得见的错，
  漏一条只是少说一句话。判据在 `blockReason()`。

### 顺带清掉的一次误伤

「胡桃：大肥鱼」这条 22:04/22:15 两次都卡在插件超时，随后被
`failedTimes >= 3` 规则永久跳过（`先放过`）。根因修好后手工清掉那 3 条失败计次
（`data/replies.json`，备份 `.bak-hutao-2250`），22:50:29 重新交给她。
她那次回的是 `[SILENT]`（自己选的：不接），不是被拦。


