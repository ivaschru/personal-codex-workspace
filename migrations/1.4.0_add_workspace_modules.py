#!/usr/bin/env python3
"""Идемпотентно добавляет реестр общих Workspace-объектов."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "workspace.json"


def migrate(path: Path = WORKSPACE) -> bool:
    """Добавляет только отсутствующий modules и не меняет личную конфигурацию."""

    if not path.exists():
        raise FileNotFoundError("workspace.json отсутствует")
    data = json.loads(path.read_text(encoding="utf-8"))
    if "modules" in data:
        if not isinstance(data["modules"], list):
            raise ValueError("workspace.json.modules должен быть массивом")
        return False
    data["modules"] = []
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


if __name__ == "__main__":
    changed = migrate()
    print("Реестр общих модулей добавлен." if changed else "Реестр общих модулей уже настроен.")
