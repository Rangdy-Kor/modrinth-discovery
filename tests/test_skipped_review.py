from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modrinth_discovery.database import Database
from modrinth_discovery.feed import filter_unseen
from modrinth_discovery.models import Decision, Project


def project(value: str="skip-me") -> Project:
    return Project(value,value,"Skipped project","","",0,None,f"https://modrinth.com/mod/{value}","")


class SkippedReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/"discovery.db"
        self.db=Database(self.path); self.item=project(); self.db.save_decision(self.item,Decision.SKIPPED)
    def tearDown(self): self.db.close(); self.tmp.cleanup()

    def test_skipped_list_only_returns_skipped(self):
        self.db.save_decision(project("maybe"),Decision.MAYBE)
        self.assertEqual([p.project_id for p in self.db.skipped_projects()],["skip-me"])

    def test_keep_skip_preserves_first_and_updates_last_timestamp(self):
        self.db.connection.execute("UPDATE reviewed_projects SET first_reviewed_at='2000-01-01',last_reviewed_at='2001-01-01' WHERE project_id=?",(self.item.project_id,)); self.db.connection.commit()
        self.db.save_decision(self.item,Decision.SKIPPED); snapshot=self.db.decision_snapshot(self.item.project_id)
        self.assertEqual(snapshot.first_reviewed_at,"2000-01-01"); self.assertNotEqual(snapshot.last_reviewed_at,"2001-01-01")

    def test_skipped_to_maybe(self):
        self.db.save_decision(self.item,Decision.MAYBE); self.assertEqual(self.db.decision_for(self.item.project_id),Decision.MAYBE)

    def test_skipped_to_reviewed(self):
        self.db.save_decision(self.item,Decision.REVIEWED); self.assertEqual(self.db.decision_for(self.item.project_id),Decision.REVIEWED)

    def test_changed_project_remains_excluded_from_discovery(self):
        self.db.save_decision(self.item,Decision.MAYBE)
        self.assertEqual(filter_unseen([self.item],self.db.reviewed_ids(),set()),[])

    def test_undo_restores_exact_skipped_snapshot(self):
        before=self.db.decision_snapshot(self.item.project_id); self.db.save_decision(self.item,Decision.REVIEWED)
        self.db.restore_snapshot(self.item,before); after=self.db.decision_snapshot(self.item.project_id)
        self.assertEqual(after,before)

    def test_decision_survives_restart(self):
        self.db.save_decision(self.item,Decision.REVIEWED); self.db.close(); self.db=Database(self.path)
        self.assertEqual(self.db.decision_for(self.item.project_id),Decision.REVIEWED)
