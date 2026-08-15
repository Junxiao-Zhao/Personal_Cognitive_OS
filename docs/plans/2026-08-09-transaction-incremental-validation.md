# PCO 事务侧增量校验实施计划

> 日期：2026-08-09
> 关联：[性能基准](../PERFORMANCE.md) · [P0–P3 重构设计](../spec/2026-08-08-pco-refactor-design.md) · PRD §25.4

## 进度更新（2026-08-15）

原计划（2026-08-09）的 Task 1–4 已全部完成并合入 `main`；随后又完成原计划标记为“后续优化项”的 **结构化 pre-commit hook 增量校验**。

### 已完成提交

| 日期 | Commit | 内容 |
| --- | --- | --- |
| 2026-08-09 | `cc3d712` | Profile schema validator 缓存 |
| 2026-08-09 | `370e932` | messages-only 事务增量校验 |
| 2026-08-09 | `280b53a` | 结构化事务增量校验（事务侧） |
| 2026-08-09 | `ebe7198` | benchmark 扩展与文档同步 |
| 2026-08-15 | `fdf1a9f` | 结构化 pre-commit hook 增量校验（本轮追加） |

### 2026-08-15 追加内容

- 新增 `mem_core/delta.py::validate_structured_delta`，事务侧与 hook 共用同一套 base + delta 增量校验；
- `mem_core/hook.py` 结构化分支不再 `git archive` 整树物化 + `MemoryRepository.validate_all(staged)`：
  - 历史记录：HEAD 字节严格 envelope 校验（`JSONL_INVALID` / `ENVELOPE_INVALID` 同错码）；
  - delta 记录：envelope + schema + revision 连续性；
  - Profile Validator：base + delta 合并视图交叉校验；
  - 仅按 `artifact_roots` 物化 staged artifact 目录；
- 新增测试：`test_structured_pre_commit_hook_uses_incremental_validation`、`test_structured_pre_commit_hook_still_runs_cross_validators`；
- 基准（100k messages / 10k events / 5k concepts / 1k sources）：

| 指标 | 2026-08-09 | 2026-08-15 |
| --- | --- | --- |
| 结构化 checkpoint 型事务 commit | 10.40 s | 4.25 s |
| 结构化 checkpoint 型事务 validate | 0.81 s | 0.88 s |
| warm search | 1.27 s | 1.48 s |

- `pytest -q` 全绿；`PCO_RUN_MILVUS=1 python scripts/benchmark_corpus.py` 已重跑；
- `docs/PERFORMANCE.md` 已同步更新。

---

**Goal:** 让 `TransactionManager.validate / commit` 在 PRD 规模（100,000 messages / 10,000 events / 5,000 concepts / 1,000 sources）下、无模型调用时 < 10s。

**现状：** messages-only 单消息事务 commit 340.8s、全量 `validate_all` 343.2s（均为 34x 超标）。根因有两个，且第一个才是大头：

1. `Profile.validate_record_schema` 对每条记录重建 `Draft202012Validator`（读 schema 文件 + 编译），实测 **5.5ms/条**，100k 条 ≈ 553s；
2. 事务热路径对每个事务做全量 `validate_all`（O(N) jsonschema + envelope），pre-commit hook 已增量但事务侧没有。

**Architecture:** 保持 mem-core / pco 两层与 canonical/derived 分离不变。schema 校验器改为 Profile 实例级缓存；事务校验按"messages-only / 结构化"分档：messages-only 只做 delta envelope + schema + revision 连续性；结构化事务对历史记录做严格 envelope 校验（与 `validate_all` 同错码，实测 115k ≈ 0.16s）、对 delta 做 envelope/schema/revision，交叉校验在 base + delta 合并视图（等价 worktree 内容）上运行（读取 ≈0.5s + validator ≈0.03s，事务侧 validate 总计 ≈1s，语义与现状完全一致）；全量 `validate_all` 保留给 `mem doctor` / `mem git verify` / `mem profile validate`。注意：原计划撰写时结构化 `commit` 的 pre-commit hook 仍会物化 staged tree 并跑全量 `validate_all`（缓存后 ≈8s），故结构化 commit 实测 ≈9s；该限制已在 2026-08-15 追加优化中消除（见文首“进度更新”）。

