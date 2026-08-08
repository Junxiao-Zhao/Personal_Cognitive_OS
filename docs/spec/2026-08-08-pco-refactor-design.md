# PCO 重构设计：P0–P3 契约、结构与性能改造

> 日期：2026-08-08
> 状态：已评审通过（brainstorming 流程）
> 依据：[PROJECT_REVIEW_v0.3.1.md](../PROJECT_REVIEW_v0.3.1.md)
> 基线：docs/PCO_PRD_v0.3.1.md

## 1. 背景与目标

基于项目评审，当前实现与 PRD v0.3.1 符合度高、AC-01～17 全部有测试且通过，但存在四类问题需要在本轮改造中解决：

1. **契约正确性（P0）**：mem-core 反向依赖 pco（registry 硬编码）；一次性 Profile 迁移逻辑侵入 domain-neutral 核心并绕过 `mem txn` 写 canonical；checkpoint canonical 记录缺少真实 `git_commit` 与派生结果；审批校验在 transaction 与 pre-commit hook 重复实现；`mem` 命令面缺 `--dry-run`。
2. **结构（P1）**：`CheckpointEngine` 903 行 god class；`_legacy_external_refs` 三份拷贝；小工具（`_now` / `_current` / `_profile` / `_repository`）四处重复；锁重复写入与死代码。
3. **性能（P2）**：pre-commit hook 每次 commit 全树物化 + 全量校验（O(N²)）；`search()` 每次查询全量 `_documents` + chunking；archive 去重每次全量加载消息流；自建 fallback 引擎重复 Tantivy/Milvus 功能。
4. **收尾（P3）**：`--dry-run`、死代码/未用 import、receipt 呈现评估、README/MVP_VERIFICATION 同步。

目标：在不改变对外行为（CLI、OpenCode 插件、canonical 结构、AC 语义）的前提下完成上述改造，全部现有测试保持全绿。

## 2. 决策记录

| # | 决策 | 选择 | 理由 |
| --- | --- | --- | --- |
| D1 | registry 改造方式 | **Python entry points（B）** | 标准插件机制，消除 mem-core 对 pco 的反向依赖；mem-core 只依赖 entry-point group 契约 |
| D2 | Profile 迁移机制 | **删除迁移支持（B）** | 项目处于开发阶段、无已有 workspace；顺带删除三处 legacy refs 重复 |
| D3 | 检索 fallback 去留 | **硬依赖 + 删除 fallback（A）** | Tantivy/Milvus 本就是硬依赖；自建倒排/哈希向量属重复造轮子，失败改为明确报错 |
| D4 | CheckpointEngine 拆法 | **纯 Python step 函数（1）** | 零依赖、恢复逻辑可单测；PRD §7.3 明确"优先使用能清晰表达状态机的最小 runner"；Prefect/Temporal 属 PRD 排除的持久化工作流引擎 |

## 3. 架构变更

### 3.1 mem-core

**registry 改为 entry point 发现**：

- `ProfileRegistry` 类保留（显式 allowlist + lazy resolve）；
- 新增 `discover_registry()`，通过 `importlib.metadata.entry_points(group="mem_core.capabilities")` 填充；
- `default_registry()` 语义改为"发现"，调用方不传 registry 时默认发现；
- pco distribution 在 pyproject 声明 `[project.entry-points."mem_core.capabilities"]`，包含 pco 与 research 全部入口点；
- 测试/开发环境保留"显式传入 registry"的兜底，不依赖安装顺序。

**hook 增量校验**：

- 删除 `_verify_profile_migration`、`_legacy_external_refs`、`PCO_SEARCH_RECEIPT_*` 及迁移分支；
- append-only 字节比对改为按变更文件执行 `git show HEAD:<path>`，不再 tar 物化整树；
- `validate_all` 全量校验仅当 staged delta 含结构化 stream / checkpoint / profile / schema / validator 变更时执行；messages-only 归档 commit 走轻量 envelope + schema 校验 + append-only 检查。

**审批校验收敛**：新增 `mem_core/approval.py` 公共函数，transaction 与 hook 共用（proposal hash / transaction hash / fingerprint / protected hash / approval_ref_pointer 五重校验）。

**CLI**：`mem txn commit` 增加 `--dry-run`（只 validate + 报告，不提交、不改状态）。

**小工具**：`mem_core/models.py` 新增 `latest_by_id(records)`，供 `repository.current_records()` 与 pco validation 共用。

### 3.2 pco

**迁移删除**：

- `workspace.py` 删除 `_migrate_canonical_profile` 与 `_legacy_external_refs`；`refresh_repository_profile` 直接加载 canonical profile，并新增 marker 校验（版本不匹配抛 `PROFILE_MARKER_MISMATCH` + 明确 recovery）；
- `validation.py` 删除 `_legacy_external_refs` 与外部引用 legacy 豁免；
- 删除 `test_workspace_migrates_old_canonical_profile_before_refresh`，替换为"旧版本 marker 明确报错"测试。

**CheckpointEngine 拆分**（D4）：`src/pco/checkpoint/` 包：

| 模块 | 职责（自 CheckpointEngine 迁出） |
| --- | --- |
| `state.py` | `CheckpointState`、状态常量、`_save/_load/status`、锁写入 |
| `steps.py` | `_freeze`、`_worker_profile_contract`、`_prepare_candidate`、`_validate_rejection_candidate`、`_protected_diff`、`_effective_search_receipts`、`_checkpoint_operation` |
| `approval.py` | `decide`（Yes/No 流程）、决策归档编排 |
| `finalize.py` | `_commit_and_finalize`、`_finalize_committed`、`_receipt`、`_write_checkpoint_record` |
| `derivations.py` | `_run_derivations`、`_cleanup_worker`、`retry_derivations` |
| `recovery.py` | `retry`、`abort`、`_recover` |
| `__init__.py` | `CheckpointEngine` 薄 facade：持有 workspace/adapter，路由到各模块 |

