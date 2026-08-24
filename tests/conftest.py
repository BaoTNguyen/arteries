import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_arteries_env():
    """Keep ARTERIES_*/AGENT_* env out of the next test.

    `cli_normalize.apply_event_env` writes identity into os.environ by design —
    hooks are one-shot processes where that is free. Under pytest the process
    is shared, so a test that exercises a hook leaks its CLI into every test
    that follows, and run attribution silently reads the wrong one.
    """
    keys = {k for k in os.environ if k.startswith(("ARTERIES_", "AGENT_"))}
    saved = {k: os.environ[k] for k in keys}
    yield
    for key in {k for k in os.environ if k.startswith(("ARTERIES_", "AGENT_"))} - keys:
        del os.environ[key]
    os.environ.update(saved)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "writes_events: test genuinely exercises the runlog write path and may "
        "reach the live event store",
    )


@pytest.fixture(autouse=True)
def _no_live_events(request, monkeypatch):
    """Keep test runs out of the real event store.

    The suite has no test database -- it points at the live one. A test that
    patches the thing it asserts on and leaves runlog.log_event real writes a
    genuine event, and because the reporting path swallows its own failures by
    design ("reporting a bug must not raise"), nothing says so.

    test_degrade did exactly that for two days: 30 fabricated
    internal.bug_swallowed rows, in the one channel whose value depends on
    every entry being real.

    Tests that mean to exercise the write path opt in with
    @pytest.mark.writes_events.
    """
    if request.node.get_closest_marker("writes_events"):
        return
    real = None

    def _blocked(event_type, source, payload=None, **kwargs):
        return {"event_type": event_type, "source": source, "payload": payload or {},
                "blocked_by": "conftest._no_live_events"}

    monkeypatch.setattr("arteries.runlog.log_event", _blocked)
    return real
