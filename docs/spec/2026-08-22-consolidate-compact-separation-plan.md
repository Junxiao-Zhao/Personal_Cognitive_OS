# PCO v0.4.0：Consolidate 与 Compact 语义拆分实施计划

> 状态：计划中，尚未实施  
> 目标版本：PRD v0.4.0  
> 计划日期：2026-08-22  
> 变更性质：核心交互、信任边界与 checkpoint 状态机语义升级  
> 核心定义：`consolidate = memory checkpoint`；`compact = memory checkpoint + harness compaction`

## 1. 目标与完成定义

本轮不新增第二套认知整理工作流。继续保留一个 `CheckpointEngine`，把现有 checkpoint 中的“认知记忆提交”和“Harness 上下文压缩”拆成两个有先后约束的责任：所有 compact intent 必须先完成或安全复用 consolidate 结果，只有 context publication 成功后才允许调用 Harness 原生 compact。

完成后必须满足：

- `/consolidate` 归档增量公开对话、运行 worker、校验并提交 canonical memory、更新 Meta-memory、发布最新 system context，但不调用 native compact；
- `/compact` 执行同一 consolidate 流程，并在成功发布 context 后额外调用一次 native compact；
- `/pco-status`、`/pco-retry`、`/pco-abort` 继续服务同一个 durable checkpoint；
- `trigger` 只表示来源 `manual | auto`，`intent` 只表示行为 `consolidate | compact`；
- intent 由可信 Host provenance 决定，Agent 不能通过 `pco_checkpoint` 参数选择或伪造 intent；
- 普通对话中没有合法 command/auto provenance 的 `pco_checkpoint` 调用一律 fail closed；
- consolidate 与 compact 使用分离 cursor，已经 consolidate 的消息不会因后续 compact 被重复处理；
- 同一冻结边界的 canonical 内容与 intent 无关；compact 只是成功提交和发布后的可选 Harness side effect；
- Plugin 或 Python 进程重启后仍能恢复原始 trigger、intent 和精确失败边界；
- PRD、配置、schema、命令、状态提示、receipt、迁移说明和验收材料统一升级到 v0.4.0 语义。

## 2. 非目标与不变约束

- 不建立独立的 `ConsolidateEngine` 与 `CompactEngine`；
- 不允许 `/compact` 绕过 consolidate 校验、授权、commit 或 context publication；
- 不把 `intent`、`trigger` 暴露为 Agent 可控的 `pco_checkpoint` tool 参数；
- 不因 intent 不同改变 frozen input、worker 输出、transaction fingerprint 或 canonical 内容指纹；
- 不把 slash command、control prompt、tool call 或 assistant 对命令的复述归档为用户证据；
- 不改变 append-only canonical memory、Git transaction、protected Meta-memory 授权和 pre-commit validation 合同；
- canonical commit 后派生目标失败继续遵循现有“记忆提交保留、派生可重试”合同；
- MVP 仍只允许一个 active PCO checkpoint 和一个 active Harness binding。

## 3. 术语与产品合同

### 3.1 用户命令

| 命令 | 合同 |
| --- | --- |
| `/consolidate` | 执行 memory checkpoint，提交并发布最新认知上下文，不压缩原始会话上下文 |
| `/compact` | 先完成或复用 memory checkpoint，再执行 Harness 原生 compact |
| `/pco-status` | 展示 trigger、intent、consolidation、publication、compaction 和恢复状态 |
| `/pco-retry` | 从最近持久化边界恢复；不得重做已成功的不可逆阶段 |
| `/pco-abort` | 仅能中止尚未 canonical commit 的操作 |

用户提示必须区分结果：

```text
/consolidate：记忆已更新，对话上下文未压缩。
/compact：记忆已更新，对话上下文已压缩。
```

### 3.2 Trigger 与 Intent

```json
{
  "trigger": "manual",
  "intent": "consolidate"
}
```

| 场景 | trigger | intent |
| --- | --- | --- |
| 用户 `/consolidate` | `manual` | `consolidate` |
| 自动新增公开对话阈值 | `auto` | `consolidate` |
| 用户 `/compact` | `manual` | `compact` |
| 上下文逼近硬阈值 | `auto` | `compact` |
| Harness 请求自动压缩 | `auto` | `compact` |

`trigger` 和 `intent` 必须进入 durable state、canonical checkpoint record、receipt 与恢复测试，但 canonical changeset 的内容指纹必须排除 `intent`。审计记录可以记载 intent，认知内容本身不能因此分叉。

## 4. Host Provenance 与信任边界

### 4.1 手动命令绑定

Plugin 为 `/consolidate` 和 `/compact` 建立一次性、session-bound、带过期时间的 command provenance：

```text
slash command
→ command.execute.before 识别 command 并登记 intent
→ chat.message 绑定 Host 生成的 user message ID
→ assistant parent message / pco_checkpoint call ID 绑定该 Host message
→ tool.execute.before 消费匹配 provenance
→ Plugin 内部调用 CLI，传入已验证的 trigger + intent
```

命令 markdown 只要求 Agent 无参数调用 `pco_checkpoint`。Plugin 必须忽略或拒绝任何模型提供的 trigger/intent 字段，而不是用默认 `manual` 补全缺失 provenance。

### 4.2 自动触发绑定

沿用一次性 auto marker，但 marker 增加 intent，并与 session、nonce、Host message、parent/call ID 和 expiry 完整绑定。两个自动检查都只由 `session.idle` 发起：

- 达到新增公开对话阈值：生成 `{trigger:auto, intent:consolidate}`；
- 达到上下文安全阈值：生成 `{trigger:auto, intent:compact}`；
- 同一 idle tick 两者同时满足时只调度一个 `compact` intent，因为它包含 consolidate；
- active checkpoint、未消费 marker、输入锁或待授权期间不得并发调度；
- 超时、重放、错误 session、错误 parent/call ID、重启后无法验证的旧 marker全部拒绝，不降级为 manual。

### 4.3 Control message 排除

`/consolidate`、`/compact` 的 slash message、注入的 `[PCO_CONTROL]` 内容、native compact 控制消息和相关 tool-call parts 都标记为 control provenance，并在 archive 层统一过滤。过滤依据必须是 Host metadata/ID 绑定，不依赖文本前缀这一单点判断。

### 4.4 Harness 自动 compaction 拦截与路由

`session.idle` 阈值只是提前量，不能作为唯一 auto compact 入口。OpenCode 因上下文耗尽而发起的 Harness 自动 compaction 也必须进入 PCO 门禁；`experimental.session.compacting` 不得再只替换 summary prompt 后放行当前操作。

Plugin 在该 hook 中必须：

1. 区分“外部/Harness 发起的自动 compaction”和“PCO 在 durable checkpoint 的 `NATIVE_COMPACT` 阶段主动发起的 compaction”；
2. 对外部请求同步取消或阻止本次原生 compaction，不能让它在后台继续；
3. 以 Host 提供的 session ID、compaction event/request ID、当前上下文边界和原因建立 durable `{trigger:auto, intent:compact, origin:harness_auto_compaction}` 请求；
4. 立即锁定输入并调度同一个 `CheckpointEngine`，完成 archive、consolidate、commit 和 context publication；
5. 只有状态机进入 `NATIVE_COMPACT` 后，才重新调用一次 Harness native compact；该调用携带 Plugin 私有的一次性 bypass token，并与 checkpoint ID、session ID、attempt ID 绑定；
6. hook 再次收到 PCO 发起的 compaction 时，只消费完全匹配的 bypass token 并放行一次；缺失、过期、重放或绑定不匹配的 token 一律按外部请求重新拦截，不能用布尔全局开关绕过；
7. native compact 成功或失败后都退休 token；失败进入 compact-only recovery，不得重新 consolidate。

```text
Harness overflow/auto compact request
→ experimental.session.compacting 拦截并取消当前 native compact
→ durable compact checkpoint(origin=harness_auto_compaction)
→ consolidate / approval / commit / publish
→ PCO mint one-time native-compact bypass token
→ PCO invoke native compact
→ compacting hook consume token and allow exactly once
```

如果当前已有 active checkpoint：

- active intent 已为 `compact`：把 Harness request ID 作为重复请求附加到现有 durable state，不创建第二个 checkpoint；
- active intent 为 `consolidate` 且尚未完成：保持原始 intent 不变，持久化一个 `pending_compaction` 请求；当前 consolidate 成功发布 context 后，无缝创建/进入 no-op durable compact checkpoint，再放行 native compact；输入锁在两个操作之间不得释放；
- active checkpoint 正在等待 Meta 审核：同样持久化 `pending_compaction` 并保持输入锁，不得因上下文压力绕过授权；
- active checkpoint 已 commit 但 publication/derivation 尚未结束：等待其 publication 成功后执行 durable no-op compact；publication 失败则继续阻止 native compact；
- active checkpoint 已处于 PCO `NATIVE_COMPACT`：只有匹配 bypass token 的事件可放行，其他请求合并为重复请求。

`pending_compaction` 至少持久化 request/event ID、session ID、requested boundary、requested_at 和 origin，Plugin 重启后必须继续完成它。若当前 OpenCode hook/API 无法可靠取消原生 compaction，v0.4.0 不得宣称该 Harness 满足 compact 门禁；实现前必须先用锁定版本的 loopback/真实事件验证取消合同，不得以改写 prompt 作为替代。

## 5. 单一 CheckpointEngine 状态机

逻辑阶段统一为：

```text
SYNC_AND_ARCHIVE
→ FREEZE_BOUNDARY
→ CONSOLIDATE
→ VALIDATE
→ META_APPROVAL（可选）
→ CANONICAL_COMMIT
→ RENDER_AND_PUBLISH_CONTEXT
→ DERIVATIONS
→ NATIVE_COMPACT（仅 compact intent）
→ DONE
```

实现时可以保留兼容性的细粒度状态名，但 durable 状态至少要明确区分：

- consolidate 尚未提交；
- canonical memory 已提交；
- context 尚未/已经发布；
- derivations 尚未/已经完成或处于 pending；
- compaction 未请求、待执行、已完成或失败；
- receipt 尚未/已经插入；
- input lock 尚未/已经释放。

建议新增/调整字段：

```text
trigger: manual | auto
intent: consolidate | compact
consolidation_status: pending | no_op | committed
context_publication_status: pending | completed | failed
compaction_requested: bool
compaction_status: not_requested | pending | completed | failed
compaction_origin: command | idle_threshold | harness_auto_compaction | null
pending_compaction: durable request | null
native_compact_attempt_id: string | null
archive_cursor
consolidation_cursor_before
consolidation_cursor_after
compaction_cursor_before
compaction_cursor_after
```

旧的 `compacted: bool` 只能作为读取旧 state 的迁移输入，不能继续承担完整恢复语义。

## 6. Cursor 与增量边界

### 6.1 三类 cursor

- `archive_cursor`：Harness 公开消息已逐 turn 持久归档到的位置；
- `consolidation_cursor`：公开归档中已进入成功 canonical consolidate 的位置；
- `compaction_cursor`：Harness 已确认完成原生压缩的位置。

当前 `ThreadState.last_consolidated_message_id` 应迁移或明确映射为 `consolidation_cursor`；不得再把 checkpoint state 中的 archive snapshot 当作 consolidate 或 compact 进度。cursor 仅在对应动作成功边界后推进：

| 边界 | archive | consolidation | compaction |
| --- | --- | --- | --- |
| 公开 turn 成功归档 | 前进 | 不变 | 不变 |
| canonical commit 成功 | 不变 | 前进到 frozen `through` | 不变 |
| native compact 成功 | 不变 | 不变 | 前进到本次 compact 覆盖边界 |

典型流程：

```text
对话 A → /consolidate
archive=A, consolidation=A, compaction=旧值

继续对话 B → /compact
freeze 范围仅为 (consolidation=A, through=B]
commit B → publish → compact Harness 当前 A+B 上下文
archive=B, consolidation=B, compaction=B
```

### 6.2 Source-only 变化

no-op 判定不能只看 conversation cursor。若没有新增公开消息，但已授权 source 的规范化内容相对最近成功 consolidate 有变化，仍须运行 consolidate。为此 frozen boundary/last successful consolidation 必须记录 source hashes；只有“无新增公开材料且 source hashes 未变化”才是 no-op。

## 7. No-op Consolidate 与 Canonical 指纹

当 `/compact` 紧跟成功 `/consolidate`，且没有新增公开消息或 source 变化：

