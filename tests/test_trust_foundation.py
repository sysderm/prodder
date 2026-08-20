"""Regression tests for local-dashboard safety and durable private state."""
import json
import os
import stat

import prodtop


def agent(**overrides):
    base = dict(host="local", ssh="", pid=1234, name="codex", cwd="/tmp/demo",
                tty="/dev/ttys001", cpu=0.0)
    base.update(overrides)
    return prodtop.AgentProc(**base)


def test_action_identity_rejects_a_reused_terminal():
    original = agent()
    replacement = agent(pid=5678)
    assert prodtop.action_target_matches(original, original.pid, original.action_id)
    assert not prodtop.action_target_matches(replacement, original.pid, original.action_id)
    assert not prodtop.action_target_matches(original, "not-a-pid", original.action_id)


def test_history_redaction_removes_common_credential_shapes(tmp_path):
    screen = "password=correct-horse\nAuthorization: Bearer abcdefghijklmnop\nsk-test-1234567890abcdef"
    config_path = tmp_path / "prodtop.toml"
    config_path.write_text("[[hosts]]\nname = 'local'\nlocal = true\nroots = []\n")
    patterns = prodtop.load_config(str(config_path))["agents"]["history_redact_patterns"]
    out = prodtop.redact_history(screen, patterns)
    assert "correct-horse" not in out
    assert "abcdefghijklmnop" not in out
    assert "1234567890abcdef" not in out
    assert out.count("[REDACTED]") == 3


def test_state_is_atomic_json_with_private_permissions(tmp_path):
    cfg = {"_dir": str(tmp_path)}
    prodtop.save_state(cfg, {"local|/tmp/demo": "ignore"}, {"local|/tmp/demo": "wait"})
    path = tmp_path / "prodtop-state.json"
    assert json.loads(path.read_text())["policies"]["local|/tmp/demo"] == "ignore"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_instance_lock_excludes_a_second_engine(tmp_path):
    cfg = {"_dir": str(tmp_path)}
    first = prodtop.acquire_instance_lock(cfg)
    try:
        try:
            prodtop.acquire_instance_lock(cfg)
        except prodtop.ActionError as e:
            assert e.status == 409
        else:
            raise AssertionError("second instance lock was acquired")
    finally:
        if first is not None:
            os.close(first)
