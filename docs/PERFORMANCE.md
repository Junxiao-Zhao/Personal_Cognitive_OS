# PCO 性能基准（PRD §25.4 对照）

> 基准日期：2026-08-15
> 复现命令：`PCO_RUN_MILVUS=1 python scripts/benchmark_corpus.py`
> 上一轮基准：2026-08-09

## 环境

- Python 3.13（miniconda），WSL2，非沙箱环境（Milvus Lite loopback 可用）；
- Milvus Lite + Tantivy 原生后端；
- Git 2.x，单分支 `main`；
- 内存目录位于 /tmp（本地磁盘）。

## 规模

| 项目 | 数量 |
| --- | --- |
| 公开对话消息 | 100,000 |
| 事件 | 10,000 |
| 心理概念 | 5,000 |
| 来源注册 | 1,000 |
| search receipt | 1（共享） |
| 总计记录 | 116,001 |

## 结果

| 指标 | mode | 实测 | PRD §25.4 目标 | 达标 |
| --- | --- | --- | --- | --- |
| `validate_all`（全量校验，doctor/verify 入口） | full | 8.23 s | < 10 s | ✅ |
| messages-only 事务 `validate` | incremental | 0.70 s | < 10 s | ✅ |
| messages-only 事务 `commit`（含增量 hook） | incremental | 3.05 s | < 10 s | ✅ |
| 结构化 checkpoint 型事务 `validate` | incremental | 0.88 s | < 10 s | ✅ |
| 结构化 checkpoint 型事务 `commit`（含增量 hook） | incremental validate + incremental hook | 4.25 s | < 10 s | ✅ |
| 首次 `search`（含全量索引构建） | index build + query | 31.30 s | — | 参考 |
| warm `search`（索引已构建） | query only | 1.48 s | 本地普通检索 < 2 s | ✅ |

所有数值取 2026-08-15 本地实测（脚本输出 `validate_all / messages_validate / messages_commit / structured_validate / structured_commit / cold_search / warm_search` 字段）。

## 本轮优化内容（2026-08-15）

1. **pre-commit hook 结构化分支改为增量校验**（主因）：
   - 删除整树 `git archive` 物化 + `MemoryRepository.validate_all(staged)`；
   - 历史记录只做严格 envelope 校验（从 HEAD 字节读取，115k ≈ 0.16s）；
   - delta 记录复用事务侧同款 `validate_delta_records`（envelope + schema + revision 连续性）；
   - Profile Validator 在 base + delta 合并视图上运行，交叉引用语义与旧全量一致；
   - 仅按 `artifact_roots` 物化 staged artifact 目录，供自定义 Validator 读取。
   - 效果：结构化 checkpoint 型 commit 从上一轮 10.40 s 降至 4.25 s（约 -59%）。
2. **共享校验函数**：`mem_core.delta.validate_structured_delta` 统一事务侧与 hook 的结构化增量校验，避免两套实现漂移。

## 上一轮优化内容（2026-08-09）

1. **jsonschema validator 实例级缓存**（主因）：`Profile.schema_validator` 由"每次调用重建 `Draft202012Validator`"改为 Profile 实例级 dict 缓存；`validate_record_schema` 热路径先用 `is_valid` 快速失败，仅失败时 `iter_errors` 提取首个错误 JSON Pointer。逐条重建 5.5ms/条（100k ≈ 553s）→ 缓存后 0.06ms/条（115k ≈ 7s）。
2. **事务侧增量校验**：
   - messages-only 事务：不建 worktree，仅对 delta 做 envelope + schema + revision 连续性；base 流通过字节型 `git show`（`show_bytes`）读取并容错解析 latest 基线。基线含损坏行时视为不可靠，跳过该流 revision 断言，由 `mem git verify` 兜底（不误拒合法 delta）。
   - 结构化事务：历史记录只做严格 envelope 校验（115k ≈ 0.16s，与 `validate_all` 同错码）；delta 做 envelope/schema/revision；交叉校验在 base + delta 合并视图上运行（与 worktree 内容逐行一致，新增记录的 EVIDENCE_INELIGIBLE / EVIDENCE_REFERENCE_* / REFERENCE_NOT_FOUND 等全部执行）。
   - validation 结果新增 `mode: "incremental" | "full"` 与 `delta: {stream: count}`；`records` 语义统一为"本次实际校验的记录数"。
