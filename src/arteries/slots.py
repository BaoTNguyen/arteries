"""One pool of model-server slots, shared by every process on the box.

A local model server answers `--parallel N` requests at once and queues the rest
invisibly -- a queued request looks exactly like a slow one, with no error and
nothing to shed. So the number of in-flight requests has to be bounded, and
bounded *once*: arteries capping itself at 2 and heart capping itself at 2 means
4 requests for 2 slots, which is the same overload with more bookkeeping.

The coordination point has to satisfy three things, and they rule out the obvious
options:

* **Every process in the stack, in any language.** heart is stdlib-only and
  cannot import psycopg2, so a Postgres advisory lock is out -- which is what
  arteries used before this.
* **No registry and no protocol.** plexus, marrow, or something written next year
  should participate by doing the same simple thing, not by being added to a list.
* **Crash-safe.** A killed agent must not hold a slot. The kernel drops an flock
  the instant its holder dies, which no application-level counter can promise.

So: a directory of lock files per endpoint, and whoever holds one holds a slot.

    $XDG_RUNTIME_DIR/model-slots/<host>_<port>/slot0 .. slotN-1

Keyed by `host:port` so two servers get independent pools -- one llama.cpp per
GPU on :8001 and :8002 is two queues and should be bounded as two. A single
tensor-parallel server spanning both GPUs is one port and therefore one pool,
which is also right: it is one queue.

The convention is the contract. Anything that wants a slot flocks a file in that
directory; nothing needs to know who else is there. heart implements the same
thing in `runner._flock_pool` against the same path, deliberately duplicated
rather than shared, because a shared module would make the two repos depend on
each other to avoid twenty lines.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

# What a server reports when asked, cached per endpoint: one probe per process,
# not one per request.
_slots_cache: dict[str, int] = {}

# When the server will not say. Unknown parallelism is better served by a guess
# than by "unlimited", which is what no cap at all means.
DEFAULT_SLOTS = 2


def base_dir() -> Path:
    """Where the pools live. XDG_RUNTIME_DIR is per-user and cleared on logout,
    which is the right lifetime for something the kernel is also tracking."""
    root = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(root) / "model-slots"


def pool_for(endpoint: str) -> Path:
    key = (urlsplit(endpoint).netloc or "local").replace(":", "_")
    return base_dir() / key


def slot_count(endpoint: str, timeout: float = 1.0) -> int:
    """The server's real parallelism, asked once.

    Asking beats hardcoding the same number in two repos and then having them
    disagree after someone edits a systemd unit. llama.cpp's /slots returns one
    object per slot; /health only says "ok".
    """
    override = int(os.environ.get("ARTERIES_MODEL_SLOTS", "") or 0)
    if override > 0:
        return override

    parts = urlsplit(endpoint)
    key = parts.netloc or endpoint
    if key in _slots_cache:
        return _slots_cache[key]

    slots = DEFAULT_SLOTS
    try:
        import json
        import urllib.request

        base = f"{parts.scheme or 'http'}://{key}"
        with urllib.request.urlopen(f"{base}/slots", timeout=timeout) as resp:
            reported = json.load(resp)
        if isinstance(reported, list) and reported:
            slots = len(reported)
    except Exception:
        pass
    _slots_cache[key] = slots
    return slots


@contextlib.contextmanager
def hold(endpoint: str | None, wait: bool = False):
    """Hold a slot for this endpoint, yielding True, or yield False at once.

    Refusing beats queueing here. A caller that cannot get a slot has not yet
    claimed any work, so its rows stay where they are and the next turn finds
    them -- back-pressure that defers rather than drops. Waiting would park a
    process on a slot it cannot use for the length of someone else's generation
    call, and under load that turns one overloaded server into a pile of stalled
    processes.

    `wait=True` exists for a caller with nothing to defer to, and blocks.
    """
    if not endpoint:
        yield True                      # not a local server; not our business
        return

    pool = pool_for(endpoint)
    pool.mkdir(parents=True, exist_ok=True)
    count = slot_count(endpoint)
    mode = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB

    held = None
    try:
        for index in range(count):
            handle = open(pool / f"slot{index}", "w")
            try:
                fcntl.flock(handle, mode)
                held = handle
                break
            except OSError:
                handle.close()
                if wait:
                    raise
        yield held is not None
    finally:
        if held is not None:
            # No explicit unlock: closing the descriptor drops the flock, and so
            # does the process dying, which is the property this is here for.
            held.close()
