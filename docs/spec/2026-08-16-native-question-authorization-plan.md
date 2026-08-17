# PCO MVP Closure：原生 Question 授权与剩余合同修复计划

> 状态：已实施；真实 OpenCode loopback 待环境验收（2026-08-16）
> Review baseline：`6b2032c511545e4129c3dc8a57de66fa62451454`
> 优先级：P0 授权闭环与归档身份 → P1 正式入口与触发准确性 → P2 capability、错误和 publication 合同 → release evidence
> 本计划不修改 `docs/DSH_PLUGIN_DRIVEN_HARNESS_ADAPTER_DESIGN.md`。

## 1. 目标与完成定义

本轮把 Meta-memory 的 Yes/No 决定都收束到 OpenCode 原生 question 的结构化生命周期。模型只能请求展示固定表单，并在主机已经生成匹配的一次性 grant 后调用内部工具；模型看到的普通 question 文本、assistant tool-call message ID、slash command 文本都不是用户授权证据。

同时完成当前 MVP closure 中仍未落地的正式 remote source CLI、auto trigger、context usage、Profile capability 调度、结构化派生错误、context publication cache 和文档证据修订。

完成后必须满足：

- PCO slash command 仅有 `/compact`、`/pco-status`、`/pco-retry`、`/pco-abort`；
- Yes 和 No 都必须消费由 `question.replied` 生成、与当前 proposal 完整绑定的一次性 decision grant；
- No 的 canonical reason 是用户回答原文，native ID 为 `question:<requestID>`，不会与真实 assistant turn 去重冲突；
- manual/auto 共用同一 checkpoint 状态机，仅 `trigger` 与触发来源不同；
- checkpoint 和 CLI 派生路径通过 Profile capability 调度，错误在 state、canonical record、receipt 中保持同一结构；
- OpenCode system transform 只读内存缓存，缓存与最新 `ContextBundle.content_hash` 对齐；
- Python、Milvus 和可执行插件状态测试已通过；真实 OpenCode loopback 仍需在安装 Bun/OpenCode SDK 的环境验收。

## 2. 不变约束

- 不新增 slash command，不引入 SQLite；
- 不改变 append-only canonical memory、Git worktree transaction 或 pre-commit 最终校验；
- 不把 AFFiNE、飞书或其他平台 adapter 放入 mem-core；
- worker 不能直接写 canonical JSONL 或 source snapshot；
- 不重新合并 `content_commit` 与 `audit_commit`；
- 不恢复多 PCO session 并发；
- 不把 question 的普通文本结果、tool permission 或模型复述当作授权；
- AFFiNE 保持 `CONTRACT PASS; LIVE PENDING`，直到真实实例验收完成。

## 3. 核心状态与信任边界

### 3.1 Durable 与 ephemeral 状态

Python checkpoint state 持久保存当前 proposal：

```text
checkpoint_id
proposal_hash
approval_challenge_id
parent_session_id
status=AWAITING_META_APPROVAL
protected diff / evidence
```

OpenCode Plugin 只在进程内保存：

```text
PendingQuestion
  checkpoint_id
  proposal_hash
  approval_challenge_id
  session_id
  question_tool_call_id
  question_request_id?       # question.asked 后补齐
  expires_at

PendingDecision
  grant
  decision=yes|no
  raw_reason?                # 仅 no；直接来自 question.replied.answers
```

Plugin 重启后可以从 `/pco-status` 恢复 durable proposal，但不能恢复旧 question、旧回答或旧 grant。用户必须重新显示 question 并再次作答。

### 3.2 固定 question form

当且仅当主 session 的 durable 状态为 `AWAITING_META_APPROVAL` 时，Plugin 允许授权 question。`tool.execute.before` 不信任模型传入的标题、选项和字段，而是使用 durable proposal 重写为固定表单：

```text
是否批准 Meta-memory 提案 <proposal_hash>？

[批准此次更新]

按 Tab / Other 输入拒绝理由或补充经历
```

展示 proposal 时必须同时包含精确 protected Meta diff、主要 evidence、proposal hash。固定选项只包含批准，不提供无理由 No。空白 Other、未知 option、多个矛盾 answer、旧 proposal 或错误 session 全部 fail closed。

question tool call 只建立待绑定状态，不能铸造 grant：