3. **pre-commit hook 字节读取去重**：`_check_append_only` 与 `_verify_increment` 原本各自对每个流调用 `git show` 读 old/staged 字节，改为每路径读取一次并复用（行为与错误码不变）。
4. **commit 自建 worktree**：`validate()` 不再有"准备 worktree"的副作用；`commit()` 在 validate 后自建（不存在时），messages-only 快路径不建 worktree。

## 偏差原因（相对上一版 343 s）

1. 上一版 343 s 的 ~99% 是"每条记录重建 jsonschema validator"（5.5ms/条）；缓存后全量 `validate_all` 降至约 7–8 s。
2. 上一版事务热路径对每个事务全量 `validate_all`（O(N)）；现按"messages-only / 结构化"分档增量，逐 turn 归档路径从 O(N) 压到 O(delta)。
3. 上一轮结构化 `commit` 的 pre-commit hook 仍物化 staged tree 并跑全量 `validate_all`（≈7–8 s），叠加事务侧增量 validate（≈0.8 s）与 hook 进程/profile 加载/字节读取/git 开销（≈2 s），实测 10.40 s。
4. 本轮把结构化 hook 改为增量后，剩余 4.25 s 主要是：hook 子进程 Python 启动 + Profile 加载（≈0.8 s）、base 字节读取与 envelope 校验（≈0.5 s）、增量校验与 Validator（≈0.2 s）、Git commit/worktree/索引无关开销（约 2–3 s）。全量 `validate_all` 仍保留给 `mem doctor` / `mem git verify` / `mem profile validate`。
5. 首次 `search` 仍含全量索引构建（Tantivy + Milvus，116k docs），31.3 s 不是纯查询延迟；warm query（索引已存在）为 1.48 s。

## 行为变更（本轮已文档化）

- messages-only 事务不再探测历史损坏：历史 envelope/schema/revision 问题只在 `mem doctor` / `mem git verify` / `mem profile validate` 全量入口暴露（与 hook 现有 messages-only 快速路径一致）。
- `mem txn validate` / `--dry-run` 同样走增量。
- 全量入口不变：`mem doctor`、`mem git verify`、`mem profile validate`。
- **新增（2026-08-15）**：结构化 pre-commit hook 不再对全部历史重复做 schema/revision 校验。历史完整性由 append-only 字节前缀校验 + transaction receipt 精确匹配 + base 严格 envelope 校验保证；历史损坏仍由 `mem doctor` / `mem git verify` / `mem profile validate` 全量暴露。Profile Validator 的交叉引用（`REFERENCE_NOT_FOUND`、`EVIDENCE_INELIGIBLE`、`EXTERNAL_REFERENCE_INVALID`、`META_PREVIOUS_REVISION_INVALID` 等）仍在 base + delta 合并视图上执行。

## 后续优化项（未纳入本轮任务，建议下一轮）

1. hook 进程固定开销（~0.8 s：Python 启动 + Profile 加载 + 入口点解析）可通过常驻服务或合并校验阶段摊薄。
2. 结构化 hook 的 artifact 物化可进一步按“本次变更的 artifact 文件”裁剪；当前 PCO/Research Validator 不读 artifact，成本已可忽略。
3. 全量 `validate_all` 仅用于 `mem doctor` / `mem git verify` / `mem profile validate`，不作为逐 turn 归档或 checkpoint 提交热路径。
4. 若未来 Profile 提供“Validator 文件依赖”声明，可将 `run_validators` 的 `repo_root` 进一步替换为仅含所需文件的轻量视图。
