from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "modrinth-discovery"


def user_data_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return Path(base) / APP_NAME if base else Path.home() / f".{APP_NAME}"


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path
    page_size: int = 50
    prefetch_threshold: int = 8
    timeout_seconds: float = 30.0


CONFIG = AppConfig(database_path=user_data_directory() / "discovery.db")