```text
question tool call
  → tool.execute.before 规范化 args 并绑定 tool call ID
  → question.asked 绑定 requestID/sessionID
  → question.replied 读取结构化 answers
  → Plugin 判定 Yes 或 No(raw reason)
  → mint one-time decision grant
  → pco_approve 或 pco_reject 消费匹配 grant
```

`question` dismissal/close 只清除 ephemeral question/grant，不调用 Python decide，不修改 canonical memory，checkpoint 保持 `AWAITING_META_APPROVAL`。

### 3.3 DecisionGrant v2

扩展现有签名 payload 为：

```json
{
  "grant_id": "...",
  "checkpoint_id": "...",
  "proposal_hash": "sha256:...",
  "approval_challenge_id": "...",
  "session_id": "...",
  "question_request_id": "...",
  "decision": "yes|no",
  "reason_hash": "sha256:... | null",
  "issued_at": 0,
  "expires_at": 0
}
```

授权模块必须强制校验签名、TTL、checkpoint、proposal、challenge、session、question request、decision 与 reason hash；校验成功后通过已有原子消费标记保证 grant 只使用一次。缺失任一字段不得退回 v1 宽松行为。

Plugin 密钥继续为进程级随机值，只通过本次 CLI 子进程环境传递。进程崩溃时未消费 grant 可以丢失；不得从 durable state 推断用户决定。

## 4. P0 实施工作流

### 4.1 原生 question 生命周期适配

修改 `packages/pco/src/pco/resources/opencode/plugins/pco.ts`：

1. 删除 `/pco-yes` mint grant 和 `/pco-no` 路径；
2. 增加按 session 键控的 `PendingQuestion`/`PendingDecision`，当前单主 session 合同下仍拒绝其他 session；
3. 在 `tool.execute.before` 识别 OpenCode 原生 question tool，先读取/校验 durable proposal，再覆盖为固定 args；
4. 在 `question.asked` 事件把 host request ID 绑定到预先记录的 tool call；
5. 在 `question.replied` 事件只从结构化 answers 判定：固定 option 为 Yes，非空 Other 原文为 No；
6. 对 dismissal/rejected/cancel 生命周期清空 ephemeral state；
7. `pco_approve` 与 `pco_reject` 都无模型可控参数，分别只接受匹配 decision 的闭包 grant；
8. `pco_reject` 从闭包取原始 reason 和 request ID，并传给 CLI；如果暂时因 SDK schema 必须保留 reason 参数，执行前必须逐字节比对其 hash；
9. 每次工具调用都在进入 CLI 前取走 grant，并在 `finally` 清理，Python 端再执行持久的一次性消费；
10. `/pco-status` command prompt 在 pending 时重新展示同一 proposal 并触发新 question；它不会恢复旧 grant。

实施前先用当前锁定的 OpenCode 版本记录真实事件 payload，建立最小 loopback fixture。类型不明确的字段必须通过运行时 schema guard 读取，不能以 `as unknown` 后盲信。

### 4.2 Python 核心门禁

修改：

- `checkpoint/authorization.py`
- `checkpoint/approval.py`
- `checkpoint/__init__.py`
- `cli.py`

具体合同：

- `decide("yes")` 和 `decide("no")` 都要求 decision grant；
- `verify()` 接收 expected decision、question request ID 和 No 的原始 reason，严格比较所有绑定字段；
- No 的 `reason_hash` 使用规范明确的 UTF-8 原文字节计算，不 trim、改写、概括或 Unicode 重规范化；仅用 `reason.strip()` 判断是否非空；
- CLI `checkpoint decide` 增加内部 `--question-request-id`，Yes/No 都要求 `--approval-grant`；该参数是内部工具接口，不是 slash command；
- Agent 无 grant、错 decision、错 reason、错 session/checkpoint/proposal/challenge/request 或 expired/replayed grant 均返回结构化授权错误；
- approval receipt 的 `authorization_source` 改为 `opencode_question`，并保留 grant ID 与 question request provenance。

### 4.3 拒绝归档身份

修改 `archive.py`、conversation message schema、approval/recovery/finalize 相关 payload：

- `archive_decision()` 仅允许 `decision=no`，Yes 永远不合成 user conversation message；
- native ID 固定由核心根据验证后的 request ID 生成：`question:<question_request_id>`，不接受 assistant `context.messageID`；
- content 精确保留 `question.replied.answers` 的原始非空 Other；
- decision record 增加结构化 provenance：`checkpoint_id`、`proposal_hash`、`question_request_id`、`decision=no`、`authorization_id`；
- refs 保留可检索引用，但不能用 refs 替代 provenance 字段校验；
- decision record 的 canonical message ID 可继续作为 rejection revision 的 evidence ref；
- archive 去重键仍为 harness/session/native ID，因此 question decision 与同轮 assistant tool-call message 使用不同 native ID，后续 sync 必须继续归档真实 assistant turn；
- crash retry 对相同 request ID 幂等，不重复追加 decision；不同 request ID 代表新的真实用户决定。

