# PCO P0–P3 重构实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **Codex/OpenCode 执行提示：** 本环境未安装 executing-plans，按本文件任务逐项执行即可。每任务先写失败测试 → 跑失败 → 实现 → 跑通 → 提交；每任务结束时 `pytest -q` 全绿。

**Goal:** 依据 [PROJECT_REVIEW_v0.3.1.md](../PROJECT_REVIEW_v0.3.1.md) 完成 P0–P3 改造：entry-point 化 registry、删除 0.3.1→0.3.2 迁移、checkpoint 记录补全、审批校验收敛、`--dry-run`、CheckpointEngine 包拆分、小工具去重、pre-commit hook 增量校验、search 复用索引 generation、删除检索 fallback、archive 去重、性能基准、文档同步。

**Architecture:** 保持 mem-core / pco 两层与 canonical/derived 分离不变。mem-core 通过 `mem_core.capabilities` entry-point group 发现 Profile 能力，不再 import pco；pco 的 checkpoint 逻辑拆为 `src/pco/checkpoint/` 下的 step 模块 + 薄 facade；检索与 pre-commit 改为"构建产物复用 + 增量校验"，原生后端失败直接报错而非静默降级。

**Tech Stack:** Python 3.11+、pydantic、jsonschema、Hydra、Tantivy、Milvus Lite、Git。**本轮不新增任何运行时依赖。**

**执行前置：** 确认已 editable 安装（entry point 发现依赖安装元数据）：

```bash
python -m pip install -e '.[dev]'
```

---

## P0 契约正确性

### Task 1: mem-core registry 改为 entry point 发现

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/mem_core/registry.py`
- Create: `tests/test_registry_discovery.py`

**Step 1: 写失败测试**

```python
import pytest

from mem_core.errors import MemError
from mem_core.registry import default_registry, discover_registry


def test_default_registry_discovers_pco_and_research_capabilities():
    registry = default_registry()
    assert callable(registry.resolve("pco.validate"))
    assert callable(registry.resolve("pco.retrieval.search"))
    assert callable(registry.resolve("pco.projection.affine"))
    assert callable(registry.resolve("research.retrieval.search"))


def test_discover_registry_unknown_group_resolves_nothing():
    registry = discover_registry(group="no.such.group")
    with pytest.raises(MemError) as exc:
        registry.resolve("anything")
    assert exc.value.detail.code == "ENTRYPOINT_NOT_ALLOWED"
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_registry_discovery.py -v`
Expected: 失败（`ENTRYPOINT_NOT_ALLOWED` 或导入错误，取决于当前实现）。

**Step 3: 实现**

`pyproject.toml` 增加：

```toml
[project.entry-points."mem_core.capabilities"]
pco.validate = "pco.validation:validate_profile"
pco.retrieval.search = "pco.retrieval:search"
pco.backlinks.build = "pco.backlinks:build"
pco.context.render = "pco.context:render"
pco.index.build = "pco.retrieval:build_index"
pco.projection.markdown = "pco.projections:project_markdown"
pco.projection.affine = "pco.projections:project_affine"
research.validate = "pco.research_profile:validate_profile"
research.retrieval.search = "pco.research_profile:search"
research.projection.markdown = "pco.research_profile:project_markdown"
```

`src/mem_core/registry.py` 整体替换为：

```python
from __future__ import annotations

from importlib import metadata

from .profile import ProfileRegistry


ENTRY_POINT_GROUP = "mem_core.capabilities"


def discover_registry(group: str = ENTRY_POINT_GROUP) -> ProfileRegistry:
    """Build an allowlist from installed entry points.

    The distribution that ships each Profile declares its capabilities under
    the ``mem_core.capabilities`` group. mem-core never imports domain
    modules by name, keeping it profile-neutral.
    """
    registry = ProfileRegistry()
    for entry in metadata.entry_points(group=group):
        registry.register_lazy(entry.name, entry.value)
    return registry


def default_registry() -> ProfileRegistry:
    return discover_registry()
