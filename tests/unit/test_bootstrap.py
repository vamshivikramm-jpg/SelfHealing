"""Unit tests for Submodule 1.1 — project bootstrap.

Run from repo root:  python3 -m pytest tests/unit -v
"""

import pathlib
import py_compile
import re
import shutil
import stat
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]

REQUIRED_FILES = [
    ".env.example",
    ".gitignore",
    "Makefile",
    "README.md",
    "docker-compose.yml",
    "network/Dockerfile",
    "network/entrypoint.sh",
    "network/README.md",
    "controller/Dockerfile",
    "controller/apps/selfhealing_app.py",
    "controller/apps/sh_logic.py",
    "controller/README.md",
    "tests/smoke/test_bootstrap.sh",
]

EXECUTABLE_FILES = [
    "network/entrypoint.sh",
    "tests/smoke/test_bootstrap.sh",
]

ENV_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def test_required_files_exist():
    missing = [p for p in REQUIRED_FILES if not (ROOT / p).exists()]
    assert not missing, f"missing files: {missing}"


def test_scripts_are_executable():
    for rel in EXECUTABLE_FILES:
        mode = (ROOT / rel).stat().st_mode
        assert mode & stat.S_IXUSR, f"{rel} is not executable"


def test_env_example_parses():
    lines = (ROOT / ".env.example").read_text().splitlines()
    bad = [
        ln
        for ln in lines
        if ln.strip() and not ln.lstrip().startswith("#") and not ENV_LINE_RE.match(ln)
    ]
    assert not bad, f"invalid .env.example lines: {bad}"


def test_controller_apps_compile():
    for name in ("selfhealing_app.py", "sh_logic.py"):
        py_compile.compile(str(ROOT / "controller/apps" / name), doraise=True)


def test_compose_config_valid():
    if shutil.which("docker") is None:
        import pytest

        pytest.skip("docker CLI not available")
    result = subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
