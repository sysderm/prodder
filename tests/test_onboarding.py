"""New-user onboarding must not fail with a traceback, whatever the user has
installed. These pin the config layer where a first-timer's hand-edited
prodtop.toml is most likely to be wrong, plus the agent-name matcher that keeps
a "just Codex" / "just Claude" / "only the desktop app" user from seeing either
nothing useful or phantom agents.
"""
import re

import pytest

import prodtop


def write_cfg(tmp_path, text):
    p = tmp_path / "prodtop.toml"
    p.write_text(text)
    return str(p)


# ---- config validation: friendly SystemExit, never a raw traceback ----

def test_host_missing_name_exits_friendly(tmp_path):
    cfg = write_cfg(tmp_path, "[[hosts]]\nlocal = true\nroots = []\n")
    with pytest.raises(SystemExit) as e:
        prodtop.load_config(cfg)
    assert "name" in str(e.value).lower()


def test_remote_host_missing_ssh_exits_friendly(tmp_path):
    cfg = write_cfg(tmp_path, '[[hosts]]\nname = "srv"\nroots = ["/tmp"]\n')
    with pytest.raises(SystemExit) as e:
        prodtop.load_config(cfg)
    assert "ssh" in str(e.value).lower()


def test_local_host_needs_no_ssh(tmp_path):
    cfg = write_cfg(tmp_path, '[[hosts]]\nname = "local"\nlocal = true\nroots = []\n')
    c = prodtop.load_config(cfg)
    assert c["hosts"][0]["name"] == "local"


def test_malformed_toml_exits_friendly(tmp_path):
    cfg = write_cfg(tmp_path, '[agents\nprocess_names = ["claude"')
    with pytest.raises(SystemExit) as e:
        prodtop.load_config(cfg)
    assert "toml" in str(e.value).lower()


def test_minimal_config_gets_all_defaults(tmp_path):
    # A user who deletes everything but one host must still get working defaults.
    cfg = write_cfg(tmp_path, '[[hosts]]\nname = "local"\nlocal = true\n')
    c = prodtop.load_config(cfg)
    assert c["agents"]["process_names"]          # non-empty default agent list
    assert c["web"]["port"] == 8737
    assert c["agents"]["auto_prod"] is False     # safe default: never auto-type
    assert c["settings"]["window_days"] >= 1


def test_disabled_host_is_dropped(tmp_path):
    cfg = write_cfg(
        tmp_path,
        '[[hosts]]\nname = "local"\nlocal = true\n'
        '[[hosts]]\nname = "srv"\nenabled = false\n')  # missing ssh but disabled
    c = prodtop.load_config(cfg)               # must NOT trip the ssh check
    assert [h["name"] for h in c["hosts"]] == ["local"]


# ---- agent matching: right tokens, no phantom GUI-app matches ----

@pytest.mark.parametrize("args", [
    "claude", "/opt/homebrew/bin/claude --resume x", "codex resume --last",
    "node /usr/local/bin/codex", "python -m aider",
])
def test_agent_regex_matches_real_clis(args):
    rx = prodtop.agent_regex(["claude", "codex", "aider"])
    assert rx.search(args)


@pytest.mark.parametrize("args", [
    "mycursor --foo",          # substring, not a real token
    "vim cursor.py",           # a file that mentions the name
    "less claude_notes.md",
])
def test_agent_regex_ignores_lookalikes(args):
    rx = prodtop.agent_regex(["cursor", "claude"])
    assert not rx.search(args)
