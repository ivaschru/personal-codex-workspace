from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    """Load repository scripts without requiring them to be Python packages."""

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Dataclasses resolve module annotations through sys.modules during import.
    import sys

    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


workspace_modules = load_module("workspace_modules", ROOT / "scripts/workspace_modules.py")
module_migration = load_module(
    "module_migration", ROOT / "migrations/1.4.0_add_workspace_modules.py"
)


class WorkspaceModulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "lichnoe"
        self.source = self.workspace / "projects" / "health"
        self.source.mkdir(parents=True)
        (self.workspace / ".gitignore").write_text("/shared/\n.local/\n", encoding="utf-8")
        (self.workspace / "workspace.json").write_text(
            json.dumps({"initialized": True, "modules": []}) + "\n", encoding="utf-8"
        )
        (self.source / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
        (self.source / "README.md").write_text(
            "# Health\n\nExternal: projects/personal-documents/registry.yml\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-b", "main", self.workspace], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.workspace, "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", self.workspace, "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", self.workspace, "add", "."], check=True)
        subprocess.run(["git", "-C", self.workspace, "commit", "-m", "initial"], check=True, capture_output=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def inspect(self):
        return workspace_modules.inspect_source(
            self.workspace, self.source, module_slug="family-health"
        )

    def test_inspect_uses_workspace_type_and_neutral_name(self) -> None:
        report = self.inspect()
        self.assertTrue(report.safe_to_prepare)
        self.assertEqual(report.object_type, "project")
        self.assertEqual(report.suggested_repository_name, "lichnoe-project-family-health")
        self.assertTrue(
            any(item.kind == "external_reference" for item in report.warnings), report.warnings
        )

    def test_inspect_blocks_secret_and_private_tree(self) -> None:
        secret = self.source / ".env"
        secret.write_text("TOKEN=secret\n", encoding="utf-8")
        private = self.source / "local-assets"
        private.mkdir()
        (private / "scan.pdf").write_bytes(b"pdf")
        subprocess.run(["git", "-C", self.workspace, "add", "-f", secret], check=True)
        subprocess.run(["git", "-C", self.workspace, "commit", "-m", "secret fixture"], check=True, capture_output=True)
        report = self.inspect()
        kinds = {item.kind for item in report.blockers}
        self.assertIn("sensitive_filename", kinds)
        self.assertIn("excluded_private_tree", kinds)

    def test_prepare_register_and_replace_source(self) -> None:
        report = self.inspect()
        module_root = self.workspace / "shared" / "family-health"
        workspace_modules.prepare_snapshot(
            report,
            module_root,
            owner="owner",
            participants=["recipient"],
            suggested_mount_path="projects/health",
        )
        manifest = workspace_modules.load_module_manifest(module_root)
        self.assertEqual(manifest["source"]["historyMode"], "fresh-snapshot")
        self.assertEqual(manifest["repository"]["suggestedName"], "lichnoe-project-family-health")

        subprocess.run(["git", "init", "-b", "main", module_root], check=True, capture_output=True)
        subprocess.run(["git", "-C", module_root, "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", module_root, "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", module_root, "add", "."], check=True)
        subprocess.run(["git", "-C", module_root, "commit", "-m", "snapshot"], check=True, capture_output=True)

        remote = "https://github.com/example/lichnoe-project-family-health.git"
        workspace_modules.replace_source_with_reference(
            self.workspace,
            self.source,
            module_root,
            self.workspace / "workspace.json",
            remote,
            "shared/family-health",
        )
        reference = json.loads(
            (self.source / "workspace-reference.json").read_text(encoding="utf-8")
        )
        self.assertEqual(reference["moduleId"], "family-health")
        self.assertFalse((self.source / "AGENTS.md").exists())
        config = json.loads((self.workspace / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(config["modules"][0]["repository"], remote)

    def test_register_is_idempotent_and_rejects_drift(self) -> None:
        report = self.inspect()
        module_root = self.workspace / "shared" / "family-health"
        workspace_modules.prepare_snapshot(report, module_root, None, [], None)
        remote = "https://github.com/example/repo.git"
        self.assertTrue(
            workspace_modules.register_module(
                self.workspace / "workspace.json", module_root, remote, "shared/family-health"
            )
        )
        self.assertFalse(
            workspace_modules.register_module(
                self.workspace / "workspace.json", module_root, remote, "shared/family-health"
            )
        )
        with self.assertRaises(workspace_modules.WorkspaceModuleError):
            workspace_modules.register_module(
                self.workspace / "workspace.json",
                module_root,
                "https://github.com/example/other.git",
                "shared/family-health",
            )

    def test_modules_migration_preserves_existing_configuration(self) -> None:
        path = self.workspace / "migration-workspace.json"
        original = {"initialized": True, "owner": {"displayName": "Example"}}
        path.write_text(json.dumps(original), encoding="utf-8")
        self.assertTrue(module_migration.migrate(path))
        migrated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["owner"], original["owner"])
        self.assertEqual(migrated["modules"], [])
        self.assertFalse(module_migration.migrate(path))


if __name__ == "__main__":
    unittest.main()
