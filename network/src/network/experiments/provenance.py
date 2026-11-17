"""Provenance for reproducible experiments (Module 9, research doc §9.5).

Records exactly what produced a result: git commit (and whether the tree
had uncommitted changes), Python and library versions, the platform, a
SHA-256 of every configuration file, and the experiment settings/seeds.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, Optional

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
CONFIG_DIR = os.path.join(REPO, "network", "config")


def _git(*args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=10, check=True)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def _version(module: str) -> Optional[str]:
    try:
        return getattr(__import__(module), "__version__", "unknown")
    except Exception:  # noqa: BLE001
        return None


def file_hashes(directory: str = CONFIG_DIR) -> Dict[str, str]:
    out = {}
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and name.endswith((".yaml", ".yml")):
            with open(path, "rb") as fh:
                out[name] = hashlib.sha256(fh.read()).hexdigest()
    return out


def collect(settings: dict) -> dict:
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "created": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_uncommitted_changes": None if status is None else bool(status),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "libraries": {m: _version(m) for m in ("numpy", "sklearn", "scipy", "yaml", "fastapi")},
        "config_sha256": file_hashes(),
        "settings": settings,
        "simulated": True,
        "note": "All experiments run on the simulated network; failures, warning patterns and attacks are synthetic.",
    }