- 不 spawn/resume worker；
- 不创建 transaction 或空 canonical checkpoint/content commit；
- 不生成新的 Meta-memory、continuation 或派生任务；
- 复用最新已成功发布且 content hash 与当前 canonical commit 对齐的 context bundle；
- 建立 durable compact operation 状态后，直接执行一次 native compact；
- receipt 标记 `consolidation.status = no_op`，并引用复用的 `content_commit` 与 context hash。

若最新 context bundle 缺失、hash 不匹配或上次 publication 未成功，则不能直接 compact；先重建并发布 context，成功后才继续。

同一 frozen input、worker contract、策略版本和 source hashes 必须产生相同 transaction/content fingerprint，无论 intent 是 consolidate 还是 compact。intent 只属于请求/审计与 Harness side-effect 层。

## 8. Context Publication 与派生顺序

- `/consolidate` canonical commit 成功后立即 render/publish `currentContext`，下一轮模型请求必须看到最新 Meta-memory 和 continuation；
- consolidate 后原始对话仍保留在 Harness 上下文，短期内与 Meta-memory 重复是允许的；
- Meta-memory 是理解框架，不是独立于原始对话的新证据；
- compact 后由已发布 Meta-memory、continuation、最近未压缩消息和按需检索承担主要连续性；
- context publication 是 compact 的硬门禁；render、cache 写入或 Host publication 任一步失败都禁止 native compact；
- Harness 自发的 overflow/automatic compaction 同样受该硬门禁约束；它必须先被 hook 取消并路由到 durable compact checkpoint，不能仅替换 summarize prompt 后放行；
- canonical commit 后 AFFiNE/index/backlinks 等派生失败仍可按现有合同继续 compact，但必须记录 pending derivations；
- 若实现仍以同步顺序运行 derivations，应保证派生失败被结构化捕获后继续到 compact，而不是由异常意外阻断；后续可在不改变合同的前提下异步化。

## 9. 授权、失败与恢复矩阵

原始 intent 必须在创建 checkpoint 时持久化，Meta 审核、进程重启和 `/pco-retry` 均不得重算或降级 intent。

| 失败/暂停边界 | compact 行为 | `/pco-retry` 行为 |
| --- | --- | --- |
| archive/freeze/worker/validation 失败 | 禁止 | 从最近安全边界继续 consolidate |
| `AWAITING_META_APPROVAL` | 禁止，保持输入锁 | 先恢复同一 proposal 的授权流程 |
| canonical commit 前 abort | 不执行 | 可由新命令创建新 checkpoint |
| canonical commit 成功、state save 临界崩溃 | 禁止重复 commit | 从 Git/canonical record 恢复 commit provenance |
| derivation 失败 | 允许继续 compact | 单独重试 pending derivations，不重复 consolidate |
| render/publication 失败 | 禁止 | 只重试 render/publication，成功后按原 intent 继续 |
| native compact 失败 | canonical memory 与 published context 保留 | 只重试 native compact，不启动 worker、不提交新 commit |
| receipt 插入失败 | 不重复 compact | 只重试 receipt/final unlock |

`/pco-abort` 仅在 canonical commit 前有效。commit 后即使 compact 尚未成功，也不得把操作标记为 aborted；应保留为可恢复的 compaction failure。

Plugin 重启恢复时以 Python durable checkpoint 为权威来源，重建与该 checkpoint 对应的 command intent/lock UI。一次性 tool provenance 和 question grant 仍需重新建立，不能仅凭 durable intent 信任旧 Agent tool call。

## 10. 自动阈值与配置迁移

替换单一 `checkpoint.trigger_ratio`：

```yaml
checkpoint:
  auto_consolidate:
    enabled: true
    new_public_tokens: 32768

  auto_compact:
    enabled: true
    context_ratio: 0.90
```

合同：

- `auto_consolidate.new_public_tokens` 统计上次成功 consolidation cursor 之后的公开 user/assistant 内容；control/tool reasoning 不计入；
- token 估算算法必须确定、可测试，并保存必要的累计基线，不能因 Plugin 重启而从零开始；
- `auto_compact.context_ratio` 使用 Harness 对当前模型上下文占用的估算；默认从 `0.50` 提升到 `0.90`；
- `auto_compact.enabled=false` 是合法配置，适用于 1M context 等长上下文模型；
- 两类自动触发只在 `session.idle` 检查；
- Harness 自发 compaction 是独立的兜底事件入口，不受 `session.idle` 限制；无论 `auto_compact.enabled` 是否关闭都必须拦截。`enabled=false` 只关闭 PCO 的提前阈值调度，不能授权 Harness 绕过 consolidate；
- 旧 `trigger_ratio` 在升级时给出明确迁移错误或一次性配置迁移提示，不静默解释成任一新阈值；
- Profile 中重复存在的 checkpoint 配置与应用配置必须统一来源或同时校验，避免默认值漂移。

## 11. Receipt、Schema 与用户状态

compact receipt：

```json
{
  "trigger": "manual",
  "intent": "compact",
  "consolidation": {
    "status": "committed",
    "content_commit": "..."
  },
  "context_publication": {
    "status": "completed",
    "content_hash": "..."
  },
  "compaction": {
    "requested": true,
    "status": "completed"
  }
}
```

consolidate receipt：

```json
{
  "trigger": "manual",
  "intent": "consolidate",
  "consolidation": {
    "status": "committed",
    "content_commit": "..."
  },
  "context_publication": {
    "status": "completed"
  },
  "compaction": {
    "requested": false,
    "status": "not_requested"
  }
}
```

为与产品文案兼容，UI 可显示“skipped”，但机器 schema 使用 `not_requested`，避免把“未请求”混同于“请求后跳过”。no-op compact 的 consolidation status 使用 `no_op`。

同步更新：

- `CheckpointState` Pydantic schema 与旧 state migration；
- canonical checkpoint JSON Schema；
- CLI JSON 输出与 Plugin runtime guards；
- `/pco-status` 和错误恢复提示；
- workflow 版本 `consolidate@0.4.0`；
- PRD 文档版本、术语、主流程、配置和 migration 章节。

## 12. 分阶段实施

### Phase 0：PRD v0.4.0 与合同冻结

1. 从 `docs/PCO_PRD_v0.3.1.md` 派生 v0.4.0，保留旧版作为历史基线；
2. 重写产品主循环、命令表、checkpoint 定义、状态机、自动阈值、cursor、receipt、失败恢复和 Harness 迁移语义；
3. 迁移建议改为：“迁移前执行 `/consolidate`，确保最新认知状态和 continuation 已进入 canonical memory”；
4. 在实现前冻结 durable state、canonical schema、CLI 和 Plugin provenance 合同。

完成条件：PRD 不再把 checkpoint 定义为必然 compact，也不再出现默认 50% 单阈值合同。

### Phase 1：核心模型、配置与 schema

1. 引入 `CheckpointIntent`，让 `CheckpointEngine.request(trigger, intent)` 只由可信调用层使用；
2. 持久化 intent 和分阶段 result；提供 v0.3.1 active/completed state 的显式读取迁移；
3. 引入三 cursor 语义和 source hash baseline；
4. 拆分 auto config，并更新默认 YAML、Profile、校验与安装模板；
5. 更新 canonical checkpoint schema 和 receipt schema。

完成条件：Python 单元测试可表达 consolidate、compact、no-op 和 compact-only retry，不依赖 Plugin。

### Phase 2：CheckpointEngine 编排拆分

1. 将 commit 后 finalize 拆为 context publication、derivations、optional native compact、receipt/unlock 的幂等阶段；
2. 增加 no-op 检测与已发布 context 复用；
3. 在 commit 成功边界推进 consolidation cursor；在 native compact 成功边界推进 compaction cursor；
4. 确保 context publication 失败硬阻断 compact，derivation 失败不阻断 compact；
5. 将 retry 按 failure phase 精确路由，避免重复 worker、commit、compact 或 receipt。

完成条件：FakeHarness 事件序列测试证明所有副作用恰好执行一次。

### Phase 3：OpenCode Plugin 命令与 provenance

1. 新增 `commands/consolidate.md`，调整 `compact.md`；
2. 将 command/auto marker 抽象扩展为携带 intent 的一次性 provenance；
3. `pco_checkpoint` 保持无参数，并对无 provenance 的普通 Agent 调用 fail closed；
4. 更新 `command.execute.before → chat.message → parent/call ID → tool` 绑定及 tombstone/replay 防护；
5. session idle 分别评估 auto consolidate/compact，合并同时触发；
6. 将 `experimental.session.compacting` 从 prompt 替换 hook 改为强制拦截入口：取消 Harness 当前 compaction，创建 durable auto compact checkpoint；
7. 为 PCO 状态机发起的 native compact 增加 checkpoint/session/attempt 绑定的一次性 bypass token，防止 hook 递归；
8. 对 active checkpoint 持久化并恢复 `pending_compaction`，保证 manual consolidate 的原始 intent 不被覆盖；
9. Plugin 启动时从 durable status 恢复 intent、pending compaction、输入锁和下一步动作；
10. 继续通过 `experimental.chat.system.transform + currentContext cache` 让 consolidate 后的 context 立即生效。

完成条件：TypeScript loopback 覆盖两个手动命令、idle 自动 intent、Harness overflow 自动 compaction、直接 tool 拒绝、bypass 重放拒绝和重启恢复；真实锁定版 OpenCode 验证 hook 能阻止原操作。

### Phase 4：文档、安装与迁移

1. 更新 README、SKILL、命令清单、默认配置、MVP verification 与项目 review；
2. installer manifest 安装新增 command，并更新现有 managed 文件；
3. 对既有 workspace 执行 state/config/cursor migration；
4. 明确旧 completed checkpoint 的 `intent=compact` 兼容推断仅用于历史展示；active state 若无法安全判断则停止并要求迁移，不能猜测；
5. 更新 Harness 迁移文案为建议 `/consolidate` 而非 `/compact`。

完成条件：新装与 v0.3.1 升级均通过 package/CLI/Plugin smoke test。

## 13. 必需测试与验收矩阵

以下测试全部为 release blocker：

1. `/consolidate` 完成 canonical commit 和 context publication，但 native summarize/compact 调用次数为 0；
2. `/compact` 必须先完成 consolidate 和 publication，再且仅再调用一次 native summarize；
3. consolidate 后无新消息/source 变化再 compact：worker 调用 0 次、空 commit 0 个、Meta/continuation 重生成 0 次、native compact 1 次；
4. consolidate 后有新消息再 compact：frozen range 只包含 consolidation cursor 之后的增量；
5. auto consolidate 不触发 native compact；
6. auto compact 必须先 consolidate；若两阈值同时满足只产生一个 compact checkpoint；
7. Meta 审核暂停、批准/拒绝和进程恢复前后保留原始 intent；
8. context render/cache/publication 失败均阻止 compact；
9. native compact 失败后 `/pco-retry` 只重试 compact，content commit、worker 和 publication 不重复；
10. `/consolidate`、`/compact` 及其 control message/tool parts 都不进入用户证据；
11. 普通对话中直接调用 `pco_checkpoint`，即使伪造 trigger/intent 参数，也必须被拒绝；
12. Plugin 重启后能从 durable state 恢复 command intent、checkpoint status 和输入锁，旧的一次性 provenance 不可重放；
13. canonical commit 成功但 AFFiNE/index 失败时 compact 仍执行，receipt 保留 pending derivation；
14. publication 成功、compact 成功、receipt 插入失败后重试不得再次 compact；
15. source-only 变化会触发 consolidate；完全无增量才走 no-op；
16. intent 不参与认知 changeset fingerprint：同一 frozen boundary 的两种 intent 产生同一候选内容指纹；
17. `auto_compact.enabled=false` 时达到 context ratio 也不 compact，但 auto consolidate 仍可独立触发；
18. v0.3.1 workspace 升级后 cursor、历史 checkpoint、配置和 current context 均保持可读；
19. Harness 在 `session.idle` 检查前因上下文耗尽请求自动 compaction：原请求被取消，先创建 durable auto compact checkpoint，publish 成功后只放行一次 PCO native compact；
20. Harness compaction 到达时已有 manual consolidate：原始 consolidate intent 保留，durable `pending_compaction` 在 publication 后转为 no-op compact，两个操作间输入锁不释放；
21. Harness compaction 到达时正在等待 Meta 授权或 publication 失败：保持拦截且不得 native compact，授权/重试后按 durable pending request 续接；
22. PCO native compact 的 bypass token 只能匹配 checkpoint/session/attempt 使用一次；递归 hook、重放、错误 session 和 Plugin 重启后的旧 token均被拒绝；
23. `auto_compact.enabled=false` 只禁用 idle 阈值调度；Harness overflow compaction 仍被拦截并路由到 durable checkpoint。

建议分层落点：