**Tech Stack:** Python 3.11+、pydantic、jsonschema、Git。**本轮不新增任何运行时依赖。**

---

## 实测依据（2026-08-09，本地微基准）

| 项目 | 耗时 |
| --- | --- |
| 100k messages 读取 + json 解析 | 0.55s |
| 115k 记录 envelope + schema（缓存 validator，`is_valid`） | 6.98s（≈0.06ms/条） |
| 全量 `validate_profile`（116k 最新视图，含外部引用/证据/链接检查） | 0.03s |
| 现状逐条重建 validator | 5.5ms/条 → 100k ≈ 553s |

结论：

1. 343s 的 ~99% 是"每条记录重建 jsonschema validator"；缓存后**全量 validate_all ≈ 8s**，单独即可达标；
2. 增量校验把逐 turn 归档路径从 O(N) 压到 O(delta)，messages-only 事务预期 ≈ **0.6s**；
3. 交叉校验（validator 逻辑）本身极廉价，不需要为此设计"仅 delta 的引用解析"。

---

## 设计决策

### D1：schema validator 缓存（前置，两条路径都受益）

- `Profile.schema_validator(stream)` 改为实例级 dict 缓存（首次构建、后续复用）；
- `validate_record_schema` 热路径先用 `validator.is_valid(record)` 快速失败，仅失败时才 `iter_errors` 提取首个错误 JSON Pointer。
- 收益：100k messages 逐条 schema 校验 553s → 7s（实测）。

### D2：messages-only 快路径判定与实现

- 判定与 hook 一致：`all(op.op == "append" and op.stream == "messages" for op in operations)`；
- 快路径**不建 worktree**，直接对 `operation.record` 做：
  a. `RecordEnvelope.model_validate`（复用 hook `_delta_validate_messages` 逻辑，泛化到 delta 模块）；
  b. `profile.validate_record_schema("messages", record)`；
  c. revision 连续性：base 流用 `show_bytes(root, base_commit, path)`（delta.py 的字节型 `git show`，与 hook `_old_bytes` 同一模式；`MemoryRepository._git` 固定返回 str、不支持 `text=False`，不能复用；路径在 base_commit 缺失时返回 `b""`），逐行**容错解析**为 latest 基线（跳过解析失败/缺 `id`/`revision` 的行，历史损坏不阻断热路径，与 D6 一致），对 delta 按顺序断言 `revision == latest_by_id(id) + 1`；若基线**不可靠**（存在被跳过的行——损坏行可能是某 delta id 的最新 revision，继续断言会误拒合法 delta），则对该流跳过 revision 连续性断言，由 `mem git verify` 兜底；
- 跳过：全量 `validate_all`、`run_validators`、历史记录校验。

### D3：结构化事务增量

- delta 记录（所有涉及的 stream）做 envelope + schema + revision 连续性；
- `run_validators` 在 **base + delta 合并视图**（与 worktree 内容逐行一致）上运行，不改变 validator 契约：
  - 历史记录先做严格 envelope 校验（复用 `validate_all` 同款 `RecordEnvelope.model_validate` + `ENVELOPE_INVALID` 错码，防止 `latest_by_id` 对缺 `id`/`revision` 的历史记录抛裸 `KeyError`；实测 115k ≈ 0.16s），读取全量流 ≈0.5s；
  - 本次新增结构化记录的 `EVIDENCE_INELIGIBLE` / `EVIDENCE_REFERENCE_INVALID` / `EVIDENCE_REFERENCE_AMBIGUOUS` / `REFERENCE_NOT_FOUND` / `EXTERNAL_REFERENCE_INVALID` / `META_PREVIOUS_REVISION_INVALID` 等交叉校验全部执行——base 视图（不含 delta）会静默漏掉这些校验，必须用 base + delta 合并视图；
  - 天然覆盖"新增记录使既有引用歧义/失效"的语义，无需专门交叉检查；
  - 不需要改 `validate_profile` 签名或引入 `current` 参数。
