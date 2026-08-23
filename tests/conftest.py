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