`workflow/consolidate.yaml` 保留，但加头部注释声明"仅供文档，实际执行走 step 函数"。

**checkpoint 记录补全（关键修订）**：评审结论从"主事务写 rev1（git_commit=null）+ 追加 rev2"修订为更简方案——**checkpoint 记录移出主事务**，在 commit + derivations + worker cleanup 全部完成后，以独立小事务写入 revision 1（携带真实 `git_commit`、`derivations` 结果、最终 `status`、`ended_at`）。理由：commit hash 在提交前不可知，独立写入可保证 canonical 记录永远不含 null/占位值；PRD §19.4 的主事务清单本就不包含 checkpoint 记录；retry 以"记录已存在则跳过"实现幂等。

**去重**：

- `_now()`（archive/sources）统一改用 `mem_core.models.utc_now()`；
- `validation._current()` 改用 `latest_by_id`；
- 新增 `pco/repo_loader.py`：`profile_for_repo(repo_root)` / `repository_for_repo(repo_root)`，backlinks / context / projections / retrieval 收敛调用；
- 锁写入移出 `_save()`，改为显式状态迁移点调用；
- 删除 `_freeze` 死代码行、`__import__("datetime")`、未用 import（checkpoint.py 的 datetime/timezone、cli.py 的 subprocess/sys）。

### 3.3 retrieval

- 删除本地倒排索引（`terms`/`lexical.json`）、哈希向量稠密 fallback（`dense.json`）、`_cosine` fallback 路径、`_eligible_backend_hits` 的 fetch 扩展协调、manifest `backend_errors`；
- 保留 `_vector`（索引构建与查询向量）、`_merge_no_proxy`（Milvus Lite localhost）；
- 原生后端失败直接抛 `MemError` + recovery 提示，不再静默降级；
- `search()` 改为复用 generation 产物：manifest 存在时从 `documents.json` / `backlinks.json` 加载，不再每次查询全量 `_documents` + chunking；索引缺失时才构建。

### 3.4 归档去重

`ConversationArchive.archive` 不再每次全量加载消息流构建去重集合，改为基于 `thread.archive_cursor` 语义 + 读取消息流尾部（tail read）做崩溃恢复，保持幂等且 O(1) 摊销。

## 4. 数据流与边界

改造后主链路：

```text
freeze → spawn_worker → resume_worker → prepare_candidate
  → [AWAITING_META_APPROVAL: decide yes/no]
  → 主事务（worker ops + source ops + search receipts）
  → Git commit → render → publish_context → compact
  → receipt → unlock → derivations → checkpoint record（独立小事务）
```

边界保持：

- canonical 与 derived 分离不变；派生失败不阻塞解锁、不回滚 commit；
- 单个 checkpoint 的记忆内容（结构化 JSONL、source snapshot、Meta、continuation、approval receipt、transaction record）仍在同一 commit；
- checkpoint 记录是审计性追加记录，与 raw archive 同类，允许独立小事务；
- approval 语义（字节级 hash 绑定、拒绝必填理由、禁止再提 Meta）不变。

## 5. 错误处理与恢复

- 旧版本 workspace：`PROFILE_MARKER_MISMATCH` + recovery（重建 workspace 或恢复匹配版本）；
- 检索后端失败：`MemError`（code/phase/recovery），checkpoint canonical 不受影响；
- checkpoint 记录写入失败：进入 `COMMITTED_WITH_PENDING_DERIVATIONS`/RECOVERY 可重试，幂等（存在即跳过）；
- `--dry-run` 不产生任何 Git/状态副作用。

## 6. 测试策略

- 删除/替换：迁移测试、`_eligible_backend_hits` 引用、`backend_errors == {}` 断言；
- 新增：entry point 发现、hook 增量路径（messages-only 快速校验仍拦截篡改）、checkpoint 记录真实值、`--dry-run`、search 复用 generation（documents.json 不被重写）、旧 marker 报错；
- 既有 46 个测试（除被替换者）保持全绿；
- 性能基准：新增 `scripts/benchmark_corpus.py`（10k 事件 / 100k 消息 / 5k concept/hypothesis revision / 1k source checkpoint），测量 validate/commit（<10s，无模型调用）与检索（<2s），结果记录到 `docs/PERFORMANCE.md`。

## 7. 里程碑与交付物

| 阶段 | 内容 | 退出标准 |
| --- | --- | --- |
| P0 | entry points、删迁移、checkpoint 记录、审批收敛、dry-run | 上述测试新增并通过 |
| P1 | checkpoint 包拆分、去重、锁/死代码清理 | god class 消除，全部测试绿 |
| P2 | hook 增量、search 复用 generation、删 fallback、archive 去重、基准 | 基准达标或记录偏差 |
| P3 | 文档同步、最终全量回归 | pytest 全绿，文档一致 |

交付物：本设计文档 + `docs/plans/2026-08-08-pco-refactor.md` 实施计划（两文档一起提交一次）。

## 8. 风险与回滚

- entry point 依赖安装元数据：测试/CI 需保证 editable install；保留显式 registry 兜底；
- hook 增量校验可能漏检：结构化变更仍走全量路径，messages-only 路径以 append-only + delta 校验兜底；
- search 复用 generation 在 canonical 有未索引变更时可能返回旧数据：manifest 绑定 commit，检测 HEAD != manifest.memory_commit 时强制重建；
- 本轮全部为内部重构 + 删除，无 canonical 格式变更；回滚方式为 revert 对应 commit（内存数据不受影响）。
