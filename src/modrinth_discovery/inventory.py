from __future__ import annotations

from pathlib import Path
from .api import ModrinthAPI
from .models import DependencyHealth, DependencyIssue, InstalledMod
from .scanner import scan_installed_mods


def build_inventory(api: ModrinthAPI, mods_directory: Path) -> list[InstalledMod]:
    local=scan_installed_mods(mods_directory,calculate_hashes=True)
    hashes=[m.sha512 for m in local if m.sha512]
    versions=api.get_versions_from_hashes(hashes)
    project_ids=list({v["project_id"] for v in versions.values() if v.get("project_id")})
    projects=api.get_projects(project_ids)
    result=[]
    for mod in local:
        version=versions.get(mod.sha512 or "")
        project_id=version.get("project_id") if version else None
        result.append(InstalledMod(mod.path,mod.mod_ids,mod.name,mod.version,mod.loaders,mod.sha512,
            project_id,version.get("id") if version else None,
            projects.get(project_id,{}).get("title") if project_id else None))
    return result


def validate_inventory(api: ModrinthAPI, inventory: list[InstalledMod]) -> tuple[DependencyIssue,...]:
    installed={m.project_id for m in inventory if m.project_id}; issues=[]
    versions=api.get_versions_by_ids([m.version_id for m in inventory if m.version_id])
    for mod in inventory:
        if not mod.version_id or not mod.project_id:
            issues.append(DependencyIssue("unknown",mod.project_id or mod.path.name,None,"Modrinth version을 확인할 수 없습니다.")); continue
        version=versions.get(mod.version_id)
        if not version:
            issues.append(DependencyIssue("unknown",mod.project_id,None,"version metadata를 찾을 수 없습니다.")); continue
        for dep in version.get("dependencies",[]):
            kind=dep.get("dependency_type","unknown"); target=dep.get("project_id")
            if kind=="required" and (not target or target not in installed):
                issues.append(DependencyIssue(kind,mod.project_id,target,"필수 dependency가 없습니다."))
            elif kind=="incompatible" and target in installed:
                issues.append(DependencyIssue(kind,mod.project_id,target,"비호환 dependency가 설치되어 있습니다."))
            elif kind in {"optional","embedded"}: continue
            elif kind not in {"required","incompatible"}:
                issues.append(DependencyIssue("unknown",mod.project_id,target,"알 수 없는 dependency입니다."))
    return tuple(issues)
