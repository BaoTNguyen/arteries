"""The node hooks run python3 in the session's folder, not the plugin's.

A fake python3 on PATH records its cwd and the ARTERIES_* env it was given,
so these tests need node but no database, network or real arteries import.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

REPO = Path(__file__).parents[1]
HOOKS = ["arteries-observe.js", "arteries-tool.js"]
FAILING_TOOL = {"tool_name": "Bash", "tool_input": {"command": "false"},
                "tool_response": {"exit_code": 1}}


def _run(tmp_path, hook, payload):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n\' "$(pwd)" "$ARTERIES_EVENT_CWD" '
        '"$ARTERIES_SESSION_ID" "$ARTERIES_CLI" "$ARTERIES_REPO" "$ARTERIES_PROJECT" '
        '"$ARTERIES_AGENT_ID" "$ARTERIES_TRANSCRIPT" > "$FAKE_PY_RECORD"\n'
    )
    fake.chmod(0o755)
    record = tmp_path / "record.txt"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARTERIES_")}
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_PY_RECORD"] = str(record)
    body = {"prompt": "hi", **payload}
    if hook == "arteries-tool.js":
        body = {**FAILING_TOOL, **payload}
    subprocess.run(["node", str(REPO / "hooks" / hook)], input=json.dumps(body),
                   text=True, capture_output=True, cwd=tmp_path, env=env, timeout=30)
    if not record.exists():
        return None
    pwd, event_cwd, session, cli, repo, project, agent, transcript = \
        record.read_text().split("\n")[:8]
    return {"pwd": pwd, "event_cwd": event_cwd, "session": session, "cli": cli, "repo": repo,
            "project": project, "agent": agent, "transcript": transcript}


def _wired(folder, config=None):
    marker = folder / ".vascular" / "arteries" / "config.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(config or {}) + "\n")
    return folder


@pytest.mark.parametrize("hook", HOOKS)
def test_cwd_field_sets_folder(tmp_path, hook):
    folder = tmp_path / "session"
    _wired(folder)
    rec = _run(tmp_path, hook, {"cwd": str(folder)})
    assert rec is not None
    want = os.path.realpath(folder)
    assert os.path.realpath(rec["pwd"]) == want
    assert os.path.realpath(rec["event_cwd"]) == want
    assert rec["cli"] == "claude"


@pytest.mark.parametrize("hook", HOOKS)
def test_cursor_workspace_roots(tmp_path, hook):
    folder = tmp_path / "session"
    _wired(folder)
    rec = _run(tmp_path, hook, {"workspace_roots": [str(folder)],
                                "conversation_id": "c1", "cursor_version": "1"})
    assert rec is not None
    want = os.path.realpath(folder)
    assert os.path.realpath(rec["pwd"]) == want
    assert os.path.realpath(rec["event_cwd"]) == want
    assert rec["session"] == "c1"
    assert rec["cli"] == "cursor"


@pytest.mark.parametrize("hook", HOOKS)
def test_no_folder_never_spawns(tmp_path, hook):
    assert _run(tmp_path, hook, {}) is None


@pytest.mark.parametrize("hook", HOOKS)
def test_cli_never_defaults_to_codex(tmp_path, hook):
    folder = tmp_path / "session"
    _wired(folder)
    for payload in ({"cwd": str(folder)},
                    {"workspace_roots": [str(folder)], "cursor_version": "1"},
                    {}):
        rec = _run(tmp_path, hook, payload)
        assert rec is None or rec["cli"] != "codex"
        (tmp_path / "record.txt").unlink(missing_ok=True)


@pytest.mark.parametrize("hook", HOOKS)
def test_unwired_folder_never_spawns(tmp_path, hook):
    """The plugin is global; a folder `arteries setup` never touched is not
    the user's to observe. Nothing runs, so nothing lands in .vascular or the DB."""
    folder = tmp_path / "client-project"
    folder.mkdir()
    for payload in ({"cwd": str(folder)},
                    {"workspace_roots": [str(folder)], "cursor_version": "1"}):
        assert _run(tmp_path, hook, payload) is None
    assert not (folder / ".vascular").exists()


@pytest.mark.parametrize("hook", HOOKS)
def test_bare_vascular_dir_is_not_approval(tmp_path, hook):
    folder = tmp_path / "stray"
    (folder / ".vascular" / "arteries" / "runs").mkdir(parents=True)
    assert _run(tmp_path, hook, {"cwd": str(folder)}) is None


@pytest.mark.parametrize("hook", HOOKS)
def test_subfolder_of_wired_repo_attributes_to_repo(tmp_path, hook):
    repo = _wired(tmp_path / "repo")
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    rec = _run(tmp_path, hook, {"cwd": str(sub)})
    assert rec is not None
    assert os.path.realpath(rec["pwd"]) == os.path.realpath(sub)
    assert os.path.realpath(rec["repo"]) == os.path.realpath(repo)


def _activate(tmp_path, folder):
    return subprocess.run(["node", str(REPO / "hooks" / "arteries-activate.js")],
                          input=json.dumps({"cwd": str(folder)}), text=True,
                          capture_output=True, cwd=tmp_path, timeout=30).stdout


def test_activate_silent_outside_wired_repo(tmp_path):
    folder = tmp_path / "client-project"
    folder.mkdir()
    assert _activate(tmp_path, folder) == ""
    assert "ARTERIES" in _activate(tmp_path, _wired(tmp_path / "repo"))


@pytest.mark.parametrize("hook", HOOKS)
def test_identity_and_transcript_come_from_setup_and_the_host(tmp_path, hook):
    """ARTERIES-B14: without these a plugin turn is filed under the cwd's name,
    logged by a pid and priced at $0."""
    repo = _wired(tmp_path / "repo", {"project": "proj", "agent_id": "proj-hook"})
    sub = repo / "src"
    sub.mkdir()
    rec = _run(tmp_path, hook, {"cwd": str(sub), "transcript_path": "/t/session.jsonl"})
    assert rec is not None
    assert (rec["project"], rec["agent"], rec["transcript"]) == \
        ("proj", "proj-hook", "/t/session.jsonl")
