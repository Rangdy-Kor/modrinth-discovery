from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from modrinth_discovery.cache import PersistentCache
from modrinth_discovery.database import Database
from modrinth_discovery.models import Instance, ModFile, Project
from modrinth_discovery.reviewed_service import ReviewedService


def jar(path: Path, mod_id: str) -> None:
    with zipfile.ZipFile(path,"w") as archive:
        archive.writestr("fabric.mod.json",json.dumps({"id":mod_id,"version":"1"}))


class CacheAPI:
    def __init__(self): self.hash_calls=0; self.project_calls=0; self.version_calls=0
    def get_versions_from_hashes(self, hashes, **_kwargs):
        self.hash_calls+=1; return {h:{"id":"version-a","project_id":"project-a"} for h in hashes}
    def get_projects(self, ids):
        self.project_calls+=1; return {i:{"id":i,"title":"Project A"} for i in ids}
    def get_latest_file(self, project_id, **_kwargs):
        self.version_calls+=1; return ModFile(project_id,"latest-"+project_id,"2",project_id+".jar","url",None)


class PersistentCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name); self.mods=root/"mods"; self.mods.mkdir()
        self.db=Database(root/"cache.db"); self.cache=PersistentCache(self.db.path)
        self.instance=Instance("test",root,self.mods,"1.21.1","fabric",instance_id="instance")
    def tearDown(self): self.db.close(); self.tmp.cleanup()

    def test_inventory_fingerprint_reuses_hash_and_remote_mapping(self):
        jar(self.mods/"a.jar","a"); api=CacheAPI()
        cold,_=self.cache.inventory(api,self.instance); warm,timing=self.cache.inventory(api,self.instance)
        self.assertEqual(cold[0].project_id,"project-a"); self.assertEqual(warm[0].version_id,"version-a")
        self.assertEqual((api.hash_calls,api.project_calls),(1,1)); self.assertLess(timing.hash_lookup,0.001)

    def test_refresh_forces_mapping_but_not_rehash(self):
        jar(self.mods/"a.jar","a"); api=CacheAPI(); self.cache.inventory(api,self.instance)
        self.cache.inventory(api,self.instance,force=True)
        self.assertEqual((api.hash_calls,api.project_calls),(2,2))

    def test_compatibility_cache_and_force_refresh(self):
        api=CacheAPI(); first=self.cache.compatibility(api,"p","1","fabric")
        second=self.cache.compatibility(api,"p","1","fabric"); forced=self.cache.compatibility(api,"p","1","fabric",force=True)
        self.assertFalse(first[3]); self.assertTrue(second[3]); self.assertFalse(forced[3]); self.assertEqual(api.version_calls,2)

    def test_progressive_emits_inventory_before_compatibility_and_warm_makes_no_requests(self):
        jar(self.mods/"a.jar","a"); api=CacheAPI(); service=ReviewedService(api,self.cache,max_concurrency=2)
        projects=[Project("project-a","a","A","","",0,None,"","")]; events=[]
        cold=service.load(projects,self.instance,emit_item=events.append)
        before=(api.hash_calls,api.project_calls,api.version_calls); events.clear()
        warm=service.load(projects,self.instance,emit_item=events.append)
        self.assertEqual(events[0]["phase"],"inventory"); self.assertEqual(events[-1]["phase"],"compatibility")
        self.assertEqual((api.hash_calls,api.project_calls,api.version_calls),before)
        self.assertEqual(cold["compat_requests"],1); self.assertEqual(warm["compat_requests"],0)
