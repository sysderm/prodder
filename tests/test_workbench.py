"""The local-first task, evidence, and provider-event contract."""
import os
import stat
import sys

import pytest

from prodder_workbench import EVENT_TYPES, WorkspaceStore, WorkbenchError


def store(tmp_path):
    return WorkspaceStore(tmp_path)


def test_task_moves_through_dashboard_queues(tmp_path):
    db = store(tmp_path)
    task = db.create_task("local", str(tmp_path), "Ship the checkout", acceptance=["Tests pass"])
    queues = db.dashboard()["queues"]
    assert [x["id"] for x in queues["working"]] == [task["id"]]

    db.set_status(task["id"], "blocked")
    assert db.dashboard()["queues"]["needs_you"][0]["id"] == task["id"]

    db.set_status(task["id"], "needs_review")
    assert db.dashboard()["queues"]["ready"][0]["acceptance"] == ["Tests pass"]


def test_events_are_typed_and_linked_to_tasks(tmp_path):
    db = store(tmp_path)
    task = db.create_task("local", str(tmp_path), "Fix menu")
    event_id = db.add_event("codex-hook", "PlanProposed", task["id"], {"steps": 3})
    assert event_id
    event = db.recent_events()[0]
    assert event["type"] == "PlanProposed"
    assert event["payload"] == {"steps": 3}
    with pytest.raises(WorkbenchError):
        db.add_event("codex-hook", "NotAnEvent", task["id"], {})
    assert "AgentBlocked" in EVENT_TYPES


def test_provider_events_drive_the_human_review_queues(tmp_path):
    db = store(tmp_path)
    task = db.create_task("local", str(tmp_path), "Build preview")
    db.add_event("claude-hook", "ToolStarted", task["id"])
    assert db.get_task(task["id"])["status"] == "in_progress"
    db.add_event("claude-hook", "ApprovalNeeded", task["id"], {"question": "Deploy?"})
    assert db.get_task(task["id"])["status"] == "blocked"
    db.add_event("claude-hook", "PreviewReady", task["id"])
    assert db.get_task(task["id"])["status"] == "needs_review"


def test_database_is_private_and_repository_evidence_is_read_only(tmp_path):
    db = store(tmp_path)
    assert stat.S_IMODE((tmp_path / "prodder-workspace.sqlite3").stat().st_mode) == 0o600
    evidence = db.repository_evidence(tmp_path)
    assert evidence["status"] == "not_git"


def test_verify_uses_only_explicit_commands(tmp_path):
    db = store(tmp_path)
    task = db.create_task("local", str(tmp_path), "Check it")
    command = f"{sys.executable} -c \"print('verified')\""
    result = db.verify_task(task["id"], [command], timeout=20)
    assert result["checks"][1]["status"] == "passed"
    assert "verified" in result["checks"][1]["output"]
    assert db.list_tasks()[0]["latest_check"]["status"] == "passed"
