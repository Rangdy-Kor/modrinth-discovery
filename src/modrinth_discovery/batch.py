from __future__ import annotations

from pathlib import Path
from .api import CompatibleVersionNotFound, ModrinthAPI
from .installer import ExistingModConflict, ModInstaller
from .models import BatchPlan, BatchPlanItem, BatchResult, BatchStatus, InstalledMod, Project
from .resolver import DependencyResolver


class BatchInstaller:
    def __init__(self, api: ModrinthAPI, installer: ModInstaller) -> None:
        self.api=api; self.installer=installer; self.resolver=DependencyResolver(api)

    def plan(self, projects: list[Project], inventory: list[InstalledMod], *, minecraft_version: str, loader: str,
             automatic_dependencies: bool = False) -> BatchPlan:
        installed={m.project_id for m in inventory if m.project_id}; items=[]
        for project in projects:
            if project.project_id in installed:
                items.append(BatchPlanItem(project,BatchStatus.ALREADY_INSTALLED)); continue
            try: file=self.api.get_latest_file(project.project_id,minecraft_version=minecraft_version,loader=loader)
            except CompatibleVersionNotFound as error:
                items.append(BatchPlanItem(project,BatchStatus.INCOMPATIBLE,detail=str(error))); continue
            except Exception as error:
                items.append(BatchPlanItem(project,BatchStatus.LOOKUP_FAILED,detail=str(error))); continue
            if any(mod.path.name.lower()==file.filename.lower() for mod in inventory):
                items.append(BatchPlanItem(project,BatchStatus.CONFLICT,file,"같은 파일명의 기존 모드가 있습니다.")); continue
            items.append(BatchPlanItem(project,BatchStatus.INSTALLABLE,file))
        roots=[item.mod_file for item in items if item.status==BatchStatus.INSTALLABLE and item.mod_file]
        dependencies=self.resolver.resolve(roots,minecraft_version=minecraft_version,loader=loader,
            installed_project_ids=installed,automatic=automatic_dependencies)
        root_ids={f.project_id for f in roots}
        for file in dependencies.files:
            if file.project_id not in root_ids:
                project=Project(file.project_id,file.project_id,f"Dependency {file.project_id}","","",0,None,
                                f"https://modrinth.com/mod/{file.project_id}","")
                items.append(BatchPlanItem(project,BatchStatus.INSTALLABLE,file,"required dependency"))
        return BatchPlan(tuple(items),dependencies.issues)

    def apply(self, plan: BatchPlan, mods_directory: Path) -> tuple[BatchResult,...]:
        results=[]
        for item in plan.items:
            if item.status!=BatchStatus.INSTALLABLE or not item.mod_file: continue
            try:
                prepared=self.installer.prepare(item.mod_file,mods_directory)
                path,_=self.installer.install(prepared,mods_directory)
                results.append(BatchResult(item.project.project_id,True,path.name))
            except Exception as error:
                results.append(BatchResult(item.project.project_id,False,str(error)))
        return tuple(results)