- 理由：结构化事务的 O(N) 成本中 jsonschema/envelope 占 ~99%（缓存后 envelope 仅 ≈0.16s），validator 交叉检查仅 0.03s，保留 base + delta 合并视图零语义风险。

### D4：commit 自建 worktree

- `validate()` 不再有"准备 worktree"的副作用（messages-only 快路径不建；结构化路径仍需要）；
- `commit()` 在 validate 之后自行确保 worktree 存在（不存在才 `_prepare_worktree`），复用已存在的 worktree。

### D5：validation 结果元数据

- 增加 `mode: "incremental" | "full"`、`delta: {stream: count}`；
- `records` 语义统一为"本次实际校验的记录数"（增量 = delta 记录数；全量 = 总记录数）；
- 保留 `ok / profile / protected_streams / approval_* / proposal_hash / transaction_fingerprint`，checkpoint 代码只消费 `protected_streams`，不受影响。

### D6：行为变更（文档化）

- messages-only 事务不再探测历史损坏（历史 envelope/schema/revision 问题只在 `mem doctor` / `mem git verify` 暴露）——与 hook 现有 messages-only 快速路径行为一致；
- `mem txn validate` / `--dry-run` 同样走增量（它们是同一热路径）；
- 全量入口不变：`mem doctor`、`mem git verify`、`mem profile validate`。

---

## 正确性论证

1. **追加模型**：canonical 只追加，历史记录不可变；历史记录在其各自 commit 时已通过 envelope/schema/revision/validator 校验。增量只验证"本次新增"，历史正确性由历史校验 + append-only 不可变保证。
2. **引用方向**：消息只被结构化记录引用（`message:`），新增消息不可能使既有证据失效或产生歧义；结构化记录引用消息/来源/结构化实体——新增结构化记录只影响自身引用（D3 在 base + delta 合并视图上运行 `run_validators`，歧义语义与现状完全一致）。
3. **revision 连续性**：仅新增记录需要检查；历史序列在其 commit 时已验证且 append-only 保证不可改。messages-only 快路径对 base 流做一次 `latest_by_id` 基线（0.55s），按 delta 顺序维护局部 latest；基线不可靠（历史含损坏行）时跳过该流 delta 的 revision 断言，避免把合法 delta 误判为 `REVISION_SEQUENCE_INVALID`。
4. **hook 最终防线**：messages-only 提交被 hook 的 append-only + delta + receipt 校验拦截篡改；结构化提交的 pre-commit hook 已改为增量路径（2026-08-15 追加），最终防线仍为 append-only + transaction receipt 精确匹配 + base 严格 envelope + base/delta 交叉 Validator。

---

## 任务列表

每任务：先写失败测试 → 跑失败 → 实现 → 跑通 → 独立 commit；每任务结束时 `pytest -q` 全绿。

### Task 1：Profile schema validator 缓存

**Files:**
- Modify: `src/mem_core/profile.py`
- Modify: `tests/test_mem_core.py`

**Step 1: 写失败测试**

```python
def test_schema_validator_reused_within_profile(workspace) -> None:
    profile = workspace.repository.profile
    assert profile.schema_validator("messages") is profile.schema_validator("messages")


def test_cached_schema_validator_still_reports_first_error_pointer(workspace) -> None:
    record = {
        "id": "msg_bad",
        "revision": 1,
        "recorded_at": NOW,
        "schema_version": "conversation-message/v1",
        "payload": {
            "thread_id": "t", "epoch_id": "e", "harness": "h",
            "native_session_id": "s", "native_message_id": "n",
            "role": "robot", "kind": "conversation", "content": "x",
            "reasoning": None, "refs": [], "created_at": NOW,
        },
    }
    with pytest.raises(MemError) as exc:
        workspace.repository.profile.validate_record_schema("messages", record)
    assert exc.value.detail.code == "SCHEMA_VALIDATION_FAILED"
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_mem_core.py -k schema_validator -v`
Expected: 第一条失败（每次调用新建实例）。

