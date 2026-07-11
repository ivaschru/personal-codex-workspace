"""Регрессионные тесты минимальных гарантий первичной настройки."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_workspace.py"
SPEC = importlib.util.spec_from_file_location("check_workspace", SCRIPT)
assert SPEC and SPEC.loader
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ConfiguredWorkspaceTests(unittest.TestCase):
    def make_workspace(self, visibility: str = "private") -> Path:
        """Создаёт минимальную временную конфигурацию без настоящих данных."""

        root = Path(self.tempdir.name)
        (root / ".local").mkdir()
        (root / "PROFILE.md").write_text("# Тестовый профиль\n", encoding="utf-8")
        (root / "workspace.json").write_text(
            json.dumps(
                {
                    "initialized": True,
                    "privacy": {"repositoryVisibility": visibility},
                }
            ),
            encoding="utf-8",
        )
        (root / ".local/machine-setup.json").write_text(
            json.dumps({"checked": True}), encoding="utf-8"
        )
        return root

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_private_workspace_passes(self) -> None:
        root = self.make_workspace("private")
        errors: list[str] = []
        with patch.object(CHECK, "ROOT", root):
            CHECK.check_configured(errors)
        self.assertEqual(errors, [])

    def test_public_workspace_is_rejected(self) -> None:
        root = self.make_workspace("public")
        errors: list[str] = []
        with patch.object(CHECK, "ROOT", root):
            CHECK.check_configured(errors)
        self.assertTrue(any("private или local-only" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