- Python：`test_checkpoint.py`、`test_acceptance_flows.py`、`test_archive_sources.py`、`test_cli_integration.py`；
- Plugin loopback：`tests/opencode_question_loopback.ts` 及新的 consolidate/compact provenance fixture；
- Adapter：`test_opencode_adapter.py` 验证 native compact 调用、message filtering 与 context publication；
- schema/migration：新增 v0.3.1 fixture 驱动的 state/config upgrade 测试；
- package smoke：验证两个命令都安装，旧 workspace 可启动和恢复。

## 14. 发布门槛

v0.4.0 只有在以下条件同时满足时可发布：

- PRD v0.4.0、实现、schema、默认配置和用户文案语义一致；
- 上述 23 项 release-blocking 测试通过；
- 完整 Python suite、TypeScript loopback、安装/升级 smoke test 通过；
- 真实 OpenCode 环境分别验收 `/consolidate`、`/compact`、授权恢复、publication failure 和 compact-only retry；
- receipt 能让用户明确判断“记忆是否提交、context 是否发布、Harness 是否压缩”；
- 不存在无 Host provenance 的 `pco_checkpoint` 成功路径；
- 迁移文档不再建议为废弃旧 Harness session 而先做无必要的 compact。

## 15. 实施顺序中的关键风险

- **原生 compact 覆盖边界不可观测**：先确认 OpenCode native compact API/事件能否返回本次实际覆盖的 message boundary；若不能，`compaction_cursor` 必须记录“请求时已发布/冻结的 Host boundary + compact 成功回执”，并在 PRD 中标明它是确认边界而非推测 token 范围。
- **no-op 与审计记录冲突**：不创建空 canonical commit，但仍需 durable operation/receipt 记录本次 compact 尝试；该记录保存在 checkpoint state/receipt，若未来要求 canonical 审计，必须采用不改变认知 content fingerprint 的独立 audit commit 合同。
- **commit 后顺序变更**：现有 finalize 可能把 publication、compact、receipt 和 derivations 耦合；拆分时每个外部 side effect 前后都要持久化，以保证 crash recovery 恰好一次或安全幂等。
- **旧 cursor 语义不足**：历史数据只有 `archive_cursor` 和 `last_consolidated_message_id`；迁移必须通过 canonical checkpoint/message range 重建可证明的边界，无法证明的 compaction cursor 保留为 unknown，不伪造已压缩位置。
- **双自动阈值竞争**：idle hook、延迟 Host message 和 Plugin restart 可能造成 marker 竞争；必须以 durable active checkpoint + 单次 provenance/tombstone 双层去重。
- **Harness 自动 compaction 早于 idle hook**：`experimental.session.compacting` 必须是可取消的强制入口；所有外部 compaction 都先落 durable compact request。PCO 自己的 native compact 只能凭一次性、强绑定 bypass token 放行，避免递归与全局绕过窗口。
- **溢出时已有 consolidate**：不能覆盖用户原始 intent，也不能并发第二个 checkpoint；以 durable `pending_compaction` 串接 no-op compact，并跨授权、publication failure 和 Plugin restart 保持输入锁与恢复语义。

## 16. Review 修订计划（2026-08-22）

> 本节是对首次实现 review 的整改计划。以下问题在修复并通过新增验收前，均视为 v0.4.0 release blocker；不得以“Plugin 已生成 token”“pending 文件已存在”或“旧测试仍通过”代替真实 durable 恢复合同。

### 16.1 整改优先级与依赖顺序

按以下顺序实施，避免先修改 Plugin 而无法验证 Python durable 边界：

1. **Phase R1：统一 native compact 门禁协议**：先定义 Python Adapter、Plugin hook、bypass token 的请求/响应字段和一次性消费边界；完成后才能接入 `/compact`。
2. **Phase R2：修复 CheckpointEngine 的 active/pending 串接与恢复**：先让已有 consolidate、Meta approval、publication failure 都能接收 pending compact，再实现 Plugin 重试路由。
3. **Phase R3：修复 no-op、source-only、fingerprint 与状态机顺序**：补齐无增量、仅 source 变化和副作用失败的 Python 语义。
4. **Phase R4：配置迁移与文档/验收同步**：迁移行为明确后，再更新 schema、CLI、Plugin 提示和 release blocker 测试。

### 16.2 R1：一次性 bypass token 必须贯穿真实 native compact 调用（P1）

涉及文件：

- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/harness.py`
- `packages/pco/src/pco/cli.py`
- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `tests/test_opencode_adapter.py`
- `tests/opencode_question_loopback.ts`

修改合同：

1. 在进入 durable `NATIVE_COMPACT` 阶段前持久化 `native_compact_attempt_id` 和一次性 token 绑定信息（checkpoint ID、session ID、attempt ID、expiry）；token 未持久化成功不得调用 Harness。
2. `OpenCodeAdapter.compact(...)` 必须接收并传递私有 token/绑定 metadata 到实际 `/summarize` 请求；不能由 Python 先完成 summarize，再由 Plugin 事后 mint token。
3. Plugin 的 `experimental.session.compacting` hook 只对完全匹配的 token 放行一次，并在放行前将 token 标记 consumed；成功、失败、超时、异常和 Plugin 重启都必须退休 token。
4. Python 只有在收到 native compact 成功回执后推进 `compaction_cursor_after`、`compaction_status=completed`；hook 拦截、token 不匹配或请求失败都进入 `NATIVE_COMPACT` recovery，不得将 `compacted=true` 提前落盘。
5. 若当前锁定版 OpenCode API 无法把 token 传到 hook 或无法取消外部 compaction，停止宣称支持该 Harness，并保留 fail-closed；必须补充真实 loopback 证据后才可关闭该风险。

验收：同一个 `/compact` 只有一次真实 summarize；递归 hook、重放、错误 session/attempt、过期 token、重启后的旧 token 全部被拒绝；`/pco-retry` 只重试 native compact。

### 16.3 R2：active checkpoint 与 pending compaction 必须进入同一 durable 状态机（P1）

涉及文件：

- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/checkpoint/__init__.py`
- `packages/pco/src/pco/checkpoint/approval.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `tests/test_checkpoint.py`
- `tests/test_acceptance_flows.py`
- `tests/opencode_question_loopback.ts`

修改合同：

1. Plugin 收到 Harness compaction 时先生成带 request/event ID、session ID、boundary、origin 的 durable pending request，并通过受信 CLI/API 写入 Python `CheckpointState.pending_compaction`；仅写 Plugin 文件不能作为权威状态。
2. Python 返回 `CHECKPOINT_ACTIVE` 时，必须将 pending request 合并到当前 active checkpoint，而不是丢弃；重复 request 只追加审计关联，不创建第二个 active checkpoint。
3. active intent 为 `consolidate` 时保持原始 intent 不变；在 consolidate 成功 publication 后创建/进入 compact-only no-op 阶段，两个操作之间 input lock 不得释放。
4. `AWAITING_META_APPROVAL`、`COMMITTED_CONTEXT_PENDING`、`NATIVE_COMPACT` 和 Plugin 重启后都必须恢复 pending request；授权、publication 或 compact 失败不得清除 pending request。
5. `/pco-retry` 必须先读取 durable state：若 pending compact 尚未满足 publication 门禁，继续恢复原 checkpoint；若 publication 已成功，则只执行 compact-only 阶段；只有 native compact 成功、receipt 成功且 unlock 成功后才退休 pending request。
6. Plugin 不得因为一次 `CHECKPOINT_ACTIVE` 或一次 `pco_retry` 返回 consolidate receipt 就删除 pending 文件；删除动作必须以 Python durable state 明确确认 pending 已完成为条件。

验收：手动 consolidate 期间、Meta 审核期间、publication failure 期间和 Plugin 重启后触发 Harness compaction，都能最终只执行一次 native compact，原始 consolidate intent 保留，输入锁跨阶段保持。

### 16.4 R3：no-op compact 的 publication/hash/receipt/unlock 必须可恢复（P1）

涉及文件：

- `packages/pco/src/pco/checkpoint/__init__.py`
- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `tests/test_checkpoint.py`
- `tests/test_acceptance_flows.py`

修改合同：

1. no-op 只能复用最近一次成功 canonical commit 的 context；必须同时验证 `content_commit`、context bundle `content_hash`、`currentContext` cache metadata 和 publication receipt 的 hash/commit 互相对齐。
2. `context/current.json` 存在不等于 publication 成功。文件缺失、JSON 损坏、hash 不匹配、source commit 不匹配或上次 publication 状态为 pending/failed 时，必须重建并发布 context，成功后才允许 native compact。
3. no-op 在 receipt 插入、state save 或 unlock 任一步失败时，必须写入可重试状态（如 `RECOVERY`/`RECEIPT_INSERTED`/`INPUT_UNLOCKED` 对应边界），保留 input lock 和 `compaction_status`，不能先持久化为不可重试的 `DONE`。
4. `/pco-retry` 必须按 no-op 失败边界分别重试 publication、native compact、receipt 或 unlock；不能重新 spawn worker、创建 canonical transaction 或重复 native compact。
5. `/pco-abort` 仍只允许 canonical commit 前；no-op 若复用了已提交 content，必须按 commit 后 recovery 处理，不能通过 abort 清除锁。

验收：人为注入 stale/corrupt context、publication、compact、receipt、unlock 故障；每种故障都能恢复，且 worker/commit/native compact 的调用次数符合恰好一次合同。

### 16.5 R4：按 durable 状态机固定 derivations 与 native compact 顺序（P2）

涉及文件：

- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/derivations.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `tests/test_checkpoint.py`
- `tests/test_phase_c_derivations.py`

修改合同：

1. 普通 compact 的持久化顺序必须明确为 `CANONICAL_COMMIT → RENDER_AND_PUBLISH_CONTEXT → DERIVATIONS → NATIVE_COMPACT → RECEIPT → UNLOCK → DONE`；每个外部副作用前后保存状态。
2. derivation 失败不阻止 native compact，但必须先将 derivations 标记为 pending/failed，再进入 `NATIVE_COMPACT`；receipt 必须保留 pending derivations。
3. native compact 失败后只重试 `NATIVE_COMPACT`；derivation retry 不能重新调用 compact，compact retry 不能重新运行 worker、commit、publication 或 derivations。
4. consolidate intent 永远不进入 `NATIVE_COMPACT`，即使 derivations 失败也只完成可恢复的 memory/context/derivation receipt。

### 16.6 R5：旧 trigger_ratio 必须显式迁移，不得静默改变阈值（P1）

涉及文件：

- `packages/pco/src/pco/config.py`
- `packages/pco/src/pco/cli.py`
- `packages/pco/src/pco/resources/config/default.yaml`
- `packages/pco/src/pco/resources/profiles/pco/profile.yaml`
- `tests/test_cli_integration.py`
- 新增 `tests/test_config_migration.py`

修改合同：

1. 发现旧 `checkpoint.trigger_ratio` 且没有明确 `auto_consolidate`/`auto_compact` 时，配置加载必须返回结构化迁移错误或一次性、机器可检测的 migration notice；不能只发通常被隐藏的 `DeprecationWarning`。
2. 旧值不得被默认 `auto_compact.context_ratio=0.90` 静默覆盖，也不得自动解释成任一新阈值。迁移命令/提示应要求用户明确填写两个新字段。
3. 配置写回后必须删除旧 `trigger_ratio`，并验证 Profile 配置与应用配置一致；缺失、冲突或漂移都要在 `doctor`/启动阶段报告。
4. migration notice/error 必须出现在 CLI JSON 输出和文档中，并提供“先执行 `/consolidate`，再迁移配置”的建议。

### 16.7 R6：source-only、空边界与 canonical fingerprint（P2）

涉及文件：

- `packages/pco/src/pco/checkpoint/__init__.py`
- `packages/pco/src/pco/checkpoint/steps.py`
- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `tests/test_archive_sources.py`
- `tests/test_checkpoint.py`

修改合同：

1. `request()` 必须先归档/收集 source diff，再判断是否存在公开消息；不得在 source-only 场景先以 `last_archived_message_id is None` 失败。
2. 无公开消息但 source hash 变化时允许 `FREEZE_BOUNDARY → CONSOLIDATE`，message range 可为 `after=null, through=null` 或明确的 source-only boundary；worker 读取 source operations，成功后推进 consolidation cursor/source baseline。
3. 完全无公开增量且 source hash 未变化时才进入 no-op；没有历史成功 context 时不得 compact，必须返回明确的 `CONTEXT_BUNDLE_MISSING`/migration 错误。
4. canonical transaction fingerprint 只包含 frozen input、worker contract、Profile/policy version、source hashes 和稳定 message boundary；必须排除 checkpoint UUID、intent、trigger、attempt ID、时间戳和 audit receipt 字段。
5. 增加测试：同一 frozen fixture 以 consolidate/compact 两种 intent 运行，候选 content/transaction fingerprint 完全相同；不同 boundary 或 source hash 必须不同。

### 16.8 Review 修复后的新增 release blocker

在第 13 节原有 23 项之外，追加以下检查项：

24. `/compact` 的实际 summarize 请求携带匹配的 checkpoint/session/attempt bypass token；hook 放行一次后 token 立即退休。
25. Harness compaction 在 active consolidate、Meta approval、publication failure 和 Plugin restart 场景写入 Python durable `pending_compaction`，并在恢复后只执行一次 compact-only 阶段。
26. no-op 复用 context 前验证 content commit/context hash/cache metadata；receipt 或 unlock 失败后状态可由 `/pco-retry` 恢复，不能卡在 `DONE`。
27. derivations 的 durable 阶段先于 native compact；derivation failure 允许 compact，但 receipt 保留 pending derivations。
28. 旧 `trigger_ratio` 配置产生明确 migration error/notice，旧阈值不被静默忽略或映射。
29. 无归档对话但 source-only 变化可以执行 consolidate；完全无增量才 no-op。
30. 同一 frozen input 在 consolidate/compact 两种 intent 下产生相同 canonical transaction/content fingerprint。

只有第 24–30 项以及原第 1–23 项全部通过，且真实锁定版 OpenCode 完成 R1 token/cancel 合同验证后，才允许将 v0.4.0 标记为可发布。

## 17. Review 修订计划（二）（2026-08-22）

> 本节针对第二轮 review 追加整改。第 16 节的修复不能视为已完成，除非以下 durable pending compaction、派生恢复、token 隔离、全局 checkpoint 排序和 Schema 合同全部通过。

### 17.1 整改顺序与优先级

按以下顺序实施：

1. **P1-A：Harness pending compaction 写入 Python durable state**：先打通 Plugin → CLI → `CheckpointEngine` 的 request/event/boundary/origin 传递，确保 active checkpoint 不丢请求。
2. **P1-B：pending compaction 的 idle/restart 恢复**：修复 Plugin 的 early-return，使已存在的 pending request 必须进入 `/pco-retry` 或等价 compact-only 路径。
3. **P1-C：派生失败状态不可被覆盖**：修正 worker cleanup 与 index/backlinks/projection 的状态合并规则，保证 pending derivations 仍可重试。
4. **P1-D：native bypass token 严格隔离**：旧 token 必须按 checkpoint/session/attempt/expiry 校验，不能跨新 compact 复用。
5. **P2-A：全局选择最新成功 checkpoint**：按全局时间/提交顺序选择记录，不使用单一 checkpoint 内的 revision 作为全局排序。
6. **P2-B：统一 PendingCompaction Schema**：使 JSON Schema 与 Pydantic/Plugin 实际载荷一致，并增加 schema validation 测试。

### 17.2 P1-A：Harness compaction 请求必须进入 Python durable state

涉及文件：

- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `packages/pco/src/pco/cli.py`
- `packages/pco/src/pco/checkpoint/__init__.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `tests/opencode_question_loopback.ts`
- `tests/test_checkpoint.py`
- `tests/test_acceptance_flows.py`

