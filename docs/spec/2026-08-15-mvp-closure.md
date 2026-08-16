# PCO MVP Closure：授权、自动 checkpoint 与 canonical 合同

> 状态：历史实施规格；原生 question 授权、remote source CLI、auto trigger、Profile capability 和 publication cache 的后续修复计划见 [2026-08-16-native-question-authorization-plan.md](2026-08-16-native-question-authorization-plan.md)
> 依据：HEAD 63d96a8 的审查结论
> 范围：本地文件输入、Markdown 投影、手动 /compact 主闭环，以及为 MVP 完成签字所需的通用合同修复

## 目标

本轮不是重新设计 PCO，而是把已经可运行的 checkpoint 骨架补齐为可审计、可恢复、可扩展的合同。MVP 的硬性签字门槛是：

1. Meta 写入必须有 Harness 主机产生的用户授权 provenance，模型不能通过直接调用 pco_approve 自行批准；
2. 自动 checkpoint 必须进入与手动 /compact 等价的前台 proposal/question 流程，不得静默锁死会话；
3. canonical content commit 与 checkpoint audit commit 语义明确，派生状态必须标明其来源 commit；
4. current backlinks、source materialization、外部 receipt 和 mem-core fast path 都不能依赖隐含的 PCO 特判。

## 不变约束

- append-only JSONL、Git worktree 事务和 pre-commit 最终防线保持不变；
- worker 不直接写 canonical JSONL、source snapshot 或 runtime state；
- Meta user_approval 仍由 Profile 声明，mem-core 只处理通用授权合同；
- 已提交 canonical memory 不回滚，派生失败仍是 post-commit pending；
- 用户已有的 DSH 设计稿只作为未来 Harness adapter 参考，不在本轮实现 DSH。

## 1. Host-issued Meta approval grant（P0）

### 合同

    proposal
      -> host 通过固定 OpenCode question 展示 diff/evidence/hash
      -> question.asked/question.replied 绑定 request、session 和原始答案
      -> OpenCode 插件铸造一次性 Yes/No DecisionGrant
      -> pco_approve/pco_reject 只能消费匹配的 grant
      -> Python 校验 grant、challenge、session、proposal、request、decision、reason hash 和过期时间
      -> attach ApprovalReceipt

permission: ask 不能作为唯一门禁，因为 OpenCode auto mode 可以自动批准 ask。授权必须绑定原生 question 的结构化 asked/replied 生命周期；普通文本、permission 结果和模型复述均不是 provenance。

### 实施

- checkpoint proposal 生成一次性 challenge_id，绑定 checkpoint、proposal hash、main session 和 expiry；
- 插件维护 pending challenge 和 grant，不把签名密钥或 grant 写入 workspace；
- grant 使用插件进程内随机密钥签名，CLI 子进程只在本次调用中接收验证材料；
- pco_approve 无模型可控参数，但无 grant 时必须返回 APPROVAL_PROVENANCE_REQUIRED；
- grant 只能消费一次；proposal/session/challenge/expiry 任一不匹配都拒绝；
- Yes 的授权不再由模型调用直接触发，也不生成 synthetic `role=user` conversation message；
- No 只归档 question reply 的真实 custom answer，native ID 使用 `question:<question_request_id>`，不能使用 assistant tool-call message ID；
- ApprovalReceipt 增加通用 opaque provenance 字段，不能把 OpenCode 领域类型泄漏进 mem-core。

### 验收

- 直接调用 pco_approve、伪造 messageID、错误 hash、过期 grant、重复 grant 均失败；
- 固定 question 的 Yes/No reply 能完成同一 proposal；dismissal 保持 `AWAITING_META_APPROVAL` 并可由 `/pco-status` 重显；
- canonical history 中没有合成的 Yes role=user 消息；
- pre-commit 仍校验 proposal hash、transaction fingerprint、protected operations hash 和 approval reference。

## 2. Foreground automatic checkpoint（P0）

    session.idle
      -> sync
      -> auto-probe（只读、不锁）
      -> 调度主会话 PCO control turn（与手动 /compact 共用前台路径）
      -> pco_checkpoint(trigger=auto)
      -> freeze/lock
      -> proposal/question/grant
      -> commit 或 rejection

- 新增只读 checkpoint auto-probe，只返回是否达到阈值；
- idle hook 不直接执行会锁输入的 request；达到阈值后调度主会话 control turn，复用 /compact 的 proposal/question 文案；
- 前台调度不可用时显示提示，但不创建 active checkpoint、不锁输入；pending marker 作为下一步增强项；
- pco_status 在 AWAITING_META_APPROVAL 返回完整 proposal、protected diff 和 proposal hash；
- 只有真正开始 checkpoint request 后才锁定普通输入，control turn 和授权交互必须放行；
- auto 与 manual 除 trigger 外必须返回同构状态和 receipt。

