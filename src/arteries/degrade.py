"""Telling "the environment broke" apart from "the code is broken".

Arteries has a correct principle -- memory must never fail a turn -- implemented
with a mechanism that cannot tell why something failed. `except Exception`
catches a database being down, which should degrade quietly, and a NameError,
which should not. Seven times in this rework a defect shipped green because a
handler swallowed it.

Two handlers made it worse by naming a cause they had not checked. When the
scoring loop in packet._load_memories raised, the user was told "Memory storage
was unavailable" -- a specific claim about the database, asserted without
looking, while the real fault was in the line above.

So: keep catching everything, because the principle holds. But classify first.
An environment failure is expected and logged at debug. Anything else is a bug
that happens to have been caught, and says so loudly with a traceback and its
own event type, so it appears in `art trace` instead of vanishing.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)

# Failures that mean the world is unavailable, not that the code is wrong.
# Kept as strings where importing the module would be a new dependency on a
# path that must stay cheap.
_ENVIRONMENT_TYPES: tuple[type[BaseException], ...] = (
    OSError,                 # covers socket, file, and most connection errors
    socket.timeout,
    json.JSONDecodeError,    # a malformed response is the sender's problem
    TimeoutError,
)
_ENVIRONMENT_NAMES = frozenset({
    # psycopg2 and httpx are real dependencies, but naming them by string keeps
    # this module importable from anywhere without dragging them in.
    "OperationalError", "InterfaceError", "DatabaseError", "PoolError",
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
    "PoolTimeout", "HTTPStatusError", "RequestError", "TransportError",
    "ImportError", "ModuleNotFoundError",   # an optional extra is absent
})


def is_environment(exc: BaseException) -> bool:
    """Is this the world failing, rather than the code being wrong?"""
    if isinstance(exc, _ENVIRONMENT_TYPES):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in _ENVIRONMENT_NAMES:
            return True
    return False


def note(exc: BaseException, what: str, **context: Any) -> str:
    """Record a caught failure at the volume it deserves. Returns a reason.

    Call this from every degradation handler instead of passing silently. The
    return value is a short string safe to show a user or store in a payload --
    and it says "unavailable" only when that was actually true.
    """
    kind = type(exc).__name__
    if is_environment(exc):
        logger.debug("%s degraded: %s", what, kind, exc_info=True)
        return f"{what} unavailable ({kind})"

    # Not an environment failure, so something is wrong with the code. The turn
    # still survives -- that principle is not negotiable -- but this must not be
    # quiet, because quiet is how it ships.
    logger.error("BUG caught while degrading %s: %s", what, kind, exc_info=True)
    try:
        from arteries import runlog
        runlog.log_event("internal.bug_swallowed", "arteries",
                         {"where": what, "error_type": kind,
                          "error": (str(exc) or repr(exc))[:300], **context})
    except Exception:  # noqa: BLE001 -- reporting a bug must not raise
        pass
    return f"{what} failed ({kind})"
