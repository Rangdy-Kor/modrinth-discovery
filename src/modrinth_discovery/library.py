from __future__ import annotations

from .models import LibraryEntry


def filter_entries(entries: list[LibraryEntry], query: str) -> list[LibraryEntry]:
    needle=query.strip().casefold()
    if not needle:return list(entries)
    return [entry for entry in entries if needle in entry.project.title.casefold() or needle in entry.project.slug.casefold()]
