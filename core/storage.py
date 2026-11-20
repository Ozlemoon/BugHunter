"""File-based run history: one JSON document per hunt under reports/runs/."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from core.engine import HuntRun

logger = logging.getLogger(__name__)

RUN_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


class RunStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.match(run_id):
            raise ValueError("Invalid run id.")
        return self.directory / f"{run_id}.json"

    def save(self, run: HuntRun) -> Path:
        """Atomic write so a crash never leaves a half-written report."""
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(run.run_id)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(run.model_dump_json(indent=2))
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    def load(self, run_id: str) -> Optional[HuntRun]:
        from core.engine import HuntRun

        try:
            path = self._path(run_id)
        except ValueError:
            return None
        if not path.is_file():
            return None
        return HuntRun.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, limit: int = 20) -> List[HuntRun]:
        from core.engine import HuntRun

        if not self.directory.is_dir():
            return []
        runs = []
        for path in sorted(self.directory.glob("*.json"), reverse=True)[:limit]:
            try:
                runs.append(HuntRun.model_validate_json(path.read_text(encoding="utf-8")))
            except ValueError as error:  # skip corrupted/old-format files
                logger.warning("Skipping unreadable run file %s: %s", path.name, error)
        return runs
