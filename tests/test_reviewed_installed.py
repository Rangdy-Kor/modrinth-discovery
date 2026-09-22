from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
import zipfile
import json
from pathlib import Path

from modrinth_discovery.api import CompatibleVersionNotFound, ModrinthAPI
from modrinth_discovery.batch import BatchInstaller
from modrinth_discovery.database import Database
from modrinth_discovery.inventory import build_inventory, validate_inventory
from modrinth_discovery.models import (BatchStatus, Decision, Dependency, InstalledMod,
    InstallPlan, Instance, ModFile, Project, SearchIndex)
from modrinth_discovery.resolver import DependencyResolver
from modrinth_discovery.trash import TrashManager


def p(value: str) -> Project:
    return Project(value,value,value,"","",0,None,f"https://modrinth.com/mod/{value}","")


def f(value: str, deps=()) -> ModFile:
    return ModFile(value,"v-"+value,"1.0",value+".jar","url",None,dependencies=tuple(deps))


class FakeAPI:
    def __init__(self, files=None): self.files=files or {}; self.calls=[]; self.hash_versions={}; self.projects={}; self.versions={}
    def get_latest_file(self, project_id, **_kwargs):
        self.calls.append(project_id); value=self.files.get(project_id)
        if isinstance(value,Exception): raise value
        if value is None: raise CompatibleVersionNotFound("none")
        return value
    def get_versions_from_hashes(self, hashes, **_kwargs): return {h:self.hash_versions[h] for h in hashes if h in self.hash_versions}
    def get_projects(self, ids): return {i:self.projects[i] for i in ids if i in self.projects}
    def get_versions_by_ids(self, ids): return {i:self.versions[i] for i in ids if i in self.versions}


class FakeInstaller:
    def __init__(self, failures=()): self.failures=set(failures); self.calls=[]
    def prepare(self, file, _directory): return InstallPlan(file,())
    def install(self, plan, directory):
        self.calls.append(plan.mod_file.project_id)
        if plan.mod_file.project_id in self.failures: raise RuntimeError("download failed")
        return directory/plan.mod_file.filename,plan


class StateModelTests(unittest.TestCase):
    def test_default_sort_is_downloads(self):
        self.assertEqual(inspect.signature(ModrinthAPI.search_projects).parameters["index"].default,SearchIndex.DOWNLOADS)

    def test_reviewed_is_global_and_installed_is_per_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); db=Database(root/"db"); db.save_decision(p("A"),Decision.REVIEWED)
            one=root/"one"; two=root/"two"; one.mkdir(); two.mkdir()
            with zipfile.ZipFile(one/"a.jar","w") as z:z.writestr("fabric.mod.json",json.dumps({"id":"a","version":"1"}))
            api=FakeAPI(); import hashlib
            digest=hashlib.sha512((one/"a.jar").read_bytes()).hexdigest(); api.hash_versions[digest]={"id":"vA","project_id":"A"}; api.projects["A"]={"id":"A","title":"A"}
            self.assertEqual([m.project_id for m in build_inventory(api,one)],["A"])
            self.assertEqual(build_inventory(api,two),[]); self.assertEqual(db.selected_ids(),{"A"}); db.close()

    def test_installed_decision_migrates_to_reviewed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"db"; con=sqlite3.connect(path)
            con.execute("CREATE TABLE reviewed_projects(project_id TEXT PRIMARY KEY,slug TEXT,title TEXT,decision TEXT,reviewed_at TEXT)")
            con.execute("INSERT INTO reviewed_projects VALUES('A','a','A','installed','2020-01-01')"); con.commit(); con.close()
            db=Database(path); self.assertEqual(db.decision_for("A"),Decision.REVIEWED); db.close()


