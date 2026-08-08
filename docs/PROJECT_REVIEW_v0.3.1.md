# PCO 项目评审：PRD v0.3.1 交叉比对与代码质量评估

> 评审日期：2026-08-08
> 评审对象：全项目源码（约 5,300 行 Python）、OpenCode 插件与资源包、测试套件
> 评审基线：docs/PCO_PRD_v0.3.1.md
> 测试结果：`pytest -q` 46 passed / 1 skipped（skip 为需本机 Milvus loopback 的 marker）

## 1. 总评

这是一个与 PRD 符合度相当高的实现：架构上真正做到了 canonical/derived 分离、mem-core 与 PCO Profile 分层、append-only + Git worktree 原子事务、approval receipt 绑定 proposal hash、pre-commit 最终防线；AC-01～AC-17 全部有对应测试且通过。MVP 主闭环（逐 turn 归档 → checkpoint → 冻结 → worker → 授权 → 原子 commit → 渲染发布 → compact → 派生 → receipt）是完整可跑的，MVP_VERIFICATION.md 还记录了真实 OpenCode 1.17.18 loopback 全链路验收。

主要差距不在功能，而在三处：

1. 真实 AFFiNE 部署仍是 contract-only（PRD §28 门槛未全过）；
2. 性能目标（10 万消息、2 秒检索）没有被验证且实现路径上有明显风险；
3. "core 不 core"的边界被侵蚀、存在重复代码与过度防御。

## 2. 与 PRD 交叉比对

### 2.1 覆盖良好的部分

| PRD 章节 | 实现状态 |
| --- | --- |
| §6 组件职责（Wrapper/Adapter/Worker/SKILL/mem-core/Profile） | 全部落地，HarnessAdapter Protocol 与 WorkerHandle 契约一致 |
| §8/§9 checkpoint 状态机与失败重试 | 状态枚举、COMMITTED_CONTEXT_PENDING / COMMITTED_WITH_PENDING_DERIVATIONS / RECOVERY / ABORTED 语义一致 |
| §10 compact 后上下文 | 只发布一次 ContextBundle，plugin 用 compaction marker 取代默认 summary |
| §11 对话归档、reasoning 规则 | 双 cursor（last_archived / last_consolidated），decision 走独立事务先行归档 |
| §12 来源注册/快照/diff | SHA-256 + unified diff，未变化不重复抽取 |
| §13/§19 事务、校验层级、Git 语义 | envelope→schema→revision→validator→fingerprint→write policy→pre-commit 七层齐全 |
| §15/§16 四分类、meta/continuation 合同 | schema 与 payload 字段和 PRD 一致，meta 全量 snapshot + previous_revision |
| §17 置信度与授权式晋升 | 低置信度 hypothesis 自动落盘、meta 必须 user_approval、No 必填理由且禁止再提 Meta |
| §20 检索 | 五种模式、turn-aware chunker、RRF、时间衰减、graph 一跳 boost、资格标记 |
| §21 投影 | Markdown 完整；AFFiNE 通过严格 JSON stdin/stdout bridge 隔离 |
| §24 权限 | worker 显式 deny edit/bash/task/question，allow websearch/webfetch |
| §26 AC-01～17 | 均有测试（AC-09/17 为 contract PASS，live AFFiNE 待部署验证） |

### 2.2 与 PRD 有偏差或缺口的地方

1. **canonical checkpoint 记录不完整**（§25.5 可观测性）。
   `src/pco/checkpoint.py` 的 `_checkpoint_operation` 写入的 checkpoint record 中 `git_commit` 恒为 `None`、`derivations` 恒为 `"scheduled"`，提交后从不追加 revision 更新。Git commit 与索引/投影结果只存在于可重建的 state/receipt 里，canonical 侧永远不知道结果——这与"每个 checkpoint 至少记录 Git commit、index/AFFiNE 状态"不符。

