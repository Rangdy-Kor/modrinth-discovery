from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable

from .api import ModrinthAPI
from .cache import PersistentCache
from .diagnostics import get_logger
from .models import BatchPlan, BatchPlanItem, BatchStatus, Instance, Project

logger=get_logger("reviewed")


class ReviewedService:
    def __init__(self, api: ModrinthAPI, cache: PersistentCache, max_concurrency: int=4) -> None:
        self.api=api; self.cache=cache; self.max_concurrency=max(1,min(max_concurrency,6))

    def load(self, projects: list[Project], instance: Instance, *, force: bool=False,
             emit_item: Callable[[dict],None]|None=None) -> dict:
        emit=emit_item or (lambda _item:None); total=time.perf_counter(); requests=0
        inventory,timing=self.cache.inventory(self.api,instance,force=force)
        installed={m.project_id:m for m in inventory if m.project_id}
        for project in projects:
            mod=installed.get(project.project_id)
            emit({"phase":"inventory","project_id":project.project_id,"installed":mod})
        compat_start=time.perf_counter(); cache_hits=0
        def lookup(project: Project):
            return project,self.cache.compatibility(self.api,project.project_id,instance.minecraft_version,instance.loader,force=force)
        with ThreadPoolExecutor(max_workers=self.max_concurrency,thread_name_prefix="reviewed-lookup") as executor:
            futures=[executor.submit(lookup,p) for p in projects]
            for future in as_completed(futures):
                project,(status,file,detail,hit)=future.result(); cache_hits+=int(hit); requests+=int(not hit)
                emit({"phase":"compatibility","project_id":project.project_id,"status":status,"file":file,"detail":detail})
        compatibility=time.perf_counter()-compat_start
        result={"count":len(projects),"inventory":inventory,"scan":timing.scan,"metadata":timing.metadata,"hashing":timing.hashing,
            "hash_lookup":timing.hash_lookup,"project_lookup":timing.project_lookup,"compatibility":compatibility,
            "dependency_lookup":0.0,"cache_hits":cache_hits,"compat_requests":requests,"total":time.perf_counter()-total}
        logger.info("load.timing %s",result); return result

    def batch_plan(self, projects: list[Project], instance: Instance, *, force_stale: bool=False) -> BatchPlan:
        inventory,_=self.cache.inventory(self.api,instance)
        installed={m.project_id for m in inventory if m.project_id}; items=[]
        for project in projects:
            if project.project_id in installed:
                items.append(BatchPlanItem(project,BatchStatus.ALREADY_INSTALLED)); continue
            status,file,detail,_hit=self.cache.compatibility(self.api,project.project_id,instance.minecraft_version,instance.loader,force=force_stale)
            items.append(BatchPlanItem(project,status,file,detail))
        return BatchPlan(tuple(items))
