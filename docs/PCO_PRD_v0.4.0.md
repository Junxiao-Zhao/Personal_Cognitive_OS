# PCO 个人认知操作系统 PRD

> 文档版本：v0.4.0  
> 状态：Consolidate/Compact 语义冻结合同  
> 修订日期：2026-08-22  
> 历史基线：[PCO_PRD_v0.3.1.md](PCO_PRD_v0.3.1.md)

本文件是 v0.3.1 的派生版本。v0.3.1 保留为历史基线，不随本版本修改。v0.4.0 只冻结产品、状态、信任边界、receipt 和迁移合同；实现是否满足这些合同须由对应测试与真实 Harness 验证。

## 1. 产品主循环

PCO 将公开对话、用户授权来源、结构化记忆、Meta-memory 和 continuation 保存为本地 append-only canonical memory。每个 checkpoint 使用一个 durable `CheckpointEngine`，但把“记忆提交”和“Harness 上下文压缩”作为两个有先后约束的责任：

```text
公开消息/授权来源
→ archive
→ freeze boundary
→ consolidate
→ validate / Meta approval（如有）
→ canonical commit
→ render and publish currentContext
→ derivations（失败可 pending）
→ native compact（仅 intent=compact）
→ receipt / unlock
```

`consolidate` 是一次 memory checkpoint：归档增量公开对话、运行 worker、校验并提交 canonical memory、更新 Meta-memory、发布最新 system context，但不调用 Harness native compact。`compact` 包含同一 consolidate 流程，且只有 context publication 成功后才调用一次 Harness native compact。

因此：

- `/consolidate` 的成功提示必须明确“记忆已更新，对话上下文未压缩”；
- `/compact` 的成功提示必须明确“记忆已更新，对话上下文已压缩”；
- compact 不能绕过 archive、freeze、worker、授权、commit 或 context publication；
- consolidate 后短期保留原始对话与 Meta-memory 的重复是允许的；compact 后由已发布 context、最近未压缩消息和按需检索维持连续性。

## 2. 范围与不变约束

v0.4.0 保留一个 `CheckpointEngine`、一个 active PCO checkpoint 和一个 active Harness binding。不建立独立的 ConsolidateEngine 或 CompactEngine，不把 `intent`/`trigger` 暴露为 Agent 可控的 `pco_checkpoint` 参数。

以下合同保持不变：canonical memory append-only；Git transaction 原子提交；Meta-memory 受保护并需要 Host 产生的一次性授权；worker 输出不是用户证据；control message、tool-call parts 和 worker 日志不得进入用户证据；canonical commit 后派生失败不得回滚记忆。

## 3. 术语与请求合同

| 字段 | 合法值 | 含义 |
| --- | --- | --- |
| `trigger` | `manual` 或 `auto` | 请求来源，不描述行为 |
| `intent` | `consolidate` 或 `compact` | 请求要完成的行为 |
| `origin` | `command`、`idle_threshold` 或 `harness_auto_compaction` | 可选的自动/命令来源审计信息 |

典型 durable 请求：

```json
{
  "trigger": "manual",
  "intent": "compact",
  "origin": "command"
}
```

| 入口 | `trigger` | `intent` |
| --- | --- | --- |
| `/consolidate` | `manual` | `consolidate` |
| `/compact` | `manual` | `compact` |
| 新增公开消息达到阈值 | `auto` | `consolidate` |
| 上下文达到安全阈值 | `auto` | `compact` |
| Harness 自发 overflow compaction | `auto` | `compact` |

原始 `trigger`、`intent`、来源和失败边界必须写入 durable state、canonical checkpoint record、receipt，并在重启、Meta 审核和 retry 中保留。相同 frozen input、worker contract、策略版本和 source hashes 下，`intent` 不得改变 canonical changeset/content fingerprint；它只决定请求审计和可选 Harness side effect。

## 4. Host provenance 与控制边界

手动命令必须由 Host 建立一次性、session-bound、带 expiry 的 provenance：

```text
slash command
→ command.execute.before 登记 intent
→ chat.message 绑定 Host message ID
→ assistant parent / tool-call ID 绑定该 message
→ tool.execute.before 消费 provenance
→ Plugin 传入已验证 trigger + intent
```

命令 markdown 只要求 Agent 无参数调用 `pco_checkpoint`。Plugin 必须拒绝或忽略模型提供的 `trigger`/`intent`，不能用默认 `manual` 补全缺失 provenance。普通对话中没有合法 command/auto provenance 的 checkpoint 调用必须 fail closed。

自动 marker 必须绑定 session、nonce、Host message、parent/call ID、intent 和 expiry，一次消费后退休并保留有界 tombstone。阈值只在 `session.idle` 检查；同一 idle tick 同时满足两个阈值时只调度一个 `compact`，因为 compact 已包含 consolidate。