2. **`mem` 命令面缺 `--dry-run`**（§19.2 明确要求"所有命令支持 JSON 输入和输出；`--dry-run` 返回拟执行变更"）。`src/mem_core/cli.py` 没有 dry-run 参数，`mem txn commit` 只能真跑。

3. **Profile 版本漂移**。PRD 基线是 pco@0.3.1，实现已升到 0.3.2（新增 `search_receipts` stream 以落实 AC-14），并写了一个只允许 0.3.1→0.3.2 的一次性迁移路径。这是合理演进，但迁移逻辑同时硬编码进了 mem-core 的 pre-commit hook（见 §3.2），属于 PRD §7.2"Profile 变更必须产生新 version 或 policy hash"之外的临时机制。

4. **workflow 声明未被执行**。PRD §18.3 给出 YAML step 清单，资源里也确实有 `src/pco/resources/profiles/pco/workflow/consolidate.yaml`，但 `CheckpointEngine` 并不加载它，所有 step 是硬编码在一个 903 行的类里。文档与实际执行双轨（§29 也把"是否直接使用 Domino"列为待定，所以不算硬缺口，但属于"声明与实现不一致"）。

5. **`insert_receipt` 用 toast 而非消息插入**。PRD §5.5/§23 说"主会话插入简短 receipt"；`src/pco/harness.py` 实现是 `/tui/show-toast`。UI 呈现方式与 PRD 描述有差异，可作为 MVP 可接受的适配，但值得记录。

6. **性能目标未验证且路径可疑**（§25.4）。见 §3.1 过度防御部分。

## 3. 编程质量与项目结构

### 3.1 过度防御（方向对，但粒度失控）

1. **pre-commit hook 是全量重炮**。`src/mem_core/hook.py` 的 `validate_repository` 每次 commit 都 `git archive` 物化 HEAD 和 staged 两棵完整树（含 symlink/path 安全扫描），再全量 `validate_all`。这意味着**每个逐 turn 归档小 commit 都要 O(N) 校验整个语料库**；按 PRD 的 10 万消息规模，是 O(N²) 累计开销，直接威胁 §25.4 的"validate/commit 10 秒内"。作为最终防线应该只增量校验本次 delta + 抽查。

2. **检索三层回退**。`src/pco/retrieval.py` 的 `search()` 每次查询都先 `build_index()`（幂等但重复读全量）再 `_documents()` 全量加载所有 revision；无 Tantivy/Milvus 时又各起一份本地倒排索引和哈希向量实现；`_eligible_backend_hits` 的 fetch 扩展循环带四五个边界条件。这套"原生库失败也能跑"的保障很完整，但把主路径复杂度抬得过高，且 10 万消息下每次查询全量 chunking 必然超 2 秒目标。

3. **worker 结果三重回退**。`src/pco/harness.py` 的 `resume_worker` 依次尝试 structured → 纯文本 JSON → repair_json → 修正对话，80 多行。MVP_VERIFICATION 里真实跑过 `validated_json_text_repair`，说明有实际依据，但这属于把模型输出不确定性全部堆在 adapter 层；长期应上移到 worker 契约/重试层。

4. **批准校验重复两份**。`src/mem_core/transaction.py` 的 `_verify_approval` 与 `src/mem_core/hook.py` 的 `_verify_increment` 各实现一遍约 40 行的 approval hash/pointer 校验，后续容易漂移。

5. 小处：
   - `src/pco/checkpoint.py` 的 `_save()` 每个状态转换都重写 `checkpoint-lock.json`（测试里能看到十几条 lock action）；
   - `_freeze()` 的三行 cursor 过滤有两行是死代码（光标消息根本不会进 `selected`）；
   - `src/pco/retrieval.py` 手动补 `NO_PROXY` 环境变量。

### 3.2 结构不清晰/边界侵蚀

1. **mem-core 反向依赖 pco，分层倒挂**。`src/mem_core/registry.py` 的 `default_registry()` 硬编码注册 `pco.*` 和 `research.*` 入口点。PRD §13 说"Python entry point 由安装包注册，YAML 只引用注册名称"、§25.3 说 Profile 可独立替换；现在换第三个 Profile 必须改 mem-core。AC-16 的"不改 mem-core"只对测试恰好成立。

