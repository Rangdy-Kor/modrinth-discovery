from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path

from .api import ModrinthAPI
from .models import DependencyStatus, InstallPlan, ModFile
from .scanner import inspect_mod_archive, scan_installed_mods
from .diagnostics import get_logger

logger = get_logger("installer")


class InstallError(RuntimeError): pass
class HashMismatchError(InstallError): pass


class ExistingModConflict(InstallError):
    def __init__(self, conflicts: tuple, message: str) -> None:
        super().__init__(message)
        self.conflicts = conflicts


class ModInstaller:
    def __init__(self, api: ModrinthAPI) -> None:
        self.api = api

    def prepare(self, mod_file: ModFile, mods_directory: Path) -> InstallPlan:
        logger.info("prepare.scan.start project=%s directory=%s dependencies=%d",
                    mod_file.project_id, mods_directory, len(mod_file.dependencies))
        installed = scan_installed_mods(mods_directory)
        # Modrinth dependency project IDs are not equivalent to loader mod IDs.
        # Unknown is deliberately reported instead of pretending it is resolved.
        statuses = tuple(DependencyStatus(dep, None) for dep in mod_file.dependencies)
        logger.info("prepare.scan.done project=%s installed_archives=%d",
                    mod_file.project_id, len(installed))
        return InstallPlan(mod_file, statuses)

    def install(self, plan: InstallPlan, mods_directory: Path, *,
                replace_paths: Iterable[Path] = (),
                progress: Callable[[int], None] | None = None) -> tuple[Path, InstallPlan]:
        mods_directory.mkdir(parents=True, exist_ok=True)
        destination = mods_directory / Path(plan.mod_file.filename).name
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", suffix=".download",
                    dir=mods_directory, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                sha512, sha1, written = hashlib.sha512(), hashlib.sha1(), 0
                for chunk in self.api.iter_download(plan.mod_file.url):
                    temporary.write(chunk); sha512.update(chunk); sha1.update(chunk); written += len(chunk)
                    if progress and plan.mod_file.size:
                        progress(min(99, int(written * 100 / plan.mod_file.size)))
                temporary.flush(); os.fsync(temporary.fileno())
            _verify_hash(plan.mod_file, sha512.hexdigest(), sha1.hexdigest())
            target = inspect_mod_archive(temporary_path)
            target_ids = target.mod_ids if target else ()
            conflicts = tuple(mod for mod in scan_installed_mods(mods_directory)
                              if target_ids and set(mod.mod_ids) & set(target_ids))
            approved = {path.resolve() for path in replace_paths}
            unexpected = [mod for mod in conflicts if mod.path.resolve() not in approved]
            if unexpected:
                details = ", ".join(f"{m.name or m.path.name} {m.version or ''}".strip() for m in unexpected)
                raise ExistingModConflict(tuple(unexpected), f"동일한 mod ID의 기존 파일이 있습니다: {details}")
            if destination.exists() and destination.resolve() not in approved:
                raise InstallError(f"대상 파일명이 이미 사용 중이지만 mod ID를 확인할 수 없습니다: {destination.name}")
            backups: list[tuple[Path, Path]] = []
            try:
                for old in conflicts:
                    backup = old.path.with_name(f".{old.path.name}.{uuid.uuid4().hex}.replace-backup")
                    os.replace(old.path, backup); backups.append((old.path, backup))
                os.replace(temporary_path, destination); temporary_path = None
            except Exception:
                for original, backup in reversed(backups):
                    if backup.exists(): os.replace(backup, original)
                raise
            for _original, backup in backups:
                if backup.exists(): backup.unlink()
            if progress: progress(100)
            return destination, InstallPlan(plan.mod_file, plan.dependencies, conflicts, target_ids)
        finally:
            if temporary_path and temporary_path.exists(): temporary_path.unlink()


def _verify_hash(mod_file: ModFile, sha512: str, sha1: str) -> None:
    if mod_file.sha512 and sha512.lower() != mod_file.sha512.lower():
        raise HashMismatchError("다운로드 파일의 SHA-512 해시가 Modrinth 값과 다릅니다.")
    if not mod_file.sha512 and mod_file.sha1 and sha1.lower() != mod_file.sha1.lower():
        raise HashMismatchError("다운로드 파일의 SHA-1 해시가 Modrinth 값과 다릅니다.")
