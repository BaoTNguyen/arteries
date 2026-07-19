"""Normalize raw CLI hook/extension payloads into Arteries metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class NormalizedCliEvent:
    cli: str
    event: str
    project_id: str
    agent_id: str
    parent_agent_id: str | None
    agent_role: str
    session_id: str | None
    cwd: str | None
    raw_event_name: str | None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize agent CLI hook payloads for Arteries.")
    parser.add_argument("--cli", default=os.getenv("ARTERIES_CLI", "generic"))
    parser.add_argument("--event", default=None, help="fallback event name when payload has none")
    parser.add_argument("--project", default=os.getenv("ARTERIES_PROJECT", "default"))
    parser.add_argument("--agent", default=os.getenv("ARTERIES_AGENT_ID"))
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    parser.add_argument("--field", choices=("message", "transcript"), help="print a single normalized field")
    args = parser.parse_args(argv)

    raw = _read_payload()
    normalized = normalize(raw, cli=args.cli, fallback_event=args.event, project_id=args.project, agent_id=args.agent)
    if args.field == "message":
        print(_message(raw))
        return 0
    if args.field == "transcript":
        print(_transcript(raw))
        return 0
    if args.format == "shell":
        print(_shell_exports(normalized))
    else:
        print(json.dumps(asdict(normalized), indent=2, sort_keys=True))
    return 0


def normalize(
    payload: dict[str, Any],
    cli: str,
    fallback_event: str | None = None,
    project_id: str = "default",
    agent_id: str | None = None,
) -> NormalizedCliEvent:
    cli_name = (cli or "generic").lower()
    raw_event = _event_name(payload, fallback_event)
    event = _canonical_event(raw_event)
    cwd = _first_text(payload, "cwd", "working_directory", "project_dir", "projectDirectory")
    session_id = _first_text(payload, "session_id", "sessionId", "session", "session_file", "sessionFile")
    parent = _first_text(payload, "parent_agent_id", "parentAgentId", "parent_session_id", "parentSessionId")

    role = _role(payload, event)
    base_agent = agent_id or _first_text(payload, "agent_id", "agentId") or _default_agent(project_id)
    normalized_agent = _agent_id(base_agent, cli_name, event, payload, role)
    if role == "subagent" and not parent:
        parent = base_agent

    return NormalizedCliEvent(
        cli=cli_name,
        event=event,
        project_id=project_id,
        agent_id=normalized_agent,
        parent_agent_id=parent,
        agent_role=role,
        session_id=session_id,
        cwd=cwd,
        raw_event_name=raw_event,
    )


def _message(payload: dict[str, Any]) -> str:
    return _first_text(
        payload,
        "prompt",
        "message",
        "user_prompt",
        "userPrompt",
        "input",
        "text",
        "content",
        "body",
        "value",
        "reason",
        "trigger",
    ) or ""


def _transcript(payload: dict[str, Any]) -> str:
    return _first_text(
        payload,
        "transcript_path",
        "transcriptPath",
        "transcript_file",
        "transcriptFile",
        "session_file",
        "sessionFile",
    ) or ""


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _event_name(payload: dict[str, Any], fallback: str | None) -> str | None:
    return _first_text(
        payload,
        "hook_event_name",
        "hookEventName",
        "event_name",
        "eventName",
        "event",
        "type",
        "name",
    ) or fallback


def _canonical_event(name: str | None) -> str:
    compact = (name or "prompt").replace("-", "_").replace(" ", "_").lower()
    mapping = {
        "userpromptsubmit": "prompt",
        "user_prompt_submit": "prompt",
        "input": "prompt",
        "message_updated": "prompt",
        "message.updated": "prompt",
        "tui_prompt_append": "prompt",
        "tui.prompt.append": "prompt",
        "before_agent_start": "prompt",
        "assistantresponse": "assistant_response",
        "assistant_response": "assistant_response",
        "agent_response": "assistant_response",
        "stop": "assistant_response",
        "sessionstart": "session_start",
        "session_start": "session_start",
        "session_created": "session_start",
        "session.created": "session_start",
        "subagentstart": "subagent_start",
        "subagent_start": "subagent_start",
        "subagentstop": "subagent_stop",
        "subagent_stop": "subagent_stop",
        "taskcreated": "subagent_start",
        "task_created": "subagent_start",
        "taskcompleted": "subagent_stop",
        "task_completed": "subagent_stop",
        "precompact": "compact",
        "postcompact": "compact",
        "pre_compact": "compact",
        "post_compact": "compact",
        "session_before_compact": "compact",
        "session_compact": "compact",
        "session_compacted": "compact",
        "session.compacted": "compact",
        "experimental_session_compacting": "compact",
        "experimental.session.compacting": "compact",
        "compact": "compact",
    }
    return mapping.get(compact, compact)


def _role(payload: dict[str, Any], event: str) -> str:
    explicit = _first_text(payload, "agent_role", "agentRole", "role")
    if explicit in {"parent", "subagent", "background", "unknown"}:
        return explicit
    if event in {"subagent_start", "subagent_stop"}:
        return "subagent"
    if _first_text(payload, "subagent_type", "subagentType", "subagent", "subagent_name", "subagentName"):
        return "subagent"
    return "parent"


def _agent_id(base_agent: str, cli: str, event: str, payload: dict[str, Any], role: str) -> str:
    explicit = _first_text(payload, "agent_id", "agentId", "subagent_id", "subagentId")
    if explicit:
        return explicit
    if role != "subagent":
        return base_agent
    subagent_type = _first_text(payload, "subagent_type", "subagentType", "subagent", "subagent_name", "subagentName") or "subagent"
    seed = json.dumps(payload, sort_keys=True, default=str)[:1000]
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return f"{base_agent}:{cli}:{_clean(subagent_type)}:{digest}"


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _nested_get(payload, key)
        if value is not None and value != "":
            return str(value)
    return None


def _nested_get(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    for container in ("data", "payload", "details", "hook_input", "hookInput"):
        nested = payload.get(container)
        if isinstance(nested, dict) and key in nested:
            return nested[key]
    return None


def _default_agent(project_id: str) -> str:
    cleaned = _clean(project_id)
    return f"{cleaned}-hook"


def _clean(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.lower()).strip("-") or "agent"


def _shell_exports(event: NormalizedCliEvent) -> str:
    values = {
        "ARTERIES_CLI": event.cli,
        "ARTERIES_EVENT": event.event,
        "ARTERIES_AGENT_ID": event.agent_id,
        "ARTERIES_AGENT_ROLE": event.agent_role,
    }
    if event.parent_agent_id:
        values["ARTERIES_PARENT_AGENT_ID"] = event.parent_agent_id
    if event.session_id:
        values["ARTERIES_SESSION_ID"] = event.session_id
    if event.cwd:
        values["ARTERIES_EVENT_CWD"] = event.cwd
    return "\n".join(f"export {key}={shlex.quote(value)}" for key, value in values.items())


if __name__ == "__main__":
    raise SystemExit(main())
