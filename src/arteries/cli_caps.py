"""CLI capability metadata for memory selection and packet assembly."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CliCapabilities:
    name: str
    observes_subagents: bool = False
    observes_compaction: bool = False
    observes_assistant: bool = False
    can_inject_context: bool = False
    can_replace_compaction: bool = False
    can_override_compact_prompt: bool = False
    can_emit_resume_packet: bool = True


_CAPABILITIES = {
    "pi": CliCapabilities(
        name="pi",
        observes_subagents=True,
        observes_compaction=True,
        observes_assistant=True,
        can_inject_context=True,
        can_replace_compaction=True,
    ),
    "claude": CliCapabilities(
        name="claude",
        observes_subagents=True,
        observes_compaction=True,
        observes_assistant=True,
        can_inject_context=True,
    ),
    "claude-code": CliCapabilities(
        name="claude-code",
        observes_subagents=True,
        observes_compaction=True,
        observes_assistant=True,
        can_inject_context=True,
    ),
    "codex": CliCapabilities(
        name="codex",
        observes_subagents=True,
        observes_compaction=True,
        observes_assistant=True,
        can_inject_context=True,
        can_override_compact_prompt=True,
    ),
    "opencode": CliCapabilities(
        name="opencode",
        observes_subagents=True,
        observes_compaction=True,
        observes_assistant=True,
        can_inject_context=True,
        can_replace_compaction=True,
        can_override_compact_prompt=True,
    ),
    "cursor": CliCapabilities(
        name="cursor",
        observes_assistant=True,
        can_inject_context=True,
    ),
    # Hermes is intentionally conservative until its hook/event model is known.
    "hermes": CliCapabilities(
        name="hermes",
        observes_assistant=True,
        can_inject_context=True,
    ),
}


def get_capabilities(cli: str | None = None) -> CliCapabilities:
    name = (cli or os.getenv("ARTERIES_CLI") or "generic").strip().lower()
    return _CAPABILITIES.get(name, CliCapabilities(name=name or "generic"))