验收覆盖：阈值未到无副作用、auto 无 Meta、auto 有 Meta、前台调度失败、不静默锁死、status 可恢复 proposal。

## 3. Content commit / audit commit（P0）

定义：

- content_commit：结构化 memory、source snapshot、Meta、continuation、approval receipt 的主事务 commit；
- audit_commit：checkpoint record/revision 独立事务的 commit；
- derivation_source_commit：派生索引、backlinks、Markdown 实际读取的 content commit；
- checkpoint audit stream 默认不进入检索、backlinks 和 Markdown/AFFiNE 实体投影。

实现要求：

- CheckpointState.commit 迁移为 content_commit，保留兼容读取；
- write_checkpoint_record() 返回 audit transaction commit，并在 runtime receipt 标记 audit_commit；
- checkpoint record 不保存无法自引用的自身 hash，只保存 content commit、derivation source commit 和 audit transaction ID；
- 更新 PRD §19.4，删除“checkpoint audit record 与主结构化 memory 属于同一 commit”的歧义；
- DONE 代表 content commit 成功、派生结果已明确、audit revision 已提交。

验收：HEAD 可以是 audit commit，但 generation 必须标记 source_commit=content_commit；retry derivations 只追加 audit revision，不重写 content commit。

## 4. Current / historical backlinks（P1）

- current_backlinks 只基于每个 ID 的最新有效 revision，并按 target/source/relation 去重；
- historical_backlinks 保留 source revision、status、recorded_at，不参与默认相关性计数；
- Markdown、AFFiNE 和 retrieval graph expansion 只消费 current backlinks；
- 修订三次同一关系时默认计数为 1，最新 revision 删除关系后 current backlink 消失。

## 5. Source materialization contract（P1）

统一 reader 返回：

    {
      "locator": "...",
      "reader": "...",
      "normalized_content": "...",
      "media_type": "text/markdown",
      "read_metadata": {}
    }

- local file 是内置 reader；AFFiNE/飞书等由 reader Skill/CLI 负责读取；
- wrapper 验证 locator、规范化内容、计算 hash/diff 并生成 source revision 与 write_artifact；
- worker 不获得 source snapshot 写权限；凭据与未清洗 metadata 不进入 Git；
- 先用 fake remote reader 验证契约，再接真实平台。

## 6. External reference receipt provenance（P1）

- search receipt 增加规范化 result_urls；
- websearch 只从工具输出提取 URL，query 中的 URL 不算证据；
- webfetch 仅在成功完成时绑定目标 URL；
- external ref 必须精确属于 receipt 的 result_urls，不能对整个 receipt payload 做字符串包含判断；
- 需要 schema/version 兼容策略，历史 v1 receipt 不能被静默伪装成 v2 结果。

## 7. Profile-driven mem-core fast path（P1）

在 StreamConfig 增加领域无关的 validation policy，例如：

    validation:
      transaction_mode: delta_only
      run_cross_validators: false

- is_messages_only() 改为基于 Profile 的 fast-path 判断；
- transaction 和 pre-commit hook 读取同一声明；
- PCO profile 显式声明 messages fast path，Research profile 可独立选择；
- mem doctor、git verify、profile validate 始终全量；
- 任意名为 logs 的 stream 都能测试 fast path，证明不依赖 messages 字符串。

## 8. Context publication strategy（P2）

publish_context() 的通用语义定义为“使 ContextBundle 对后续请求生效”，而不是保证所有 Harness 都持久化 system message。

- OpenCode 明确标记为 request_system_transform strategy；
- 插件通过 watcher/cache 使用已发布内容，不在每次 transform 重新读取 canonical 文件；
- 支持持久 system context 的 Harness 可实现 session_persistent strategy；
- PRD 和 Adapter Protocol 同步这一区分。

## 提交与依赖顺序

1. 先提交本规格和合同/迁移说明；
2. approval grant；
3. foreground auto checkpoint；
4. content/audit commit；
5. current/historical backlinks；
6. source materialization 与 external receipt；
7. profile-driven mem-core fast path；
8. context strategy 文档和缓存实现。

阶段 1–3 完成并通过 OpenCode loopback 后，才可重新标记 MVP complete。最终验证包括 pytest -q、PCO_RUN_MILVUS=1 pytest -q、manual/auto Yes/No、grant replay、100k corpus benchmark，以及 Git clone 后重建 derivations。