### 4.4 删除旧 slash command 与安全升级

删除 package 中：

```text
commands/pco-yes.md
commands/pco-no.md
```

同步删除 Plugin `controlCommands`、command hook、runtime contract、`compact.md`、`pco-memory/SKILL.md`、README、PRD、verification 和测试中的引用。输入锁定提示仅列出四个保留命令，并放行原生 question lifecycle 所需事件。

installer 引入 PCO managed manifest，记录本次安装的相对路径与内容 hash。升级时：

- 显式迁移删除且只删除 `.opencode/commands/pco-yes.md` 和 `pco-no.md` 两个已知 PCO legacy 路径；
- 不扫描或删除其他 command 文件；
- 新 manifest 以后只清理上一个 manifest 记录、当前 package 已移除且内容仍匹配 PCO 安装 hash 的文件；
- 安装结果返回 `installed`、`updated`、`removed_legacy`，便于验收。

## 5. P1 工作流

### 5.1 正式 remote source CLI

以下是外部 reader 扩展安装完成后的合同示例，不是默认 PCO 安装即可执行的内置命令；默认 Profile 不注册 `affine-cli`。

把 `pco source add` 设计为互斥入口：

```text
pco source add /local/path [--name journal]
pco source add --locator affine://workspace/document-id \
  --reader affine-cli --provider affine --name journal
```

约束：

- local path 继续映射内置 `local-readonly`；remote locator 必须同时指定 reader；
- reader 只可来自 Profile `source_readers` 显式 allowlist，并通过 entry-point registry 解析；YAML 不允许 module import 字符串；
- 不可用 reader 返回结构化、`retryable=true` 的错误及 recovery；
- 同一 locator 注册返回已有 active source，不产生新 revision/commit；reader/provider/name 冲突返回显式 conflict；
- materialization 输出严格限制为 locator、reader、normalized_content、media_type、read_metadata；只有前四项中允许的 canonical 字段进入 source record，credentials 和 read metadata 不写 Git；
- snapshot/diff 仍由 wrapper transaction 写入 allowlisted artifact root；
- 集成测试经 CLI parser/run 和临时安装的 fake reader entry point 注册 remote locator，再执行 diff/checkpoint snapshot，不直接构造 `SourceManager`。

### 5.2 Auto trigger provenance

Plugin 增加 session-bound、一次性的 `ForegroundAutoMarker`：

```text
session.idle → auto-probe needed=true
→ 写入 ephemeral marker(sessionID, nonce, expiry)
→ 调度同一个 /compact
→ pco_checkpoint 无参数地消费 marker
→ checkpoint request --trigger auto
```

用户主动 `/compact` 没有 marker，因此为 manual。marker 在读取时先清除，并在调度失败、session 不匹配、超时、checkpoint 调用异常时清理。Agent 不能向 `pco_checkpoint` 传 trigger，也不能自行铸造 marker。

测试同时断言 receipt 与 canonical checkpoint record 的 trigger。两条路径除 trigger/调度来源外调用同一个 `CheckpointEngine.request()`。

### 5.3 Context usage 估算

重写 `OpenCodeAdapter.estimate_context_usage()`：

1. 找到最新 assistant message，不累加历史 assistant usage；
2. 优先取该 message 的 `tokens.total`；
3. 无 total 时计算 `input + output + cache.read + cache.write`，兼容缺失/非数字字段；
4. 只补充最新 assistant turn 之后、尚未计入 usage 的 user/text 内容，采用现有保守文本估算；
5. 从实际 session model/provider metadata 获取 context limit，并按 model 缓存；取不到时才使用 `model_context_tokens` 配置；
6. 无 usage 时退化为可见上下文文本估算；
7. ratio clamp 到 `[0, 1]`。

覆盖单轮、三轮递增 input、cache read/write、尾部 user、无 usage、clamp 和 trigger ratio 等于边界值。

## 6. P2 工作流

### 6.1 Profile capability 调度

修改 checkpoint derivations、context finalize 和 CLI：

