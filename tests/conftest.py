from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Procedural memory is runtime state; never let tests read or write the repo's copy."""
    monkeypatch.setenv("SRE_MEMORY_FILE", str(tmp_path / "memory.json"))