**Step 3: 实现**

`Profile` 是 `@dataclass(slots=True)`（`__slots__` 仅含 `root/config/registry/raw`）且无自定义 `__init__`，在 `__init__` 里赋值新属性会抛 `AttributeError`。改为声明 dataclass 字段（`field(default_factory=dict, init=False, repr=False)`），`schema_validator` 查缓存；`validate_record_schema` 热路径改为：

```python
from dataclasses import dataclass, field  # 现有 `from dataclasses import dataclass` 增加 field

@dataclass(slots=True)
class Profile:
    root: Path
    config: ProfileConfig
    registry: ProfileRegistry
    raw: dict[str, Any]
    _schema_validators: dict[str, Draft202012Validator] = field(default_factory=dict, init=False, repr=False)

    def schema_validator(self, name: str) -> Draft202012Validator:
        cached = self._schema_validators.get(name)
        if cached is not None:
            return cached
        stream = self.stream(name)
        schema = json.loads((self.root / stream.schema_path).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self._schema_validators[name] = validator
        return validator


validator = self.schema_validator(stream_name)
if validator.is_valid(record):
    return
errors = sorted(validator.iter_errors(record), key=lambda e: list(e.absolute_path))
...
```

**Step 4: 跑测试确认通过**

Run: `pytest -q`

**Step 5: 提交**

```bash
git add src/mem_core/profile.py tests/test_mem_core.py
git commit -m "perf: cache compiled jsonschema validators per profile stream"
```

---

### Task 2：messages-only 事务增量校验

**Files:**
- Modify: `src/mem_core/transaction.py`
- Modify: `src/mem_core/hook.py`（复用 delta 判定/校验原语，行为不变）
- Create: `src/mem_core/delta.py`（`is_messages_only`、`validate_delta_records`）
- Modify: `tests/test_mem_core.py`

**Step 1: 写失败测试**