修改合同：

1. `experimental.session.compacting` 拦截后，必须把 `request_id/event_id`、`session_id`、请求 boundary、`requested_at` 和 `origin=harness_auto_compaction` 作为结构化参数传给受信 CLI/Python；仅写 `pending-compaction.json` 不算持久化成功。
2. CLI 必须把该结构化请求传入 `CheckpointEngine.request(..., pending_compaction=...)`；Python 创建新 checkpoint 时写入 `CheckpointState.pending_compaction`，已有 active checkpoint 时调用 durable merge，而不是仅返回 `CHECKPOINT_ACTIVE`。
3. merge 必须保持原 active checkpoint 的 `intent` 和 trigger，不创建第二个 active checkpoint；重复 request 只追加 request/event 审计关联。
4. Python 写 state 成功前不得清除 Plugin pending 文件；Plugin 只能在 Python 返回明确的 durable acknowledgement 且 pending 已完成、receipt 已插入、input lock 已释放后退休本地文件。
5. pending request 的 schema、字段命名和时间单位在 Plugin、CLI、Pydantic、receipt、status 输出中统一；不得在边界层依赖隐式 camelCase/snake_case 猜测。

验收：active consolidate、Meta approval、publication failure 和重启期间触发 Harness overflow，request 均能在 Python state 中查到；原始 consolidate intent 保留，最终只执行一次 native compact。

### 17.3 P1-B：已存在 pending compaction 必须可由 idle/restart 恢复

涉及文件：

- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `tests/opencode_question_loopback.ts`
- `tests/test_checkpoint.py`

修改合同：

1. `session.idle` 不得在发现 `pendingCompaction` 后直接 return；该 guard 只能阻止并发 task，随后必须进入“读取 durable pending → sync → retry/resume”分支。
2. Plugin 重启恢复 pending 文件后，必须先向 Python 查询 durable state；若 Python state 已存在 pending，继续同一 checkpoint；若只存在 Plugin 文件，则先执行 durable import/attach API，成功后再 retry。
3. `/pco-retry` 返回 consolidate receipt 不能退休 pending；只有 compact-only 阶段成功、receipt 插入成功且 unlock 成功后才能删除本地 pending 文件。
4. retry 必须根据状态选择 publication、derivations、native compact 或 receipt/unlock 阶段，不能因一次 `CHECKPOINT_ACTIVE` 或早退而丢弃原 Harness request。

验收：Plugin 重启、一次处理失败、active consolidate、approval waiting 和 idle tick 竞争场景中，pending 文件最终都会进入 compact-only recovery，并且 native compact 恰好一次。

### 17.4 P1-C：派生失败不得被 worker cleanup 覆盖为 DONE

涉及文件：

- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/derivations.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `tests/test_phase_c_derivations.py`
- `tests/test_checkpoint.py`

修改合同：

1. `finalize` 必须以所有 derivation 结果的并集计算终态：只要 index、backlinks、projection 或 worker cleanup 任一项 `pending/failed`，终态必须保持 `COMMITTED_WITH_PENDING_DERIVATIONS`，不能被 cleanup 成功改写为 `DONE`。
2. worker cleanup 成功只能更新 `derivations.worker_cleanup`，不得覆盖其他派生项的失败状态；cleanup 失败也不能抹掉已存在的 index/backlinks/projection failure。
3. `/pco-retry` 在 `COMMITTED_WITH_PENDING_DERIVATIONS` 下必须可重试失败派生，不重复 worker、canonical commit、context publication 或 native compact；派生重试成功后才允许转 `DONE`。
4. receipt、checkpoint record 和 `/pco-status` 必须同时保留每个派生目标的 `ok/status/pending/error/attempts`，不得只依赖顶层终态判断。

验收：分别注入 affine、index、backlinks、projection、worker cleanup failure；第一次结果均可恢复，retry 后 derivation 恰好重试，native compact 不重复，最终状态与各项派生结果一致。

### 17.5 P1-D：native bypass token 不得跨 checkpoint/attempt 复用

涉及文件：

- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `packages/pco/src/pco/harness.py`
- `packages/pco/src/pco/cli.py`
- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `tests/test_opencode_adapter.py`
- `tests/opencode_question_loopback.ts`

修改合同：

1. `mintNativeCompactBypass` 复用已有 token 前必须同时匹配当前 checkpoint ID、session ID、durable `native_compact_attempt_id` 和未过期时间；任一不匹配都必须退休旧 token 并生成/要求新的 token。
2. `checkpointID=pending` 只能用于创建当前新 checkpoint 的一次性握手，Python 绑定真实 checkpoint 后，旧 token 不得再次作为另一个 checkpoint 的授权凭据。
3. 授权等待、abort、CLI 失败、超时、Plugin 重启和 session 切换都必须使未消费 token 失效；不得用“文件存在且未过期”作为唯一复用条件。
4. hook 放行前必须完成完整绑定比较并原子标记 consumed；错误 checkpoint/session/attempt、重放和跨操作 token 全部 fail closed。

验收：旧 compact 在 approval/abort/CLI failure 后启动新 compact，旧 token 均不能放行；同一 checkpoint 的同一 attempt 只允许一次 native compact。

### 17.6 P2-A：按全局时间/提交顺序选择最新成功 checkpoint

涉及文件：

- `packages/pco/src/pco/checkpoint/__init__.py`
- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `tests/test_checkpoint.py`

修改合同：

1. `_last_consolidation_source_hashes()` 与 `_latest_successful_checkpoint_payload()` 不得把每个 checkpoint 的 `revision` 当作全局排序键。
2. 候选记录必须按全局 `recorded_at`、canonical commit 顺序或 repository 提供的全局 append sequence 排序；排序键必须在同一 workspace 内稳定且可恢复。
3. 选择“最新成功”时只接受已 canonical commit 且 context publication 合同满足的记录；旧 checkpoint 后续 revision 不能遮蔽较新 checkpoint 的 revision 1。
4. 增加交错 fixture：旧 checkpoint 产生 revision 2，新 checkpoint 产生 revision 1，source baseline 和 reusable context 必须选择新 checkpoint。

### 17.7 P2-B：PendingCompaction JSON Schema 与运行时模型一致

涉及文件：

- `packages/pco/src/pco/resources/profiles/pco/schemas/checkpoint.schema.json`
- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `tests/test_config_migration.py`
- `tests/test_checkpoint.py`

修改合同：

1. `requested_boundary` 必须允许 `string | object | null`，与 Pydantic `str | dict[str, Any] | None` 一致；object 需限制为可序列化 JSON object。
2. `requested_at` 必须允许 Plugin/Python 实际使用的数值 epoch milliseconds；若同时兼容 ISO date-time，需明确两者的迁移与规范化规则。
3. `request_id`、`event_id`、`session_id`、`origin` 的必填/可选性必须与 `PendingCompaction` 模型和 durable state 一致。
4. 使用真实 Plugin 载荷、Python model dump 和 canonical checkpoint record 分别做 schema validation，避免只验证手写字符串 fixture。

### 17.8 第二轮 review 新增 release blockers

在第 16 节的 24–30 项之外追加：

31. Harness overflow compaction 的 request/event/boundary/session/origin 已写入 Python durable `pending_compaction`，不是只写 Plugin 文件。
32. Plugin restart/idle tick 不会因已有 `pendingCompaction` 直接 return；pending 最终进入 `/pco-retry` compact-only recovery。
33. 任一 derivation 失败时状态保持 `COMMITTED_WITH_PENDING_DERIVATIONS`，worker cleanup 成功不能覆盖为 `DONE`；retry 后不重复不可逆阶段。
34. approval、abort、CLI failure、session 切换和 checkpoint 切换后，旧 native bypass token 全部拒绝；匹配 token 只消费一次。
35. 交错 checkpoint revision fixture 始终选择全局最新成功 checkpoint，不因 revision 数值较大选中旧记录。
36. 实际 `PendingCompaction` payload 通过 checkpoint JSON Schema，包含 null boundary 与 epoch millisecond `requested_at`。

只有第 31–36 项、24–30 项和原第 1–23 项全部通过，且完整 Python suite、Plugin loopback、真实锁定版 OpenCode token/cancel 测试均通过，才允许将 v0.4.0 标记为可发布。

## 18. Review 修订计划（三）（2026-08-23）

本轮 review 表明第 17 节的测试覆盖已经通过，但仍有四个会影响真实 compact 恢复、source baseline 和 receipt 一致性的 release blocker。整改顺序固定为：先修复 native compact 的 provenance 边界，再修复 no-op 的 source baseline，最后修复 receipt 的二次持久化，并为每项补充回归测试。

### 18.1 整改优先级与依赖顺序

1. **P1-A：新 compact 必须生成新的 checkpoint/attempt 绑定**。不得从已完成 checkpoint 的 status 结果继承 ID 或 attempt。
2. **P1-B：bypass token 的 expiry 是强绑定字段**。缺失、类型错误、过期或与存储值不一致均必须 fail closed。
3. **P1-C：no-op 必须持久化真实 source hash baseline**。后续相同 source/message 输入必须继续命中 no-op。
4. **P2-A：worker cleanup 与 receipt 一致性**。本项的旧“cleanup 后覆盖磁盘 receipt”方案已由第 21 节取代：cleanup 必须先于 final Host receipt，磁盘 receipt、checkpoint record、durable state、Host receipt 和返回值必须一致。