Harness 自发的自动 compaction 是独立入口，不得只替换 summary prompt 后放行。PCO 必须先拦截并取消外部 compaction，创建 durable `{trigger:auto, intent:compact, origin:harness_auto_compaction}` 请求；只有状态机进入 `NATIVE_COMPACT` 后，才用与 checkpoint/session/attempt 绑定的一次性 bypass token 放行一次 PCO native compact。缺失、过期、重放或绑定不匹配的 token 继续按外部请求拦截。

`/consolidate`、`/compact` 的 slash message、`[PCO_CONTROL]`、native compact 控制消息及相关 tool-call parts 都是 control provenance，不是用户证据。过滤依据是 Host metadata/ID 绑定，不能只依赖文本前缀。

## 5. Checkpoint 状态机

逻辑阶段为：

```text
SYNC_AND_ARCHIVE
→ FREEZE_BOUNDARY
→ CONSOLIDATE
→ VALIDATE
→ META_APPROVAL（可选）
→ CANONICAL_COMMIT
→ RENDER_AND_PUBLISH_CONTEXT
→ DERIVATIONS
→ NATIVE_COMPACT（仅 intent=compact）
→ DONE
```

durable state 至少要能区分：

- consolidate 是否 pending、no-op 或 committed；
- canonical memory 是否提交；
- context 是否 pending、completed 或 failed；
- derivations 是否完成或 pending；
- compaction 是否未请求、pending、completed 或 failed；
- receipt 是否插入；
- input lock 是否释放。

建议字段：`trigger`、`intent`、`consolidation_status`、`context_publication_status`、`compaction_requested`、`compaction_status`、`compaction_origin`、`pending_compaction`、`native_compact_attempt_id`，以及下节的三个 cursor。旧 `compacted: bool` 只能作为迁移输入，不能继续承担恢复语义。

若 active checkpoint 为 `consolidate` 而 Harness 同时请求 compact，必须保留原始 intent，持久化 `pending_compaction`，在 publication 成功后进入 no-op compact；两个操作之间不得释放输入锁。等待 Meta approval、publication 失败或 Plugin 重启也必须保留该请求。

## 6. Cursor 与冻结边界

三个 cursor 具有互不替代的语义：

- `archive_cursor`：Harness 公开消息已逐 turn 持久归档到的位置；
- `consolidation_cursor`：公开归档中已进入成功 canonical consolidate 的位置；旧 `last_consolidated_message_id` 映射到此字段；
- `compaction_cursor`：Harness 已确认完成 native compact 的位置；无法观测实际覆盖范围时，只记录请求时的 Host boundary，并标明它是确认边界而非推测 token 范围。

公开 turn 归档成功只推进 `archive_cursor`；canonical commit 成功才推进 `consolidation_cursor`；native compact 成功才推进 `compaction_cursor`。后续 `/compact` 只冻结 `consolidation_cursor` 之后的公开增量，不重复处理已经 consolidate 的消息。

没有新增公开消息时，source 的规范化内容 hash 相对最近成功 consolidate 发生变化，仍须运行 consolidate。只有“无新增公开材料且 source hashes 未变化”才允许 no-op。

紧跟成功 consolidate 的 no-op compact 不 spawn/resume worker，不创建空 canonical commit，不重生成 Meta/continuation；它复用 hash 对齐且已成功发布的 context，记录 durable compact operation/receipt，并只调用一次 native compact。若 context 缺失、未发布或 hash 不匹配，必须先重建并发布。

## 7. 配置与迁移

v0.4.0 用两个独立阈值替换单一 `checkpoint.trigger_ratio`：

```yaml
checkpoint:
  auto_consolidate:
    enabled: true
    new_public_tokens: 32768
  auto_compact:
    enabled: true
    context_ratio: 0.90
```

`auto_consolidate.new_public_tokens` 从上次成功 consolidation cursor 之后统计公开 user/assistant 内容；control、tool reasoning 不计入，累计基线必须可重启恢复。`auto_compact.context_ratio` 使用 Harness 当前上下文占用估算，默认 `0.90`。`auto_compact.enabled=false` 只关闭 PCO 的 idle 提前调度，不能让 Harness overflow compaction 绕过 PCO 门禁。

旧 `trigger_ratio` 不得静默解释成任一新阈值。升级时必须给出明确迁移错误或一次性迁移提示，并同时校验 profile 与应用配置，避免默认值漂移。

迁移前建议执行 `/consolidate`，确保最新公开消息、来源变化、canonical memory 和 continuation 已提交并发布；不再把“为迁移先执行 `/compact`”作为默认建议。旧 completed checkpoint 的 `intent=compact` 兼容推断只用于历史展示；active state 若无法安全判断，必须停止并要求迁移，不能猜测。无法证明旧 `compaction_cursor` 的历史数据保持 `unknown`，不伪造已压缩位置。