class BatchTests(unittest.TestCase):
    def test_plan_excludes_installed_and_classifies_incompatible_and_failed(self):
        api=FakeAPI({"new":f("new"),"bad":CompatibleVersionNotFound("none"),"fail":RuntimeError("network")})
        batch=BatchInstaller(api,FakeInstaller())  # type: ignore[arg-type]
        inv=[InstalledMod(Path("old.jar"),(),project_id="old")]
        plan=batch.plan([p("old"),p("new"),p("bad"),p("fail")],inv,minecraft_version="1",loader="fabric")
        self.assertEqual([i.status for i in plan.items],[BatchStatus.ALREADY_INSTALLED,BatchStatus.INSTALLABLE,BatchStatus.INCOMPATIBLE,BatchStatus.LOOKUP_FAILED])

    def test_partial_failure_continues(self):
        api=FakeAPI({"a":f("a"),"b":f("b")}); installer=FakeInstaller({"a"}); batch=BatchInstaller(api,installer)  # type: ignore[arg-type]
        plan=batch.plan([p("a"),p("b")],[],minecraft_version="1",loader="fabric")
        result=batch.apply(plan,Path("mods")); self.assertEqual([r.success for r in result],[False,True]); self.assertEqual(installer.calls,["a","b"])


class DependencyTests(unittest.TestCase):
    def test_off_reports_missing_but_does_not_fetch(self):
        api=FakeAPI({"dep":f("dep")}); root=f("root",[Dependency("required",project_id="dep")])
        plan=DependencyResolver(api).resolve([root],minecraft_version="1",loader="fabric",installed_project_ids=set(),automatic=False)
        self.assertEqual(len(plan.files),1); self.assertEqual(plan.issues[0].dependency_type,"required"); self.assertEqual(api.calls,[])

    def test_on_recurses_deduplicates_and_detects_cycle(self):
        a=f("a",[Dependency("required",project_id="b"),Dependency("required",project_id="b")])
        b=f("b",[Dependency("required",project_id="a")]); api=FakeAPI({"b":b})
        plan=DependencyResolver(api).resolve([a],minecraft_version="1",loader="fabric",installed_project_ids=set(),automatic=True)
        self.assertEqual({x.project_id for x in plan.files},{"a","b"}); self.assertEqual(api.calls,["b"])
        self.assertTrue(any(i.dependency_type=="cycle" for i in plan.issues))

    def test_optional_embedded_not_errors_and_uncertain_not_installed(self):
        root=f("r",[Dependency("optional",project_id="o"),Dependency("embedded",project_id="e"),Dependency("required",file_name="external.jar")])
        plan=DependencyResolver(FakeAPI()).resolve([root],minecraft_version="1",loader="fabric",installed_project_ids=set(),automatic=True)
        self.assertEqual(len(plan.files),1); self.assertEqual([i.dependency_type for i in plan.issues],["unknown"])

    def test_validation_missing_incompatible_optional_and_unknown(self):
        api=FakeAPI(); api.versions["v"]={"dependencies":[
            {"dependency_type":"required","project_id":"missing"},
            {"dependency_type":"incompatible","project_id":"conflict"},
            {"dependency_type":"optional","project_id":"optional"}]}
        inventory=[InstalledMod(Path("a"),(),project_id="a",version_id="v"),InstalledMod(Path("c"),(),project_id="conflict",version_id=None)]
        issues=validate_inventory(api,inventory); kinds=[i.dependency_type for i in issues]
        self.assertIn("required",kinds); self.assertIn("incompatible",kinds); self.assertNotIn("optional",kinds); self.assertIn("unknown",kinds)


class TrashTests(unittest.TestCase):
    def test_remove_moves_to_trash_and_reviewed_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); mods=root/"instance"/"mods"; mods.mkdir(parents=True); jar=mods/"a.jar"; jar.write_bytes(b"x")
            db=Database(root/"db"); db.save_decision(p("A"),Decision.REVIEWED)
            instance=Instance("one",mods.parent,mods,"1","fabric",instance_id="id")
            moved=TrashManager(root/"trash").remove(instance,[InstalledMod(jar,("a",))])
            self.assertFalse(jar.exists()); self.assertTrue(moved[0].exists()); self.assertEqual(db.selected_ids(),{"A"}); db.close()
