"""The subagent identity contract, in one place.

A subagent inherits its parent's memory and writes ephemeral tagged with the
parent, which the parent's compilation pass claims under the [SUBAGENT] bar. The
env vars below are what config.py / memory_select.py key off. `art spawn` and any
orchestrator (heart) build the child env from here so the contract has a single
definition — identity only; the memory *mode* (`ARTERIES_MEMORY=subagent`) is set
separately by whoever launches the child, since a launcher may want a different
read/write policy than the default.
"""
from __future__ import annotations

import uuid


def subagent_env(parent_agent_id: str, agent_id: str | None = None) -> dict[str, str]:
    """The env that marks a child process as a subagent of `parent_agent_id`.
    A fresh `agent_id` is minted when not supplied, so parallel subagents of one
    parent stay distinct while all pointing back to the same parent."""
    return {
        "ARTERIES_PARENT_AGENT_ID": parent_agent_id,
        "ARTERIES_AGENT_ID": agent_id or f"{parent_agent_id}-sub-{uuid.uuid4().hex[:8]}",
        "ARTERIES_AGENT_ROLE": "subagent",
    }