- index：`workspace.profile.invoke("index.build", ...)`；
- backlinks：`workspace.profile.invoke("backlinks.build", ...)`；
- context：`workspace.profile.invoke("context_renderer.render", ...)`；
- projection：按配置选择 `projections.<target>` 后 invoke；
- search/derive CLI 使用同一 Profile capability；
- 所有调用显式传 `source_commit=content_commit` 及 capability 所需的 wrapper-owned output path；
- 删除 checkpoint/CLI 对 PCO 实现函数的直接 import；
- capability 缺失原样返回 `CAPABILITY_NOT_FOUND`，不 fallback；
- 测试 Profile 用 registry 中的替代 callable 记录调用参数，证明无需改 checkpoint 代码即可替换实现。

mem-core 只维持通用 registry/invoke，不新增任何 PCO backend 知识。

### 6.2 统一结构化 derivation error

新增单一规范化 helper，供 index、backlinks、projection、context publication、worker cleanup 使用：

- `MemError` 直接保存 `exc.as_dict()`；
- 未知异常包装为 `UNEXPECTED_DERIVATION_FAILURE`，包含 phase、message、`retryable=true` 和针对该 phase 的 recovery；
- runtime state、checkpoint canonical payload 和 receipt 不做 stringify，使用同一 JSON-compatible schema；
- 每项 derivation 保存 append-only attempt history；retry 新增 attempt/recovery result，不在 state 中覆盖首次错误；
- canonical checkpoint 通过新 revision 表达恢复结果，旧 revision 保留原失败；
- `_checkpoint_derivations()` 只验证 JSON compatibility，不降级丢失 code/path/record_id/recovery。

建议状态形状：

```json
{
  "ok": false,
  "pending": true,
  "attempts": [
    {"attempt": 1, "at": "...", "error": {"code": "...", "phase": "..."}}
  ],
  "error": {"code": "...", "phase": "..."}
}
```

成功 retry 追加 `{attempt, at, recovered_from, result}`，并将 current `ok` 设为 true；canonical revision diff 能观察恢复过程。

### 6.3 Context publication cache

保留通用语义：`publish_context(bundle)` 使 bundle 对后续请求生效。OpenCode adapter strategy 为 `request_system_transform`，未来 Harness 可实现 `session_persistent`。

Plugin：

- 初始化读取 `current.md` 与 bundle metadata；
- watcher 只作为外部变化同步路径；
- `pco_checkpoint`/`pco_retry` 成功且 receipt 表明 context 已发布后显式 refresh；
- refresh 同时计算或读取 hash，仅在与 `ContextBundle.content_hash` 一致时原子替换 cached snapshot；不一致时保留旧 cache 并报结构化/日志错误；
- system transform 只拼接 cached snapshot，不执行 `existsSync/readFileSync`；
- context publication/retry 错误进入统一 derivation/error schema。

文档明确 OpenCode 没有被宣称持久写入 system message，也不再描述 instruction file strategy。

## 7. 测试设计

### 7.1 Python

授权与归档：

1. Yes 无 question-issued grant 失败；
2. direct `pco_reject` 无 grant 失败；
3. No grant 绑定 exact raw reason，任一字节变化失败；
4. grant decision 与 approve/reject 工具不一致失败；
5. session/checkpoint/proposal/challenge/request ID 任一错误失败；
6. replay、expired grant 失败；
7. dismissal 不调用 decide、不产生 canonical decision；
8. rejection native ID 为 `question:<requestID>`，且 provenance 完整；
9. decision 与同轮 assistant tool call 均被归档，cursor 不跳过 assistant；
10. Yes 不产生 synthetic user message。

安装、来源、trigger 与估算：

11. package 和安装目录都不存在 pco-yes/pco-no；
12. upgrade 只删除两个 legacy managed 文件，保留用户其他 command；
13. remote source 通过真实 CLI 路径注册、materialize、snapshot、diff；
14. reader allowlist/unavailable/idempotency/metadata secrecy；
15. manual receipt/record 为 manual，threshold foreground 为 auto；marker replay/session mismatch 失败；
16. context usage 的七类边界测试。

capability、error、context：

17. 测试 Profile 替换 index/backlinks/projection/context/search capability；
18. 缺 capability 返回 `CAPABILITY_NOT_FOUND`；
19. `MemError` 全字段在 state/canonical/receipt 一致；未知异常 schema 一致；retry 保留首次错误；
20. system transform 不读文件；watcher 与 checkpoint explicit refresh 更新 hash-matched cache。

