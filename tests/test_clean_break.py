from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]

REMOVED_MODULES = (
    "pco.archive",
    "pco.checkpoint",
    "pco.context",
    "pco.harness",
    "pco.repo_loader",
    "pco.research_profile",
)

REMOVED_RESOURCES = {
    "packages/pco/src/pco/resources/config",
    "packages/pco/src/pco/resources/opencode",
    "packages/pco/src/pco/resources/profiles/research",
    "packages/pco/src/pco/resources/profiles/pco/prompts",
    "packages/pco/src/pco/resources/profiles/pco/workflow",
    "packages/pco/src/pco/resources/profiles/pco/schemas/checkpoint.schema.json",
    "packages/pco/src/pco/resources/profiles/pco/schemas/continuation.schema.json",
    "packages/pco/src/pco/resources/profiles/pco/schemas/conversation-message.schema.json",
    "packages/pco/src/pco/resources/profiles/pco/schemas/search-receipt.schema.json",
}

REPLACED_DOCUMENTS = {
    "docs/AFFINE_BRIDGE.md",
    "docs/MVP_VERIFICATION.md",
    "docs/PCO_MRD_v0.3.md",
    "docs/PCO_PRD_v0.3.1.md",
    "docs/PCO_PRD_v0.4.0.md",
    "docs/PERFORMANCE.md",
    "docs/PROJECT_REVIEW_v0.3.1.md",
    "docs/plans/2026-08-08-pco-refactor.md",
    "docs/plans/2026-08-09-transaction-incremental-validation.md",
    "docs/spec/2026-08-08-pco-refactor-design.md",
    "docs/spec/2026-08-15-mvp-closure.md",
    "docs/spec/2026-08-16-native-question-authorization-plan.md",
    "docs/spec/2026-08-22-consolidate-compact-separation-plan.md",
}

AUTHORITATIVE_DOCUMENTS = {
    "docs/adr/0001-pco-v0.5-clean-break.md",
    "docs/superpowers/specs/2026-08-30-pco-v0.5-agent-agnostic-design.md",
    "docs/superpowers/plans/2026-08-30-pco-v0.5-canonical-core.md",
    "docs/superpowers/plans/2026-08-30-pco-v0.5-workspace-and-skills.md",
    "docs/superpowers/plans/2026-08-30-pco-v0.5-proposal-and-sources.md",
    "docs/superpowers/plans/2026-08-30-pco-v0.5-derivation-cli-clean-break.md",
}


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("clean-break-wheel")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
            str(ROOT / "packages/pco"),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wheels = list(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_removed_runtime_modules_are_not_importable(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_removed_runtime_resources_are_absent() -> None:
    assert not [
        relative for relative in sorted(REMOVED_RESOURCES) if (ROOT / relative).exists()
    ]


def test_only_v05_public_capabilities_remain() -> None:
    project = tomllib.loads(
        (ROOT / "packages/pco/pyproject.toml").read_text(encoding="utf-8")
    )
    capabilities = project["project"]["entry-points"]["mem_core.capabilities"]
    assert capabilities == {
        "pco.validate": "pco.validation:validate_profile",
        "pco.retrieval.search": "pco.retrieval:search",
    }

    projections = importlib.import_module("pco.projections")
    retrieval = importlib.import_module("pco.retrieval")
    assert not hasattr(projections, "project_markdown")
    assert not hasattr(retrieval, "build_index")


def test_v04_only_dependencies_are_removed() -> None:
    project = tomllib.loads(
        (ROOT / "packages/pco/pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = project["project"]["dependencies"]
    assert not any(
        dependency.startswith(("hydra-core", "httpx", "json-repair"))
        for dependency in dependencies
    )


def test_wheel_contains_no_removed_runtime_or_resources(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as wheel:
        names = wheel.namelist()
    forbidden_files = {
        "pco/archive.py",
        "pco/context.py",
        "pco/harness.py",
        "pco/repo_loader.py",
        "pco/research_profile.py",
        "pco/resources/profiles/pco/schemas/checkpoint.schema.json",
        "pco/resources/profiles/pco/schemas/continuation.schema.json",
        "pco/resources/profiles/pco/schemas/conversation-message.schema.json",
        "pco/resources/profiles/pco/schemas/search-receipt.schema.json",
    }
    forbidden_prefixes = (
        "pco/checkpoint/",
        "pco/resources/config/",
        "pco/resources/opencode/",
        "pco/resources/profiles/research/",
        "pco/resources/profiles/pco/prompts/",
        "pco/resources/profiles/pco/workflow/",
    )
    assert not [
        name
        for name in names
        if name in forbidden_files or name.startswith(forbidden_prefixes)
    ]


def test_all_replaced_product_documents_are_deleted() -> None:
    assert not [
        relative
        for relative in sorted(REPLACED_DOCUMENTS)
        if (ROOT / relative).exists()
    ]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert not [
        relative for relative in sorted(REPLACED_DOCUMENTS) if relative in readme
    ]


def test_readme_links_every_authoritative_v05_document_without_broken_links() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert not [
        relative
        for relative in sorted(AUTHORITATIVE_DOCUMENTS)
        if relative not in readme
    ]
    assert not [
        relative
        for relative in sorted(AUTHORITATIVE_DOCUMENTS)
        if not (ROOT / relative).is_file()
    ]


def test_repository_contains_no_host_eval_runner() -> None:
    assert not (ROOT / "scripts/run_skill_evals.py").exists()
    assert not (ROOT / "tests/test_skill_eval_runner.py").exists()
    assert not (ROOT / "tests/opencode_question_loopback.ts").exists()


def test_shared_v05_fixture_module_has_no_legacy_runtime_imports() -> None:
    conftest = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    forbidden = (
        "from pco.checkpoint",
        "from pco.harness",
        "ApprovalGrant",
        "Continuation",
    )
    assert not [term for term in forbidden if term in conftest]
