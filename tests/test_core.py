from __future__ import annotations

import hashlib
import gc
import os
import io
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import httpx
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from pathlib import Path

from modrinth_discovery.database import Database
from modrinth_discovery.api import ModrinthAPI
from modrinth_discovery.feed import filter_unseen
from modrinth_discovery.installer import HashMismatchError, ModInstaller
from modrinth_discovery.instances import discover_instances, manual_instance, InstanceDiscoveryError
from modrinth_discovery.models import Decision, Dependency, InstallPlan, Instance, ModFile, Project
from modrinth_discovery.config import AppConfig
from modrinth_discovery.scanner import scan_installed_mods
from modrinth_discovery.workers import Worker, WorkerController


def project(project_id: str) -> Project:
    return Project(project_id, project_id, project_id, "", "", 0, None,
                   f"https://modrinth.com/mod/{project_id}", "")


def fabric_jar(mod_id: str, version: str = "1.0") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": mod_id, "name": mod_id, "version": version}))
    return output.getvalue()


class FakeAPI:
    def __init__(self, data: bytes) -> None: self.data = data
    def iter_download(self, _url: str):
        yield self.data[:3]; yield self.data[3:]


class DatabaseTests(unittest.TestCase):
    def test_migrates_mvp_and_preserves_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE reviewed_projects(project_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,title TEXT NOT NULL,decision TEXT NOT NULL,
                reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            connection.execute("INSERT INTO reviewed_projects VALUES('p','slug','Title','skip','2024-01-01 00:00:00')")
            connection.commit(); connection.close()
            db = Database(path)
            row = db.connection.execute("SELECT * FROM reviewed_projects").fetchone()
            self.assertEqual(row["decision"], "skipped")
            self.assertEqual(row["first_reviewed_at"], "2024-01-01 00:00:00")
            db.close()

    def test_maybe_and_undo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "db.sqlite")
            item = project("one"); db.save_decision(item, Decision.MAYBE)
            self.assertEqual([p.project_id for p in db.maybe_projects()], ["one"])
            previous=db.decision_for("one"); db.save_decision(item,Decision.SKIPPED)
            db.restore_decision(item,previous); self.assertEqual(db.decision_for("one"),Decision.MAYBE)
            db.restore_decision(item,None); self.assertFalse(db.has_reviewed("one")); db.close()


class FeedTests(unittest.TestCase):
    def test_filters_reviewed_and_cross_page_duplicates(self) -> None:
        seen: set[str] = set()
        self.assertEqual([p.project_id for p in filter_unseen([project("a"),project("b")], {"a"}, seen)], ["b"])
        self.assertEqual([p.project_id for p in filter_unseen([project("b"),project("c")], set(), seen)], ["c"])


class APITests(unittest.TestCase):
    def test_search_uses_documented_facets_and_pagination(self) -> None:
        captured = {}
        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, json={"hits":[{"project_id":"p","slug":"s","title":"T"}],
                "offset":50,"limit":25,"total_hits":125})
        client=httpx.Client(base_url="https://api.modrinth.com/v2",transport=httpx.MockTransport(handler))
        api=ModrinthAPI(client=client); page=api.search_projects(minecraft_version="1.21.1",loader="NeoForge",limit=25,offset=50,index="newest")
        self.assertEqual((page.offset,page.limit,page.total_hits),(50,25,125))
        facets=json.loads(captured["facets"])
        self.assertIn(["project_type:mod"],facets); self.assertIn(["categories:neoforge"],facets)
        self.assertEqual(captured["offset"],"50"); client.close()

    def test_version_dependencies_are_parsed_without_recursive_requests(self) -> None:
        calls=[]
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200,json=[{"id":"v","version_number":"1","version_type":"release",
                "dependencies":[{"dependency_type":"required","project_id":"dep"}],
                "files":[{"filename":"x.jar","url":"https://cdn/x","primary":True,"hashes":{}}]}])
        client=httpx.Client(base_url="https://api.modrinth.com/v2",transport=httpx.MockTransport(handler))
        api=ModrinthAPI(timeout=7,client=client); result=api.get_latest_file("p",minecraft_version="26.2",loader="fabric")
        self.assertEqual(len(calls),1); self.assertEqual(result.dependencies[0].project_id,"dep")
        timeouts=calls[0].extensions["timeout"]
        self.assertEqual(timeouts["read"],7); self.assertEqual(timeouts["write"],7); client.close()


class WorkerRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def _run_worker(self, function):
        pool=QThreadPool(); controller=WorkerController(pool); loop=QEventLoop()
        state={"result":None,"error":None,"finished":False,"timed_out":False}
        worker=Worker(function,name="fabric-api-prepare-regression")
        worker.signals.result.connect(lambda value: state.update(result=value))
        worker.signals.error.connect(lambda error: state.update(error=error))
        controller.start(worker,lambda:(state.update(finished=True),loop.quit()))
        del worker; gc.collect()
        timer=QTimer(); timer.setSingleShot(True)
        timer.timeout.connect(lambda:(state.update(timed_out=True),loop.quit())); timer.start(2000)
        loop.exec(); timer.stop(); pool.waitForDone(2000)
        return state,controller.active_count

    def test_prepare_worker_survives_local_scope_and_delivers_finished(self) -> None:
        state,active=self._run_worker(lambda: InstallPlan(ModFile("P7dR8mSH","v","1","fabric-api.jar","url",None),()))
        self.assertFalse(state["timed_out"]); self.assertTrue(state["finished"])
        self.assertIsInstance(state["result"],InstallPlan); self.assertEqual(active,0)

    def test_exception_is_delivered_and_finished_still_runs(self) -> None:
        def fail(): raise RuntimeError("version lookup failed")
        state,active=self._run_worker(fail)
        self.assertFalse(state["timed_out"]); self.assertTrue(state["finished"])
        self.assertIsInstance(state["error"],RuntimeError); self.assertEqual(active,0)

    def test_fabric_api_install_prepare_always_releases_busy(self) -> None:
        import time
        import modrinth_discovery.app as app_module

        class FabricAPIFake:
            def get_latest_file(self, *_args, **_kwargs):
                dependencies=tuple(Dependency("required",project_id=f"dep-{n}") for n in range(100))
                return ModFile("P7dR8mSH","fabric-api-version","0.161.0+26.2",
                               "fabric-api.jar","url",None,dependencies=dependencies)
            def close(self): pass

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app_module,"CONFIG",AppConfig(Path(tmp)/"gui.db")), \
             patch.object(app_module,"discover_instances",return_value=[]), \
             patch.object(QMessageBox,"question",return_value=QMessageBox.StandardButton.No):
            window=app_module.MainWindow(); fake=FabricAPIFake(); window.api=fake  # type: ignore[assignment]
            window.installer=ModInstaller(fake)  # type: ignore[arg-type]
            instance=Instance("Fabric 26.2",Path(tmp),Path(tmp)/"mods","26.2","fabric")
            window.selected_instance=lambda: instance  # type: ignore[method-assign]
            window.projects=[project("P7dR8mSH")]; window.version_edit.setText("26.2")
            window.install()
            deadline=time.monotonic()+2
            while window.busy and time.monotonic()<deadline:
                self.application.processEvents(); time.sleep(0.005)
            self.application.processEvents()
            self.assertFalse(window.busy); self.assertTrue(window.install_button.isEnabled())
            self.assertEqual(window.workers.active_count,0); window.close()


class InstanceTests(unittest.TestCase):
    def test_discovers_current_schema_with_custom_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings=Path(tmp)/"settings"; custom=Path(tmp)/"custom"; settings.mkdir()
            db=sqlite3.connect(settings/"app.db")
            db.executescript("""CREATE TABLE settings(id INTEGER,custom_dir TEXT);
                CREATE TABLE instances(id TEXT,path TEXT,name TEXT,applied_content_set_id TEXT,install_stage TEXT);
                CREATE TABLE instance_content_sets(id TEXT,game_version TEXT,loader TEXT,loader_version TEXT);""")
            db.execute("INSERT INTO settings VALUES(0,?)",(str(custom),))
            db.execute("INSERT INTO instances VALUES('i','profile-a','Pack','set','installed')")
            db.execute("INSERT INTO instance_content_sets VALUES('set','1.21.1','neoforge','21.1.1')")
            db.commit(); db.close()
            found=discover_instances(settings)
            self.assertEqual(found[0].path, custom/"profiles"/"profile-a")
            self.assertEqual((found[0].minecraft_version,found[0].loader),("1.21.1","neoforge"))

    def test_manual_missing_path(self) -> None:
        with self.assertRaises(InstanceDiscoveryError): manual_instance(Path("Z:/definitely-missing/path"))


class InstallTests(unittest.TestCase):
    def test_scans_fabric_and_installs_atomically(self) -> None:
        data=fabric_jar("example"); digest=hashlib.sha512(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp); api=FakeAPI(data); installer=ModInstaller(api)  # type: ignore[arg-type]
            mod_file=ModFile("p","v","1.0","example.jar","url",digest,size=len(data))
            path, result=installer.install(InstallPlan(mod_file,()),directory)
            self.assertEqual(path.read_bytes(),data); self.assertEqual(result.target_mod_ids,("example",))
            self.assertFalse(list(directory.glob("*.download")))
            self.assertEqual(scan_installed_mods(directory)[0].mod_ids,("example",))

    def test_bad_hash_leaves_no_file(self) -> None:
        data=fabric_jar("bad")
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp); installer=ModInstaller(FakeAPI(data))  # type: ignore[arg-type]
            mod_file=ModFile("p","v","1","bad.jar","url","0"*128)
            with self.assertRaises(HashMismatchError): installer.install(InstallPlan(mod_file,()),directory)
            self.assertFalse((directory/"bad.jar").exists()); self.assertFalse(list(directory.glob("*.download")))


if __name__ == "__main__": unittest.main()
