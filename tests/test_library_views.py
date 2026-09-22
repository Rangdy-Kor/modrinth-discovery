from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from modrinth_discovery.config import AppConfig
from modrinth_discovery.database import Database
from modrinth_discovery.library import filter_entries
from modrinth_discovery.models import Decision, Project


def project(project_id: str, title: str | None = None) -> Project:
    return Project(project_id,project_id,title or project_id,"","",0,None,
                   f"https://modrinth.com/mod/{project_id}","")


class LibraryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp=tempfile.TemporaryDirectory(); self.db=Database(Path(self.tmp.name)/"library.db")

    def tearDown(self) -> None:
        self.db.close(); self.tmp.cleanup()

    def _seed(self, item: Project, decision: Decision, first: str, last: str) -> None:
        self.db.save_decision(item,decision)
        self.db.connection.execute("UPDATE reviewed_projects SET first_reviewed_at=?,last_reviewed_at=? WHERE project_id=?",
                                   (first,last,item.project_id)); self.db.connection.commit()

    def test_maybe_list_sorting_and_skipped_list(self) -> None:
        self._seed(project("z","Zulu"),Decision.MAYBE,"2024-01-01","2024-01-02")
        self._seed(project("a","Alpha"),Decision.MAYBE,"2024-02-01","2024-02-02")
        self._seed(project("skip","Skipped"),Decision.SKIPPED,"2024-03-01","2024-03-02")
        self.assertEqual([e.project.title for e in self.db.library_entries(Decision.MAYBE,"title")],["Alpha","Zulu"])
        self.assertEqual([e.project.project_id for e in self.db.library_entries(Decision.MAYBE,"first_reviewed_at")],["a","z"])
        self.assertEqual([e.project.project_id for e in self.db.library_entries(Decision.SKIPPED)],["skip"])

    def test_all_symmetric_decision_transitions(self) -> None:
        cases=((Decision.MAYBE,Decision.SKIPPED),(Decision.MAYBE,Decision.REVIEWED),
               (Decision.SKIPPED,Decision.MAYBE),(Decision.SKIPPED,Decision.REVIEWED),
               (Decision.REVIEWED,Decision.MAYBE),(Decision.REVIEWED,Decision.SKIPPED))
        for index,(source,target) in enumerate(cases):
            item=project(f"p{index}"); self.db.save_decision(item,source)
            self.db.change_decisions([item.project_id],target)
            self.assertEqual(self.db.decision_for(item.project_id),target)

    def test_batch_change_and_undo_restore_exact_snapshots(self) -> None:
        first=project("one"); second=project("two")
        self._seed(first,Decision.MAYBE,"2000-01-01","2001-01-01")
        self._seed(second,Decision.MAYBE,"2010-01-01","2011-01-01")
        before={p.project_id:self.db.decision_snapshot(p.project_id) for p in (first,second)}
        changes=self.db.change_decisions(["one","two"],Decision.REVIEWED)
        self.assertEqual(self.db.count_by_decision(Decision.REVIEWED),2)
        self.db.restore_snapshots(changes)
        self.assertEqual({p.project_id:self.db.decision_snapshot(p.project_id) for p in (first,second)},before)

    def test_filter_matches_title_or_slug_case_insensitively(self) -> None:
        self.db.save_decision(project("fabric-api","Fabric API"),Decision.MAYBE)
        self.db.save_decision(project("sodium","Sodium"),Decision.MAYBE)
        entries=self.db.library_entries(Decision.MAYBE,"title")
        self.assertEqual([e.project.project_id for e in filter_entries(entries,"FABRIC")],["fabric-api"])
        self.assertEqual([e.project.project_id for e in filter_entries(entries,"dium")],["sodium"])


class LibraryViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application=QApplication.instance() or QApplication([])

    def test_maybe_and_skipped_views_are_local_only(self) -> None:
        import modrinth_discovery.app as app_module

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app_module,"CONFIG",AppConfig(Path(tmp)/"gui.db")), \
             patch.object(app_module,"discover_instances",return_value=[]):
            window=app_module.MainWindow()
            window.database.save_decision(project("maybe","Maybe Project"),Decision.MAYBE)
            window.database.save_decision(project("skip","Skipped Project"),Decision.SKIPPED)
            # Any accidental remote enrichment on these screens must fail this test.
            window.api.search_projects=lambda **_kwargs: self.fail("unexpected network request")  # type: ignore[method-assign]
            window.api.get_latest_file=lambda *_args,**_kwargs: self.fail("unexpected network request")  # type: ignore[method-assign]
            window.show_library(Decision.MAYBE)
            self.assertEqual(window.collection_list.count(),1)
            self.assertIn("Maybe Project",window.collection_list.item(0).text())
            window.collection_list.item(0).setSelected(True)
            window.change_selected_decision(Decision.SKIPPED)
            self.assertEqual(window.database.decision_for("maybe"),Decision.SKIPPED)
            window.undo()
            self.assertEqual(window.database.decision_for("maybe"),Decision.MAYBE)
            window.show_library(Decision.SKIPPED)
            self.assertEqual(window.collection_list.count(),1)
            self.assertIn("Skipped Project",window.collection_list.item(0).text())
            self.assertEqual(window.workers.active_count,0)
            window.close()


if __name__ == "__main__": unittest.main()
