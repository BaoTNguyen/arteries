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
        'printf \'%s\\n%s\\n%s\\n%s\\n\' "$(pwd)" "$ARTERIES_EVENT_CWD" '
        '"$ARTERIES_SESSION_ID" "$ARTERIES_CLI" > "$FAKE_PY_RECORD"\n'
    )
    fake.chmod(0o755)
    record = tmp_path / "record.txt"
    env = {k: v for k, v in os.environ.items() if k != "ARTERIES_CLI"}
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_PY_RECORD"] = str(record)
    body = {"prompt": "hi", **payload}
    if hook == "arteries-tool.js":
        body = {**FAILING_TOOL, **payload}
    subprocess.run(["node", str(REPO / "hooks" / hook)], input=json.dumps(body),
                   text=True, capture_output=True, cwd=tmp_path, env=env, timeout=30)
    if not record.exists():
        return None
    pwd, event_cwd, session, cli = record.read_text().split("\n")[:4]
    return {"pwd": pwd, "event_cwd": event_cwd, "session": session, "cli": cli}


@pytest.mark.parametrize("hook", HOOKS)
def test_cwd_field_sets_folder(tmp_path, hook):
    folder = tmp_path / "session"
    folder.mkdir()
    rec = _run(tmp_path, hook, {"cwd": str(folder)})
    assert rec is not None
    want = os.path.realpath(folder)
    assert os.path.realpath(rec["pwd"]) == want
    assert os.path.realpath(rec["event_cwd"]) == want
    assert rec["cli"] == "claude"


@pytest.mark.parametrize("hook", HOOKS)
def test_cursor_workspace_roots(tmp_path, hook):
    folder = tmp_path / "session"
    folder.mkdir()
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
    folder.mkdir()
    for payload in ({"cwd": str(folder)},
                    {"workspace_roots": [str(folder)], "cursor_version": "1"},
                    {}):
        rec = _run(tmp_path, hook, payload)
        assert rec is None or rec["cli"] != "codex"
        (tmp_path / "record.txt").unlink(missing_ok=True)
