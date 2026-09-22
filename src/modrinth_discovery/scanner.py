from __future__ import annotations

import json
import tomllib
import zipfile
import hashlib
from pathlib import Path
from .models import InstalledMod


def scan_installed_mods(directory: Path, *, calculate_hashes: bool = False) -> list[InstalledMod]:
    if not directory.is_dir(): return []
    result = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in {".jar", ".zip"}:
            try:
                item = inspect_mod_archive(path)
                item = item or InstalledMod(path,())
                if calculate_hashes:
                    item = InstalledMod(item.path,item.mod_ids,item.name,item.version,item.loaders,_sha512(path))
                result.append(item)
            except (OSError, zipfile.BadZipFile, KeyError, ValueError, tomllib.TOMLDecodeError):
                pass
    return result


def _sha512(path: Path) -> str:
    digest=hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()


def inspect_mod_archive(path: Path) -> InstalledMod | None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        metadata_name = next((n for n in ("fabric.mod.json", "quilt.mod.json") if n in names), None)
        if metadata_name:
            data = json.loads(archive.read(metadata_name))
            quilt = metadata_name.startswith("quilt")
            if quilt:
                loader = data.get("quilt_loader", {}); meta = loader.get("metadata", {})
                data = meta | {"id": loader.get("id"), "version": loader.get("version")}
            if data.get("id"):
                return InstalledMod(path, (str(data["id"]),), data.get("name"),
                    str(data["version"]) if data.get("version") is not None else None,
                    ("quilt" if quilt else "fabric",))
        meta_path = next((n for n in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml") if n in names), None)
        if meta_path:
            entries = tomllib.loads(archive.read(meta_path).decode("utf-8")).get("mods") or []
            ids = tuple(str(e["modId"]) for e in entries if e.get("modId"))
            if ids:
                return InstalledMod(path, ids, entries[0].get("displayName"), entries[0].get("version"),
                    ("neoforge" if "neoforge" in meta_path else "forge",))
        if "mcmod.info" in names:
            data = json.loads(archive.read("mcmod.info")); entries = data if isinstance(data, list) else data.get("modList", [])
            ids = tuple(str(e["modid"]) for e in entries if e.get("modid"))
            if ids: return InstalledMod(path, ids, entries[0].get("name"), entries[0].get("version"), ("forge",))
    return None