```python
def test_messages_only_validate_is_incremental(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    state = manager.begin(fingerprint_context={"kind": "delta"})
    manager.append(state.id, Operation(op="append", stream="messages", record=_message_record("native_delta_1")))
    validation = manager.validate(state.id)
    assert validation["mode"] == "incremental"
    assert validation["delta"] == {"messages": 1}
    assert validation["records"] == 1


def test_messages_only_validate_rejects_revision_collision(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    first = manager.begin(transaction_id="txn_collision_a", fingerprint_context={"kind": "delta"})
    manager.append(first.id, Operation(op="append", stream="messages", record=_message_record("native_collision")))
    manager.commit(first.id)

    second = manager.begin(transaction_id="txn_collision_b", fingerprint_context={"kind": "delta"})
    manager.append(second.id, Operation(op="append", stream="messages", record=_message_record("native_collision")))
    with pytest.raises(MemError) as exc:
        manager.validate(second.id)
    assert exc.value.detail.code == "REVISION_SEQUENCE_INVALID"


def test_messages_only_validate_skips_historical_corruption(workspace) -> None:
    path = workspace.config.memory_root / "raw" / "conversations" / "messages.jsonl"
    original = path.read_text(encoding="utf-8")
    path.write_text(original + '{"broken"\n{}\n', encoding="utf-8")
    workspace.repository._git("add", ".")
    workspace.repository._git("commit", "--no-verify", "-m", "fixture: corrupt history")
    assert workspace.repository.is_clean()

    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    state = manager.begin(fingerprint_context={"kind": "delta"})
    manager.append(state.id, Operation(op="append", stream="messages", record=_message_record("native_after_corruption")))
    validation = manager.validate(state.id)
    assert validation["mode"] == "incremental"
    assert validation["ok"] is True

    with pytest.raises(MemError) as exc:
        workspace.repository.verify()
    assert exc.value.detail.code == "JSONL_INVALID"


def test_messages_only_validate_skips_revision_check_when_baseline_corrupt(workspace) -> None:
    # 历史：msg_native_rev_corrupt rev 1 之后跟一条损坏行（实际应为 rev 2，但不可解析）。
    # 若按"容忍后的最新基线"继续断言，delta rev 3 会被误判 REVISION_SEQUENCE_INVALID；
    # 基线不可靠时应跳过该流 delta 的 revision 连续性断言。
    path = workspace.config.memory_root / "raw" / "conversations" / "messages.jsonl"
    base = _message_record("native_rev_corrupt")
    path.write_text(
        json.dumps(base, ensure_ascii=False, sort_keys=True)
        + '\n{"id":"msg_native_rev_corrupt","revision":2,"broken"\n',
        encoding="utf-8",
    )
    workspace.repository._git("add", ".")
    workspace.repository._git("commit", "--no-verify", "-m", "fixture: corrupt latest revision")
    assert workspace.repository.is_clean()

    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    state = manager.begin(fingerprint_context={"kind": "delta"})
    manager.append(state.id, Operation(op="append", stream="messages", record={**base, "revision": 3}))
    validation = manager.validate(state.id)
    assert validation["mode"] == "incremental"
    assert validation["ok"] is True


def _user_message(workspace) -> dict:
    """event() 的 evidence_refs 引用 message:msg_user_1，结构化测试必须先有这条消息。"""
    return envelope(
        "msg_user_1",
        "conversation-message/v1",
        {
            "thread_id": workspace.thread().thread_id,
            "epoch_id": workspace.thread().active_epoch_id,
            "harness": "fake",
            "native_session_id": "ses_fake",
            "native_message_id": "native_user_1",
            "role": "user",
            "kind": "conversation",
            "content": "这是用户证据。",
            "reasoning": None,
            "refs": [],
            "created_at": NOW,
        },
    )


def test_structured_validate_stays_full_until_task_3(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    state = manager.begin(transaction_id="txn_structured", fingerprint_context={"kind": "checkpoint"})
    manager.append(state.id, Operation(op="append", stream="messages", record=_user_message(workspace)))
    manager.append(state.id, Operation(op="append", stream="events", record=event()))
    validation = manager.validate(state.id)
    assert validation["mode"] == "full"
    # manager.validate() 在当前实现下对 worktree（base + delta）做 validate_all，
    # records 含本次新增的 2 条；canonical 主库只有 base。fixture 新 workspace 为 0，故 + 2。
    assert validation["records"] == workspace.repository.validate_all()["records"] + 2
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_mem_core.py -k "messages_only or structured_validate" -v`
Expected: 增量断言失败（当前 mode 无/records 为全量）。

**Step 3: 实现**

`src/mem_core/delta.py`：