### 7.2 可执行 OpenCode Plugin 状态测试

新增 TypeScript/Bun 测试或版本锁定的 loopback harness，实例化 Plugin 并驱动真实 hook/event payload，不再用源码字符串作为 AC-12 证据。至少覆盖：

```text
proposal → normalized question args → question.asked(requestID)
→ question.replied(fixed option) → pco_approve → matching Yes grant
```

```text
proposal → normalized question args → question.asked(requestID)
→ question.replied(Other raw reason) → pco_reject → matching No grant/reason
```

并覆盖 direct tool bypass、错误 session、旧 proposal、dismissal、restart 后旧 ephemeral grant、auto marker 与 auto `/compact`。测试捕获 Plugin 启动的 CLI argv/env，验证 secret/grant 不进入 workspace 和日志。

如果 SDK 本身无法直接实例化事件总线，则建立本地 OpenCode loopback 进程测试；静态字符串断言只能作为安装 smoke test，不能标记授权闭环 PASS。

## 8. 文档与 release evidence

统一更新：

- `README.md`
- `docs/PCO_PRD_v0.3.1.md`
- `docs/MVP_VERIFICATION.md`
- `docs/spec/2026-08-15-mvp-closure.md`
- OpenCode command prompts
- `pco-memory/SKILL.md`

必须删除以下失效叙述：`/pco-yes`/`/pco-no`、question 仅 display、所有 canonical 变更一个 commit、OpenCode instruction file/永久 system message、每轮 transform 读文件与“不读文件”的矛盾、仅凭静态字符串宣称 AC-12 PASS。

统一 commit 时序：

```text
content_commit
→ Profile capabilities 基于 content_commit 构建 derivations/context
→ audit_commit 记录 checkpoint outcome 与 recovery revisions
```

`MVP_VERIFICATION.md` 的每项 PASS 必须链接到可执行测试或真实 loopback 记录。AFFiNE 继续写 `CONTRACT PASS; LIVE PENDING`。

## 9. 实施顺序与提交边界

### Phase A：P0 question authorization

- 先建立 Plugin event fixture 和 DecisionGrant v2 单测；
- 实现固定 form、question lifecycle、Yes/No 核心门禁；
- 修复 No canonical provenance 与 archive 去重；
- 删除 slash command，完成 installer migration；
- 跑授权、archive、installer、Plugin executable tests。

Phase A 是阻塞门。未通过前不宣称 MVP complete。

### Phase B：P1 operational correctness

- remote source CLI + entry-point allowlist integration；
- foreground auto marker/trigger；
- context usage estimator；
- 跑 CLI、acceptance、adapter threshold tests。

### Phase C：P2 extensibility and observability

- Profile capability dispatch；
- structured derivation errors and retry history；
- context publication cache；
- 跑 profile replacement、failure/recovery、cache tests。

### Phase D：evidence and live verification

- 更新全部文档与安装资源；
- 完整 Python/Milvus suites；
- 真实 OpenCode 十项 loopback；
- 检查 wheel 安装清单与升级清理；
- 最后才更新 MVP 状态。

建议每个 Phase 独立 commit，Phase 内先测试后文档，避免授权协议、迁移和派生重构混在同一不可审计提交中。

## 10. 最终验收

自动化门：

```bash
pytest -q
PCO_RUN_MILVUS=1 pytest -q
```

真实 OpenCode loopback：

1. manual `/compact` 无 Meta，commit/context/compact 成功，trigger=manual；
2. manual Meta proposal，原生 question fixed option Yes；
3. manual Meta proposal，Other 原始理由 No；
4. 模型 direct approve/reject 均失败；
5. dismissal 无 canonical 变化；
6. `/pco-status` 重显 pending proposal 和新 form；
7. threshold 前台 checkpoint，trigger=auto；
8. derivation 失败保留 content commit，`/pco-retry` 幂等追加恢复 revision；
9. Plugin restart 后旧授权不可复用；
10. upgrade 后 pco-yes/pco-no 消失，其他用户 command 保留。

签字前额外检查：

- canonical conversation 中没有 synthetic Yes；
- No reason 与 captured answer 原文字节一致；
- content/audit/derivation provenance 可从 receipt 和 checkpoint record 自洽重建；
- system transform 不进行文件读取；
- release evidence 不包含未执行或仅静态断言的 PASS。