```

**Step 4: 跑测试确认通过**

Run: `pytest tests/test_registry_discovery.py -v`
Expected: PASS。随后跑全量 `pytest -q` 确认既有测试不受影响（如有失败，是测试环境未 editable install，先执行前置安装）。

**Step 5: 提交**

```bash
git add pyproject.toml src/mem_core/registry.py tests/test_registry_discovery.py
git commit -m "refactor: discover profile capabilities via entry points"
```

---

### Task 2: 删除 0.3.1→0.3.2 Profile 迁移

**Files:**
- Modify: `src/mem_core/hook.py`
- Modify: `src/pco/workspace.py`
- Modify: `src/pco/validation.py`
- Modify: `tests/test_cli_integration.py`

**Step 1: 写失败测试（替换旧迁移测试）**

删除 `tests/test_cli_integration.py` 中的 `test_workspace_migrates_old_canonical_profile_before_refresh`，替换为：

```python
def test_old_profile_version_fails_with_clear_error(workspace) -> None:
    marker_path = workspace.config.memory_root / ".mem-profile.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["version"] = "0.3.1"
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    workspace.repository._git("add", ".mem-profile.json")
    workspace.repository._git("commit", "--no-verify", "-m", "fixture: emulate unsupported profile version")
    with pytest.raises(MemError) as exc_info:
        Workspace(workspace.config).refresh_repository_profile()
    assert exc_info.value.detail.code == "PROFILE_MARKER_MISMATCH"
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_cli_integration.py -v`
Expected: 新测试失败（当前会尝试自动迁移），旧测试删除。

**Step 3: 实现**

`src/mem_core/hook.py`：
- 删除模块常量 `PCO_SEARCH_RECEIPT_SCHEMA_SHA256`、`PCO_SEARCH_RECEIPT_STREAM`；
- 删除函数 `_legacy_external_refs`、`_verify_profile_migration`；
- `validate_repository` 中删除迁移分支，恒走 `_verify_increment`：

```python
increment = _verify_increment(root, old_root, staged_root, profile)
```

- 删除因此不再使用的 import（`copy`、`hashlib`；`tarfile` 保留到 Task 8 再做增量改造）。

`src/pco/workspace.py`：
- 删除 `_legacy_external_refs` 与 `_migrate_canonical_profile` 全部代码；
- `refresh_repository_profile` 替换为：

```python
def refresh_repository_profile(self) -> MemoryRepository:
    canonical = self.canonical_profile()
    marker_path = self.config.memory_root / ".mem-profile.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if canonical.name != marker.get("name") or canonical.version != marker.get("version"):
        raise MemError(
            "PROFILE_MARKER_MISMATCH",
            "profile_load",
            f"Canonical Profile {canonical.name}@{canonical.version} does not match .mem-profile.json",
            path=str(marker_path),
            recovery=["Recreate the workspace", "Restore a matching Profile version"],
        )
    self.profile = canonical
    self.profile_path = self.config.memory_root / "profiles" / self.profile.name
    self.repository = MemoryRepository(self.config.memory_root, self.profile)
    return self.repository