```python
import subprocess


def show_bytes(root: Path, commit: str, path: str) -> bytes:
    """Read a path's bytes at a revision; missing path at that revision -> b''."""
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{path}"],
        capture_output=True,
        text=False,
        check=False,
    )
    return b"" if result.returncode else result.stdout


def is_messages_only(operations: Iterable[Operation]) -> bool:
    return all(op.op == "append" and op.stream == "messages" for op in operations)


def latest_base_by_id(lines: Iterable[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    """Tolerant latest-by-id over raw JSONL lines.

    Returns (latest, baseline_reliable). Lines that fail to parse, or whose
    id/revision are missing or of the wrong type, are skipped: historical
    corruption is deliberately not surfaced on the hot path (D6). Any skipped
    line marks the baseline unreliable: it could be the latest revision of a
    delta id, so revision-continuity assertions based on the tolerant baseline
    would spuriously reject valid deltas. The caller must then skip those
    assertions for the stream; `mem git verify` remains the strict entry point.
    """
    current: dict[str, dict[str, Any]] = {}
    reliable = True
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            record_id = record["id"]
            revision = record["revision"]
        except (json.JSONDecodeError, KeyError, TypeError):
            reliable = False
            continue
        if not isinstance(record_id, str) or not isinstance(revision, int) or isinstance(revision, bool):
            reliable = False
            continue
        previous = current.get(record_id)
        if previous is None or revision > previous["revision"]:
            current[record_id] = record
    return current, reliable


def validate_delta_records(profile: Profile, delta: dict[str, list[dict[str, Any]]],
                           current: dict[str, dict[str, dict[str, Any]]] | None = None) -> int:
    """Envelope + schema (+ revision continuity when current is provided) for delta records only."""
    count = 0
    for stream, records in delta.items():
        stream_current = current.get(stream, {}) if current is not None else None
        for record in records:
            try:
                envelope = RecordEnvelope.model_validate(record)
            except Exception as exc:
                raise MemError("ENVELOPE_INVALID", "envelope_validation", str(exc), stream=stream, record_id=record.get("id")) from exc
            if stream_current is not None:
                expected = stream_current.get(envelope.id, {}).get("revision", 0) + 1
                ensure(envelope.revision == expected, "REVISION_SEQUENCE_INVALID", "revision_validation",
                       f"Expected revision {expected}, got {envelope.revision}", stream=stream,
                       record_id=envelope.id, path="/revision", value=envelope.revision)
                stream_current[envelope.id] = record
            profile.validate_record_schema(stream, record)
            count += 1
    return count
```

`TransactionManager.validate` 分档：

```python
profile = self.repository.profile
delta = {}
for op in state.operations:
    if op.op == "append" and op.stream:
        delta.setdefault(op.stream, []).append(op.record)
if is_messages_only(state.operations):
    messages_path = profile.stream("messages").path
    base_bytes = show_bytes(self.repository.root, state.base_commit, messages_path)
    current, baseline_reliable = latest_base_by_id(base_bytes.decode("utf-8").splitlines())
    # 基线不可靠（历史含损坏行）时跳过 revision 连续性断言，避免误拒合法 delta（D6）
    count = validate_delta_records(profile, delta, {"messages": current} if baseline_reliable else None)
    validation = {"ok": True, "profile": f"{profile.name}@{profile.version}", "mode": "incremental",
                  "records": count, "delta": {name: len(records) for name, records in delta.items()}}
else:
    worktree = self._prepare_worktree(state)
    validation = {**self.repository.validate_all(root=worktree), "mode": "full"}
```

`commit()` 在 validate 后自建 worktree（不存在时才 `_prepare_worktree`）。hook.py 的 `messages_only` 判定与 `_delta_validate_messages` 改为引用 `delta.py` 的 `is_messages_only` / `validate_delta_records(..., current=None)`（行为不变，hook 不做 revision 连续性检查）。

**Step 4: 跑测试确认通过**

Run: `pytest -q`

**Step 5: 提交**

```bash
git add src/mem_core/transaction.py src/mem_core/hook.py src/mem_core/delta.py tests/test_mem_core.py
git commit -m "perf: validate messages-only transactions incrementally"
```

---

### Task 3：结构化事务增量（envelope/schema/revision delta + 合并 validator 视图）

**Files:**
- Modify: `src/mem_core/transaction.py`
- Modify: `tests/test_mem_core.py`

**Step 1: 写失败测试**

