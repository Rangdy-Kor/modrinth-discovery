from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from .models import Instance


class InstanceDiscoveryError(RuntimeError):
    pass


def default_modrinth_settings_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "ModrinthApp" if appdata else None


def discover_instances(settings_dir: Path | None = None) -> list[Instance]:
    settings_dir = settings_dir or default_modrinth_settings_dir()
    if settings_dir is None or not (settings_dir / "app.db").is_file():
        return []
    db_path = settings_dir / "app.db"
    try:
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        root = _config_root(connection, settings_dir)
        if {"instances", "instance_content_sets"}.issubset(tables):
            rows = connection.execute("""SELECT i.id,i.path,i.name,cs.game_version,
                cs.loader,cs.loader_version FROM instances i JOIN instance_content_sets cs
                ON cs.id=i.applied_content_set_id WHERE i.install_stage='installed'
                ORDER BY i.name COLLATE NOCASE""").fetchall()
            result = [_instance_from_row(row, root, "app-db") for row in rows]
        elif "profiles" in tables:
            rows = connection.execute("""SELECT path,name,game_version,mod_loader AS loader,
                mod_loader_version AS loader_version FROM profiles WHERE install_stage='installed'
                ORDER BY name COLLATE NOCASE""").fetchall()
            result = [_instance_from_row(row, root, "app-db-legacy") for row in rows]
        else:
            result = []
        connection.close()
        return result
    except sqlite3.Error as error:
        raise InstanceDiscoveryError(f"Modrinth App 데이터베이스를 읽지 못했습니다: {error}") from error


def manual_instance(path: Path, *, minecraft_version: str = "", loader: str = "fabric",
                    loader_version: str | None = None) -> Instance:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise InstanceDiscoveryError("선택한 인스턴스 디렉터리가 존재하지 않습니다.")
    metadata = _legacy_profile_metadata(path)
    return Instance(str(metadata.get("name") or path.name), path, path / "mods",
        minecraft_version or str(metadata.get("game_version", "")),
        str(metadata.get("mod_loader", loader)).lower(),
        str(metadata.get("mod_loader_version", loader_version)) if metadata.get("mod_loader_version", loader_version) else None)


def _config_root(connection: sqlite3.Connection, settings_dir: Path) -> Path:
    try:
        row = connection.execute("SELECT custom_dir FROM settings WHERE id=0").fetchone()
        if row and row[0]: return Path(row[0]).expanduser()
    except sqlite3.Error:
        pass
    return settings_dir


def _instance_from_row(row: sqlite3.Row, root: Path, source: str) -> Instance:
    path = root / "profiles" / row["path"]
    return Instance(row["name"], path, path / "mods", row["game_version"],
        str(row["loader"]).lower(), row["loader_version"],
        row["id"] if "id" in row.keys() else None, source)


def _legacy_profile_metadata(path: Path) -> dict[str, object]:
    try:
        value = json.loads((path / "profile.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