```

- 删除因此不再使用的 import（`hashlib`、`yaml` 如不再被引用）。

`src/pco/validation.py`：
- 删除 `_legacy_external_refs` 函数；
- `EXTERNAL_REFERENCE_INVALID` 校验删除 `legacy_key` 分支，恒要求"completed 的 search receipt 且 URL 出现在 receipt payload 中"。

**Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿（迁移测试被替换；其余不受影响）。

**Step 5: 提交**

```bash
git add src/mem_core/hook.py src/pco/workspace.py src/pco/validation.py tests/test_cli_integration.py
git commit -m "refactor: drop 0.3.1 profile migration support"
```

---

### Task 3: checkpoint canonical 记录携带真实 commit 与派生结果

**Files:**
- Modify: `src/pco/checkpoint.py`
- Modify: `tests/test_checkpoint.py`

**Step 1: 写失败测试**

```python
def test_checkpoint_record_carries_real_commit_and_derivations(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    result = CheckpointEngine(workspace, adapter).request("manual")
    record = workspace.repository.current_records("checkpoints")[result["checkpoint_id"]]
    assert record["revision"] == 1
    assert record["payload"]["git_commit"] == result["receipt"]["git_commit"]
    assert record["payload"]["status"] in {"committed", "committed_with_pending_derivations"}
    assert record["payload"]["derivations"] != {"index": "scheduled", "backlinks": "scheduled", "projection": "scheduled"}
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_checkpoint.py::test_checkpoint_record_carries_real_commit_and_derivations -v`
Expected: 失败（当前 `git_commit` 为 None、derivations 为 scheduled）。

**Step 3: 实现**

`src/pco/checkpoint.py`：
- `_prepare_candidate` 中删除 `_checkpoint_operation(...)` 调用与 `operations.append(checkpoint_operation)`；
- 删除 `_checkpoint_operation` 方法；
- 新增 `_write_checkpoint_record`，在 `_finalize_committed` 与 `retry_derivations` 完成派生、设好最终 status、`self._save(state)` 之后调用（幂等：记录已存在则跳过）：

```python
def _write_checkpoint_record(self, state: CheckpointState) -> None:
    if self.workspace.repository.current_records("checkpoints").get(state.id) is not None:
        return
    pending = any(not item.get("ok", False) for item in state.derivations.values())
    binding = self.workspace.binding()
    approval = state.decision or ("not_required" if not state.protected_streams else "no")
    record = {
        "id": state.id,
        "revision": 1,
        "recorded_at": utc_now(),
        "schema_version": "pco/checkpoint/v1",
        "payload": {
            "thread_id": binding.thread_id,
            "harness_binding_id": binding.id,
            "parent_session_id": state.parent_session_id,
            "archive_cursor": state.archive_cursor,
            "source_hashes": state.source_hashes,
            "worker": state.worker_handle,
            "runtime": state.harness_runtime,
            "trigger": state.trigger,
            "status": "committed_with_pending_derivations" if pending else "committed",
            "message_range": {"after": state.after_message_id, "through": state.through_message_id},
            "transaction_id": state.transaction_id,
            "git_commit": state.commit,
            "operation_counts": dict(state.operation_counts),
            "proposal_hash": state.proposal_hash,
            "promotion_proposal_hash": state.promotion_proposal_hash,
            "approval_decision": approval,
            "protected_streams": state.protected_streams,
            "promotion_protected_streams": state.promotion_protected_streams,
            "meta_revision": state.meta_revision,
            "continuation_revision": state.continuation_revision,
            "derivations": {
                name: ({"ok": True} if item.get("ok") else {"ok": False, "pending": True})
                for name, item in state.derivations.items()
            },
            "versions": {
                "profile": f"{self.workspace.profile.name}@{self.workspace.profile.version}",
                "policy_hash": self.workspace.profile.policy_hash,
                "workflow": "consolidate@0.3.1",
                "skills": state.skill_versions,
            },
            "warnings": [],
            "started_at": state.created_at,
            "ended_at": utc_now(),
            "retry_count": state.retries,
        },
    }
    manager = TransactionManager(self.workspace.repository, self.workspace.config.state_root)
    txn = manager.begin(
        transaction_id=f"txn_checkpoint_{state.id[5:17]}_{uuid.uuid4().hex[:8]}",
        fingerprint_context={"kind": "checkpoint_result", "checkpoint_id": state.id},
    )
    manager.append(txn.id, Operation(op="append", stream="checkpoints", record=record))
    manager.commit(txn.id)
```

**Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿。注意 `request()` 返回前会额外产生一个 checkpoint-result 小事务，receipt/`last_consolidated_message_id` 语义不变。

**Step 5: 提交**

```bash
git add src/pco/checkpoint.py tests/test_checkpoint.py
git commit -m "fix: persist real git commit and derivation results in checkpoint records"
```

---

### Task 4: 审批校验收敛到 mem-core 公共函数

**Files:**
- Create: `src/mem_core/approval.py`
- Modify: `src/mem_core/transaction.py`
- Modify: `src/mem_core/hook.py`

**Step 1: 写失败测试（重构前先锁行为）**

`tests/test_mem_core.py` 追加：

```python
from mem_core.approval import verify_approval_receipt


def test_shared_approval_verification_rejects_stale_hash(workspace) -> None:
    state = workspace.manager.begin(fingerprint_context={"kind": "shared_approval"})
    workspace.manager.append(state.id, Operation(op="append", stream="meta_revisions", record=meta()))
    workspace.manager.attach_approval(
        state.id,
        checkpoint_id="ckpt_shared",
        proposal_hash_value=state.proposal_hash,
    )
    state = workspace.manager.load(state.id)
    protected = {"meta_revisions"}
    with pytest.raises(MemError) as exc:
        verify_approval_receipt(
            receipt=state.approval_receipt,
            operations=state.operations,
            protected=protected,
            profile=workspace.repository.profile,
            reviewed_proposal_hash=state.proposal_hash,
            transaction_proposal_hash="sha256:stale",
            transaction_fingerprint=state.transaction_fingerprint,
        )
    assert exc.value.detail.code == "APPROVAL_STALE"
```

（`workspace` fixture 需暴露 `manager`；若无，测试内自行 `TransactionManager(workspace.repository, workspace.config.state_root)`。）

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_mem_core.py -k shared_approval -v`
Expected: 失败（`from mem_core.approval import ...` ImportError）。

**Step 3: 实现**

`src/mem_core/approval.py`：

```python
from __future__ import annotations

from typing import Any

from .errors import ensure
from .models import ApprovalReceipt, Operation, protected_operations_hash
from .profile import Profile


def verify_approval_receipt(
    *,
    receipt: ApprovalReceipt | None,
    operations: list[Operation],
    protected: set[str],
    profile: Profile,
    reviewed_proposal_hash: str,
    transaction_proposal_hash: str,
    transaction_fingerprint: str,
) -> None:
    """Verify an approval receipt against the exact operations under review."""
    ensure(
        receipt is not None,
        "USER_APPROVAL_REQUIRED",
        "write_policy",
        f"Protected streams require approval: {', '.join(sorted(protected))}",
        retryable=True,
        recovery=["Show the exact protected diff to the user and attach a matching approval receipt"],
    )
    assert receipt is not None
    ensure(receipt.proposal_hash == reviewed_proposal_hash, "APPROVAL_STALE", "write_policy", "Reviewed proposal changed after approval")
    ensure(receipt.transaction_proposal_hash == transaction_proposal_hash, "APPROVAL_STALE", "write_policy", "Transaction proposal changed after approval")
    ensure(receipt.transaction_fingerprint == transaction_fingerprint, "APPROVAL_STALE", "write_policy", "Transaction changed after approval")
    ensure(
        receipt.protected_operations_hash == protected_operations_hash(operations, protected),
        "APPROVAL_STALE",
        "write_policy",
        "Protected operations changed after approval",
    )
    for operation in operations:
        if operation.op != "append" or operation.stream not in protected or operation.record is None:
            continue
        pointer = profile.stream(operation.stream).approval_ref_pointer
        if not pointer:
            continue
        value: Any = operation.record
        for part in pointer.lstrip("/").split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            ensure(
                isinstance(value, dict) and key in value,
                "APPROVAL_REF_MISSING",
                "write_policy",
                f"Protected record is missing approval reference at {pointer}",
                stream=operation.stream,
                record_id=operation.record.get("id"),
                path=pointer,
            )
            value = value[key]
        ensure(
            value == receipt.id,
            "APPROVAL_REF_MISMATCH",
            "write_policy",
            "Protected record approval reference does not match the attached receipt",
            stream=operation.stream,
            record_id=operation.record.get("id"),
            path=pointer,
            value=value,
        )
```

`src/mem_core/transaction.py`：`_verify_approval` 方法体替换为对 `verify_approval_receipt` 的调用（参数取 `state.fingerprint_context` / `state.proposal_hash` / `state.transaction_fingerprint`）。

`src/mem_core/hook.py`：`_verify_increment` 的 protected 分支替换为对 `verify_approval_receipt` 的调用（receipt 从 `transaction_record["approval_receipt"]` 解析，参数取 transaction_record 的 proposal_hash / fingerprint_context / fingerprint）。

**Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿。若有测试断言错误 phase 为 `pre_commit`，改为断言 code（新 phase 统一为 `write_policy`）。

**Step 5: 提交**

```bash
git add src/mem_core/approval.py src/mem_core/transaction.py src/mem_core/hook.py tests/test_mem_core.py
git commit -m "refactor: share approval receipt verification between transaction and pre-commit"
```

---

### Task 5: `mem txn commit --dry-run`

**Files:**
- Modify: `src/mem_core/transaction.py`
- Modify: `src/mem_core/cli.py`
- Modify: `tests/test_mem_core.py`

**Step 1: 写失败测试**

```python
def test_txn_commit_dry_run_does_not_commit(workspace) -> None:
    manager = workspace.manager
    state = manager.begin(fingerprint_context={"kind": "dry_run_test"})
    manager.append(state.id, Operation(op="append", stream="events", record=event()))
    head_before = workspace.repository.head()
    result = manager.commit(state.id, dry_run=True)
    assert result["dry_run"] is True
    assert result["validation"]["ok"] is True
    assert workspace.repository.head() == head_before
    loaded = manager.load(state.id)
    assert loaded.commit is None
    assert loaded.status == "validated"
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_mem_core.py -k dry_run -v`
Expected: 失败（`commit()` 不接受 `dry_run` 关键字）。

**Step 3: 实现**

`src/mem_core/transaction.py` 的 `commit` 签名改为 `def commit(self, transaction_id: str, *, dry_run: bool = False) -> dict[str, Any]`，方法开头：

```python
if dry_run:
    validation = self.validate(transaction_id, require_approval=True)
    state = self.load(transaction_id)
    return {"ok": True, "dry_run": True, "transaction_id": transaction_id, "validation": validation, "would_commit": True}
```

`src/mem_core/cli.py`：

```python
commit = txn_commands.add_parser("commit")
commit.add_argument("--id", required=True)
commit.add_argument("--dry-run", action="store_true")
```

```python
if args.txn_command == "commit":
    return manager.commit(args.id, dry_run=args.dry_run)
```

**Step 4: 跑测试确认通过**

Run: `pytest tests/test_mem_core.py -k dry_run -v`，再 `pytest -q`。
Expected: 全绿。

**Step 5: 提交**

```bash
git add src/mem_core/transaction.py src/mem_core/cli.py tests/test_mem_core.py
git commit -m "feat: add mem txn commit --dry-run"
```

---

## P1 结构

### Task 6: 拆分 CheckpointEngine 为 step 模块

**Files:**
- Create: `src/pco/checkpoint/__init__.py`、`state.py`、`steps.py`、`approval.py`、`finalize.py`、`derivations.py`、`recovery.py`
- Modify: `src/pco/checkpoint.py`（删除，改为 facade）→ 建议 `git mv src/pco/checkpoint.py src/pco/checkpoint/__init__.py` 后逐步抽出

**Step 1: 先写模块边界测试（行为不变）**

现有 `tests/test_checkpoint.py`、`tests/test_acceptance_flows.py` 就是行为契约。先跑一遍作为基线：

Run: `pytest tests/test_checkpoint.py tests/test_acceptance_flows.py -q`
Expected: 全绿（基线）。

**Step 2: 拆分（纯移动，不改行为）**

模块映射（方法 → 目标模块函数，函数签名统一 `fn(engine, ...)`，`engine` 提供 `workspace`/`adapter`/`archive`/`sources`/`manager`；`from . import CheckpointEngine` 仅在 `TYPE_CHECKING` 下使用避免循环导入）：

| 方法 | 目标模块 |
| --- | --- |
| `CheckpointState`、状态常量、`_save/_load/status/should_auto_checkpoint`、锁逻辑 | `state.py` |
| `_freeze`、`_worker_profile_contract`、`_prepare_candidate`、`_validate_rejection_candidate`、`_effective_search_receipts`、`_protected_diff`、`_write_checkpoint_record` | `steps.py` / `finalize.py` |
| `decide` | `approval.py` |
| `_commit_and_finalize`、`_finalize_committed`、`_receipt` | `finalize.py` |
| `_run_derivations`、`_cleanup_worker`、`retry_derivations` | `derivations.py` |
| `retry`、`abort`、`_recover` | `recovery.py` |

`src/pco/checkpoint/__init__.py` 保留 `CheckpointEngine` 类作为 facade：持有 workspace/adapter、初始化三个子对象，公开方法 `request/decide/status/should_auto_checkpoint/retry/retry_derivations/abort`，各自一行委托给模块函数。`active_path`、`_checkpoint_dir` 等基础设施留在 facade 或 `state.py`。

**Step 3: 验证行为不变**

Run: `pytest -q`
Expected: 全绿。若测试 import 了 `from pco.checkpoint import CheckpointEngine`，facade 保证该路径不变。

**Step 4: 提交**

```bash
git add src/pco/checkpoint.py src/pco/checkpoint
git commit -m "refactor: split CheckpointEngine into step modules"
```

---

### Task 7: 小工具去重

**Files:**
- Create: `src/pco/repo_loader.py`
- Modify: `src/mem_core/models.py`、`src/mem_core/repository.py`
- Modify: `src/pco/backlinks.py`、`src/pco/context.py`、`src/pco/projections.py`、`src/pco/retrieval.py`、`src/pco/validation.py`、`src/pco/archive.py`、`src/pco/sources.py`

**Step 1: 写失败测试**

`tests/test_mem_core.py` 追加：

```python
from mem_core.models import latest_by_id


def test_latest_by_id_keeps_highest_revision():
    records = [
        {"id": "a", "revision": 1, "payload": {}},
        {"id": "a", "revision": 2, "payload": {}},
        {"id": "b", "revision": 1, "payload": {}},
    ]
    assert latest_by_id(records) == {"a": records[1], "b": records[2]}
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_mem_core.py -k latest_by_id -v`
Expected: 失败（ImportError）。

**Step 3: 实现**

`src/mem_core/models.py` 新增：

```python
def latest_by_id(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for record in records:
        previous = current.get(record["id"])
        if previous is None or record["revision"] > previous["revision"]:
            current[record["id"]] = record
    return current
```

（顶部补 `from collections.abc import Iterable` 或 `from typing import Iterable`。）

`src/mem_core/repository.py`：`current_records` 改为 `return latest_by_id(self.iter_records(stream_name, root=root))`。

`src/pco/repo_loader.py`：

```python
from __future__ import annotations

from pathlib import Path

from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from .paths import bundled_profile


def profile_for_repo(repo_root: Path) -> Profile:
    canonical = repo_root / "profiles" / "pco"
    return Profile.load(canonical if canonical.exists() else bundled_profile(), default_registry())


def repository_for_repo(repo_root: Path) -> MemoryRepository:
    return MemoryRepository(repo_root, profile_for_repo(repo_root))
```

替换点（各自删除本地重复实现，改调 `repo_loader`）：
- `src/pco/backlinks.py`：`_profile(repo_root)` → `profile_for_repo`；
- `src/pco/context.py`：`profile_path`/`Profile.load` 逻辑 → `profile_for_repo`；`_current` → 直接 `repository.current_records(stream).get(record_id)`；
- `src/pco/projections.py`：`_repository(repo_root)` → `repository_for_repo`；
- `src/pco/retrieval.py`：`build_index` 与 `search` 中的 profile 加载 → `profile_for_repo`；
- `src/pco/validation.py`：`_current(records)` → `latest_by_id(records)`；
- `src/pco/archive.py`、`src/pco/sources.py`：删除本地 `_now`，改 `from mem_core.models import utc_now`，调用处替换。

**Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿。

**Step 5: 提交**

```bash
git add src/mem_core/models.py src/mem_core/repository.py src/pco/repo_loader.py src/pco/backlinks.py src/pco/context.py src/pco/projections.py src/pco/retrieval.py src/pco/validation.py src/pco/archive.py src/pco/sources.py tests/test_mem_core.py
git commit -m "refactor: deduplicate repo loading and latest-revision helpers"
```

---

### Task 8: 锁写入、死代码与未用 import 清理

**Files:**
- Modify: `src/pco/checkpoint/state.py`（或拆分前的 `src/pco/checkpoint.py`）
- Modify: `src/pco/workspace.py`、`src/pco/cli.py`

**Step 1: 现有测试为基线**

Run: `pytest -q`
Expected: 全绿。

**Step 2: 实现**

1. `_save` 不再调用 `adapter.lock_input`；新增 `_transition(state, new_status)`：

```python
def _transition(self, state: CheckpointState, new_status: str) -> None:
    state.status = new_status
    if not state.input_unlocked and new_status not in {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS", "ABORTED"}:
        self.adapter.lock_input(state.id, new_status)
    self._save(state)
```

所有 `state.status = ...; self._save(state)` 成对调用替换为 `self._transition(state, ...)`；`decide("no")`、`retry` 重新进入 `WORKER_RUNNING` 时同样走 `_transition`（保证锁恢复）。

2. `_freeze` 删除两行死代码：

```python
if selected and selected[0]["id"] == thread.last_consolidated_message_id:
    selected = selected[1:]
selected = [message for message in selected if message["id"] != thread.last_consolidated_message_id]
```

3. `src/pco/workspace.py`：`__import__("datetime")` 改为顶部 `from datetime import datetime, timezone`，使用 `datetime.now(timezone.utc).isoformat()`。
4. `src/pco/cli.py`：删除未使用的 `import subprocess`、`import sys`；`checkpoint.py` 中未使用的 `from datetime import datetime, timezone` 一并清理。

**Step 3: 验证**

Run: `pytest -q`
Expected: 全绿，且 `pytest -q -k "manual_and_auto"` 中 lock 动作次数减少（不再每个状态转换都写锁文件）。

**Step 4: 提交**

```bash
git add src/pco/checkpoint src/pco/workspace.py src/pco/cli.py
git commit -m "refactor: move input locking to explicit transitions and remove dead code"
```

---

### Task 9: workflow YAML 标注为参考文档

**Files:**
- Modify: `src/pco/resources/profiles/pco/workflow/consolidate.yaml`

**Step 1: 修改文件头部**

```yaml
# Reference only: execution follows src/pco/checkpoint step modules.
# This YAML documents the PRD 18.3 step list and is not loaded by any runner.
version: consolidate@0.3.1
```

**Step 2: 验证**

Run: `pytest -q`（YAML 不影响行为）。
Expected: 全绿。

**Step 3: 提交**

```bash
git add src/pco/resources/profiles/pco/workflow/consolidate.yaml
git commit -m "docs: mark consolidate workflow yaml as reference only"
```

---

## P2 性能

### Task 10: pre-commit hook 增量校验

**Files:**
- Modify: `src/mem_core/hook.py`
- Modify: `tests/test_mem_core.py`

**Step 1: 写失败测试（篡改历史字节仍被拦截）**

```python
def test_messages_only_commit_still_rejects_append_only_violation(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    state = manager.begin(fingerprint_context={"kind": "tamper_test"})
    manager.append(state.id, Operation(op="append", stream="messages", record=message_record()))
    manager.commit(state.id)
    # 篡改历史行
    path = workspace.config.memory_root / "raw" / "conversations" / "messages.jsonl"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace(original.splitlines()[0], "{}"), encoding="utf-8")
    workspace.repository._git("add", ".")
    result = subprocess.run(["git", "-C", str(workspace.config.memory_root), "commit", "-m", "tamper"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "APPEND_ONLY_VIOLATION" in result.stderr + result.stdout
```

（`message_record()` 参照 conftest 构造 conversation-message envelope；`subprocess` 仅在测试内 import。）

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_mem_core.py -k append_only_violation -v`
Expected: 失败原因不限（当前实现已能拦截，本测试用于锁定增量路径后的行为；若当前已通过，改为断言"增量路径仍拦截"，见 Step 3 后应保持通过）。

**Step 3: 实现**

`src/mem_core/hook.py` 重构 `validate_repository`：

- 删除 `_materialize_tree` 与 tarfile/tempfile 物化；old 内容按变更文件读取：

```python
def _old_bytes(root: Path, relative: str) -> bytes:
    try:
        return _git(root, "show", f"HEAD:{relative}", text=False)
    except MemError:
        return b""
```

- `_appended_json` 改为接收 old/new bytes 两个参数（不再依赖物化目录）；
- `_verify_increment` 签名改为 `(root, profile, changed)`，内部用 `_old_bytes` 逐文件比对；
- 全量校验判定：

```python
def _needs_full_validation(changed: set[str], profile: Profile) -> bool:
    messages_path = profile.stream("messages").path
    stream_paths = {stream.path for stream in profile.config.streams.values()}
    profile_files = {
        ".mem-profile.json",
        *(f"profiles/{profile.name}/" + p for p in ("profile.yaml",)),
    }
    return bool(changed - {messages_path}) or bool(changed & profile_files)
```

  - `True` → `MemoryRepository(root, profile).validate_all(root=root)`（工作树即 staged 树，事务提交前 repo 必然 clean）；
  - `False`（messages-only）→ 仅对增量记录做 `RecordEnvelope.model_validate` + `profile.validate_record_schema`，再走 `_verify_increment` 的 append-only 与 delta 校验。

**Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿（含既有 hook 拦截测试与 Step 1 新测试）。

**Step 5: 提交**

```bash
git add src/mem_core/hook.py tests/test_mem_core.py
git commit -m "perf: incremental pre-commit validation without full-tree materialization"
```

---

### Task 11: search 复用索引 generation

**Files:**
- Modify: `src/pco/retrieval.py`
- Modify: `tests/test_retrieval_projection.py`

**Step 1: 写失败测试**

```python
def test_search_reuses_generation_documents_without_rebuild(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    CheckpointEngine(workspace, adapter).request("manual")
    indexes_root = workspace.config.indexes_root
    result = search(repo_root=workspace.config.memory_root, query="拖延", indexes_root=indexes_root)
    generation = Path(result["generation_path"] if "generation_path" in result else indexes_root / "generations" / workspace.repository.head())
    docs_path = generation / "documents.json"
    assert docs_path.is_file()
    digest_before = hashlib.sha256(docs_path.read_bytes()).hexdigest()
    search(repo_root=workspace.config.memory_root, query="评价", indexes_root=indexes_root)
    assert hashlib.sha256(docs_path.read_bytes()).hexdigest() == digest_before
```

（`search()` 当前不返回 `generation_path`；若断言不便，直接取 `indexes_root / "generations" / head`。）

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_retrieval_projection.py -k reuses_generation -v`
Expected: 失败（当前每次查询重写 documents.json 或路径断言失败）。

**Step 3: 实现**

`src/pco/retrieval.py` `search()`：

```python
index_result = build_index(repo_root=repo_root, indexes_root=indexes_root)
generation = Path(index_result["generation_path"])
if (generation / "documents.json").is_file():
    docs = json.loads((generation / "documents.json").read_text(encoding="utf-8"))
else:
    docs = _documents(repository)
```

- graph 模式（`mode in {"pattern", "historical", "change"}`）改为从 `generation / "backlinks.json"` 加载 backlinks（不存在才 `build_backlinks`）；
- `build_index` 保持幂等：manifest 存在直接返回；manifest 缺失或 `force` 才重建并写 `documents.json`/`lexical.json`/`dense.json`（后者 Task 12 删除）。

**Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿。

**Step 5: 提交**

```bash
git add src/pco/retrieval.py tests/test_retrieval_projection.py
git commit -m "perf: reuse index generation documents and backlinks in search"
```

---

### Task 12: 删除检索 fallback 引擎

**Files:**
- Modify: `src/pco/retrieval.py`
- Modify: `tests/test_retrieval_projection.py`、`tests/test_real_index_backends.py`

**Step 1: 更新测试（先改断言再删实现）**

- `tests/test_retrieval_projection.py`：删除 `from pco.retrieval import _eligible_backend_hits` 及 `test_...` 中对该函数的直接测试；删除针对本地 fallback 行为（lexical/dense JSON、cosine fallback）的断言；
- `tests/test_real_index_backends.py`：`assert built["backend_errors"] == {}` 改为断言真实后端：

```python
assert built["dense_backend"] == "milvus-lite"
assert built["lexical_backend"] == "tantivy"
assert "backend_errors" not in built
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_retrieval_projection.py tests/test_real_index_backends.py -v`
Expected: 失败（`backend_errors` 仍存在于 manifest / 导入仍存在）。

**Step 3: 实现**

`src/pco/retrieval.py`：
- 删除 `_cosine`、`_eligible_backend_hits`、`lexical.json`/`dense.json` 写入、`backend_errors` 记录；
- `_index_scores` 直接调用 Tantivy/Milvus（`fetch_limit=candidate_limit`），失败抛 `MemError`：

```python
except Exception as exc:
    raise MemError(
        "INDEX_BACKEND_FAILED",
        "retrieval",
        f"{backend} failed: {exc}",
        retryable=True,
        recovery=["Verify the local backend installation and retry", "Run `pco derive index --force`"],
    ) from exc
```

- `search()` 的 Python 侧重排只使用后端分数：`dense[index] = backend_dense.get(key, 0.0)`、`lexical[index] = backend_lexical.get(key, 0.0)`；删除 `query_counts` fallback 与 `_cosine` 调用；
- `build_index` 不再吞异常（移除外层 try/except），保留 `_merge_no_proxy` 与 `_vector`；
- manifest 删除 `backend_errors` 字段。

**Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿（无 loopback 环境的 CI 中，`build_index` 相关断言改为验证明确报错路径）。

**Step 5: 提交**

```bash
git add src/pco/retrieval.py tests/test_retrieval_projection.py tests/test_real_index_backends.py
git commit -m "refactor: drop self-built retrieval fallbacks and fail loudly on backend errors"
```

---

### Task 13: archive 去重改为尾部读取

**Files:**
- Modify: `src/pco/archive.py`
- Modify: `tests/test_archive_sources.py`

**Step 1: 写失败测试（模拟崩溃恢复去重）**

```python
def test_archive_dedups_after_crash_between_commit_and_cursor_update(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages())
    engine = CheckpointEngine(workspace, adapter)
    first = engine.archive.archive(adapter.messages)
    assert first["archived"] == 2
    # 模拟崩溃：canonical 已提交但 cursor 未推进（将 thread 回滚）
    thread = workspace.thread()
    thread.last_archived_message_id = None
    thread.archive_cursor = None
    workspace.save_thread(thread)
    second = engine.archive.archive(adapter.messages)
    assert second["archived"] == 0
```

**Step 2: 跑测试确认失败**

Run: `pytest tests/test_archive_sources.py -k crash -v`
Expected: 失败或当前已通过——若当前已通过（因全量 set 去重），本测试用于锁定重构后行为，重构后仍须通过。

**Step 3: 实现**

`src/pco/archive.py` `archive()`：

- 删除全量 `existing_keys` 集合构建；
- 改为读取消息流尾部（最多 64 条）构建 `tail_keys`：

```python
def _tail_native_keys(self) -> set[tuple[str, str, str]]:
    path = self.workspace.repository.profile.stream_path(self.workspace.config.memory_root, "messages")
    if not path.is_file():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    keys: set[tuple[str, str, str]] = set()
    for line in lines[-64:]:
        if not line.strip():
            continue
        record = json.loads(line)
        payload = record["payload"]
        keys.add((payload["harness"], payload["native_session_id"], payload["native_message_id"]))
    return keys
```

- 归档循环中 `key in tail_keys` 则跳过（保留崩溃恢复分支：无新记录时按尾部最大 id 恢复 cursor）；
- 需要 `import json`。

**Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿。

**Step 5: 提交**

```bash
git add src/pco/archive.py tests/test_archive_sources.py
git commit -m "perf: archive dedup via tail read instead of full stream load"
```

---

### Task 14: 性能基准脚本

**Files:**
- Create: `scripts/benchmark_corpus.py`
- Create: `docs/PERFORMANCE.md`

**Step 1: 写脚本**

`scripts/benchmark_corpus.py`：

```python
"""Generate a PRD-scale synthetic corpus and measure validate/commit/search.

Usage: python scripts/benchmark_corpus.py [--messages 100000] [--events 10000]
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from pco.config import load_config
from pco.workspace import Workspace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=100_000)
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--concepts", type=int, default=5_000)
    parser.add_argument("--sources", type=int, default=1_000)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        config = load_config(workspace=Path(tmp) / "pco", overrides=["checkpoint.derivations.projection=markdown"])
        workspace = Workspace(config).init()
        # 直接写 canonical 文件并 --no-verify 提交一次生成语料（基准专用，不走业务事务）
        # messages / events / concepts / sources 按 schema 构造记录追加到各 stream 文件
        # 然后：validate_all 计时、一次真实 mem txn commit 计时（含 pre-commit hook）、search 计时
        ...


if __name__ == "__main__":
    main()
```

（脚本体需按 Task 3/7 后的 `latest_by_id`、repo_loader 等 API 编写；记录结构参考 `tests/conftest.py` 的 envelope 构造。计时输出为 JSON：`{"validate_seconds":..., "commit_seconds":..., "search_seconds":...}`。）

**Step 2: 运行并记录**

Run: `python scripts/benchmark_corpus.py`
Expected: 输出计时 JSON。将结果与 PRD §25.4 对比（validate/commit <10s、检索 <2s），写入 `docs/PERFORMANCE.md`（模板：环境、规模、各项耗时、达标与否、偏差原因、后续优化项）。

**Step 3: 提交**

```bash
git add scripts/benchmark_corpus.py docs/PERFORMANCE.md
git commit -m "perf: add PRD-scale benchmark script and results"
```

---

## P3 收尾

### Task 15: 文档同步与最终回归

**Files:**
- Modify: `README.md`
- Modify: `docs/MVP_VERIFICATION.md`

**Step 1: 更新文档**

- `README.md`：补充"Tantivy/Milvus 为硬依赖，后端不可用时 `pco derive index` 会明确报错"；验证命令不变；
- `docs/MVP_VERIFICATION.md`：更新受影响的证据条目（迁移测试已删除；AC-14 的测试名不变；补充 `docs/PERFORMANCE.md` 指针；AFFiNE live 待验条目保持不变）。

**Step 2: 最终回归**

Run: `pytest -q`
Expected: 全绿。

Run: `python -m pip wheel . --no-deps --no-build-isolation -w /tmp/pco-wheel-check`
Expected: wheel 构建成功（确认 entry points 已打包）。

Run: `unzip -l /tmp/pco-wheel-check/*.whl | grep entry_points`
Expected: 能看到 `entry_points.txt` 且含 `mem_core.capabilities`。

**Step 3: 提交**

```bash
git add README.md docs/MVP_VERIFICATION.md
git commit -m "docs: sync verification notes and backend dependency behavior"
```

---

## 验收标准

- `pytest -q` 全绿；
- `mem`/`pco` 两个 CLI 行为不变，`mem txn commit --dry-run` 可用；
- `pco doctor` 通过；
- 新增/替换测试全部落地（Task 1/2/3/5/7/10/11/13 各至少一个）；
- `docs/PERFORMANCE.md` 记录基准结果；
- wheel 包含 `mem_core.capabilities` entry points；
- 无新增运行时依赖，`pyproject.toml` 仅新增 entry-points 段。
