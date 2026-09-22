from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Decision(StrEnum):
    SKIPPED = "skipped"
    MAYBE = "maybe"
    REVIEWED = "reviewed"
    SKIP = "skipped"  # MVP source compatibility


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    decision: Decision
    first_reviewed_at: str
    last_reviewed_at: str


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    project: Project
    decision: Decision
    first_reviewed_at: str
    last_reviewed_at: str


class InstanceState(StrEnum):
    INSTALLED = "installed"
    NOT_INSTALLED = "not_installed"
    UNKNOWN = "unknown"


class Compatibility(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class DependencyHealth(StrEnum):
    HEALTHY = "healthy"
    MISSING_REQUIRED = "missing_required"
    CONFLICT = "conflict"
    UNVERIFIED = "unverified"


class BatchStatus(StrEnum):
    ALREADY_INSTALLED = "already_installed"
    INSTALLABLE = "installable"
    INCOMPATIBLE = "incompatible"
    CONFLICT = "conflict"
    LOOKUP_FAILED = "lookup_failed"


class SearchIndex(StrEnum):
    UPDATED = "updated"
    NEWEST = "newest"
    DOWNLOADS = "downloads"
    FOLLOWS = "follows"
    RELEVANCE = "relevance"


@dataclass(frozen=True, slots=True)
class Instance:
    name: str
    path: Path
    mods_directory: Path
    minecraft_version: str
    loader: str
    loader_version: str | None = None
    instance_id: str | None = None
    source: str = "manual"

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.minecraft_version} / {self.loader})"


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    slug: str
    title: str
    description: str
    author: str
    downloads: int
    icon_url: str | None
    project_url: str
    date_modified: str
    environment: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Dependency:
    dependency_type: str
    project_id: str | None = None
    version_id: str | None = None
    file_name: str | None = None


@dataclass(frozen=True, slots=True)
class ModFile:
    project_id: str
    version_id: str
    version_number: str
    filename: str
    url: str
    sha512: str | None
    sha1: str | None = None
    size: int | None = None
    dependencies: tuple[Dependency, ...] = ()


@dataclass(frozen=True, slots=True)
class InstalledMod:
    path: Path
    mod_ids: tuple[str, ...]
    name: str | None = None
    version: str | None = None
    loaders: tuple[str, ...] = ()
    sha512: str | None = None
    project_id: str | None = None
    version_id: str | None = None
    project_title: str | None = None


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    dependency: Dependency
    installed: bool | None


@dataclass(frozen=True, slots=True)
class InstallPlan:
    mod_file: ModFile
    dependencies: tuple[DependencyStatus, ...]
    conflicts: tuple[InstalledMod, ...] = ()
    target_mod_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class SearchPage:
    projects: list[Project] = field(default_factory=list)
    offset: int = 0
    limit: int = 0
    total_hits: int = 0


@dataclass(frozen=True, slots=True)
class BatchPlanItem:
    project: Project
    status: BatchStatus
    mod_file: ModFile | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BatchPlan:
    items: tuple[BatchPlanItem, ...]
    dependency_issues: tuple[DependencyIssue, ...] = ()

    def count(self, status: BatchStatus) -> int:
        return sum(item.status == status for item in self.items)


@dataclass(frozen=True, slots=True)
class BatchResult:
    project_id: str
    success: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DependencyIssue:
    dependency_type: str
    owner_project_id: str
    dependency_project_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class DependencyPlan:
    files: tuple[ModFile, ...]
    issues: tuple[DependencyIssue, ...]