```python
# 替换 Task 2 新增的 test_structured_validate_stays_full_until_task_3：
# 其 mode == "full" / records == 全量 的断言在 Task 3 已过时，由下列测试取代（从测试文件中删除旧测试）。
def test_structured_validate_is_incremental(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    state = manager.begin(transaction_id="txn_structured_delta", fingerprint_context={"kind": "checkpoint"})
    # 复用 Task 2 的 _user_message(workspace)：event() 的 evidence_refs 引用 message:msg_user_1，
    # 缺失时交叉校验会先抛 EVIDENCE_NOT_FOUND，mode/delta 断言永远无法到达。
    manager.append(state.id, Operation(op="append", stream="messages", record=_user_message(workspace)))
    manager.append(state.id, Operation(op="append", stream="events", record=event()))
    validation = manager.validate(state.id)
    assert validation["mode"] == "incremental"
    assert validation["delta"] == {"messages": 1, "events": 1}
    assert validation["records"] == 2


def test_structured_incremental_still_runs_cross_validators(workspace) -> None:
    # 既有 EVIDENCE_INELIGIBLE / EVIDENCE_REFERENCE_INVALID / REFERENCE_NOT_FOUND 测试继续通过
    ...


def test_structured_incremental_rejects_bad_delta_envelope(workspace) -> None:
    record = event()
    record["revision"] = 99
    state = manager.begin(transaction_id="txn_bad_delta", fingerprint_context={"kind": "checkpoint"})
    manager.append(state.id, Operation(op="append", stream="events", record=record))
    with pytest.raises(MemError) as exc:
        manager.validate(state.id)
    assert exc.value.detail.code == "REVISION_SEQUENCE_INVALID"
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_mem_core.py -k structured_incremental -v`
Expected: mode 断言失败（当前为 full）。

**Step 3: 实现**

结构化分支改为：

```python
profile = self.repository.profile
worktree = self._prepare_worktree(state)
# 严格 envelope 基线：canonical root（head == base_commit 已断言）。与 validate_all 同错码——
# JSON 语法损坏由 iter_records 抛 JSONL_INVALID；JSON 合法但缺 id/revision 或 envelope 非法的
# 历史记录由 RecordEnvelope.model_validate 抛 ENVELOPE_INVALID（而非裸 KeyError）。
# 实测 115k 条 envelope-only ≈ 0.16s。
base_records: dict[str, list[dict[str, Any]]] = {}
base_current: dict[str, dict[str, dict[str, Any]]] = {}
for stream in profile.config.streams:
    records = list(self.repository.iter_records(stream))
    for record in records:
        try:
            RecordEnvelope.model_validate(record)
        except Exception as exc:
            raise MemError("ENVELOPE_INVALID", "envelope_validation", str(exc), stream=stream, record_id=record.get("id")) from exc
    base_records[stream] = records
    base_current[stream] = latest_by_id(records)
count = validate_delta_records(profile, delta, base_current)
# 交叉校验视图 = base + delta（与 worktree 内容逐行一致，且全部记录已过 envelope 校验）：
# 仅 base 视图会漏掉本次新增记录的 EVIDENCE_INELIGIBLE / EVIDENCE_REFERENCE_INVALID /
# EVIDENCE_REFERENCE_AMBIGUOUS / REFERENCE_NOT_FOUND 等全部检查。
merged = {stream: base_records[stream] + delta.get(stream, []) for stream in profile.config.streams}
profile.run_validators(worktree, merged)
validation = {"ok": True, "profile": f"{profile.name}@{profile.version}", "mode": "incremental",
              "records": count, "delta": {name: len(records) for name, records in delta.items()}}
```

说明：结构化路径 = 历史记录只做严格 envelope 校验（≈0.16s/115k，与 `validate_all` 同错码）、delta 记录做 envelope/schema/revision（revision 基线用 base 的 latest 视图，`validate_delta_records` 内部按序更新）、交叉校验在 base + delta 合并视图上运行（与现状 `validate_all(root=worktree)` 语义完全一致）——新增结构化记录的全部交叉校验都会执行，历史 jsonschema 不再重复。读取 ≈0.5s + envelope ≈0.16s + validator ≈0.03s，事务侧 validate 总计 ≈1s（不含 commit 时 pre-commit hook 的全量校验 ≈8s）。若后续要压读取，可给 `run_validators` 增加可选 `current` 参数，但本轮不做（保持 validator 契约不变）。

**Step 4: 跑测试确认通过**

Run: `pytest -q`

**Step 5: 提交**

```bash
git add src/mem_core/transaction.py src/mem_core/repository.py tests/test_mem_core.py
git commit -m "perf: validate structured transactions via delta records plus full validator view"
```

---

### Task 4：benchmark 扩展与文档同步

