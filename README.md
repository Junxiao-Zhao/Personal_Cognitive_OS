# PCO

PCO（Personal Cognitive OS）是一个本地优先、证据可追溯的单用户长期记忆系统。它以 OpenCode 作为 MVP Agent Harness，将公开对话、用户授权的来源快照、结构化认识、Meta-memory 和 continuation 保存到本地 append-only JSONL + Git canonical memory。

代码分为两层：

- `mem-core`：领域无关的 Profile、JSON Schema、append-only stream、写策略、approval receipt、Git worktree 原子事务和 pre-commit 最终校验。
- `pco`：Thread/epoch、OpenCode Adapter、checkpoint 状态机、PCO Profile、来源 diff、混合检索、上下文发布和可替换投影。

## 安装与启动

要求 Python 3.11+、Git 和 OpenCode。Milvus Lite 与 Tantivy 已作为 Python 依赖安装。

项目拆分为两个 Python 分发包：

- `packages/mem-core`：领域无关的 `mem-core`，提供 `mem` CLI；
- `packages/pco`：PCO 应用层，依赖 `mem-core`，提供 `pco` CLI。

```bash
# 开发安装（mem-core 需先以 editable 安装，pco 才能解析本地依赖）
python -m pip install -e packages/mem-core
python -m pip install -e 'packages/pco[dev]'

# 默认使用 AFFiNE 投影；未配置 bridge 时 canonical commit 仍会成功，投影标记 pending。
pco --workspace .pco init --projection affine

# 或使用完全本地、开箱即用的 Markdown 投影。
pco --workspace .pco init --projection markdown

pco --workspace .pco doctor
pco --workspace .pco run --project .
```

`pco run` 会把 plugin、隐藏的 `pco-consolidator` subagent、`pco-memory` skill 和命令安装到项目的 `.opencode/`，然后进入唯一的 OpenCode 主 session。第一次 session 会自动绑定当前 PCO epoch。

日常入口：

- 正常对话：完整 turn 在 session idle 时独立归档。
- `/compact`：手动进入与自动阈值完全相同的 checkpoint；若出现 Meta 提案，会在主会话打开原生 Yes/No question 表单，拒绝时用 Tab/Other 输入非空理由。
- `/pco-yes`：批准屏幕上展示的精确 Meta diff。
- `/pco-no <理由或补充经历>`：同一次输入完成必填理由的拒绝。
- `/pco-status`、`/pco-retry`、`/pco-abort`：恢复控制面。
- `pco --workspace .pco source add /path/to/journal.md`：注册只读本地来源。
- `pco --workspace .pco search '为什么我总在公开前拖延' --mode pattern`：调用五种 Profile 检索模式之一。

## Checkpoint 保证

一次 checkpoint 会锁定普通输入，冻结精确消息边界与来源哈希，在隔离的原生 OpenCode child session 中生成 proposal，并先经 Profile 校验。Meta-memory 属于 `user_approval` stream：approval receipt 同时绑定用户审阅的受保护 diff hash、完整 operation-set hash 和 transaction fingerprint。

通过后，所有 canonical 变更以一个 Git commit 提交；随后发布最新 Meta + continuation，再调用一次 OpenCode compact。Milvus/Tantivy、backlinks 和投影属于可重建派生状态，失败只产生 pending receipt，不回滚 canonical commit。拒绝会先把理由作为用户 decision message 独立归档，再让同一 worker 只续跑一次；原 proposal 和 revised proposal 都保留在运行状态目录。

## AFFiNE

AFFiNE 内容使用其前端内部的 BlockSuite/Yjs 文档模型，目前没有稳定的公共“按 Markdown upsert 文档”服务端 API。因此 PCO 把 provider-specific 逻辑隔离成一个严格的 JSON stdin/stdout bridge，而没有把 AFFiNE 私有 CRDT 协议写进 `mem-core`。配置方式、幂等合同和失败恢复见 [AFFiNE bridge](docs/AFFINE_BRIDGE.md)。

没有设置 `PCO_AFFINE_COMMAND` 时，每个 commit 的完整 page batch 会写入 `.pco/state/affine/outbox/<commit>.json`，receipt 明确标记 pending；可随时设置 bridge 后执行 `/pco-retry`。Markdown 投影无需外部服务。

## 验证

```bash
pytest

# 检索功能测试需要本机 loopback；验证真实 Milvus Lite 与 Tantivy 后端。
PCO_RUN_MILVUS=1 pytest -m milvus
```

Tantivy 与 Milvus Lite 是硬依赖。后端不可用时 `pco derive index` 与 `pco search`
会返回带稳定 code 与 recovery 提示的明确错误（`INDEX_BACKEND_FAILED`），不会静默
降级到本地简化索引。PRD 规模（10 万消息）的性能基准与当前差距见
[docs/PERFORMANCE.md](docs/PERFORMANCE.md)。

产品与验收基线见 [MRD](docs/PCO_MRD_v0.3.md) 与 [PRD](docs/PCO_PRD_v0.3.1.md)。实现验收矩阵见 [MVP verification](docs/MVP_VERIFICATION.md)。