2. **一次性迁移逻辑进了 domain-neutral 核心**。`src/mem_core/hook.py` 的 `_verify_profile_migration` 硬编码 `pco`、`0.3.1→0.3.2`、schema SHA-256；`src/pco/workspace.py` 的 `_migrate_canonical_profile` 直接写 `transactions/profile-migrations.jsonl` 并调私有 `self.repository._git(...)` 绕过 `mem txn commit` 提交 canonical——这违背 PRD §7.3/§19.2"CLI 是 canonical memory 的唯一正式写入口、Git 原子事务必须封装在 mem txn 内"，虽然有 pre-commit 特例兜底，但等于在核心契约上开了一道后门。

3. **同一函数三份拷贝**：`_legacy_external_refs` 分别在 `src/mem_core/hook.py`、`src/pco/workspace.py`、`src/pco/validation.py` 实现三遍，且返回类型还不同（set vs list），是典型的复制粘贴式结构风险。

4. **checkpoint.py 是 god class**：903 行里 freeze/worker 编排/事务组装/审批/commit/render/publish/compact/receipt/派生/retry/abort 全在一个类，`_prepare_candidate` 一个方法 140 行。PRD §18.3 的 step 化 callable 设计没有落地，后续任何一步想独立测试或复用都困难。

5. 小问题：
   - `src/pco/workspace.py` 用 `__import__("datetime")` 而非正常 import；
   - `src/pco/checkpoint.py` 导入了未用的 `datetime/timezone`；
   - `src/pco/cli.py` 导入了未用的 `subprocess/sys`。

### 3.3 做得好的地方

- 错误契约统一：`MemError` 带 code/phase/path/retryable/recovery，CLI 单 JSON 输出，全项目一致。
- 事务与幂等设计扎实：worktree + fast-forward、fingerprint 查重、approval 绑定 reviewed hash + final hash + fingerprint，pre-commit 对 staged delta 做字节级 append-only 校验。
- 测试质量高：按 AC 组织、用 FakeHarnessAdapter 做状态机验收、真实 HTTP 契约测试、fresh-clone 重建派生状态的测试都有。
- 资源打包干净：profile/schema/prompt/agent/command/plugin/skill 全部走 importlib.resources，wheel 可安装。

### 3.4 重复造轮子探查

#### checkpoint.py 状态机是否可用 transitions 替代：不建议

量化：状态机部分（状态字面量 + `_save/_load/status` + 全部 22 处 `status` 赋值）约 100 行；`src/pco/checkpoint.py` 共 903 行，其余约 800 行是冻结消息边界、worker 契约组装、事务拼装与哈希、审批 diff、receipt、派生调度、重试恢复，与状态机无关。

transitions 是内存态 FSM 库，而 checkpoint 是带副作用的持久化 saga，两者需求不匹配：

- 持久化与崩溃恢复：PRD §9.3 要求每个边界先落盘再执行副作用、进程重启后从精确 phase 继续；transitions 不提供任何持久化，`_save/_load` 一行都省不掉。
- 每个"迁移"都伴随 Git commit、HTTP 调用、文件写入；`on_enter/on_exit` 回调只会把逻辑拆散到字符串派发的回调里，可读性反而变差。
- 状态机存在条件回跳（`RECOVERY → WORKER_RUNNING`、`COMMITTED_CONTEXT_PENDING → MEMORY_COMMITTED`、`AWAITING_META_APPROVAL → WORKER_RUNNING`），transitions 支持 conditional transition，但条件仍是现有守卫逻辑，无净收益。

结论：换 transitions 最多省 20–40 行样板，代价是新增依赖、字符串分发、静态检查变差。真正能减码的是按 PRD §18.3 把 god class 拆成独立 step 函数；durable runner（如 PRD §29 提到的 Domino）才与"持久化编排 + 恢复"需求对口。

