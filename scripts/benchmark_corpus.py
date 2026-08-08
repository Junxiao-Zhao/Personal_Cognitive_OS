"""Generate a PRD-scale synthetic corpus and measure validate/commit/search.

Usage:
    python scripts/benchmark_corpus.py [--messages 100000] [--events 10000]

The dense search timing requires a loopback-capable Milvus Lite environment:
    PCO_RUN_MILVUS=1 python scripts/benchmark_corpus.py

The bulk corpus is committed once with --no-verify (benchmark-only bypass of
the transaction contract) so the measurement focuses on validation throughput.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

from mem_core.errors import MemError
from mem_core.models import Operation, utc_now
from mem_core.transaction import TransactionManager
from pco.config import load_config
from pco.retrieval import search
from pco.workspace import Workspace


def _line(record_id: str, schema_version: str, payload: dict) -> str:
    return json.dumps(
        {
            "id": record_id,
            "revision": 1,
            "recorded_at": utc_now(),
            "schema_version": schema_version,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def build_corpus(workspace: Workspace, args: argparse.Namespace) -> None:
    root = workspace.config.memory_root
    now = utc_now()
    messages_path = root / "raw" / "conversations" / "messages.jsonl"
    events_path = root / "structured" / "events.jsonl"
    concepts_path = root / "structured" / "psychologies.jsonl"
    sources_path = root / "sources" / "registry.jsonl"
    receipts_path = root / "sources" / "search-receipts.jsonl"

    with messages_path.open("a", encoding="utf-8") as fh:
        for i in range(args.messages):
            fh.write(
                _line(
                    f"msg_bench_{i:06d}",
                    "conversation-message/v1",
                    {
                        "thread_id": "thread_bench",
                        "epoch_id": "epoch_bench",
                        "harness": "benchmark",
                        "native_session_id": "ses_bench",
                        "native_message_id": f"native_bench_{i:06d}",
                        "role": "user",
                        "kind": "conversation",
                        "content": f"基准对话消息 {i}：关于拖延、评价与被看见。",
                        "reasoning": None,
                        "refs": [],
                        "created_at": now,
                    },
                )
                + "\n"
            )
    with events_path.open("a", encoding="utf-8") as fh:
        for i in range(args.events):
            fh.write(
                _line(
                    f"evt_bench_{i:06d}",
                    "pco/event/v1",
                    {
                        "occurred_at": {"start": "2026-01-01", "end": "2026-01-01", "precision": "day"},
                        "description": f"基准事件 {i}：公开前的拖延与对评价的厌恶。",
                        "links": {"psychologies": [], "philosophies": [], "archetypes": []},
                        "evidence_refs": [f"message:msg_bench_{i:06d}"],
                        "revision_reason": "benchmark corpus",
                        "status": "active",
                    },
                )
                + "\n"
            )
    with concepts_path.open("a", encoding="utf-8") as fh:
        for i in range(args.concepts):
            fh.write(
                _line(
                    f"psy_bench_{i:06d}",
                    "pco/psychology/v1",
                    {
                        "name": f"基准概念 {i}",
                        "description": "基准语料用概念，不代表对用户的诊断。",
                        "aliases": [],
                        "external_refs": [
                            {
                                "url": "https://example.org/bench",
                                "title": "Benchmark reference",
                                "accessed_at": now,
                                "search_receipt": "search_bench_shared",
                            }
                        ],
                        "status": "active",
                    },
                )
                + "\n"
            )
    with receipts_path.open("a", encoding="utf-8") as fh:
        fh.write(
            _line(
                "search_bench_shared",
                "pco/search-receipt/v1",
                {
                    "worker_session_id": "ses_bench_worker",
                    "call_id": "call_bench_shared",
                    "tool": "websearch",
                    "input": {"query": "benchmark reference"},
                    "output_excerpt": "Result: https://example.org/bench",
                    "status": "completed",
                },
            )
            + "\n"
        )
    with sources_path.open("a", encoding="utf-8") as fh:
        for i in range(args.sources):
            fh.write(
                _line(
                    f"src_bench_{i:06d}",
                    "pco/source/v1",
                    {
                        "source_id": f"src_bench_{i:06d}",
                        "role": "input",
                        "provider": "local_file",
                        "locator": f"file:///bench/{i}.md",
                        "display_name": f"bench-{i}.md",
                        "reader_skill": "local-readonly",
                        "snapshot_path": f"sources/snapshots/src_bench_{i:06d}.md",
                        "registered_at": now,
                        "status": "active",
                    },
                )
                + "\n"
            )
    subprocess.run(
        ["git", "-C", str(root), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "--no-verify", "-m", "benchmark corpus"],
        check=True,
        capture_output=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=100_000)
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--concepts", type=int, default=5_000)
    parser.add_argument("--sources", type=int, default=1_000)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        config = load_config(
            workspace=Path(tmp) / "pco",
            overrides=[
                "checkpoint.derivations.projection=markdown",
                "checkpoint.derivations.index=false",
            ],
        )
        Workspace(config).init()
        opened = Workspace(config)
        opened.refresh_repository_profile()
        build_corpus(opened, args)

        started = time.monotonic()
        validation = opened.repository.validate_all()
        validate_seconds = round(time.monotonic() - started, 3)

        manager = TransactionManager(opened.repository, opened.config.state_root)
        txn = manager.begin(
            transaction_id="txn_benchmark_commit",
            fingerprint_context={"kind": "benchmark"},
        )
        manager.append(
            txn.id,
            Operation(
                op="append",
                stream="messages",
                record={
                    "id": "msg_bench_commit",
                    "revision": 1,
                    "recorded_at": utc_now(),
                    "schema_version": "conversation-message/v1",
                    "payload": {
                        "thread_id": "thread_bench",
                        "epoch_id": "epoch_bench",
                        "harness": "benchmark",
                        "native_session_id": "ses_bench",
                        "native_message_id": "native_bench_commit",
                        "role": "user",
                        "kind": "conversation",
                        "content": "基准提交消息",
                        "reasoning": None,
                        "refs": [],
                        "created_at": utc_now(),
                    },
                },
            ),
        )
        started = time.monotonic()
        commit_result = manager.commit(txn.id)
        commit_seconds = round(time.monotonic() - started, 3)

        search_seconds: float | None = None
        search_error: str | None = None
        started = time.monotonic()
        try:
            search(
                repo_root=opened.config.memory_root,
                indexes_root=opened.config.indexes_root,
                query="拖延",
                mode="current",
                limit=10,
            )
            search_seconds = round(time.monotonic() - started, 3)
        except MemError as exc:
            search_error = exc.detail.code

        result = {
            "corpus": {
                "messages": args.messages,
                "events": args.events,
                "concepts": args.concepts,
                "sources": args.sources,
            },
            "validate_seconds": validate_seconds,
            "validation": {"ok": validation["ok"], "records": validation["records"]},
            "commit_seconds": commit_seconds,
            "commit": commit_result["commit"],
            "search_seconds": search_seconds,
            "search_error": search_error,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
