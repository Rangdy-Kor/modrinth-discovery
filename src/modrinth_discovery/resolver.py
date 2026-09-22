from __future__ import annotations

from .api import ModrinthAPI
from .models import DependencyIssue, DependencyPlan, ModFile


class DependencyResolver:
    def __init__(self, api: ModrinthAPI) -> None: self.api=api

    def resolve(self, roots: list[ModFile], *, minecraft_version: str, loader: str,
                installed_project_ids: set[str], automatic: bool) -> DependencyPlan:
        files={f.project_id:f for f in roots}; issues=[]; visiting:set[str]=set(); resolved=set(files)
        def visit(owner: ModFile) -> None:
            if owner.project_id in visiting:
                issues.append(DependencyIssue("cycle",owner.project_id,owner.project_id,"Dependency cycle detected.")); return
            visiting.add(owner.project_id)
            for dep in owner.dependencies:
                target=dep.project_id
                if dep.dependency_type=="optional" or dep.dependency_type=="embedded": continue
                if dep.dependency_type=="incompatible":
                    if target and target in installed_project_ids:
                        issues.append(DependencyIssue("incompatible",owner.project_id,target,"비호환 프로젝트가 설치되어 있습니다."))
                    continue
                if dep.dependency_type!="required":
                    issues.append(DependencyIssue("unknown",owner.project_id,target,"지원하지 않는 dependency 유형입니다.")); continue
                if not target and not dep.version_id:
                    issues.append(DependencyIssue("unknown",owner.project_id,None,"project_id 없는 필수 dependency입니다.")); continue
                if not target and dep.version_id:
                    try:
                        raw=self.api.get_version(dep.version_id); target=raw.get("project_id")
                    except Exception as error:
                        issues.append(DependencyIssue("unknown",owner.project_id,None,f"dependency version 조회 실패: {error}")); continue
                if target in visiting:
                    issues.append(DependencyIssue("cycle",owner.project_id,target,"Dependency cycle detected.")); continue
                if target in installed_project_ids or target in resolved: continue
                if not automatic:
                    issues.append(DependencyIssue("required",owner.project_id,target,"필수 dependency가 누락되었습니다.")); continue
                try:
                    if dep.version_id:
                        raw=self.api.get_version(dep.version_id)
                        if minecraft_version not in raw.get("game_versions",[]) or loader not in raw.get("loaders",[]):
                            raise ValueError("지정 dependency version이 현재 인스턴스와 호환되지 않습니다.")
                        child=self.api.mod_file_from_version(raw)
                    else:
                        child=self.api.get_latest_file(target,minecraft_version=minecraft_version,loader=loader)
                except Exception as error:
                    issues.append(DependencyIssue("unknown",owner.project_id,target,f"호환 dependency를 결정할 수 없습니다: {error}")); continue
                files[target]=child; resolved.add(target); visit(child)
            visiting.discard(owner.project_id)
        for root in roots: visit(root)
        return DependencyPlan(tuple(files.values()),tuple(issues))
