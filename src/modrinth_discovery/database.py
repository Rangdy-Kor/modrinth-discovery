from __future__ import annotations

import sqlite3
from pathlib import Path
from .models import Decision, DecisionSnapshot, LibraryEntry, Project


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.connection.execute("""CREATE TABLE IF NOT EXISTS reviewed_projects (
            project_id TEXT PRIMARY KEY, slug TEXT NOT NULL, title TEXT NOT NULL,
            decision TEXT NOT NULL, first_reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(reviewed_projects)")}
        if "first_reviewed_at" not in columns:
            self.connection.execute("ALTER TABLE reviewed_projects ADD COLUMN first_reviewed_at TEXT")
        if "last_reviewed_at" not in columns:
            self.connection.execute("ALTER TABLE reviewed_projects ADD COLUMN last_reviewed_at TEXT")
        if "reviewed_at" in columns:
            self.connection.execute("""UPDATE reviewed_projects SET
                first_reviewed_at=COALESCE(first_reviewed_at, reviewed_at),
                last_reviewed_at=COALESCE(last_reviewed_at, reviewed_at)""")
        self.connection.execute("""UPDATE reviewed_projects SET
            first_reviewed_at=COALESCE(first_reviewed_at, CURRENT_TIMESTAMP),
            last_reviewed_at=COALESCE(last_reviewed_at, CURRENT_TIMESTAMP),
            decision=CASE WHEN decision='skip' THEN 'skipped'
                          WHEN decision='installed' THEN 'reviewed' ELSE decision END""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS installed_file_cache (
            instance_key TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, sha512 TEXT NOT NULL, project_id TEXT,
            version_id TEXT, project_title TEXT, mapped_at REAL,
            PRIMARY KEY(instance_key,path))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS compatibility_cache (
            project_id TEXT NOT NULL, minecraft_version TEXT NOT NULL, loader TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT, detail TEXT NOT NULL DEFAULT '', checked_at REAL NOT NULL,
            PRIMARY KEY(project_id,minecraft_version,loader))""")
        self.connection.execute("CREATE INDEX IF NOT EXISTS reviewed_decision ON reviewed_projects(decision)")
        self.connection.commit()

    def has_reviewed(self, project_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM reviewed_projects WHERE project_id=?",
                                       (project_id,)).fetchone() is not None

    def reviewed_ids(self) -> set[str]:
        return {row[0] for row in self.connection.execute("SELECT project_id FROM reviewed_projects")}

    def selected_ids(self) -> set[str]:
        return {row[0] for row in self.connection.execute(
            "SELECT project_id FROM reviewed_projects WHERE decision=?", (Decision.REVIEWED.value,))}

    def save_decision(self, project: Project, decision: Decision) -> None:
        self.connection.execute("""INSERT INTO reviewed_projects
            (project_id,slug,title,decision,first_reviewed_at,last_reviewed_at)
            VALUES (?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            ON CONFLICT(project_id) DO UPDATE SET slug=excluded.slug,title=excluded.title,
            decision=excluded.decision,last_reviewed_at=CURRENT_TIMESTAMP""",
            (project.project_id, project.slug, project.title, decision.value))
        self.connection.commit()

    def decision_for(self, project_id: str) -> Decision | None:
        row = self.connection.execute("SELECT decision FROM reviewed_projects WHERE project_id=?",
                                      (project_id,)).fetchone()
        return Decision(row[0]) if row else None

    def decision_snapshot(self, project_id: str) -> DecisionSnapshot | None:
        row=self.connection.execute("""SELECT decision,first_reviewed_at,last_reviewed_at
            FROM reviewed_projects WHERE project_id=?""",(project_id,)).fetchone()
        return None if row is None else DecisionSnapshot(Decision(row["decision"]),row["first_reviewed_at"],row["last_reviewed_at"])

    def delete_decision(self, project_id: str) -> None:
        self.connection.execute("DELETE FROM reviewed_projects WHERE project_id=?", (project_id,))
        self.connection.commit()

    def restore_decision(self, project: Project, decision: Decision | None) -> None:
        if decision is None:
            self.delete_decision(project.project_id)
        else:
            self.save_decision(project, decision)

    def restore_snapshot(self, project: Project, snapshot: DecisionSnapshot | None) -> None:
        if snapshot is None:
            self.delete_decision(project.project_id); return
        self.connection.execute("""INSERT INTO reviewed_projects
            (project_id,slug,title,decision,first_reviewed_at,last_reviewed_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET slug=excluded.slug,title=excluded.title,
            decision=excluded.decision,first_reviewed_at=excluded.first_reviewed_at,
            last_reviewed_at=excluded.last_reviewed_at""",
            (project.project_id,project.slug,project.title,snapshot.decision.value,
             snapshot.first_reviewed_at,snapshot.last_reviewed_at)); self.connection.commit()

    def restore_snapshots(self, changes: list[tuple[Project, DecisionSnapshot | None]]) -> None:
        with self.connection:
            for project,snapshot in changes:
                if snapshot is None:
                    self.connection.execute("DELETE FROM reviewed_projects WHERE project_id=?",(project.project_id,))
                else:
                    self.connection.execute("""INSERT INTO reviewed_projects
                        (project_id,slug,title,decision,first_reviewed_at,last_reviewed_at) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(project_id) DO UPDATE SET slug=excluded.slug,title=excluded.title,
                        decision=excluded.decision,first_reviewed_at=excluded.first_reviewed_at,
                        last_reviewed_at=excluded.last_reviewed_at""",
                        (project.project_id,project.slug,project.title,snapshot.decision.value,
                         snapshot.first_reviewed_at,snapshot.last_reviewed_at))

    def library_entries(self, decision: Decision, sort_by: str="last_reviewed_at") -> list[LibraryEntry]:
        order={"title":"title COLLATE NOCASE","first_reviewed_at":"first_reviewed_at",
               "last_reviewed_at":"last_reviewed_at"}.get(sort_by,"last_reviewed_at")
        direction="ASC" if sort_by=="title" else "DESC"
        rows=self.connection.execute(f"""SELECT project_id,slug,title,decision,first_reviewed_at,last_reviewed_at
            FROM reviewed_projects WHERE decision=? ORDER BY {order} {direction}""",(decision.value,)).fetchall()
        return [LibraryEntry(Project(row["project_id"],row["slug"],row["title"],"","",0,None,
            f"https://modrinth.com/mod/{row['slug']}",""),Decision(row["decision"]),
            row["first_reviewed_at"],row["last_reviewed_at"]) for row in rows]

    def change_decisions(self, project_ids: list[str], decision: Decision) -> list[tuple[Project,DecisionSnapshot]]:
        if not project_ids:return []
        placeholders=",".join("?" for _ in project_ids)
        with self.connection:
            rows=self.connection.execute(f"""SELECT project_id,slug,title,decision,first_reviewed_at,last_reviewed_at
                FROM reviewed_projects WHERE project_id IN ({placeholders})""",project_ids).fetchall()
            changes=[(Project(row["project_id"],row["slug"],row["title"],"","",0,None,
                f"https://modrinth.com/mod/{row['slug']}",""),DecisionSnapshot(Decision(row["decision"]),
                row["first_reviewed_at"],row["last_reviewed_at"])) for row in rows]
            self.connection.executemany("UPDATE reviewed_projects SET decision=?,last_reviewed_at=CURRENT_TIMESTAMP WHERE project_id=?",
                                        [(decision.value,pid) for pid in project_ids])
        return changes

    def projects_by_decision(self, decision: Decision) -> list[Project]:
        return [entry.project for entry in reversed(self.library_entries(decision,"last_reviewed_at"))]

    def maybe_projects(self) -> list[Project]:
        return self.projects_by_decision(Decision.MAYBE)

    def skipped_projects(self) -> list[Project]:
        return self.projects_by_decision(Decision.SKIPPED)

    def selected_projects(self) -> list[Project]:
        rows = self.connection.execute("""SELECT project_id,slug,title FROM reviewed_projects
            WHERE decision=? ORDER BY last_reviewed_at DESC""", (Decision.REVIEWED.value,)).fetchall()
        return [Project(row["project_id"],row["slug"],row["title"],"","",0,None,
                        f"https://modrinth.com/mod/{row['slug']}","") for row in rows]

    def remove_reviewed(self, project_id: str) -> None:
        self.delete_decision(project_id)

    def get_bool_setting(self, key: str, default: bool = False) -> bool:
        row=self.connection.execute("SELECT value FROM app_settings WHERE key=?",(key,)).fetchone()
        return default if row is None else row[0].lower() in {"1","true","yes"}

    def set_bool_setting(self, key: str, value: bool) -> None:
        self.connection.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (key,"true" if value else "false")); self.connection.commit()

    def count_by_decision(self, decision: Decision) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM reviewed_projects WHERE decision=?",
                                          (decision.value,)).fetchone()[0])

    def close(self) -> None:
        self.connection.close()
