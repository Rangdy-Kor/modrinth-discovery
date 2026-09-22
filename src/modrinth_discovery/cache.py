from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .api import CompatibleVersionNotFound, ModrinthAPI
from .diagnostics import get_logger
from .models import BatchStatus, Dependency, InstalledMod, Instance, ModFile
from .scanner import inspect_mod_archive

logger=get_logger("cache")
COMPATIBILITY_TTL=24*60*60
MAPPING_TTL=7*24*60*60


@dataclass(slots=True)
class Timing:
    scan: float=0; metadata: float=0; hashing: float=0; hash_lookup: float=0; project_lookup: float=0


class PersistentCache:
    def __init__(self, database_path: Path) -> None: self.database_path=database_path

    def _connect(self) -> sqlite3.Connection:
        connection=sqlite3.connect(self.database_path); connection.row_factory=sqlite3.Row; return connection

    def inventory(self, api: ModrinthAPI, instance: Instance, *, force: bool=False) -> tuple[list[InstalledMod],Timing]:
        timing=Timing(); started=time.perf_counter(); key=instance.instance_id or str(instance.path.resolve())
        paths=[p for p in sorted(instance.mods_directory.iterdir()) if p.is_file() and p.suffix.lower() in {".jar",".zip"}] if instance.mods_directory.is_dir() else []
        timing.scan=time.perf_counter()-started; con=self._connect(); now=time.time(); mods=[]; remote=[]
        metadata_started=time.perf_counter()
        for path in paths:
            stat=path.stat(); row=con.execute("SELECT * FROM installed_file_cache WHERE instance_key=? AND path=?",(key,str(path.resolve()))).fetchone()
            valid=row and row["size"]==stat.st_size and row["mtime_ns"]==stat.st_mtime_ns
            if valid:
                digest=row["sha512"]
            else:
                hash_started=time.perf_counter(); digest=_sha512(path); timing.hashing+=time.perf_counter()-hash_started
                con.execute("""INSERT INTO installed_file_cache(instance_key,path,size,mtime_ns,sha512,mapped_at)
                    VALUES(?,?,?,?,?,NULL) ON CONFLICT(instance_key,path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
                    sha512=excluded.sha512,project_id=NULL,version_id=NULL,project_title=NULL,mapped_at=NULL""",
                    (key,str(path.resolve()),stat.st_size,stat.st_mtime_ns,digest)); row=None
            try: local=inspect_mod_archive(path) or InstalledMod(path,())
            except Exception: local=InstalledMod(path,())
            mapped=valid and row["mapped_at"] and now-row["mapped_at"]<MAPPING_TTL and not force
            mod=InstalledMod(path,local.mod_ids,local.name,local.version,local.loaders,digest,
                row["project_id"] if mapped else None,row["version_id"] if mapped else None,row["project_title"] if mapped else None)
            mods.append(mod)
            if not mapped: remote.append((len(mods)-1,digest,path,local))
        timing.metadata=time.perf_counter()-metadata_started-timing.hashing
        con.execute("DELETE FROM installed_file_cache WHERE instance_key=? AND path NOT IN (%s)" % (",".join("?"*len(paths)) or "''"),
                    (key,*[str(p.resolve()) for p in paths])); con.commit()
        lookup=time.perf_counter(); versions=api.get_versions_from_hashes([x[1] for x in remote]) if remote else {}; timing.hash_lookup=time.perf_counter()-lookup
        ids=list({v["project_id"] for v in versions.values() if v.get("project_id")})
        lookup=time.perf_counter(); projects=api.get_projects(ids) if ids else {}; timing.project_lookup=time.perf_counter()-lookup
        for index,digest,path,local in remote:
            version=versions.get(digest); pid=version.get("project_id") if version else None; title=projects.get(pid,{}).get("title") if pid else None
            mods[index]=InstalledMod(path,local.mod_ids,local.name,local.version,local.loaders,digest,pid,version.get("id") if version else None,title)
            con.execute("UPDATE installed_file_cache SET project_id=?,version_id=?,project_title=?,mapped_at=? WHERE instance_key=? AND path=?",
                        (pid,version.get("id") if version else None,title,now,key,str(path.resolve())))
        con.commit(); con.close()
        logger.info("inventory.timing files=%d scan=%.3f metadata=%.3f hash=%.3f hash_lookup=%.3f projects=%.3f",len(paths),timing.scan,timing.metadata,timing.hashing,timing.hash_lookup,timing.project_lookup)
        return mods,timing

    def compatibility(self, api: ModrinthAPI, project_id: str, minecraft_version: str, loader: str, *, force: bool=False) -> tuple[BatchStatus,ModFile|None,str,bool]:
        con=self._connect(); row=con.execute("SELECT * FROM compatibility_cache WHERE project_id=? AND minecraft_version=? AND loader=?",
            (project_id,minecraft_version,loader)).fetchone(); now=time.time()
        if row and not force and now-row["checked_at"]<COMPATIBILITY_TTL:
            con.close(); return BatchStatus(row["status"]),_decode_file(row["payload"]),row["detail"],True
        try: file=api.get_latest_file(project_id,minecraft_version=minecraft_version,loader=loader); status=BatchStatus.INSTALLABLE; detail=""
        except CompatibleVersionNotFound as error: file=None; status=BatchStatus.INCOMPATIBLE; detail=str(error)
        except Exception as error: file=None; status=BatchStatus.LOOKUP_FAILED; detail=str(error)
        con.execute("""INSERT INTO compatibility_cache VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,minecraft_version,loader)
            DO UPDATE SET status=excluded.status,payload=excluded.payload,detail=excluded.detail,checked_at=excluded.checked_at""",
            (project_id,minecraft_version,loader,status.value,_encode_file(file),detail,now)); con.commit(); con.close()
        return status,file,detail,False


def _sha512(path: Path) -> str:
    digest=hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()


def _encode_file(file: ModFile|None) -> str|None: return json.dumps(asdict(file)) if file else None
def _decode_file(value: str|None) -> ModFile|None:
    if not value:return None
    data=json.loads(value); data["dependencies"]=tuple(Dependency(**d) for d in data.get("dependencies",[])); return ModFile(**data)