## 8. Receipt、状态和恢复

compact receipt 的机器 payload 至少为：

```json
{
  "trigger": "manual",
  "intent": "compact",
  "consolidation": {"status": "committed", "content_commit": "..."},
  "context_publication": {"status": "completed", "content_hash": "..."},
  "compaction": {"requested": true, "status": "completed"}
}
```

consolidate receipt 至少为：

```json
{
  "trigger": "manual",
  "intent": "consolidate",
  "consolidation": {"status": "committed", "content_commit": "..."},
  "context_publication": {"status": "completed"},
  "compaction": {"requested": false, "status": "not_requested"}
}
```

机器 schema 使用 `not_requested`；UI 可以显示“未请求/未执行”，不得把它写成请求后跳过。no-op compact 使用 `consolidation.status=no_op`，并引用复用的 `content_commit` 与 context hash。用户必须能从 receipt/status 明确判断：记忆是否提交、context 是否发布、Harness 是否压缩。

恢复合同：

| 边界 | 允许的 retry 行为 |
| --- | --- |
| archive/freeze/worker/validate 失败 | 从最近安全边界继续同一 consolidate |
| Meta approval 等待 | 恢复同一 proposal 的授权流程，保留原 intent |
| canonical commit 前 abort | 仅清理未提交事务并解锁；可创建新 checkpoint |
| canonical commit 成功但 state save 临界失败 | 从 Git/canonical record 恢复 provenance，禁止重复 commit |
| derivation 失败 | 保留 canonical commit，独立重试 pending derivations；不阻断 compact |
| render/publication 失败 | 只重试 publication；成功后按原 intent 继续 |
| native compact 失败 | 只重试 native compact；不重跑 worker、consolidate、commit、publication |
| receipt 插入失败 | 只重试 receipt/final unlock；不得再次 compact |

`/pco-status` 展示 durable checkpoint 的原始 `trigger`/`intent`、三个 cursor、consolidation、publication、derivation、compaction、receipt 和 input-lock 状态；读取不改变状态。`/pco-retry` 只能沿失败阶段恢复。`/pco-abort` 仅在 canonical commit 前有效；commit 后即使 native compact 失败也不能标记 aborted 或回滚记忆。

## 9. 安装命令清单

OpenCode 安装清单由 `packages/pco/src/pco/resources/opencode/commands/` 中的受管命令模板构成，并由安装器写入 `.opencode/.pco-managed.json`。v0.4.0 的命令语义为：

| 命令 | 作用 |
| --- | --- |
| `/consolidate` | memory checkpoint；发布 context，不做 native compact |
| `/compact` | consolidate（含 no-op 复用）成功并 publication 后，做一次 native compact |
| `/pco-status` | 只读展示同一 durable checkpoint 的恢复状态 |
| `/pco-retry` | 从最近持久化失败边界恢复，不重复已成功不可逆阶段 |
| `/pco-abort` | 只中止尚未 canonical commit 的 checkpoint |

命令模板必须要求无参数调用 PCO control tool；Agent 不能自行调用 native compact、伪造 provenance、提交 approval 或把 command/control 文本当作证据。安装升级只可清理上一份 manifest 授权且仍匹配 PCO hash 的受管文件；用户自有 command 不得被扫描或删除。

## 10. 发布验收

以下是 v0.4.0 的文档级 release blockers，须由实现测试覆盖：

1. `/consolidate` 完成 commit 和 publication，native compact 次数为 0；`/compact` 在 publication 后且仅调用一次 native compact。
2. consolidate 后无新增消息/source 变化再 compact 时 worker、空 commit、Meta/continuation 重生成均为 0。
3. auto consolidate 不触发 native compact；auto compact 先 consolidate；两个阈值同 tick 只产生一个 compact checkpoint。
4. 普通对话直接调用 `pco_checkpoint`，包括伪造字段，必须拒绝；旧 marker、错误 parent/session、重放 token 必须拒绝。
5. publication 失败阻止 native compact；derivation 失败不阻止 compact；native compact 失败的 retry 不重复 commit 或 publication。
6. 同一 frozen boundary 的 consolidate/compact canonical content fingerprint 相同；control message/tool parts 不进入用户证据。
7. v0.3.1 workspace 的 state/config/cursor/current context 可读；无法证明的旧 compact cursor 保持 unknown；迁移提示推荐 `/consolidate`。
8. Harness overflow compaction 被取消并路由到 durable compact checkpoint；只有匹配的一次性 bypass token 才能放行 PCO native compact。

发布前还必须通过完整 Python suite、Plugin loopback、安装/升级 smoke test，并在锁定版本的真实 OpenCode Harness 中验证“外部 compaction 可取消”的合同。若 Harness 无法取消原生 compaction，不能宣称 v0.4.0 compact 门禁已实现。
