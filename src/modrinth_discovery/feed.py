from __future__ import annotations

from collections.abc import Iterable
from .models import Project


def filter_unseen(projects: Iterable[Project], reviewed: set[str], seen: set[str]) -> list[Project]:
    result = []
    for project in projects:
        if project.project_id in reviewed or project.project_id in seen: continue
        seen.add(project.project_id); result.append(project)
    return result