P1-A 与 P1-B 必须先完成后再验证真实 native compact；P1-C 与 P2-A 可并行实现，但必须在完整回归前合并验证。

### 18.2 P1-A：每次新 compact 重新生成 checkpoint 与 attempt 绑定

涉及文件：

- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `packages/pco/src/pco/checkpoint/__init__.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/harness.py`
- `tests/test_opencode_adapter.py`
- `tests/opencode_question_loopback.ts`

问题定义：

- Plugin 在 `invokeWithNativeCompactBypass()` 中查询 `checkpoint status` 后，当前只要拿到 checkpoint ID 就直接复用；即使该 checkpoint 已经是 `DONE` 或 `COMMITTED_WITH_PENDING_DERIVATIONS`，也会把它作为新 compact 的绑定。
- 新 `/compact` 随后会创建新的 active checkpoint，但 Python adapter 会按新 checkpoint 校验 token，导致 token 在 `/summarize` 前因 checkpoint/attempt mismatch 失败。

修改合同：

1. 查询 status 后只有在 checkpoint 处于当前可继续的 active compact 状态、且其 `intent=compact`、`compaction_status=pending`、`native_compact_attempt_id` 与本次操作仍一致时，才允许沿用 durable attempt。
2. 对 `DONE`、`COMMITTED_WITH_PENDING_DERIVATIONS`、`ABORTED`、`RECOVERY` 等终态或非当前 compact 状态，必须生成新的本地 attempt，并把 checkpoint 绑定设为 `pending`；由 Python 创建新 checkpoint 后再将 token 绑定到新 ID。
3. Python 创建新 checkpoint 时必须把本次 attempt 写入 `native_compact_attempt_id`；adapter 在实际 summarize 前必须以 active durable state 的真实 checkpoint ID、session ID、attempt ID 做最终校验。
4. 不得仅因 `checkpoint status` 返回一个 ID 就把它视作新请求的 identity；新 compact 的 checkpoint identity 必须来自本次 request 创建/合并结果。
5. 新请求成功后旧 checkpoint 的 token、attempt 和 pending marker 均不得影响下一次 compact；相同 session 的连续两次 `/compact` 必须拥有两个独立的 checkpoint/attempt 绑定。

验收：

- 在一个 compact 已完成后立即发起第二个 `/compact`，第二次 native summarize 成功且使用新的 checkpoint/attempt。
- 已完成 checkpoint、active consolidate、failed compact、approval 后 retry 四种状态分别发起新 compact，均不得复用错误 identity。
- loopback 记录每次 request 的 token/checkpoint/attempt，断言连续 compact 的绑定不相同且每个 token 只消费一次。

### 18.3 P1-B：bypass token 缺失 expiry 必须 fail closed

涉及文件：

- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `packages/pco/src/pco/harness.py`
- `tests/opencode_question_loopback.ts`
- `tests/test_opencode_adapter.py`

修改合同：

1. `compactionTokenFromInput()` 只有在 token、checkpoint ID、session ID、attempt ID、expiry 全部存在且类型正确时，才返回候选 token；`expiresAt=undefined`、`null`、NaN、非正数或非整数必须返回无效。
2. `consumeNativeCompactBypass()` 的比较必须要求 candidate expiry 与 durable token expiry 精确相等且当前时间未超过 expiry；不得使用“candidate 未提供 expiry 即跳过比较”的兼容分支。
3. durable token、CLI 参数和 hook payload 的 expiry 单位统一为 epoch milliseconds；Python/Plugin 边界不得隐式转换为秒或 ISO 字符串。
4. expiry 缺失或不一致时必须拒绝并保留/记录 fail-closed 结果，不得设置 `allow_once`，也不得把不完整 token 写回 durable state。
5. token 的 identity 比较仍必须同时包含 checkpoint ID、session ID、attempt ID；expiry 是额外的强绑定字段，不可替代其他 provenance。

验收：

- 删除 expiry、使用 `null`、字符串、过期值、错误 expiry 和正确 expiry 五种 payload，只有最后一种可以一次性放行。
- 正确 token 第一次放行后，原 payload 重放必须拒绝；修改任一 identity 或 expiry 也必须拒绝。

### 18.4 P1-C：no-op compact 必须保存真实 source hash baseline

涉及文件：

- `packages/pco/src/pco/checkpoint/__init__.py`
- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `tests/test_checkpoint.py`
- `tests/test_archive_sources.py`

问题定义：

- no-op 分支当前只写入 `consolidation_source_hashes`，但 checkpoint record 的 `source_hashes` 仍为空；下一次 request 会把当前 source hash 与 `{}` 比较，错误地进入 source-only consolidate。

修改合同：

1. 进入 no-op compact 前，`source_probe.source_hashes` 必须写入 `state.source_hashes`；`state.consolidation_source_hashes` 同时保留本次 canonical consolidation baseline。
2. no-op 的 checkpoint receipt、checkpoint record、durable state 和 thread baseline 必须携带同一组 source hashes；不得以空字典作为“无 source 变化”的持久化值。
3. 下一次 request 的 source comparison 必须读取最近成功 checkpoint 持久化的真实 `source_hashes`；当消息和 source 都未变化时继续走 no-op，不得启动 worker、创建 transaction 或提交 canonical content。
4. 当 source hash 确实变化而没有新增公开消息时，仍必须走 source-only consolidate；该路径必须把新 source hashes 写入新的成功 baseline。
5. no-op 复用 context 的 hash/publication 校验继续作为硬门禁；修复 baseline 不得绕过 context bundle、receipt 和 content hash 对齐检查。

验收：

- 已注册 source 的 workspace 连续执行两次无消息 compact：第一次 no-op 后第二次仍为 no-op，worker/transaction/canonical commit 次数不增加。
- 仅修改 source hash 后执行 compact：进入 source-only consolidate，并在成功后保存新 hash；随后重复请求再次命中 no-op。
- 无 source、无消息的空 workspace 仍保持原有 no-op/空边界合同。

### 18.5 P2-A：worker cleanup 成功后重新持久化 receipt

> 已被第 21.2 节修订：本节保留 review 历史，但不得按“先发布 receipt、cleanup 后仅覆盖磁盘文件”实施。最终合同以 cleanup 先于 final Host receipt 和 unlock 为准。

涉及文件：

- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `tests/test_checkpoint.py`
- `tests/test_phase_c_derivations.py`

修改合同：

1. 普通 worker finalize 在 cleanup 前写入的 receipt 可以暂时包含 `worker_cleanup.pending=true`，但 cleanup 成功后必须基于最新 state 重新生成 receipt 并覆盖 `state/checkpoints/<id>/receipt.json`。
2. cleanup 成功后的 receipt、checkpoint record、`/pco-status` 和 finalize 返回值必须同时反映 `worker_cleanup.ok=true`、`pending=false`、完整 attempts 及最终 `DONE`/pending derivation 状态。
3. cleanup 失败时仍需持久化最新失败 attempt 与 recovery 信息；不能用旧 receipt 覆盖更完整的 durable failure state。
4. receipt 重写必须是幂等的：重复 retry 不得重复插入 receipt、重复 native compact 或增加无意义 checkpoint revision。
5. receipt 持久化失败必须进入可恢复状态，不能先返回 DONE 或删除 pending/recovery marker。

验收：

- 注入 worker cleanup failure，确认磁盘 receipt 保留失败 attempt；修复 cleanup 后 retry，确认磁盘 receipt 与 state/record 同步为成功。
- 注入 index/backlinks/projection failure 且 cleanup 成功，确认 receipt 保留其他派生失败，不被错误改写为全量 DONE。
- 对已完成 retry 再次执行 retry，receipt 内容、canonical revision 和 native compact 次数均不变化。

### 18.6 第三轮 review 新增 release blocker

在第 16 节的 24–30 项、第 17 节的 31–36 项之外追加：

37. 已完成 checkpoint 后发起的新 compact 必须生成新的 checkpoint/attempt；连续 compact 不得因 status 复用终态 identity。
38. native bypass token 缺失或不一致的 expiry 必须 fail closed；expiry 与 checkpoint/session/attempt 一样属于强绑定字段。
39. no-op compact 必须把真实 source hashes 写入 runtime `state.source_hashes`、receipt 和 thread baseline；根据第 20 节修订合同，no-op 不创建或更新 canonical checkpoint record。重复 source/message 输入不得误触发 worker。
40. worker cleanup 结果必须与 durable state、checkpoint record、Host/磁盘 receipt 和返回值一致；根据第 21 节修订合同，cleanup 必须先于首次 final receipt，失败 attempt 与重试 generation 均须可追溯。

只有第 37–40 项、第 31–36 项、第 24–30 项和原第 1–23 项全部通过，且完整 Python suite、Plugin loopback、连续 compact、source-only/no-op、receipt recovery 与真实锁定版 OpenCode token/cancel 测试均通过，才允许将 v0.4.0 标记为可发布。

## 19. Review 修订计划（四）（2026-08-23）

本轮 review 发现三类 cursor 虽然已经出现在 checkpoint state 中，但 compaction 的 durable runtime baseline 仍错误地复用了 `ThreadState.last_consolidated_message_id`。这会使“先 consolidate 到 A，再 compact 到 B”被记录成压缩前 cursor=A，并在进程重启或下一个 checkpoint 创建时丢失真正的 native compact 进度。本节将 compaction cursor 定义为独立、可恢复的 thread runtime state；在整改完成前，该问题视为 v0.4.0 release blocker。

### 19.1 P1：在 thread durable runtime state 中持久化独立 compaction cursor

涉及文件：

