"""Versioned model registry with champion / challenger (Submodule 4.1).

Layout on disk::

    <root>/<model name>/v<N>/model.joblib   the fitted model
    <root>/<model name>/v<N>/card.json      model card (see below)
    <root>/champion.json                    which version is in service

A model card records the alarm threshold, evaluation metrics, feature
names, library versions and where the training data came from,
including whether it was simulated and contained synthetic precursors.
Every newly registered version is a *challenger*; only ``promote``
makes a version the *champion* used for live prediction, and the
previous champion is remembered so it can be restored with ``rollback``.
"""

from __future__ import annotations

import json
import os
import platform
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

CHAMPION_FILE = "champion.json"


def _versions() -> Dict[str, str]:
    out = {"python": platform.python_version()}
    for module in ("numpy", "sklearn", "joblib"):
        try:
            out[module] = __import__(module).__version__
        except Exception:  # noqa: BLE001
            out[module] = None
    return out


class ModelRegistry:
    def __init__(self, root: str):
        self.root = str(root)
        os.makedirs(self.root, exist_ok=True)

    # -- registration ---------------------------------------------------------

    def register(self, model, threshold: float, metrics: Dict[str, Any],
                 provenance: Dict[str, Any], notes: str = "") -> Tuple[str, int]:
        import joblib

        name = model.name
        version = self._next_version(name)
        path = self._dir(name, version)
        os.makedirs(path)
        joblib.dump(model, os.path.join(path, "model.joblib"))
        card = {
            "name": name,
            "version": version,
            "kind": model.kind,
            "threshold": float(threshold),
            "metrics": metrics,
            "provenance": provenance,
            "feature_names": list(model.feature_names),
            "created": datetime.now(timezone.utc).isoformat(),
            "libraries": _versions(),
            "notes": notes,
        }
        with open(os.path.join(path, "card.json"), "w") as fh:
            json.dump(card, fh, indent=2, sort_keys=True, default=str)
        return name, version

    def list(self) -> List[dict]:
        out = []
        for name in sorted(os.listdir(self.root)):
            base = os.path.join(self.root, name)
            if not os.path.isdir(base):
                continue
            for entry in sorted(os.listdir(base)):
                if entry.startswith("v") and entry[1:].isdigit():
                    out.append(self.card(name, int(entry[1:])))
        return out

    def card(self, name: str, version: int) -> dict:
        with open(os.path.join(self._dir(name, version), "card.json")) as fh:
            return json.load(fh)

    def load(self, name: str, version: int):
        import joblib

        return joblib.load(os.path.join(self._dir(name, version), "model.joblib"))

    # -- champion -------------------------------------------------------------

    def promote(self, name: str, version: int, reason: str = "") -> dict:
        self.card(name, version)  # raises if missing
        current = self.champion_ref()
        record = {
            "name": name,
            "version": version,
            "reason": reason,
            "promoted": datetime.now(timezone.utc).isoformat(),
            "previous": {"name": current["name"], "version": current["version"]} if current else None,
        }
        with open(os.path.join(self.root, CHAMPION_FILE), "w") as fh:
            json.dump(record, fh, indent=2)
        return record

    def rollback(self) -> dict:
        current = self.champion_ref()
        if not current or not current.get("previous"):
            raise LookupError("no previous champion to roll back to")
        prev = current["previous"]
        return self.promote(prev["name"], prev["version"], reason="rollback")

    def champion_ref(self) -> Optional[dict]:
        path = os.path.join(self.root, CHAMPION_FILE)
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return json.load(fh)

    def load_champion(self):
        """``(model, card)`` of the champion, or raises LookupError."""
        ref = self.champion_ref()
        if ref is None:
            raise LookupError(f"no champion in {self.root}")
        return self.load(ref["name"], ref["version"]), self.card(ref["name"], ref["version"])

    # -- internals ---------------------------------------------------------------

    def _dir(self, name: str, version: int) -> str:
        return os.path.join(self.root, name, f"v{version}")

    def _next_version(self, name: str) -> int:
        base = os.path.join(self.root, name)
        if not os.path.isdir(base):
            return 1
        existing = [int(e[1:]) for e in os.listdir(base) if e.startswith("v") and e[1:].isdigit()]
        return max(existing, default=0) + 1
