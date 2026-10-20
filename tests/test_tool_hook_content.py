"""arteries-tool.js forwards tool_input and tool_response to observe_tool.

A fake python3 on PATH captures its stdin, so these tests need node but no
database, network or real arteries import.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

if shutil.which("node") is None:
    pytest.skip("node not installed", allow_module_level=True)

REPO = Path(__file__).parents[1]


def _wired(root):
    config_dir = root / ".vascular" / "arteries"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text("{}\n")


def _run(tmp_path, payload):
    _wired(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "python3"
    fake.write_text('#!/bin/sh\ncat > "$FAKE_PY_STDIN"\n')
    fake.chmod(0o755)
    capture = tmp_path / "stdin.json"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_PY_STDIN"] = str(capture)
    subprocess.run(["node", str(REPO / "hooks" / "arteries-tool.js")],
                   input=json.dumps(payload), text=True, capture_output=True,
                   cwd=tmp_path, env=env, timeout=30)
    return json.loads(capture.read_text()) if capture.exists() else None


def test_failing_bash_forwards_content(tmp_path):
    got = _run(tmp_path, {"tool_name": "Bash", "cwd": str(tmp_path),
                          "session_id": "s1",
                          "tool_input": {"command": "false"},
                          "tool_response": {"exit_code": 1, "stdout": "boom"}})
    assert got is not None
    assert got["tool_input"] == {"command": "false"}
    assert got["tool_response"]["stdout"] == "boom"
    assert got["failed"] is True
    # The original fields ride alongside the new ones.
    assert got["tool"] == "Bash"
    assert got["exit_code"] == 1
    assert got["target"] == "false"
    assert got["session_id"] == "s1"


def test_web_fetch_forwards_input(tmp_path):
    tool_input = {"url": "https://example.com/a?q=1", "prompt": "read"}
    got = _run(tmp_path, {"tool_name": "WebFetch", "cwd": str(tmp_path),
                          "tool_input": tool_input, "tool_response": {}})
    assert got is not None
    assert got["tool_input"] == tool_input
    assert got["target"] == "example.com"


def test_successful_read_records_nothing(tmp_path):
    got = _run(tmp_path, {"tool_name": "Read", "cwd": str(tmp_path),
                          "tool_input": {"file_path": "x"}, "tool_response": {}})
    assert got is None
    assert not (tmp_path / "stdin.json").exists()