#### 确实重复造轮子的地方

1. **检索层自建了两套"玩具搜索引擎"**。`src/pco/retrieval.py` 在 Tantivy/Milvus 之外完整实现了本地倒排索引（`terms: dict[str, list[str]]` + `lexical.json`）和哈希向量 + 余弦的稠密检索；而 `tantivy` 与 `pymilvus[milvus-lite]` 本来就是 pyproject 硬依赖。为"某个后端起不来"的边缘情况把两个成熟引擎的功能各抄了一遍简化版，`_eligible_backend_hits` 还要在三种后端间协调。建议改为 optional extra + 明确报错与 recovery 提示。

2. **自定义 registry 重复了 Python 的 entry point 机制**。`src/mem_core/registry.py` 手写 allowlist + lazy import，而 `importlib.metadata.entry_points` 本就提供"安装包注册、按名解析、惰性加载"的完整插件机制。这也是 §3.2 中"mem-core 硬编码 pco/research 导致分层倒挂"的根源——用 entry point group 注册 Profile 能力后，新增 Profile 就无需改 mem-core。

3. **小工具四处复制**：
   - `_now()` 在 `src/pco/archive.py` 与 `src/pco/sources.py` 各一份，而 `src/mem_core/models.py` 已有 `utc_now()`；
   - `src/pco/validation.py` 的 `_current()` 重新实现了 `src/mem_core/repository.py` 已有的 `current_records()`；
   - `_profile(repo_root)` / `_repository(repo_root)` 胶水在 backlinks / context / projections / retrieval 各写一遍，且 profile 路径回退逻辑略有差异；
   - `_legacy_external_refs` 三份拷贝（hook / workspace / validation）。

#### 不算造轮子的地方

- subprocess 调 git（而非 dulwich/pygit2）：直接用真实 git 二进制最可靠；
- argparse（而非 click/typer）：零依赖、够用；
- 自研 TransactionManager：领域相关的持久化 saga，MVP 规模无对口通用库（Temporal/Prefect 过重，PRD §7.3 已留口子）；
- 自研 markdown/上下文渲染：领域定制，模板库反而增加间接层；
- stdlib 原子写（`os.replace`）、锁文件、hash 工具：足够。

## 4. 优先级建议

### P0（契约正确性）

- 把 pco/research 注册移出 mem-core（应用层注入 registry）；
- 把 0.3.1→0.3.2 迁移改成通用机制或至少抽出到 pco 层，关闭"绕过 mem txn 写 canonical"的后门；
- 给 checkpoint 记录追加 revision 补上真实 `git_commit` 与派生结果。

### P1（结构）

- 拆分 `CheckpointEngine` 为 step 模块并让 workflow YAML 真正驱动；
- 合并三份 legacy refs；
- 合并小工具重复（`_now` / `_current` / `_profile` / `_repository` 胶水）；
- 把 approval 校验抽成 mem-core 内单一公共函数供 transaction 与 hook 复用。

### P2（性能）

- pre-commit hook 改增量校验；
- `search()` 复用已构建 generation、避免每次全量 `_documents`/chunking；
- archive 去重用持久化游标而非每次全量加载。
- 明确检索 fallback 去留：保留一套降级还是改为 optional extra + 明确报错；
- 用 10 万消息语料做一次基准，对照 §25.4。

### P3（收尾）

- 补 `--dry-run`；
- 清理死代码与未用 import；
- 评估 receipt 是否从 toast 改为真实消息插入；
- 完成真实 AFFiNE 部署验收以关闭 §28 最后一道门槛。

## 5. 结论

功能上这个项目已经非常接近 PRD v0.3.1 的 MVP 完成态，AC 覆盖和事务设计是亮点。需要警惕的不是"防御本身"，而是防御的粒度——全量校验、全量索引、三层回退把热路径复杂度推到了与目标规模不匹配的程度，以及 mem-core 与 pco 之间几处靠"特例"维持的边界侵蚀。
