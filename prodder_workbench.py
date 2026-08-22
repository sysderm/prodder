"""Durable local-first workbench state for Prodder.

The terminal monitor remains useful when a provider exposes no structured API.
New integrations should emit the standard events here instead of depending on a
screen scrape.  SQLite keeps tasks, evidence, and the audit trail coherent
across dashboard restarts without adding a service or dependency.
"""
from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path


EVENT_TYPES = frozenset({
    "TaskDefined", "PlanProposed", "ToolStarted", "ToolFinished",
    "ApprovalNeeded", "ApprovalResolved", "FileChanged", "CheckPassed",
    "CheckFailed", "PreviewReady", "AgentBlocked", "AgentCompleted",
    "CommitCreated", "PullRequestOpened", "CIPassed", "CIFailed",
    "DeployReady", "Deployed", "Rollback",
})

TASK_STATUSES = frozenset({
    "planned", "in_progress", "blocked", "needs_review", "done", "canceled",
})


class WorkbenchError(ValueError):
    pass


class WorkspaceStore:
    """Small SQLite repository with an append-only event history."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "prodder-workspace.sqlite3"
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        self._tighten_permissions()
        return conn

    def _tighten_permissions(self):
        for path in (self.path, Path(str(self.path) + "-wal"),
                     Path(str(self.path) + "-shm")):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def _init_db(self):
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    project_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    acceptance_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_status_updated
                    ON tasks(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    source TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_task_created
                    ON events(task_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS checks (
                    id TEXT PRIMARY KEY,
                    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    project_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS check_task_created
                    ON checks(task_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    tty TEXT NOT NULL DEFAULT '',
                    pid INTEGER NOT NULL DEFAULT 0,
                    agent TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    resolved_at REAL,
                    answer TEXT
                );
                CREATE INDEX IF NOT EXISTS decision_open
                    ON decisions(resolved_at, created_at DESC);
            """)
            conn.commit()
        finally:
            conn.close()
        self._tighten_permissions()

    @staticmethod
    def _text(value, label, maximum=4000, required=True):
        value = str(value or "").strip()
        if required and not value:
            raise WorkbenchError(f"{label} is required")
        if len(value) > maximum:
            raise WorkbenchError(f"{label} is too long")
        return value

    @staticmethod
    def _acceptance(value):
        if value is None:
            return []
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list):
            raise WorkbenchError("acceptance criteria must be a list")
        items = [str(x).strip() for x in value if str(x).strip()]
        if len(items) > 30 or any(len(x) > 500 for x in items):
            raise WorkbenchError("acceptance criteria are too large")
        return items

    @staticmethod
    def _task(row):
        task = dict(row)
        task["acceptance"] = json.loads(task.pop("acceptance_json"))
        return task

    def create_task(self, host, project_path, title, description="", acceptance=None):
        host = self._text(host, "host", 120)
        project_path = self._text(project_path, "project path", 4096)
        title = self._text(title, "task", 240)
        description = self._text(description, "description", 8000, required=False)
        acceptance = self._acceptance(acceptance)
        now, task_id = time.time(), uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute("""INSERT INTO tasks
                (id, host, project_path, title, description, acceptance_json, status,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?)""",
                (task_id, host, project_path, title, description,
                 json.dumps(acceptance), now, now))
            conn.commit()
        finally:
            conn.close()
        self.add_event("dashboard", "TaskDefined", task_id,
                       {"title": title, "acceptance": acceptance})
        return self.get_task(task_id)

    def get_task(self, task_id):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise WorkbenchError("task not found")
        return self._task(row)

    def set_status(self, task_id, status):
        if status not in TASK_STATUSES:
            raise WorkbenchError("invalid task status")
        now = time.time()
        conn = self._connect()
        try:
            cur = conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                               (status, now, task_id))
            conn.commit()
        finally:
            conn.close()
        if not cur.rowcount:
            raise WorkbenchError("task not found")
        event = {"planned": "PlanProposed", "in_progress": "ToolStarted",
                 "blocked": "AgentBlocked", "needs_review": "AgentCompleted",
                 "done": "CheckPassed", "canceled": "Rollback"}[status]
        self.add_event("dashboard", event, task_id, {"status": status})
        return self.get_task(task_id)

    def add_event(self, source, event_type, task_id=None, payload=None):
        source = self._text(source, "event source", 120)
        if event_type not in EVENT_TYPES:
            raise WorkbenchError("unknown event type")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise WorkbenchError("event payload must be an object")
        encoded = json.dumps(payload, separators=(",", ":"))
        if len(encoded) > 32768:
            raise WorkbenchError("event payload is too large")
        event_id, now = uuid.uuid4().hex, time.time()
        conn = self._connect()
        try:
            if task_id and not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise WorkbenchError("task not found")
            conn.execute("INSERT INTO events (id, task_id, source, type, payload_json, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (event_id, task_id or None, source, event_type, encoded, now))
            if task_id:
                status_from_event = {
                    "PlanProposed": "planned",
                    "ToolStarted": "in_progress",
                    "ApprovalNeeded": "blocked",
                    "AgentBlocked": "blocked",
                    "CheckFailed": "blocked",
                    "CIFailed": "blocked",
                    "AgentCompleted": "needs_review",
                    "PreviewReady": "needs_review",
                    "DeployReady": "needs_review",
                }.get(event_type)
                if status_from_event:
                    conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                                 (status_from_event, now, task_id))
                else:
                    conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now, task_id))
            conn.commit()
        finally:
            conn.close()
        return event_id

    # -- decisions: pending human choices detected from an agent's screen -----
    # These are the "needs a human, not a blind continue" items. Kept here so the
    # dashboard, Telegram, and the auto-prod hold all read one durable source.

    @staticmethod
    def _decision(row):
        d = dict(row)
        d["options"] = json.loads(d.pop("options_json"))
        return d

    def add_decision(self, host, tty, pid, agent, question, options=None, source=""):
        """Record a pending decision. Deduped per (host, tty): if an OPEN
        decision with the same question already exists there, it is returned
        unchanged. Returns (decision_id, created_new)."""
        host = self._text(host, "host", 120)
        question = self._text(question, "question", 4000)
        tty, agent = str(tty or "")[:120], str(agent or "")[:120]
        source = str(source or "")[:60]
        opts = [str(o)[:120] for o in (options or [])][:12]
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT id FROM decisions WHERE resolved_at IS NULL AND host=? "
                "AND tty=? AND question=? LIMIT 1", (host, tty, question)).fetchone()
            if existing:
                return existing["id"], False
            did, now = uuid.uuid4().hex, time.time()
            conn.execute(
                "INSERT INTO decisions (id, host, tty, pid, agent, question, "
                "options_json, source, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (did, host, tty, int(pid or 0), agent, question,
                 json.dumps(opts, separators=(",", ":")), source, now))
            conn.commit()
            return did, True
        finally:
            conn.close()

    def open_decisions(self):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE resolved_at IS NULL "
                "ORDER BY created_at DESC LIMIT 200").fetchall()
            return [self._decision(r) for r in rows]
        finally:
            conn.close()

    def get_decision(self, decision_id):
        conn = self._connect()
        try:
            r = conn.execute("SELECT * FROM decisions WHERE id=?",
                             (str(decision_id),)).fetchone()
            return self._decision(r) if r else None
        finally:
            conn.close()

    def resolve_decision(self, decision_id, answer=""):
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE decisions SET resolved_at=?, answer=? "
                "WHERE id=? AND resolved_at IS NULL",
                (time.time(), str(answer or "")[:400], str(decision_id)))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def sync_decisions(self, active_keys):
        """Auto-resolve open decisions whose (host, tty) no longer shows a
        decision this scan — the agent moved on or its window is gone — so the
        queue reflects reality. `active_keys` is a set of (host, tty) tuples that
        currently present a decision. Returns the number cleared."""
        active = {(str(h), str(t)) for h, t in active_keys}
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, host, tty FROM decisions WHERE resolved_at IS NULL").fetchall()
            gone = [r["id"] for r in rows if (r["host"], r["tty"]) not in active]
            now = time.time()
            for did in gone:
                conn.execute("UPDATE decisions SET resolved_at=?, answer=? WHERE id=?",
                             (now, "(cleared — no longer on screen)", did))
            conn.commit()
            return len(gone)
        finally:
            conn.close()

    def list_tasks(self, limit=100):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
                                (max(1, min(int(limit), 500)),)).fetchall()
            tasks = [self._task(row) for row in rows]
            for task in tasks:
                check = conn.execute("SELECT name, status, output, created_at FROM checks "
                                     "WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                                     (task["id"],)).fetchone()
                task["latest_check"] = dict(check) if check else None
            return tasks
        finally:
            conn.close()

    def recent_events(self, limit=30):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT ?",
                                (max(1, min(int(limit), 200)),)).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json"))
            out.append(event)
        return out

    def repository_evidence(self, project_path):
        """Read Git state only; this never executes project code."""
        path = Path(project_path).expanduser()
        if not path.is_dir():
            return {"status": "unavailable", "message": "project directory is unavailable"}

        def git(*args):
            try:
                return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                                      text=True, timeout=12)
            except (OSError, subprocess.SubprocessError):
                return None

        inside = git("rev-parse", "--is-inside-work-tree")
        if not inside or inside.returncode != 0 or inside.stdout.strip() != "true":
            return {"status": "not_git", "message": "not a Git repository"}
        branch = git("branch", "--show-current")
        status = git("status", "--short")
        commit = git("log", "-1", "--pretty=%h %s")
        diff = git("diff", "--stat")
        dirty = bool(status and status.stdout.strip())
        return {"status": "dirty" if dirty else "clean", "branch": branch.stdout.strip() if branch else "",
                "commit": commit.stdout.strip() if commit else "",
                "changed": status.stdout.splitlines()[:30] if status else [],
                "diff_stat": diff.stdout.strip() if diff else ""}

    def verify_task(self, task_id, commands=(), timeout=300):
        """Record Git evidence and explicitly configured check commands.

        Commands are never inferred from a repository.  They must come from the
        user's [workbench] configuration and are executed without a shell.
        """
        task = self.get_task(task_id)
        evidence = self.repository_evidence(task["project_path"])
        git_status = "passed" if evidence["status"] == "clean" else "attention"
        git_output = evidence.get("message") or evidence.get("diff_stat", "")
        self._record_check(task_id, task["project_path"], "Git working tree",
                           "git status --short", git_status, git_output)
        self.add_event("verifier", "CheckPassed" if evidence["status"] == "clean"
                       else "CheckFailed", task_id, {"repository": evidence})
        results = [{"name": "Git working tree", "command": "git status --short",
                    "status": git_status, "output": git_output}]
        for command in commands:
            command = self._text(command, "check command", 1000)
            argv = shlex.split(command)
            if not argv:
                continue
            started = time.time()
            try:
                run = subprocess.run(argv, cwd=task["project_path"], capture_output=True,
                                     text=True, timeout=max(1, min(int(timeout), 1800)))
                output = ((run.stdout or "") + ("\n" if run.stderr else "") +
                          (run.stderr or ""))[-12000:]
                status = "passed" if run.returncode == 0 else "failed"
            except (OSError, subprocess.SubprocessError) as exc:
                output, status = str(exc), "failed"
            self._record_check(task_id, task["project_path"], command, command,
                               status, output)
            self.add_event("verifier", "CheckPassed" if status == "passed" else "CheckFailed",
                           task_id, {"command": command, "status": status,
                                     "elapsed_seconds": round(time.time() - started, 2)})
            results.append({"name": command, "command": command, "status": status,
                            "output": output})
        return {"task": self.get_task(task_id), "repository": evidence, "checks": results}

    def _record_check(self, task_id, project_path, name, command, status, output):
        conn = self._connect()
        try:
            conn.execute("INSERT INTO checks (id, task_id, project_path, name, command, status, "
                         "output, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (uuid.uuid4().hex, task_id, project_path, name, command, status,
                          output[-12000:], time.time()))
            conn.commit()
        finally:
            conn.close()

    def dashboard(self):
        tasks = self.list_tasks()
        return {
            "tasks": tasks,
            "queues": {
                "needs_you": [t for t in tasks if t["status"] == "blocked"],
                "working": [t for t in tasks if t["status"] in ("planned", "in_progress")],
                "ready": [t for t in tasks if t["status"] == "needs_review"],
            },
            "recent_events": self.recent_events(12),
            "decisions": self.open_decisions(),
        }
