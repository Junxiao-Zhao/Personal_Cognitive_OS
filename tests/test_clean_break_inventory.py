from pathlib import Path


REMOVED_TEST_FILES = {
    "test_acceptance_flows.py", "test_approval_grant.py", "test_archive_sources.py",
    "test_checkpoint.py", "test_cli_integration.py", "test_config_migration.py",
    "test_opencode_adapter.py", "test_opencode_authorization_plugin.py", "test_phase_b.py",
    "test_phase_c_derivations.py", "test_receipt_provenance.py", "test_source_materialization.py",
    "test_retrieval_projection.py", "test_backlinks_current.py",
}


def test_v04_only_test_files_are_absent() -> None:
    tests_root = Path(__file__).parent
    assert not ({path.name for path in tests_root.glob("test_*.py")} & REMOVED_TEST_FILES)
