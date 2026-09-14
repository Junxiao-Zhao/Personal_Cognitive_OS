# PCO

PCO（Personal Cognitive OS）是本地优先、证据可追溯的单用户长期记忆系统。
v0.5 以 Agent 无关的专用工作区为边界：任何支持项目本地
`.agents/skills`、shell 命令和一次性写权限升级的 Agent Host 都可以使用它，
PCO 本身不包含 Host adapter、checkpoint、compact 或后台自动整理运行时。

代码分为两个 Python 分发包：

- `mem-core`：Profile 驱动、领域无关的 append-only JSONL、校验、Git
  worktree 原子事务与 canonical receipt；
- `pco`：专用工作区、Markdown proposal、来源 diff、当前状态投影、检索与
  Agent 可调用的 `pco` CLI。

## 安装

要求 Python 3.11+ 和 Git。Milvus Lite 与 Tantivy 是派生索引依赖。

```bash
python -m pip install -e packages/mem-core
python -m pip install -e 'packages/pco[dev]'
```

## 创建专用工作区

`pco init` 只接受不存在或为空的目标目录，不会嵌入或覆盖已有项目。初始化会
创建 canonical Git repository、proposal 与 projection 目录，并安装两个项目本地
SKILL。初始化后，从该目录重新打开 Agent Host，以便发现 `.agents/skills`。

```bash
pco init ~/my-pco
cd ~/my-pco
pco doctor
pco verify
```

工作区的权威数据位于 `.pco/canonical`。`PCO_MEMORY` 与 `.pco/indexes`
都是可丢弃派生物；canonical 数据不依赖它们恢复。

## Proposal 工作流

Agent 先生成 proposal 控制信息，再通过 Host 的 Edit 能力一次性写入完整的
`PCO_PROPOSALS/<review_id>/`。用户检查可读的 Markdown diff 后，Agent 只把
review ID 传给 commit 命令：

```bash
pco proposal status
pco proposal scaffold
pco proposal validate <review_id>
pco proposal commit <review_id>
```

也可以在 canonical commit 前运行 `pco proposal abort <review_id>`。唯一需要
强原子性与崩溃恢复保证的是 mem-core canonical transaction 及其 Git commit。
Proposal 归档和 derivation 属于非 canonical 阶段；它们失败时，已经成功的
canonical commit 不会回滚。

## 读取、来源与派生

```bash
pco search '为什么我总在公开前拖延' --mode pattern
pco history hypotheses hyp_example
pco source diff [source_id]
pco verify --path PCO_MEMORY
pco derive all
```

`doctor`、`verify`、`search`、`history`、`source diff`、`proposal status`、
`proposal scaffold` 和 `proposal validate` 都是只读命令。Search 只使用与当前
canonical HEAD 匹配且校验通过的索引；索引缺失、过期或损坏时，自动使用有界
canonical fallback。`pco derive all` 从同一个 canonical HEAD 同时重建
`PCO_MEMORY` 与检索索引。若 projection 被手工修改，先把希望保留的内容写入
proposal；确定丢弃时才使用 `pco derive all --discard-modified-projection`。

新增来源也是普通 proposal 文件；v0.5 没有 `source add`。本地来源的
`pco source diff` 会比较完整快照，远程内容由 Agent Host 的原生工具读取后作为
proposal 输入材料保存。

## 验证

```bash
pytest -q
PCO_RUN_MILVUS=1 pytest -q
git diff --check
```

## 权威设计与实施记录

- [PCO v0.5 approved design](docs/superpowers/specs/2026-08-30-pco-v0.5-agent-agnostic-design.md)
- [ADR 0001: v0.5 clean break](docs/adr/0001-pco-v0.5-clean-break.md)
- [Canonical core plan](docs/superpowers/plans/2026-08-30-pco-v0.5-canonical-core.md)
- [Workspace and skills plan](docs/superpowers/plans/2026-08-30-pco-v0.5-workspace-and-skills.md)
- [Proposal and sources plan](docs/superpowers/plans/2026-08-30-pco-v0.5-proposal-and-sources.md)
- [Derivation, CLI, and clean-break plan](docs/superpowers/plans/2026-08-30-pco-v0.5-derivation-cli-clean-break.md)

v0.3/v0.4 没有原地 migration 或 schema compatibility 路径。被替代设计仅保留在
Git 历史中；以上 approved design 与 ADR 是当前架构权威。
