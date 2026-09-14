from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from scripts import smoke_clean_wheels as smoke_module


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command, *, check):
        args = [str(item) for item in command]
        self.commands.append(args)
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


def test_smoke_builds_and_installs_only_two_exact_wheels_in_a_fresh_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "packages/mem-core").mkdir(parents=True)
    (repo_root / "packages/pco").mkdir()
    runner = RecordingRunner()
    RecordingEnvBuilder.instances.clear()
    monkeypatch.setattr(smoke_module.venv, "EnvBuilder", RecordingEnvBuilder)

    smoke_module.smoke(repo_root, run=runner)

    assert len(RecordingEnvBuilder.instances) == 1
    builder = RecordingEnvBuilder.instances[0]
    assert builder.with_pip is True
    assert len(builder.created) == 1
    assert builder.created[0].name.startswith("pco-venv-")
    assert not builder.created[0].exists()
    commands = runner.commands
    wheelhouse = Path(commands[0][commands[0].index("--outdir") + 1])
    assert wheelhouse.name.startswith("pco-wheelhouse-")
    assert commands[1][commands[1].index("--outdir") + 1] == str(wheelhouse)
    assert not wheelhouse.exists()
    assert [command[3] for command in commands[:2]] == [
        str(repo_root / "packages/mem-core"),
        str(repo_root / "packages/pco"),
    ]
    install = commands[2]
    assert install[1:4] == ["-m", "pip", "install"]
    assert [Path(item).name for item in install[4:]] == [
        "mem_core-0.5.0-py3-none-any.whl",
        "pco-0.5.0-py3-none-any.whl",
    ]
    assert all(Path(item).suffix == ".whl" for item in install[4:])
    assert str(repo_root / "packages/mem-core") not in install
    assert str(repo_root / "packages/pco") not in install
    assert commands[3][1:] == ["-c", smoke_module.RESOURCE_AND_VERSION_PROBE]
    assert Path(commands[4][0]).name == smoke_module.executable_name("pco")
    assert commands[4][1:] == ["--help"]
    assert Path(commands[5][0]).name == smoke_module.executable_name("mem")
    assert commands[5][1:] == ["--help"]


def test_exact_wheels_requires_one_matching_file_per_distribution(
    tmp_path: Path,
) -> None:
    (tmp_path / "mem_core-0.5.0-py3-none-any.whl").touch()
    (tmp_path / "pco-0.5.0-py3-none-any.whl").touch()

    assert [
        path.name
        for path in smoke_module.exact_wheels(
            tmp_path, names=("mem_core", "pco"), version="0.5.0"
        )
    ] == [
        "mem_core-0.5.0-py3-none-any.whl",
        "pco-0.5.0-py3-none-any.whl",
    ]

    (tmp_path / "pco-0.5.0-cp313-cp313-manylinux.whl").touch()
    with pytest.raises(RuntimeError, match="exactly one pco==0.5.0 wheel"):
        smoke_module.exact_wheels(tmp_path, names=("mem_core", "pco"), version="0.5.0")
