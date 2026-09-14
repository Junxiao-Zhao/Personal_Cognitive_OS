from __future__ import annotations

import os
from pathlib import Path
from subprocess import CompletedProcess
import subprocess
import sys

import pytest

from scripts import smoke_clean_wheels as smoke_module


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, command, *, check, env, cwd):
        args = [str(item) for item in command]
        self.calls.append(
            {"command": args, "check": check, "env": dict(env), "cwd": str(cwd)}
        )
        assert check is True
        if args[1:3] == ["-m", "build"]:
            project = Path(args[3]).name
            distribution = "mem_core" if project == "mem-core" else project
            wheelhouse = Path(args[args.index("--outdir") + 1])
            (wheelhouse / f"{distribution}-0.5.0-py3-none-any.whl").touch()
        return CompletedProcess(args, 0)


class RecordingEnvBuilder:
    instances: list["RecordingEnvBuilder"] = []

    def __init__(self, *, with_pip: bool) -> None:
        self.with_pip = with_pip
        self.created: list[Path] = []
        self.instances.append(self)

    def create(self, env_dir) -> None:
        root = Path(env_dir)
        self.created.append(root)
        scripts = root / ("Scripts" if smoke_module.os.name == "nt" else "bin")
        scripts.mkdir(parents=True)
        for name in (
            smoke_module.executable_name("python"),
            smoke_module.executable_name("pco"),
            smoke_module.executable_name("mem"),
        ):
            (scripts / name).touch()


