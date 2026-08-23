"""Each CLI gets its own run in a shared repo.

The bug: one current-run.json per repo meant whoever called `runs start` last
owned every following turn, so a Claude turn landed on a Codex run and priced
against the wrong rate card.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from arteries import runlog


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(runlog.psycopg2, "connect", side_effect=RuntimeError("db down")):
            yield Path(tmp)


def _observe(repo, cli):
    return runlog.log_event("turn.observed", "arteries", {}, project_id="p", cli=cli, repo_path=repo)


def test_named_clis_do_not_share_a_run(repo):
    codex = runlog.start_run(project_id="p", agent_id="a", cli="codex", repo_path=repo)
    claude = _observe(repo, "claude")
    assert claude["run_id"] != codex["run_id"]
    # and each keeps resuming its own
    assert _observe(repo, "claude")["run_id"] == claude["run_id"]
    assert _observe(repo, "codex")["run_id"] == codex["run_id"]


def test_arbitrary_cli_names_get_their_own_runs(repo):
    ids = {cli: _observe(repo, cli)["run_id"] for cli in ("zed", "aider", "nvim")}
    assert len(set(ids.values())) == 3
    for cli, run_id in ids.items():
        assert _observe(repo, cli)["run_id"] == run_id


def test_undeclared_caller_adopts_the_open_run(repo):
    # nothing to keep separate, so don't fork an "unknown" run
    started = runlog.start_run(project_id="p", agent_id="a", cli="codex", repo_path=repo)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ARTERIES_CLI", None)
        os.environ.pop("AGENT_CLI", None)
        assert _observe(repo, None)["run_id"] == started["run_id"]


def test_legacy_current_run_migrates_under_its_own_cli(repo):
    started = runlog.start_run(project_id="p", agent_id="a", cli="codex", repo_path=repo)
    (repo / ".arteries" / "runs" / "current-codex.json").unlink()  # pre-upgrade layout
    assert _observe(repo, "codex")["run_id"] == started["run_id"]
    assert (repo / ".arteries" / "runs" / "current-codex.json").exists()


def test_legacy_pointer_still_written_for_external_readers(repo):
    runlog.start_run(project_id="p", agent_id="a", cli="codex", repo_path=repo)
    assert (repo / ".arteries" / "current-run.json").exists()