- `packages/pco/src/pco/workspace.py`
- `packages/pco/src/pco/checkpoint/__init__.py`
- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/resources/profiles/pco/schemas/checkpoint.schema.json`
- `tests/test_checkpoint.py`
- `tests/test_opencode_adapter.py`

#### 问题定义

- `ThreadState.last_consolidated_message_id` 只表示最近一次成功 canonical consolidation 的边界，不表示 Harness 已经成功完成 native compact 的边界。
- 创建 compact checkpoint 时，`compaction_cursor_before` 不得从 `last_consolidated_message_id` 初始化；它必须读取 thread durable runtime state 中最近一次成功 native compact 的 cursor。
- native compact 成功后，当前 checkpoint state 的 `compaction_cursor_after` 不能只停留在 checkpoint 目录；它必须推进同一 workspace/thread 的 durable compaction cursor，供重启后的下一个 checkpoint 使用。

#### 修改合同

1. 在 `ThreadState` 增加独立字段 `compaction_cursor: str | None`（或等价的明确命名），并持久化到 `thread.json`；`last_consolidated_message_id` 继续只承担 consolidation cursor 语义，禁止作为 compaction cursor 的隐式别名。
2. `prepare_candidate` 创建 checkpoint 时分别读取：
   - `consolidation_cursor_before = thread.last_consolidated_message_id`；
   - `compaction_cursor_before = thread.compaction_cursor`。
   两者必须在 receipt、checkpoint record、status 和恢复日志中保持可区分。
3. `commit_and_finalize` 在 canonical commit 成功时只能推进 `last_consolidated_message_id`/consolidation cursor，不得提前写入或重置 `thread.compaction_cursor`；`/consolidate` 完成也不得改变 compaction cursor。
4. 只有 adapter 收到并验证 native compact 成功回执后，才允许将本次已确认的 compact 覆盖边界写入 `state.compaction_cursor_after`，并推进 `thread.compaction_cursor`。token 被拦截、缺失/错误、超时、取消或 native compact 失败时，compaction cursor 必须保持原值。
5. 推进值必须来自本次 durable compact checkpoint 的 frozen/confirmed boundary（通常为 `state.through_message_id`；若 Host 能提供实际 compact boundary，则使用 Host 回执中的边界），不得从当前最新消息或 `last_consolidated_message_id` 推测。若无法证明历史 compact 的实际边界，迁移后保留 `null/unknown`，不得伪造已压缩位置。
6. native compact 成功与 cursor 推进必须具备可恢复的持久化顺序：先记录本次成功的 checkpoint/attempt/boundary，再提交 thread cursor；任一步崩溃后，retry/reconciliation 必须依据 durable success receipt 幂等补齐 cursor，不能重复 native compact，也不能把 cursor 回退到旧值。
7. cursor 推进必须单调且按已确认的 message order 校验；旧 checkpoint 的重试、过期 attempt、重复 receipt 和较早 boundary 不得覆盖较新的 compaction cursor。consolidation cursor 可以继续独立前进，但不得反向修改 compaction cursor。
8. 对历史 `thread.json` 和 checkpoint：缺少新字段时默认 `compaction_cursor=null`；只有存在 `compaction_status=completed`、成功 receipt/context publication 和明确 `compaction_cursor_after` 的记录，才允许通过一次性迁移重建该值。无法证明的记录必须保持 unknown，并在 status/迁移诊断中报告。
9. Plugin/CLI 重启恢复、`/pco-retry` 和新 checkpoint 创建都必须从 thread durable cursor 读取 baseline，而不是从内存缓存、上一个 checkpoint 的 revision 或 `last_consolidated_message_id` 读取。

#### 必须覆盖的时序

```text
初始：consolidation=A，compaction=旧值 C
/consolidate：consolidation → A，compaction 仍为 C
继续对话到 B 后 /compact：compaction_cursor_before=C
canonical commit/publish/derivations/native compact 成功
最终：consolidation=B，compaction=B
进程重启并创建下一个 checkpoint：compaction_cursor_before=B
```

若 compact 在 native 阶段失败，最终状态必须保留 `compaction_cursor_before=C`、`compaction_cursor_after=null`（或未完成状态），retry 成功后只推进一次到 B；若之后再次执行 `/consolidate`，仍不得将 thread compaction cursor 改回 A/B 的 consolidation 值。

### 19.2 回归测试与验收

新增或调整以下测试：

1. **独立 baseline**：先 consolidate 到 A，再追加消息到 B 并 compact；断言 `consolidation_cursor_before=A`、`compaction_cursor_before` 为 compact 前 thread cursor，而不是 A。
2. **成功推进**：native compact 成功后，断言 `thread.json`、runtime checkpoint state、receipt 和 `/pco-status` 的 `compaction_cursor_after` 一致并指向 B。根据第 22 节修订合同，canonical checkpoint record 不保存 compaction cursor。
3. **失败不推进**：注入 hook 拦截、错误 token、native failure、取消和超时，断言 thread compaction cursor 保持旧值；retry 成功后只推进一次，重复 retry 不产生第二次 native compact。
4. **重启恢复**：compact 成功后模拟进程重启并创建新 checkpoint，断言新 checkpoint 从 `thread.compaction_cursor=B` 初始化；consolidate 成功后该字段仍为 B。
5. **崩溃 reconciliation**：分别模拟 native success 后 checkpoint/thread 写入之间的崩溃，验证 recovery 能根据成功 receipt 补齐 cursor；模拟 cursor 已推进后重复恢复，验证不会回退或重复副作用。
6. **迁移**：旧 `thread.json` 只有 `last_consolidated_message_id` 时不得自动把该值当成 compaction cursor；有可证明成功 compact receipt 的历史记录时才重建，否则显示 unknown 并保持 fail-closed。
7. **交错 checkpoint**：旧 checkpoint revision 较高、新 checkpoint revision 较低时，cursor 仍按全局成功提交/确认顺序恢复，不得被旧 checkpoint 覆盖。

### 19.3 第四轮 review 新增 release blockers

在第 16 节的 24–30 项、第 17 节的 31–36 项和第 18 节的 37–40 项之外追加：

41. `ThreadState`/thread durable runtime state 持久化独立 `compaction_cursor`；任何路径不得用 `last_consolidated_message_id` 代替它。
42. 只有 native compact 成功回执之后才推进 compaction cursor；compact 失败、拦截、取消、过期 token 和重试前状态均不得推进。
43. 进程重启、创建后续 checkpoint、receipt recovery 和重复 retry 都能从 durable compaction cursor 恢复，且不会回退 cursor、重复 native compact 或丢失已确认的 compact boundary。

只有第 41–43 项、第 37–40 项、第 31–36 项、第 24–30 项和原第 1–23 项全部通过，并完成独立 cursor 时序、失败恢复、迁移和真实 OpenCode native compact 回执测试，才允许将 v0.4.0 标记为可发布。

## 20. Review 修订计划（五）（2026-08-23）

本轮 review 发现 pending compaction 的串接仍存在输入锁空窗，同时 no-op compact 虽不运行 worker/content transaction，却仍通过 `write_checkpoint_record()` 产生 canonical audit transaction。两者分别破坏冻结边界和 no-op 的“无 canonical transaction”合同。本节把“锁的所有权交接”和“runtime operation 与 canonical checkpoint record 分离”列为独立整改项；在完成前均视为 v0.4.0 release blocker。

### 20.1 P1：pending compact 前不得释放输入锁

涉及文件：

- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/harness.py`
- `tests/test_checkpoint.py`
- `tests/test_opencode_adapter.py`

#### 问题定义

当前 `finalize_after_derivations()` 无条件调用 `_finish_receipt_and_unlock()`；随后 `resume_pending_compaction()` 发现 state 已 terminal 或 `input_unlocked=true`，才重新锁定并进入 compact-only tail。这形成真实的 unlock → relock 窗口。窗口内到达的 user message 可能进入 Harness 当前上下文，但不属于刚完成 consolidation 的 frozen boundary，使即将执行的 native compact 与 durable message/cursor 边界不一致。

#### 修改合同

1. finalize 在执行任何 unlock side effect 前重新读取 durable state；若 `pending_compaction != null`，必须把输入锁的所有权直接交给 compact tail，不调用 `adapter.unlock_input()`，也不把 `input_unlocked` 写成 `true`。
2. 将“receipt 插入”“terminal result 持久化”和“input unlock”拆成可独立调用的幂等步骤。建议把 `_finish_receipt_and_unlock()` 拆为：
   - `persist_or_insert_receipt(...)`；
   - `transition_to_compact_tail_without_unlock(...)`；
   - `unlock_and_finish(...)`。
   pending 路径只能执行前两项，最后一项必须等 native compact 及其最终 receipt 成功后执行。
3. consolidate 部分可以生成阶段性 receipt，但不能把 checkpoint 暴露为“已完成且可继续输入”。用户可见状态必须明确为 `CONSOLIDATION_COMMITTED_COMPACTION_PENDING`（或等价 durable 状态），`/pco-status` 仍显示 active/locked。
4. `resume_pending_compaction()` 不应通过把 `input_unlocked=true` 改回 `false` 来弥补先前 unlock。正常串接路径必须证明从 `INPUT_LOCKED` 到 native compact 完成之间从未调用 unlock。该分支只可作为旧 state/recovery 兼容，并应 fail closed 地重新确认/恢复 Host lock。
5. pending request 可能在 finalize 读取前后并发到达，因此“检查 pending → 决定 unlock”必须与 durable checkpoint lock/状态更新位于同一个受保护临界区。若 Host hook 在 unlock side effect 已开始后才登记 pending，请求处理必须等待并重新建立一个新的已确认 lock 后才能继续；不得假定写入 `input_unlocked=false` 等同于 Host 已锁定。
6. worker cleanup、derivation pending、阶段性 receipt 插入或 canonical record 写入均不得隐式释放输入锁。只有 compact tail 已完成（或 compact 前被允许且安全地 abort）后，统一执行一次最终 unlock。
7. crash recovery 必须区分“durable 标记 locked”与“Host lock 已确认”。Plugin/adapter 重启后先恢复实际 Host lock，再恢复 pending compact；native compact 前必须有可验证的 lock confirmation。

#### 必须覆盖的时序

```text
manual /consolidate（INPUT_LOCKED）
→ Harness overflow 合并 pending_compaction
→ canonical commit
→ context publish
→ derivations / consolidation receipt
→ 不 unlock，直接进入 compact-only tail
→ native compact
→ final receipt
→ unlock exactly once
```

任何失败路径（Meta 等待、publication failure、native compact failure、receipt failure、Plugin restart）中，只要 pending compact 尚未退休，输入都必须保持或恢复为 confirmed locked。

### 20.2 P2：no-op compact 不得创建 canonical transaction

涉及文件：

- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/resources/profiles/pco/schemas/checkpoint.schema.json`
- `tests/test_checkpoint.py`
- `tests/test_acceptance_flows.py`

#### 问题定义

`finalize_noop_compact()` 复用 `finalize_after_derivations()`，后者无条件调用 `write_checkpoint_record()`。因此没有新增 message/source 的 compact 仍向 canonical `checkpoints` stream append record，并产生 audit transaction/commit。即使该 transaction 不包含认知内容，它仍违反本计划第 7 节“不创建 transaction 或空 canonical checkpoint/content commit”的合同，并导致连续 compact 无意义地增长 Git 历史。

#### 修改合同

1. no-op checkpoint/compact 的所有可恢复信息只写入 runtime durable state：`state/checkpoints/<id>/state.json`、`active-checkpoint.json`、receipt 与必要的 Host provenance/tombstone；不得调用 `TransactionManager.begin/commit()`，不得 append canonical `checkpoints` record，也不得推进 Git HEAD。
2. `finalize_after_derivations()` 增加明确的 canonical-record policy，或拆成 content-checkpoint 与 runtime-only compact 两条 finalize tail。是否写 canonical record必须由“本次确有 canonical consolidation transaction/record”决定，不能仅依据 `intent=compact`、`content_commit` 非空或复用了旧 commit。
3. no-op state 引用最近成功 consolidation 的 `content_commit`、context hash、source hashes 和 consolidation cursor，但这些字段只是 provenance reference；不得把复用的旧 commit误报为本次新 commit，也不得创建新的 `audit_transaction_id/audit_commit`。
4. receipt 必须明确区分：
   - `consolidation.status = no_op`；
   - `consolidation.content_commit = <reused commit>`；
   - `canonical_transaction.created = false`（或等价机器字段）；
   - `compaction.status = completed|failed`。
   `/pco-status` 和 retry 以 runtime state 为权威恢复 no-op compact，不要求 canonical checkpoint record 存在。
5. `write_checkpoint_record()` 自身增加 fail-closed guard：若 `consolidation_status == no_op` 或 state 没有本次 consolidation transaction ID，则拒绝/跳过 canonical 写入。不能只依赖调用方记得绕开。
6. pending compact 串接在已有 consolidate checkpoint 后，也不得通过修改原 canonical checkpoint record 来审计 compact-only tail。原 consolidation record保持其已提交事实；pending/no-op compact attempt、bypass token、失败与 receipt 留在 runtime state。若未来产品要求永久审计每次 Harness compact，必须另行设计不属于 canonical memory Git history 的 operation log，不能复用 canonical checkpoint stream。
7. no-op native compact 失败后的 `/pco-retry` 只读取 runtime state并重试 native compact；不得因缺少 canonical record而重跑 worker、创建空 transaction，或把上次复用的 content commit重新提交。
8. runtime state 的保留/清理策略必须覆盖 crash recovery 与诊断：至少在 receipt 完成及 pending request 退休前不可清理；completed runtime attempts 可按独立 retention policy 清理，但不得影响 canonical memory 或 compaction cursor 恢复。

### 20.3 修正顺序

1. 先拆分 finalize 的 receipt、unlock、canonical-record 和 compact-tail职责，建立显式 policy 参数/独立函数，避免通过状态布尔值隐式分支。
2. 再实现 pending path 的 lock handoff，并用 adapter spy 验证调用序列中不存在 unlock/relock。
3. 将 no-op path 切到 runtime-only finalizer，并给 `write_checkpoint_record()` 增加防误用 guard。
4. 更新 receipt/schema/status，使 runtime-only no-op 不依赖 canonical checkpoint record仍可完整显示和恢复。
5. 最后补齐 crash injection、Plugin restart、连续 compact 和 Git HEAD 不变测试，再运行完整 Python suite 与 OpenCode loopback。

### 20.4 回归测试与验收

新增或调整以下测试：

1. **无锁空窗**：consolidate 期间合并 pending compaction，记录 adapter lock/unlock/compact 事件；断言 `lock → ... → compact → ... → unlock`，compact 前 unlock 调用次数为 0，整个操作最终只 unlock 一次。
2. **消息竞态**：在原 `_finish_receipt_and_unlock()` 边界注入 pending request/user message，断言 pending 注册与 unlock 决策受同一临界区保护，user message 不会进入本次 compact boundary。
3. **授权与 publication failure**：pending compact 在 Meta 等待或 publication failure 时保持 confirmed lock；授权/重试后直接续接 compact，不出现 terminal/unlocked 中间态。
4. **重启恢复锁**：在 consolidation receipt 后、native compact 前崩溃；重启后先确认 Host lock，再继续 compact，最终只执行一次 native compact 和一次 unlock。
5. **no-op Git 不变**：成功 consolidate 后记录 Git HEAD、canonical checkpoint record 数和 transaction 数；连续执行一次或多次 no-op compact 后三者均不变。
6. **no-op 可观察性**：虽然没有 canonical record，runtime state、receipt 和 `/pco-status` 仍包含复用 commit/context、真实 source hashes、cursor 和 compaction outcome，并明确 `canonical_transaction.created=false`。
7. **no-op compact-only retry**：注入 native failure后确认 Git HEAD 不变；`/pco-retry` 只再次调用 native compact，worker、derivations、content/audit transaction 调用次数均为 0。
8. **防误用 guard**：直接以 `consolidation_status=no_op` 调用 `write_checkpoint_record()`，断言不会创建 transaction/commit；缺少本次 transaction ID 的伪 committed state同样 fail closed。
9. **pending 串接不改 canonical record**：已有 consolidate canonical record后合并 pending compact；compact 完成只更新 runtime receipt/state，不 append该 record的新 revision，不产生 audit commit。
10. **cursor 保持**：no-op native compact 只有成功后才按第 19 节合同推进 thread compaction cursor；失败及锁恢复期间不推进，且无需 canonical no-op record参与恢复。

### 20.5 第五轮 review 新增 release blockers

在第 19 节的 41–43 项之外追加：

44. active consolidate 串接 `pending_compaction` 时，从首次 lock 到 native compact/final receipt 完成之间不得调用 unlock；最终 unlock exactly once。
45. pending 注册与 unlock 决策必须并发安全；Meta 等待、publication failure、receipt failure和进程重启均不得产生未确认锁状态下的 native compact。
46. no-op compact 只产生 runtime durable state/receipt，不创建 canonical checkpoint record、transaction、audit commit或 Git commit；连续 no-op compact 不增长 Git 历史。
47. no-op native compact failure/retry 不依赖 canonical record，只重试 compact side effect，并保持 worker、derivation和所有 canonical transaction计数为零。
48. pending compact 串接不得修改既有 consolidate canonical record；compact attempt 的审计与恢复信息留在 runtime operation state。

只有第 44–48 项、第 41–43 项、第 37–40 项、第 31–36 项、第 24–30 项和原第 1–23 项全部通过，并完成 lock handoff 并发/崩溃测试、no-op Git 不变测试、runtime-only retry 和真实 OpenCode 输入锁验收，才允许将 v0.4.0 标记为可发布。

## 21. Review 修订计划（六）（2026-08-23）

本轮 review 发现 receipt 仍早于最终可观察状态发布：一是 Harness compaction request 可能在 Host receipt 插入后才被合并，导致 Host 永久看到 consolidate-only receipt；二是 worker cleanup 在 receipt 插入和输入解锁后运行，只重写磁盘 receipt，无法修正已经发布到 Host 的旧结果。本节将 receipt 收束为最终状态的单一发布边界，并为 pending compaction 合并定义明确的线性化点。

### 21.1 P1：关闭 pending 合并窗口后再发布最终 receipt

涉及文件：

- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `packages/pco/src/pco/harness.py`
- `tests/test_checkpoint.py`
- `tests/test_opencode_adapter.py`
- `tests/opencode_question_loopback.ts`

#### 问题定义

当前 `_finish_receipt_and_unlock()` 先检查 `state.receipt_inserted` 并调用 `adapter.insert_receipt()`，之后才重新读取 durable pending state。若 Harness compaction request 恰好在两步之间合并，系统会保留输入锁并完成 compact tail，但 `receipt_inserted=true` 使后续流程跳过 Host receipt 插入。磁盘 receipt 虽可被覆盖，Host-visible receipt 仍错误地声称只完成 consolidate。

#### 修改合同

1. 为 active checkpoint 增加 durable pending-merge lifecycle，例如：

   ```text
   pending_acceptance: open | closed
   receipt_generation: integer
   host_receipt_generation: integer | null
   receipt_kind: intermediate | final
   ```

   `pending_acceptance=open` 时 Harness request 可合并；finalizer 只有在持有同一个 checkpoint operation mutex/lock 时才可将其原子关闭。
2. 最终 receipt 的线性化顺序固定为：

   ```text
   acquire checkpoint operation lock
   → reload durable pending state
   → 若有 pending：保持 acceptance/open 并转入 compact tail，不插入 final receipt
   → 若无 pending：persist pending_acceptance=closed + final outcome/generation
   → release operation lock
   → insert exactly that final receipt generation
   → persist host_receipt_generation
   → unlock
   ```

   pending 检查必须发生在 receipt 插入之前；不能再采用“先插入，后重读”的顺序。
3. Plugin 的 Harness auto-compaction handler 合并 request 时必须取得同一 operation lock并检查 `pending_acceptance`：
   - `open`：合并到当前 checkpoint；
   - `closed`：不得修改已发布/待发布 final outcome，必须创建新的 durable compact checkpoint/attempt；
   - 不允许在 `closed` 后把 `pending_compaction` 写回旧 checkpoint。
4. 正常路径只发布一次 final receipt，不再先发布 consolidate-only receipt再尝试覆盖。若产品需要显示进度，应使用 status/progress event，不得把阶段性消息标记为 final receipt或设置 `receipt_inserted=true`。
5. 为 crash recovery 保留 receipt generation：如果 final outcome 已持久化但 Host 插入前崩溃，retry 插入同一 generation；如果 Host 插入成功但 `host_receipt_generation` 保存前崩溃，adapter 必须使用稳定幂等键 `checkpoint_id + receipt_generation` 去重。
6. 对已有/异常 state（`receipt_inserted=true` 但后来存在未完成 pending compaction）提供修复路径：使旧 generation 失效，compact tail 完成后生成更高 generation 并重新插入。Host adapter 若支持 replace/update，应替换旧 receipt；若只支持 insert，则新 receipt 必须明确引用并 supersede 旧 generation，且状态/UI以最高 generation 为准。
7. `receipt_inserted` 单一布尔值不足以表达重发和崩溃边界，应迁移为 generation-aware 状态；旧 state 的 `true/false` 只能映射为 generation 0 的兼容输入，不能阻止需要发布的新 final generation。
8. input unlock 必须晚于 `host_receipt_generation == receipt_generation` 的 durable确认；若 receipt 插入失败，保持可恢复状态和输入锁，pending acceptance保持 closed，防止新 request被错误合并到已经冻结的 outcome。

### 21.2 P2：worker cleanup 必须先于最终 receipt 与 unlock

涉及文件：

- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/derivations.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `tests/test_checkpoint.py`
- `tests/test_acceptance_flows.py`

#### 问题定义

当前 `finalize_after_derivations()` 先把 `worker_cleanup` 标为 pending，执行 canonical record/native compact/receipt/unlock，随后才真正调用 `cleanup_worker()`。cleanup 改变 derivation 与 terminal status 后，只覆盖本地 `receipt.json`；已经由 `adapter.insert_receipt()` 发布的 Host receipt不会变化，因此正常成功路径也会向用户显示虚假的 `worker_cleanup.pending=true`。

#### 修改合同

1. worker cleanup 从 receipt 后置补丁改为 finalize 的必经前置阶段。所有 worker-backed checkpoint 的顺序调整为：

   ```text
   context publication
   → replaceable derivations
   → worker cleanup attempt
   → persist final derivation outcomes
   → canonical checkpoint record（仅非 no-op，且仅记录 consolidation）
   → native compact（compact intent）
   → pending acceptance linearization
   → final receipt
   → unlock
   ```

2. cleanup 成功时，在生成 canonical record、receipt和 terminal status前持久化 `worker_cleanup.ok=true, pending=false`；最终 Host receipt、磁盘 receipt、state、canonical record和返回值必须来自同一 state snapshot。
3. cleanup 失败仍按可替换派生失败处理：持久化完整 structured error/attempt history，标记 `pending=true`，允许已满足 publication 门禁的 compact继续，并最终返回 `COMMITTED_WITH_PENDING_DERIVATIONS`。失败不是再次提前发布 receipt的理由。
4. cleanup failure 的后续 `/pco-retry`/derivation retry 可以产生新的最终状态，但不得静默改写已发布 receipt。若 retry 后需要向 Host告知结果，应生成更高 `receipt_generation`并以 replace/supersede 语义发布；磁盘覆盖本身不算完成用户通知。
5. cleanup 调用必须幂等并有 durable attempt boundary：调用前持久化 attempt ID/status，成功或失败后持久化 outcome。崩溃后恢复不得关闭错误 worker、重复创建 worker，或丢失已成功 cleanup 的证据。
6. pending compaction 在 cleanup 期间到达时可按第 21.1 节合并，但不打断 cleanup。cleanup 完成后重新读取 durable pending state，再决定进入 compact tail还是关闭 pending acceptance并发布 final receipt。
7. no-op compact 没有 worker，必须跳过 cleanup，同时继续遵守第 20 节 runtime-only/canonical Git不变合同。
8. 删除“先插入 receipt，cleanup 后只重写磁盘文件”的流程和注释；本地 `receipt.json` 只能保存已发布 generation或待发布 generation，不能被当作 Host receipt替代物。

### 21.3 Finalize 状态快照与 receipt 一致性

最终 receipt 必须由一个不可变的 finalized snapshot生成。snapshot 至少绑定：

```text
checkpoint_id
receipt_generation
trigger / intent / pending origin
consolidation status + content commit
context publication status + hash
all derivation outcomes including worker_cleanup
compaction requested/status/attempt/cursors
canonical transaction created/audit commit
final checkpoint status
```

canonical checkpoint record、runtime receipt和Host receipt可以因职责不同省略字段；根据第22节，canonical record只比较consolidation allowlist内的共同字段，compact/receipt runtime字段不得进入canonical record。同一runtime generation的Host receipt、磁盘receipt、state和返回值必须来自同一snapshot。final receipt生成后，任何会改变对应共同字段的操作都必须：

- 在 pending acceptance关闭前被纳入当前 snapshot；或
- 作为后续独立 operation/retry生成更高 receipt generation，并显式 supersede旧结果。

不得只覆盖磁盘 receipt来模拟 Host结果已更新。

### 21.4 修正顺序

1. 先引入 operation mutex下的 `pending_acceptance` 关闭协议和 receipt generation schema。
2. 将 worker cleanup移动到 final receipt/unlock之前，删除 cleanup后的本地 receipt补写路径。
3. 重排 finalize：完成 derivations/cleanup/native side effect后，在锁内重读 pending并决定串接或关闭 acceptance。
4. 为 Host receipt增加稳定幂等键以及 replace/supersede能力；补齐旧 `receipt_inserted` state迁移。
5. 更新 `/pco-status`、receipt schema与 recovery，使其展示当前/已发布 generation及 pending acceptance。
6. 加入确定性的 race barrier与 crash injection测试，再执行完整 Python suite、Plugin loopback和真实 OpenCode receipt验收。

### 21.5 回归测试与验收

新增或调整以下测试：

1. **late pending race**：在 finalizer 重读 pending前注入 Harness request；断言不插入 consolidate-only final receipt，保持锁并完成 compact，Host最终只看到 compact-completed receipt。
2. **closed 后请求**：在 `pending_acceptance=closed` 后注入 Harness request；断言旧 checkpoint/receipt不变，请求创建新的 durable compact checkpoint而不是回写旧 state。
3. **legacy stale receipt recovery**：构造 `receipt_inserted=true + pending_compaction未完成` 的旧 state；完成 compact后必须发布更高 generation，旧 Host receipt被替换或明确 supersede。
4. **receipt crash幂等**：分别在 final snapshot持久化后、Host insert后、`host_receipt_generation` 保存前崩溃；retry最多产生一个逻辑 final receipt generation且不重复 compact/unlock。
5. **cleanup success ordering**：worker checkpoint的 adapter事件必须为 `cleanup_worker → insert_receipt → unlock`；Host receipt、磁盘 receipt、state、record和返回值均显示 cleanup成功。
6. **cleanup failure ordering**：cleanup失败先持久化 structured outcome，再执行允许的 compact和final receipt；Host receipt直接显示 pending derivation与 `COMMITTED_WITH_PENDING_DERIVATIONS`，不得先显示临时 pending再静默覆盖磁盘。
7. **cleanup retry notification**：cleanup后续重试成功时产生更高 receipt generation并替换/supersede旧失败 receipt；不得只修改本地文件。
8. **pending during cleanup**：cleanup运行时合并 Harness request；cleanup结束后重读到 pending，保持锁并进入 compact tail，最终 receipt同时包含cleanup最终结果和compact结果。
9. **no-op不回归**：runtime-only no-op compact跳过worker cleanup，仍不创建canonical transaction/record，receipt generation与compact-only retry正常工作。
10. **共同字段一致性**：对每个最终generation断言Host receipt、磁盘receipt、runtime state及返回结果的runtime共同字段一致；canonical record只与它们比较第22节consolidation allowlist中的字段，并断言不存在compact/receipt字段。

### 21.6 第六轮 review 新增 release blockers

在第 20 节的 44–48 项之外追加：

49. pending compaction 的最终合并判定必须先于 final receipt插入，并与关闭 pending acceptance共享同一线性化锁；关闭后到达的请求必须创建新 durable compact checkpoint。
50. Host receipt必须支持 generation幂等及 replace/supersede；`receipt_inserted` 布尔值不得阻止 late pending compact完成后的正确最终 receipt发布。
51. 所有 worker cleanup attempt必须在 final receipt和input unlock之前完成并持久化；成功或失败结果首次发布时即为最终可观察结果。
52. cleanup retry导致用户可见状态变化时必须发布更高 receipt generation，不能仅覆盖本地 `receipt.json`。
53. 同一 receipt generation内，Host receipt、磁盘 receipt、runtime state和返回值的共同字段必须来自同一 finalized runtime snapshot；canonical record仅对第 22 节定义的 consolidation 字段保持一致，不得混入 compact/receipt runtime结果。

只有第 49–53 项、第 44–48 项、第 41–43 项、第 37–40 项、第 31–36 项、第 24–30 项和原第 1–23 项全部通过，并完成 late-pending线性化、receipt generation崩溃恢复、cleanup顺序和真实 OpenCode receipt replace/supersede验收，才允许将 v0.4.0 标记为可发布。

## 22. Review 修订计划（七）（2026-08-24）

本轮 review 暴露了两个仍未彻底分层的持久化边界：canonical checkpoint record 在 native compact 之前写入，却包含尚未确定的 compact/receipt runtime字段；OpenCode Adapter 虽把 receipt key放进 toast metadata，却没有任何读取、去重或替换协议，无法覆盖“Host 已显示、ack 尚未持久化即崩溃”的经典双写窗口。本节分别冻结 canonical consolidation schema和Host receipt幂等投递合同。

### 22.1 P2：canonical checkpoint record 只审计 consolidation

涉及文件：

- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/resources/profiles/pco/schemas/checkpoint.schema.json`
- `tests/test_checkpoint.py`
- `tests/test_acceptance_flows.py`