def test_smoke_uses_exact_six_commands_with_sanitized_env_and_external_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source-path pollution or orchestration drift must fail the wheel gate."""
    repo_root = (tmp_path / "repo").resolve()
    (repo_root / "packages/mem-core").mkdir(parents=True)
    (repo_root / "packages/pco").mkdir()
    runner = RecordingRunner()
    RecordingEnvBuilder.instances.clear()
    monkeypatch.setattr(smoke_module.venv, "EnvBuilder", RecordingEnvBuilder)
    monkeypatch.setenv("PYTHONPATH", "/attacker/source")
    monkeypatch.setenv("PYTHONHOME", "/attacker/home")
    monkeypatch.setenv("PYTHONUSERBASE", "/attacker/userbase")
    monkeypatch.setenv("PYTHONSTARTUP", "/attacker/startup.py")
    monkeypatch.setenv("PYTHONINSPECT", "1")
    monkeypatch.setenv("PYTHONEXECUTABLE", "/attacker/python")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "/attacker/launcher")
    monkeypatch.setenv("VIRTUAL_ENV", "/attacker/venv")
    monkeypatch.setenv("PIP_INDEX_URL", "https://packages.example/simple")
    monkeypatch.setenv("SSL_CERT_FILE", "/certificates/ca.pem")

    smoke_module.smoke(repo_root, run=runner)

    assert len(RecordingEnvBuilder.instances) == 1
    builder = RecordingEnvBuilder.instances[0]
    assert builder.with_pip is True
    assert len(builder.created) == 1
    environment = builder.created[0]
    assert environment.name.startswith("pco-venv-")
    assert not environment.is_relative_to(repo_root)
    python, scripts = smoke_module.environment_commands(environment)

    assert len(runner.calls) == 6
    wheelhouse = Path(runner.calls[0]["command"][-1])
    assert wheelhouse.name.startswith("pco-wheelhouse-")
    assert not wheelhouse.is_relative_to(repo_root)
    expected_commands = [
        [
            sys.executable,
            "-m",
            "build",
            str(repo_root / "packages/mem-core"),
            "--wheel",
            "--outdir",
            str(wheelhouse),
        ],
        [
            sys.executable,
            "-m",
            "build",
            str(repo_root / "packages/pco"),
            "--wheel",
            "--outdir",
            str(wheelhouse),
        ],
        [
            str(python),
            "-m",
            "pip",
            "install",
            str(wheelhouse / "mem_core-0.5.0-py3-none-any.whl"),
            str(wheelhouse / "pco-0.5.0-py3-none-any.whl"),
        ],
        [str(python), "-c", smoke_module.RESOURCE_AND_VERSION_PROBE],
        [str(scripts / smoke_module.executable_name("pco")), "--help"],
        [str(scripts / smoke_module.executable_name("mem")), "--help"],
    ]
    assert [call["command"] for call in runner.calls] == expected_commands

    unsafe = {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
        "VIRTUAL_ENV",
    }
    expected_env = {
        key: value for key, value in os.environ.items() if key not in unsafe
    }
    expected_env["PYTHONNOUSERSITE"] = "1"
    assert all(call["env"] == expected_env for call in runner.calls)
    assert expected_env["PIP_INDEX_URL"] == "https://packages.example/simple"
    assert expected_env["SSL_CERT_FILE"] == "/certificates/ca.pem"
    assert all(call["cwd"] == str(wheelhouse) for call in runner.calls)

    assert not environment.exists()
    assert not wheelhouse.exists()


def test_probe_rejects_packages_imported_from_polluted_source_tree(
    tmp_path: Path,
) -> None:
    """A source checkout satisfying version/resources must not pass the probe."""
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(repo_root / "packages/pco/src"),
            str(repo_root / "packages/mem-core/src"),
        ]
    )
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", smoke_module.RESOURCE_AND_VERSION_PROBE],
        check=False,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "outside fresh environment" in result.stderr


def test_exact_wheels_normalizes_requested_distribution_names(tmp_path: Path) -> None:
    """PEP 503-equivalent requested names must resolve the built wheels."""
    (tmp_path / "mem_core-0.5.0-py3-none-any.whl").touch()
    (tmp_path / "pco-0.5.0-py3-none-any.whl").touch()

    assert [
        path.name
        for path in smoke_module.exact_wheels(
            tmp_path, names=("MEM.Core", "PcO"), version="0.5.0"
        )
    ] == [
        "mem_core-0.5.0-py3-none-any.whl",
        "pco-0.5.0-py3-none-any.whl",
    ]


@pytest.mark.parametrize(
    ("wheel_names", "message"),
    [
        (
            ["mem_core-0.5.0-py3-none-any.whl"],
            "expected exactly one pco==0.5.0 wheel, found 0",
        ),
        (
            [
                "mem_core-0.5.0-py3-none-any.whl",
                "pco-0.5.0-py3-none-any.whl",
                "pco-0.5.0-cp313-cp313-manylinux_2_17_x86_64.whl",
            ],
            "expected exactly one pco==0.5.0 wheel, found 2",
        ),
        (
            [
                "mem_core-0.5.0-py3-none-any.whl",
                "pco-0.5.1-py3-none-any.whl",
            ],
            "expected exactly one pco==0.5.0 wheel, found 0",
        ),
    ],
)
def test_exact_wheels_rejects_non_exact_wheelhouse(
    tmp_path: Path, wheel_names: list[str], message: str
) -> None:
    """Missing, duplicate, or wrong-version requested output must fail closed."""
    for name in wheel_names:
        (tmp_path / name).touch()

    with pytest.raises(RuntimeError, match=message.replace(".", r"\.")):
        smoke_module.exact_wheels(tmp_path, names=("mem-core", "pco"), version="0.5.0")


@pytest.mark.parametrize(
    ("os_name", "expected_python", "expected_scripts"),
    [
        ("posix", "bin/python", "bin"),
        ("nt", "Scripts/python.exe", "Scripts"),
    ],
)
def test_environment_commands_normalizes_platform_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    os_name: str,
    expected_python: str,
    expected_scripts: str,
) -> None:
    """Wrong platform interpreter or script paths must break orchestration."""
    monkeypatch.setattr(smoke_module.os, "name", os_name)

    python, scripts = smoke_module.environment_commands(tmp_path)

    assert python == tmp_path / expected_python
    assert scripts == tmp_path / expected_scripts
