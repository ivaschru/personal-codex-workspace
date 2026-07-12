#!/usr/bin/env python3
"""Prepare, validate, register, and diagnose shared Workspace objects.

The script deliberately stops before creating a remote repository or inviting a
person. Those are external access changes and remain visible steps in the agent
workflow. All local preparation is deterministic and uses a fresh snapshot, so
the private parent repository history is never copied accidentally.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent.parent
MANIFEST_NAME = "workspace-module.json"
MODULE_SCHEMA_VERSION = 1
TEXT_SUFFIXES = {".md", ".json", ".yml", ".yaml", ".py", ".txt", ".toml", ".csv", ".tsv"}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".local",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "local-assets",
    "local-private",
    "node_modules",
}
PRIVATE_DIRECTORY_NAMES = {".local", "local-assets", "local-private"}
SENSITIVE_FILE_PATTERNS = (
    re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE),
    re.compile(r".*\.(?:key|pem|p12|pfx)$", re.IGNORECASE),
    re.compile(r".*(?:cookie|cookies|session|token|credential|secret).*$", re.IGNORECASE),
)
SECRET_CONTENT_PATTERNS = {
    "GitHub token": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "generic bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{24,}={0,2}\b", re.IGNORECASE),
}
ABSOLUTE_PATH_PATTERN = re.compile(r"/(?:Users|home)/[^/\s]+/")
WORKSPACE_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])((?:projects|tasks|routines|entities)/[^\s`\]\[(){}<>\"']+)"
)
VALID_OBJECT_TYPES = {"project", "subproject", "task", "routine", "folder", "space"}


class WorkspaceModuleError(RuntimeError):
    """Raised for a user-actionable safety or configuration problem."""


@dataclass
class Finding:
    """One inspect result with stable machine-readable fields."""

    kind: str
    path: str
    detail: str


@dataclass
class Inspection:
    """Complete pre-share report used by both humans and automation."""

    workspace_root: str
    source_path: str
    object_type: str
    module_slug: str
    suggested_repository_name: str
    blockers: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    excluded_paths: list[str] = field(default_factory=list)
    file_count: int = 0
    byte_count: int = 0

    @property
    def safe_to_prepare(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        data = asdict(self)
        data["safe_to_prepare"] = self.safe_to_prepare
        return data


def now_iso() -> str:
    """Return an explicit UTC timestamp without depending on local locale."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    """Normalize a repository component to a portable lowercase slug."""

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise WorkspaceModuleError(f"Не удалось получить slug из значения: {value!r}")
    return slug[:80].rstrip("-")


def resolve_inside(root: Path, candidate: Path) -> Path:
    """Resolve a path and reject attempts to escape the selected Workspace."""

    resolved_root = root.expanduser().resolve()
    resolved_candidate = candidate.expanduser().resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise WorkspaceModuleError(
            f"Путь {resolved_candidate} находится вне Workspace {resolved_root}"
        ) from exc
    if resolved_candidate == resolved_root:
        raise WorkspaceModuleError("Нельзя поделиться корнем Workspace целиком через эту операцию")
    if not resolved_candidate.is_dir():
        raise WorkspaceModuleError(f"Источник не является существующей папкой: {resolved_candidate}")
    return resolved_candidate


def infer_object_type(relative: Path) -> str:
    """Infer the semantic object type from standard Workspace directories."""

    parts = relative.parts
    if not parts:
        return "folder"
    if parts[0] == "projects":
        return "project" if len(parts) == 2 else "subproject"
    if parts[0] == "tasks":
        return "task"
    if parts[0] == "routines":
        return "routine"
    if parts[0] == "shared":
        return "space"
    return "folder"


def git_output(root: Path, args: list[str]) -> str:
    """Run a read-only Git command and return empty output outside a repository."""

    process = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    return process.stdout.strip() if process.returncode == 0 else ""


def is_sensitive_filename(name: str) -> bool:
    return any(pattern.fullmatch(name) for pattern in SENSITIVE_FILE_PATTERNS)


def iter_tree(source: Path) -> Iterable[Path]:
    """Yield entries without descending into machine-local or heavy directories."""

    for current_root, dirs, files in os.walk(source, topdown=True, followlinks=False):
        current = Path(current_root)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            child = current / name
            if name in IGNORED_DIRECTORY_NAMES or child.is_symlink():
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            yield current / name


def inspect_source(
    workspace_root: Path,
    source_path: Path,
    module_slug: str | None = None,
    object_type: str | None = None,
) -> Inspection:
    """Build a conservative report without changing the source tree."""

    root = workspace_root.expanduser().resolve()
    source = resolve_inside(root, source_path)
    relative = source.relative_to(root)
    detected_type = object_type or infer_object_type(relative)
    if detected_type not in VALID_OBJECT_TYPES:
        raise WorkspaceModuleError(f"Неподдерживаемый тип объекта: {detected_type}")
    normalized_slug = slugify(module_slug or source.name)
    repository_name = f"{slugify(root.name)}-{detected_type}-{normalized_slug}"
    report = Inspection(
        workspace_root=str(root),
        source_path=relative.as_posix(),
        object_type=detected_type,
        module_slug=normalized_slug,
        suggested_repository_name=repository_name,
    )

    # A fresh snapshot is deterministic only if the selected source is not in
    # the middle of unrelated edits. The user may finish or commit them first.
    dirty = git_output(root, ["status", "--porcelain", "--", relative.as_posix()])
    if dirty:
        report.blockers.append(
            Finding("dirty_source", relative.as_posix(), "В выбранном объекте есть незакоммиченные изменения")
        )

    inherited_rules = []
    cursor = source
    while True:
        candidate = cursor / "AGENTS.md"
        if candidate.exists():
            inherited_rules.append(candidate.relative_to(root).as_posix())
        if cursor == root:
            break
        cursor = cursor.parent
    if not (source / "AGENTS.md").exists():
        report.warnings.append(
            Finding(
                "missing_local_agents",
                relative.as_posix(),
                "До передачи материализовать применимые наследуемые правила в корневом AGENTS.md модуля",
            )
        )
    for rules_path in inherited_rules:
        if rules_path != f"{relative.as_posix()}/AGENTS.md":
            report.warnings.append(
                Finding("inherited_rules", rules_path, "Правила действуют на источник, но находятся вне передаваемого корня")
            )

    for current_root, dirs, files in os.walk(source, topdown=True, followlinks=False):
        current = Path(current_root)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            child = current / name
            rel = child.relative_to(source).as_posix()
            if child.is_symlink():
                report.blockers.append(Finding("symlink", rel, "Символическая ссылка может вывести копирование за границу объекта"))
                report.excluded_paths.append(rel)
            elif name in IGNORED_DIRECTORY_NAMES:
                report.excluded_paths.append(rel + "/")
                if name in PRIVATE_DIRECTORY_NAMES and any(child.rglob("*")):
                    report.blockers.append(
                        Finding(
                            "excluded_private_tree",
                            rel + "/",
                            "Приватное или внешнее дерево содержит данные; перед заменой источника их нужно классифицировать и сохранить отдельно",
                        )
                    )
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs

        for name in sorted(files):
            path = current / name
            rel = path.relative_to(source).as_posix()
            if path.is_symlink():
                report.blockers.append(Finding("symlink", rel, "Символическая ссылка не переносится автоматически"))
                report.excluded_paths.append(rel)
                continue
            if is_sensitive_filename(name):
                report.blockers.append(Finding("sensitive_filename", rel, "Файл похож на секрет, сессию или ключ"))
                report.excluded_paths.append(rel)
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                report.blockers.append(Finding("unreadable", rel, str(exc)))
                continue
            report.file_count += 1
            report.byte_count += size
            if size > 1_000_000:
                report.warnings.append(Finding("large_file", rel, f"Файл размером {size} байт требует отдельной проверки хранения"))
            if path.suffix.lower() not in TEXT_SUFFIXES or size > 2_000_000:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            for label, pattern in SECRET_CONTENT_PATTERNS.items():
                if pattern.search(content):
                    report.blockers.append(Finding("secret_content", rel, f"Найден признак секрета: {label}"))
            if ABSOLUTE_PATH_PATTERN.search(content):
                report.warnings.append(Finding("absolute_path", rel, "Найден абсолютный пользовательский путь"))
            for match in WORKSPACE_REFERENCE_PATTERN.finditer(content):
                referenced = match.group(1).rstrip(".,;:")
                if not referenced.startswith(relative.as_posix() + "/"):
                    report.warnings.append(Finding("external_reference", rel, referenced))

    # Keep the report stable and readable when a path is mentioned repeatedly.
    report.blockers = unique_findings(report.blockers)
    report.warnings = unique_findings(report.warnings)
    report.excluded_paths = sorted(set(report.excluded_paths))
    return report


def unique_findings(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, str]] = set()
    result: list[Finding] = []
    for finding in findings:
        key = (finding.kind, finding.path, finding.detail)
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result


def copy_ignore(directory: str, names: list[str]) -> set[str]:
    """Exclude unsafe entries even after an inspect/prepare race."""

    current = Path(directory)
    ignored: set[str] = set()
    for name in names:
        candidate = current / name
        if name in IGNORED_DIRECTORY_NAMES or candidate.is_symlink() or is_sensitive_filename(name):
            ignored.add(name)
    return ignored


def prepare_snapshot(
    report: Inspection,
    destination: Path,
    owner: str | None,
    participants: list[str],
    suggested_mount_path: str | None,
) -> Path:
    """Copy a safe fresh snapshot and write its neutral module manifest."""

    if report.blockers:
        raise WorkspaceModuleError("Подготовка остановлена: pre-share report содержит блокеры")
    source = Path(report.workspace_root) / report.source_path
    target = destination.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise WorkspaceModuleError(f"Папка назначения не пуста: {target}")
    if target.exists():
        target.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=copy_ignore, symlinks=False)

    manifest = {
        "schemaVersion": MODULE_SCHEMA_VERSION,
        "format": "workspace-object",
        "moduleId": report.module_slug,
        "objectType": report.object_type,
        "source": {
            "workspaceName": Path(report.workspace_root).name,
            "path": report.source_path,
            "historyMode": "fresh-snapshot",
        },
        "repository": {"suggestedName": report.suggested_repository_name},
        "mount": {"suggestedPath": suggested_mount_path or report.source_path},
        "access": {
            "owner": owner,
            "participants": sorted(set(participants)),
            "note": "Manifest participants describe intent; the Git provider enforces real access.",
        },
        "assets": {"policy": "separate-shared-storage"},
        "createdAt": now_iso(),
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def load_module_manifest(module_root: Path) -> dict:
    """Read and validate the minimum interoperable manifest contract."""

    path = module_root.expanduser().resolve() / MANIFEST_NAME
    if not path.exists():
        raise WorkspaceModuleError(f"В корне общего объекта отсутствует {MANIFEST_NAME}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceModuleError(f"Некорректный JSON в {path}: {exc}") from exc
    required = {
        "schemaVersion": MODULE_SCHEMA_VERSION,
        "format": "workspace-object",
    }
    for key, expected in required.items():
        if data.get(key) != expected:
            raise WorkspaceModuleError(f"{key} должен быть равен {expected!r}")
    if not data.get("moduleId") or data.get("objectType") not in VALID_OBJECT_TYPES:
        raise WorkspaceModuleError("Manifest должен содержать moduleId и поддерживаемый objectType")
    if data.get("source", {}).get("historyMode") != "fresh-snapshot":
        raise WorkspaceModuleError("Текущая версия поддерживает только безопасный historyMode=fresh-snapshot")
    return data


def register_module(
    workspace_json: Path,
    module_root: Path,
    remote: str,
    local_path: str,
) -> bool:
    """Add or verify a module declaration without overwriting unrelated config."""

    manifest = load_module_manifest(module_root)
    config_path = workspace_json.expanduser().resolve()
    if not config_path.exists():
        raise WorkspaceModuleError(f"Не найден workspace.json: {config_path}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    modules = data.setdefault("modules", [])
    module_id = manifest["moduleId"]
    declaration = {
        "moduleId": module_id,
        "objectType": manifest["objectType"],
        "repository": remote,
        "localPath": local_path,
        "suggestedMountPath": manifest.get("mount", {}).get("suggestedPath"),
        "syncPolicy": "ff-only",
    }
    for existing in modules:
        if existing.get("moduleId") == module_id:
            if existing != declaration:
                raise WorkspaceModuleError(
                    f"Модуль {module_id} уже зарегистрирован с другой конфигурацией"
                )
            return False
    modules.append(declaration)
    modules.sort(key=lambda item: item.get("moduleId", ""))
    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def comparable_files(root: Path, include_manifest: bool = False) -> dict[str, str]:
    """Return SHA-256 values for files that define a snapshot's content.

    Comparing bytes before replacing the original tree prevents a successful
    remote creation from turning into accidental local data loss.
    """

    import hashlib

    result: dict[str, str] = {}
    for path in iter_tree(root):
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST_NAME and not include_manifest:
            continue
        if is_sensitive_filename(path.name) or path.is_symlink():
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def ensure_shared_ignored(workspace_root: Path) -> None:
    """Refuse nested checkouts until the parent Git explicitly ignores them."""

    gitignore = workspace_root / ".gitignore"
    if not gitignore.exists():
        raise WorkspaceModuleError("В Workspace отсутствует .gitignore")
    lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    if "/shared/" not in lines and "shared/" not in lines:
        raise WorkspaceModuleError("Перед подключением добавьте /shared/ в корневой .gitignore")


def write_reference(target: Path, declaration: dict) -> bool:
    """Create a small tracked reference without overwriting existing content."""

    reference_path = target / "workspace-reference.json"
    readme_path = target / "README.md"
    reference = {
        "schemaVersion": 1,
        "format": "workspace-reference",
        **declaration,
    }
    if target.exists():
        existing_entries = {path.name for path in target.iterdir()}
        allowed = {"workspace-reference.json", "README.md"}
        if existing_entries - allowed:
            raise WorkspaceModuleError(f"Нельзя заменить непустой путь ссылкой: {target}")
        if reference_path.exists():
            existing = json.loads(reference_path.read_text(encoding="utf-8"))
            if existing != reference:
                raise WorkspaceModuleError(f"В {target} уже находится другая workspace reference")
            return False
    else:
        target.mkdir(parents=True)
    reference_path.write_text(json.dumps(reference, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme_path.write_text(
        "# Общий объект Workspace\n\n"
        f"Каноническая рабочая копия подключена как `{declaration['localPath']}`. "
        "Перед изменением синхронизируйте соответствующий отдельный Git-репозиторий.\n",
        encoding="utf-8",
    )
    return True


def replace_source_with_reference(
    workspace_root: Path,
    source_path: Path,
    module_root: Path,
    workspace_json: Path,
    remote: str,
    local_path: str,
) -> None:
    """Replace a byte-identical source snapshot with a tracked logical reference."""

    root = workspace_root.expanduser().resolve()
    source = resolve_inside(root, source_path)
    module = resolve_inside(root, module_root)
    ensure_shared_ignored(root)
    manifest = load_module_manifest(module)
    expected_source = manifest.get("source", {}).get("path")
    actual_source = source.relative_to(root).as_posix()
    if expected_source != actual_source:
        raise WorkspaceModuleError(
            f"Manifest ожидает source.path={expected_source!r}, а выбран {actual_source!r}"
        )
    expected_local = module.relative_to(root).as_posix()
    if expected_local != local_path:
        raise WorkspaceModuleError(
            f"localPath={local_path!r} не совпадает с фактическим путём {expected_local!r}"
        )
    if not local_path.startswith("shared/"):
        raise WorkspaceModuleError("Общий checkout должен находиться внутри shared/")
    final_report = inspect_source(
        root,
        source,
        module_slug=manifest["moduleId"],
        object_type=manifest["objectType"],
    )
    if final_report.blockers:
        raise WorkspaceModuleError(
            "Финальная проверка источника обнаружила блокеры; replacement остановлен"
        )
    source_hashes = comparable_files(source)
    module_hashes = comparable_files(module)
    if source_hashes != module_hashes:
        missing = sorted(set(source_hashes) - set(module_hashes))
        extra = sorted(set(module_hashes) - set(source_hashes))
        changed = sorted(
            path for path in set(source_hashes) & set(module_hashes)
            if source_hashes[path] != module_hashes[path]
        )
        raise WorkspaceModuleError(
            "Исходник и общий checkout различаются; replacement остановлен. "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    if git_output(module, ["status", "--porcelain"]):
        raise WorkspaceModuleError("В общем checkout есть незакоммиченные изменения")
    declaration = {
        "moduleId": manifest["moduleId"],
        "objectType": manifest["objectType"],
        "repository": remote,
        "localPath": local_path,
        "suggestedMountPath": actual_source,
    }
    # The shared checkout and the parent Git history are both complete recovery
    # points at this stage. Only now is it safe to remove the duplicate tree.
    shutil.rmtree(source)
    write_reference(source, declaration)
    register_module(workspace_json, module, remote, local_path)


def create_reference(
    workspace_root: Path,
    module_root: Path,
    target_path: Path,
    remote: str,
    local_path: str,
) -> bool:
    """Create a recipient-side reference if the suggested path is free."""

    root = workspace_root.expanduser().resolve()
    module = resolve_inside(root, module_root)
    target = root / target_path
    resolve_target_parent = target.parent.resolve()
    try:
        resolve_target_parent.relative_to(root)
    except ValueError as exc:
        raise WorkspaceModuleError("Путь reference выходит за границу Workspace") from exc
    manifest = load_module_manifest(module)
    declaration = {
        "moduleId": manifest["moduleId"],
        "objectType": manifest["objectType"],
        "repository": remote,
        "localPath": local_path,
        "suggestedMountPath": target_path.as_posix(),
    }
    return write_reference(target, declaration)


def doctor(workspace_root: Path) -> tuple[list[str], list[str]]:
    """Check configured module paths and Git safety without changing them."""

    root = workspace_root.expanduser().resolve()
    workspace_json = root / "workspace.json"
    errors: list[str] = []
    warnings: list[str] = []
    if not workspace_json.exists():
        return ["workspace.json отсутствует"], warnings
    data = json.loads(workspace_json.read_text(encoding="utf-8"))
    for module in data.get("modules", []):
        module_id = module.get("moduleId", "<unknown>")
        local_path = module.get("localPath")
        if not local_path:
            errors.append(f"{module_id}: localPath отсутствует")
            continue
        module_root = root / local_path
        try:
            manifest = load_module_manifest(module_root)
        except WorkspaceModuleError as exc:
            errors.append(f"{module_id}: {exc}")
            continue
        if manifest.get("moduleId") != module_id:
            errors.append(f"{module_id}: moduleId в manifest не совпадает")
        if not (module_root / ".git").exists():
            warnings.append(f"{module_id}: локальная папка не выглядит как отдельный Git checkout")
            continue
        dirty = git_output(module_root, ["status", "--porcelain"])
        if dirty:
            warnings.append(f"{module_id}: есть незакоммиченные изменения")
        remote = git_output(module_root, ["remote", "get-url", "origin"])
        if remote and remote != module.get("repository"):
            errors.append(f"{module_id}: origin не совпадает с workspace.json")
    return errors, warnings


def print_inspection(report: Inspection) -> None:
    print(f"Объект: {report.source_path} ({report.object_type})")
    print(f"Рекомендуемый репозиторий: {report.suggested_repository_name}")
    print(f"Файлов: {report.file_count}; байт: {report.byte_count}")
    print(f"Блокеров: {len(report.blockers)}; предупреждений: {len(report.warnings)}")
    for finding in report.blockers:
        print(f"BLOCK {finding.kind}: {finding.path} – {finding.detail}")
    for finding in report.warnings:
        print(f"WARN {finding.kind}: {finding.path} – {finding.detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Построить pre-share report")
    inspect_parser.add_argument("source", type=Path)
    inspect_parser.add_argument("--workspace-root", type=Path, default=ROOT)
    inspect_parser.add_argument("--module-slug")
    inspect_parser.add_argument("--object-type", choices=sorted(VALID_OBJECT_TYPES))
    inspect_parser.add_argument("--json", action="store_true")

    prepare_parser = subparsers.add_parser("prepare", help="Подготовить fresh snapshot")
    prepare_parser.add_argument("source", type=Path)
    prepare_parser.add_argument("destination", type=Path)
    prepare_parser.add_argument("--workspace-root", type=Path, default=ROOT)
    prepare_parser.add_argument("--module-slug")
    prepare_parser.add_argument("--object-type", choices=sorted(VALID_OBJECT_TYPES))
    prepare_parser.add_argument("--owner")
    prepare_parser.add_argument("--participant", action="append", default=[])
    prepare_parser.add_argument("--suggested-mount-path")

    validate_parser = subparsers.add_parser("validate-module", help="Проверить module manifest")
    validate_parser.add_argument("module_root", type=Path)

    register_parser = subparsers.add_parser("register", help="Зарегистрировать подключённый модуль")
    register_parser.add_argument("module_root", type=Path)
    register_parser.add_argument("--workspace-json", type=Path, default=ROOT / "workspace.json")
    register_parser.add_argument("--remote", required=True)
    register_parser.add_argument("--local-path", required=True)

    replace_parser = subparsers.add_parser(
        "replace-source", help="Заменить исходный объект проверенной логической ссылкой"
    )
    replace_parser.add_argument("source", type=Path)
    replace_parser.add_argument("module_root", type=Path)
    replace_parser.add_argument("--workspace-root", type=Path, default=ROOT)
    replace_parser.add_argument("--workspace-json", type=Path, default=ROOT / "workspace.json")
    replace_parser.add_argument("--remote", required=True)
    replace_parser.add_argument("--local-path", required=True)

    reference_parser = subparsers.add_parser(
        "create-reference", help="Создать reference получателя без перезаписи данных"
    )
    reference_parser.add_argument("module_root", type=Path)
    reference_parser.add_argument("target_path", type=Path)
    reference_parser.add_argument("--workspace-root", type=Path, default=ROOT)
    reference_parser.add_argument("--remote", required=True)
    reference_parser.add_argument("--local-path", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Проверить все подключённые модули")
    doctor_parser.add_argument("--workspace-root", type=Path, default=ROOT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command in {"inspect", "prepare"}:
            report = inspect_source(
                args.workspace_root,
                args.source,
                module_slug=args.module_slug,
                object_type=args.object_type,
            )
            if args.command == "inspect":
                if args.json:
                    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
                else:
                    print_inspection(report)
                return 0 if report.safe_to_prepare else 2
            target = prepare_snapshot(
                report,
                args.destination,
                owner=args.owner,
                participants=args.participant,
                suggested_mount_path=args.suggested_mount_path,
            )
            print(f"Fresh snapshot подготовлен: {target}")
            return 0
        if args.command == "validate-module":
            manifest = load_module_manifest(args.module_root)
            print(f"Module manifest проверен: {manifest['moduleId']}")
            return 0
        if args.command == "register":
            changed = register_module(
                args.workspace_json, args.module_root, args.remote, args.local_path
            )
            print("Модуль зарегистрирован." if changed else "Модуль уже зарегистрирован.")
            return 0
        if args.command == "replace-source":
            replace_source_with_reference(
                args.workspace_root,
                args.source,
                args.module_root,
                args.workspace_json,
                args.remote,
                args.local_path,
            )
            print("Исходный объект заменён логической ссылкой.")
            return 0
        if args.command == "create-reference":
            changed = create_reference(
                args.workspace_root,
                args.module_root,
                args.target_path,
                args.remote,
                args.local_path,
            )
            print("Reference создан." if changed else "Reference уже существует.")
            return 0
        if args.command == "doctor":
            errors, warnings = doctor(args.workspace_root)
            for warning in warnings:
                print(f"WARN: {warning}")
            for error in errors:
                print(f"ERROR: {error}")
            if errors:
                return 1
            print("Проверка модулей пройдена.")
            return 0
    except (OSError, ValueError, json.JSONDecodeError, WorkspaceModuleError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