#### 问题定义

`finalize_after_derivations()` 在 `_run_native_compact()` 前调用 `write_checkpoint_record()`，但 `_checkpoint_payload()` 同时序列化 `compaction_status`、compaction cursor、native attempt、pending state和receipt generation。正常 `/compact` 因而会把 `pending` 永久写进 canonical stream，而最终 runtime/receipt已经是 `completed`。继续在 compact 后修订 canonical record又会让每次 Harness side effect增长 Git历史，并与第 20 节 runtime-only合同冲突。

#### 决策

采用“canonical record只审计 consolidation”的方案，不在 native compact后追加修订。canonical memory描述认知提交及其证据边界；Harness compaction、pending request、receipt投递和Host重试属于runtime operation state。

#### Canonical allowlist

canonical checkpoint payload只允许包含可在 consolidation 完成边界确定的字段：

```text
checkpoint_id / thread_id / harness_binding_id / parent_session_id
trigger / requested_intent              # 请求 provenance；不是 compact结果
archive_cursor
consolidation_cursor_before / consolidation_cursor_after
message_range / source_hashes / consolidation_source_hashes
worker identity/runtime（不含 cleanup/Host side-effect状态）
transaction_id / content_commit
operation_counts / proposal hashes
approval decision与authorization provenance
protected streams / meta revision / continuation revision
profile/policy/workflow/skill versions
consolidation derivation snapshot（仅在该canonical合同确需永久审计时）
started_at / consolidation_committed_at
```

以下字段必须从 canonical checkpoint schema和 `_checkpoint_payload()` 删除：

```text
compaction_requested / compaction_status / compaction_origin
pending_compaction / pending_acceptance
native_compact_attempt_id
compaction_cursor_before / compaction_cursor_after
receipt_generation / host_receipt_generation / receipt_kind / receipt_key
input lock/unlock、retry与Host投递状态
任何在 canonical record写入后才可能变化的 compact-tail outcome
```

`intent=compact` 可以作为原始请求事实保留，但不得被解释为“compaction completed”。canonical schema/字段命名使用 `requested_intent` 更清晰；如为兼容保留 `intent`，文档必须明确它只表示请求。

#### 修改合同

1. 为 canonical record建立显式字段 allowlist，禁止从整个 `CheckpointState` 投影或用 `extra` 接收未来runtime字段。
2. `write_checkpoint_record()` 只接收/构建不可变的 `ConsolidationRecordSnapshot`，不接收会继续变化的 live state，防止以后再次误加compact字段。
3. canonical write的时点固定在consolidation commit及所需canonical derivation结果确定后；native compact前后都不得为了同步runtime结果修订该record。
4. runtime state、receipt和`/pco-status`继续完整显示compact status、cursor、attempt、pending和receipt generation。查询层不得用canonical record推断当前compact结果。
5. no-op compact仍不创建canonical record。带新增材料的 `/compact` 只创建一次consolidation record；其后native success/failure/retry均不增加该record revision或Git commit。
6. 已写入compact runtime字段的v0.4开发期record需要schema迁移策略：读取时忽略deprecated字段，重建/验证时只比较allowlist；不得通过新Git rewrite删除历史。尚未发布的fixture可直接升级schema。
7. 第21.3节的“一致性”限定为各存储层职责内一致：canonical与runtime只比较consolidation共同字段；compact/receipt字段仅比较runtime state、Host/磁盘 receipt和返回结果。

### 22.2 P2：Host receipt 必须使用真正幂等的写入原语

涉及文件：

- `packages/pco/src/pco/harness.py`
- `packages/pco/src/pco/checkpoint/finalize.py`
- `packages/pco/src/pco/checkpoint/recovery.py`
- `packages/pco/src/pco/checkpoint/state.py`
- `packages/pco/src/pco/resources/opencode/plugins/pco.ts`
- `tests/test_opencode_adapter.py`
- `tests/opencode_question_loopback.ts`

#### 问题定义

当前 `insert_receipt()` 调用 `/tui/show-toast`，仅在metadata附加 `pco_receipt_key`。如果toast已经显示，而进程在保存`host_receipt_generation`前崩溃，retry无法查询toast是否存在，也没有消费方按key去重，必然再次显示。metadata不是幂等性；本地“发送前标记已发送”又会在相反崩溃窗口丢通知，不能解决双写问题。

#### 必需能力

Adapter必须提供下列至少一种Host端原子语义：

1. **Idempotent create/put**：以`receipt_key = checkpoint_id:receipt_generation`作为Host资源ID；重复put返回同一资源且不重复展示。
2. **Upsert/replace**：按稳定key查询并创建或更新同一个Host message/notification；较高generation可原子supersede较低generation。
3. **Host/plugin durable inbox + UI dedup**：Plugin先将keyed receipt持久化到Host可恢复存储，UI消费时按key只展示一次并持久化ack；不能只靠进程内Set。

单纯toast POST、metadata、进程内去重、本地发送前/发送后布尔标记均不满足合同。若锁定版本OpenCode没有上述原语，v0.4.0必须改用可寻址的session control message/part或实现Plugin持久化receipt inbox；在能力完成前不得把toast作为final receipt transport，也不得宣称exactly-once用户通知。

#### Adapter 合同

将`insert_receipt(receipt)`升级为类似：

```text
publish_receipt(
  key,
  generation,
  payload,
  supersedes_key?
) -> {
  host_resource_id,
  key,
  generation,
  disposition: created | existing | replaced,
  payload_hash
}
```

要求：

1. 相同key和相同payload hash重复调用返回`existing`，不产生第二次可见通知。
2. 相同key但payload hash不同fail closed，防止同一generation承载两个结果。
3. 更高generation通过`supersedes_key`替换或明确撤销旧结果；乱序到达的低generation不得覆盖高generation。
4. finalizer只有验证Host返回的key、generation、resource ID和payload hash后，才能持久化`host_receipt_generation`并unlock。
5. recovery在ack未知时安全重放同一key；Host保证返回existing而不是重复展示。
6. Plugin/Host重启后幂等索引仍存在；清理策略不得早于checkpoint runtime retention和所有可能retry窗口。
7. OpenCode capability探测必须在安装/启动时确认transport支持幂等/upsert；不满足时fail closed并给出结构化错误，不静默退回toast。

### 22.3 Receipt outbox 与崩溃恢复

runtime state为每个generation保存durable outbox entry：

```text
receipt_key
generation
payload_hash
payload_path或不可变payload
supersedes_key
delivery_status: pending | acknowledged
host_resource_id
host_disposition
created_at / acknowledged_at
attempts
```

顺序固定为：

```text
persist immutable finalized snapshot + outbox(pending)
→ Host publish_receipt(idempotency key)
→ verify ack/hash/resource
→ persist outbox(acknowledged) + host_receipt_generation
→ unlock
```

崩溃点语义：

- outbox写入前：没有Host side effect，重新生成同一final snapshot；
- outbox写入后、Host调用前：重放同一key；
- Host已创建、ack返回/本地保存前：重放同一key，Host返回`existing`，不重复展示；
- ack持久化后、unlock前：只恢复unlock；
- higher generation supersede途中：旧receipt保持可识别，retry幂等完成replace/supersede。

### 22.4 修正顺序

1. 先从canonical schema/payload移除所有compact-tail与receipt字段，并引入`ConsolidationRecordSnapshot` allowlist。
2. 更新查询、migration和测试，证明canonical record不再承担compact状态来源。
3. 调研锁定版OpenCode可寻址message/notification能力；选定idempotent put/upsert或Plugin durable inbox方案并建立最小loopback。
4. 实现generation-keyed receipt outbox和Adapter ack合同，移除final receipt的裸toast POST路径。
5. 接入replace/supersede、Plugin restart及所有崩溃点恢复。
6. 运行完整Python suite、Plugin loopback，并在真实OpenCode中验证同key重放不会出现第二条通知。

### 22.5 回归测试与验收

新增或调整以下测试：

1. **canonical字段隔离**：`/compact`的canonical record schema中不存在compaction status/origin/attempt/cursors、pending state和receipt generation/key。
2. **正常compact一致性**：canonical record仅显示`requested_intent=compact + consolidation committed`；runtime/receipt显示compaction completed，不存在canonical pending结果可供误读。
3. **compact failure/retry不改Git**：记录consolidation canonical commit/revision后注入native failure并retry成功；Git HEAD和checkpoint canonical revision保持不变。
4. **no-op继续无record**：连续no-op compact既不创建record，也不因receipt generation更新而产生Git commit。
5. **schema拒绝runtime字段**：向`ConsolidationRecordSnapshot`或canonical schema注入compact/receipt字段必须失败，避免未来回归。
6. **Host相同key重放**：连续两次publish相同key/hash，Host只出现一个逻辑receipt，第二次返回`existing`及同一resource ID。
7. **Host key冲突**：相同key、不同payload hash必须拒绝，不得覆盖或新增通知。
8. **ack保存前崩溃**：Host创建成功后注入崩溃；retry重放同一key并取得existing，用户只看到一次通知。
9. **supersede乱序**：generation N+1替换N后重放N，Host仍显示N+1；restart后规则不变。
10. **outbox恢复矩阵**：覆盖pending前、publish前、Host成功/ack保存前、ack后/unlock前四个崩溃点，断言无丢失、无重复、无额外compact/unlock。
11. **跨Plugin重启幂等**：重启后相同receipt key仍命中Host/plugin durable索引，不依赖内存Set。
12. **capability fail closed**：Host只有不可查询toast时，启动或final receipt阶段返回结构化不支持错误并保持可恢复状态，不发送非幂等toast。

### 22.6 第七轮 review 新增 release blockers

在第21节的49–53项之外追加：

54. canonical checkpoint record只包含consolidation allowlist；compact-tail、pending、cursor outcome和receipt delivery字段不得进入canonical schema或payload。
55. native compact success/failure/retry不得修订consolidation canonical record或推进Git历史；compact结果以runtime state/receipt为权威。
56. Host receipt transport必须按`checkpoint_id:generation`提供跨进程持久的幂等create/upsert；metadata或进程内去重不算满足。
57. Host已创建receipt但本地ack尚未保存时崩溃，retry必须返回同一Host resource且不产生第二次用户通知。
58. receipt generation supersede必须抵抗乱序、重放和Plugin重启；相同key不同payload必须fail closed。

只有第54–58项、第49–53项、第44–48项、第41–43项、第37–40项、第31–36项、第24–30项和原第1–23项全部通过，并完成canonical allowlist/schema测试、receipt outbox全崩溃矩阵、跨重启幂等和真实OpenCode Host通知验收，才允许将v0.4.0标记为可发布。
