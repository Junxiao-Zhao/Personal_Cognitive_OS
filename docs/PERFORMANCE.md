# PCO 性能基准（PRD §25.4 对照）

> 基准日期：2026-08-08
> 复现命令：`PCO_RUN_MILVUS=1 python scripts/benchmark_corpus.py`

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

| 指标 | 实测 | PRD §25.4 目标 | 达标 |
| --- | --- | --- | --- |
| `validate_all`（全量校验） | 343.2 s | 无模型调用 validate/commit < 10 s | ❌ 34x |
| `mem txn commit`（单条 messages 小事务，含 pre-commit hook） | 340.8 s | 同上 | ❌ 34x |
| 首次 `search`（含全量索引构建） | 29.2 s | 本地普通检索 < 2 s | ❌（含构建，非纯查询） |

## 偏差原因

1. **事务侧仍全量校验（主因）**：`TransactionManager.commit` → `validate()` 会在 worktree 上对整个语料库执行 `validate_all`，即每个 checkpoint/归档小事务都是 O(N)。pre-commit hook 已改为增量（Task 10），但事务热路径没有同步增量，340 s 基本全部耗在这里。
2. **逐条 jsonschema + pydantic 校验成本高**：116k 条记录逐条 `RecordEnvelope.model_validate` + `Draft202012Validator.iter_errors`，jsonschema 是主要单项开销。
3. **首次检索含索引构建**：`search()` 在 generation 缺失时先构建 Tantivy + Milvus 全量索引（116k docs），29.2 s 是"构建 + 查询"，不是纯查询延迟；warm query 未单独计时。

## 后续优化项（未纳入本轮任务，建议下一轮）

1. `TransactionManager.validate` 增量：messages-only 事务仅做 delta envelope/schema 校验（复用 hook 的 `_delta_validate_messages` 逻辑），结构化变更才全量；`mem git verify` 保留全量入口。
2. jsonschema 校验降成本：schema 已预编译，但可考虑对热路径 stream（messages）跳过 `iter_errors` 或合并 envelope 校验；评估 pydantic 模型复用。
3. warm query 计时与候选池裁剪：单独测量"索引已存在"的纯查询延迟，确认是否达到 2 s。
4. 全量 `validate_all` 仅用于 `mem doctor` / `mem git verify` / 结构化 commit，不作为逐 turn 归档热路径。