**Files:**
- Modify: `scripts/benchmark_corpus.py`
- Modify: `docs/PERFORMANCE.md`

**Step 1: 扩展脚本**

- 增加结构化 checkpoint 型事务计时（messages + event + hypothesis + continuation 多流 append）；
- warm query 单独计时：先构建一次索引，再测纯查询（不含索引构建）；
- 输出每项 `mode` 与耗时。

**Step 2: 重跑基准**

Run: `PCO_RUN_MILVUS=1 python scripts/benchmark_corpus.py`
Expected: messages-only validate ~0.6s、messages-only commit（含增量 hook）~1s、结构化 validate ~1s、结构化 commit（含全量 hook）~9s、validate_all ~8s、warm query 单独记录；全部 < 10s。（2026-08-15 追加 hook 增量后，结构化 commit 实测 4.25s。）

**Step 3: 更新文档**

更新 `docs/PERFORMANCE.md`：结果表、偏差原因（validator 重建为主因）、删除/改写已完成的优化项、记录行为变更（增量校验不探测历史损坏，verify/doctor 兜底）。

**Step 4: 提交**

```bash
git add scripts/benchmark_corpus.py docs/PERFORMANCE.md
git commit -m "perf: benchmark messages and structured transactions plus warm queries"
```

---

## 验收标准

| 指标 | 目标 | 预期实测 |
| --- | --- | --- |
| messages-only 单事务 validate | < 10s | ~0.6s |
| messages-only 单事务 commit（含增量 hook） | < 10s | ~1s |
| 结构化 checkpoint 型事务 validate | < 10s | ~1s |
| 结构化 checkpoint 型事务 commit（含增量 hook） | < 10s | ~4.25s |
| 全量 `validate_all`（doctor/verify/profile validate） | < 10s | ~8s |
| warm query（不含索引构建） | < 2s | 单独记录 |
| `PCO_RUN_MILVUS=1 pytest -q` | 全绿 | 64 passed |

证据与 revision 正确性：增量校验覆盖 delta 的 envelope/schema/revision；引用/歧义语义与现状一致（结构化走 base + delta 合并 validator 视图）；历史完整性由 verify/doctor 兜底；行为变更写入 PERFORMANCE.md。

---

## 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| messages-only 不再全量，历史损坏不在逐 turn 热路径暴露 | 与 hook 现有 messages-only 快速路径一致；`mem git verify` / `mem doctor` 全量兜底；写入 PERFORMANCE.md |
| Profile 长驻进程内 schema 热更新不生效 | schema 属 profile 包静态文件；每次 `Profile.load` 重建缓存（hook 子进程、workspace refresh 均如此） |
| `show_bytes(root, commit, path)` 内存占用（100k 行 ≈ 10–20MB） | 可接受；如需再降可改用流式 `git cat-file` |
| 历史损坏行恰好是被追加 id 的最新 revision 时，容错基线会误拒合法 delta | `latest_base_by_id` 返回 `(latest, baseline_reliable)`：任一历史行被跳过即视为基线不可靠，messages-only 路径对该流跳过 revision 连续性断言；由 `mem git verify` 全量兜底；回归测试 `test_messages_only_validate_skips_revision_check_when_baseline_corrupt` |
| Task 3 引入结构化增量后语义回归 | `run_validators` 在 base + delta 合并视图运行，正确性与现状完全一致；既有 EVIDENCE_* / REFERENCE_* 测试兜底 |
| 缓存后全量路径接近 10s 上限 | 2026-08-09 时结构化 commit 含 hook ≈9s（validate ~1s + pre-commit hook 全量 validate_all ≈8s），仍 <10s 但余量小；该风险已在 2026-08-15 追加优化中消除：hook 结构化分支改增量后实测 4.25s；doctor/verify 属低频命令，全量 ≈8s 可接受 |

---

## 执行前置

- 确认工作树干净、editable 安装（`python -m pip install -e '.[dev]'`）；
- 每任务提交前 `pytest -q` 全绿；
- 提交后按序推进，不并行改同一文件。
