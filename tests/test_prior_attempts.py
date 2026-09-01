"""Memory records carry the episode that wrote them, and retrieval can use it.

The decisions/rewards ledger has had episode_id and task_id all along; memory
did not, so nothing could say which run produced a fact. That is the one
question separating useful context from an answer sheet.
"""
import os

import pytest

from arteries import memory_select as ms


@pytest.fixture
def at(monkeypatch):
    def _set(episode, task):
        monkeypatch.setenv("ARTERIES_EPISODE_ID", episode)
        monkeypatch.setenv("ARTERIES_TASK_ID", task)
    return _set


def _row(episode, task):
    return {"id": "r1", "fact": "x", "episode_id": episode, "task_id": task}


def test_the_current_episodes_own_notes_are_kept(at):
    """Not "same task" -- ephemeral within a run is the whole point of it."""
    at("ep-1", "task-T")
    assert not ms._prior_attempt_at_this_task(_row("ep-1", "task-T"))


def test_an_earlier_attempt_at_the_same_task_is_excluded(at):
    """If an episode's reward is inflated by recalling its own previous
    solution, a retriever trained on episode outcome learns that fetching last
    time's answer is the best action -- it scores perfectly and generalises to
    nothing."""
    at("ep-2", "task-T")
    assert ms._prior_attempt_at_this_task(_row("ep-1", "task-T"))


def test_another_tasks_memory_is_untouched(at):
    # general context is the thing worth retrieving; only same-task history leaks
    at("ep-2", "task-T")
    assert not ms._prior_attempt_at_this_task(_row("ep-1", "task-U"))


def test_untagged_records_predate_this_and_are_kept(at):
    at("ep-2", "task-T")
    assert not ms._prior_attempt_at_this_task(_row(None, None))


def test_nothing_is_excluded_outside_an_episode(monkeypatch):
    # an interactive session sets no task id; exclusion must not fire on it
    monkeypatch.delenv("ARTERIES_TASK_ID", raising=False)
    monkeypatch.setenv("ARTERIES_EPISODE_ID", "ep-2")
    assert not ms._prior_attempt_at_this_task(_row("ep-1", "task-T"))


def test_the_packet_can_report_what_went_into_it():
    """Ids exist through selection and were dropped at the MemoryItem boundary,
    so a caller saw the text and never what produced it. Training retrieval on
    episode outcome needs that link."""
    from arteries import packet

    provenance: list[dict] = []
    packet.build_packet(message="probe", budget=400, provenance=provenance)
    assert isinstance(provenance, list)
    for record in provenance:
        assert {"tier", "id", "score"} <= set(record)
