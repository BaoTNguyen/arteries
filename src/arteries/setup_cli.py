# Install arteries integrations into agent CLI projects.

from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

MARKER_START = "<!-- arteries:start -->"
MARKER_END = "<!-- arteries:end -->"
CODEX_MARKER_START = "# arteries:start - managed by `art setup codex`"
CODEX_MARKER_END = "# arteries:end"
PROVIDERS = ("pi", "codex", "claude")


@dataclass
class Result:
    success: bool
    message: str


@dataclass
class Context:
    cwd: Path
    arteries_root: Path
    project_name: str
    cli_name: str
    capillaries_root: Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install arteries into agent CLI projects.")
    parser.add_argument("provider", nargs="?", choices=PROVIDERS)
    parser.add_argument("--cwd", type=Path, default=_default_cwd(), help="target repo directory")
    parser.add_argument("--arteries-root", type=Path, default=_default_arteries_root())
    parser.add_argument("--project", help="ARTERIES_PROJECT value; defaults to repo directory name")
    parser.add_argument("--cli", help="ARTERIES_CLI value; defaults to provider name")
    parser.add_argument("--capillaries-root", type=Path, help="capillaries repo root for local editable imports")
    parser.add_argument("--check", action="store_true", help="verify provider integration")
    parser.add_argument("--remove", action="store_true", help="remove provider integration")
    parser.add_argument("--list", action="store_true", help="list supported providers")
    args = parser.parse_args(argv)

    if args.list:
        for provider in PROVIDERS:
            print(provider)
        return 0

    if not args.provider:
        parser.error("provider is required unless --list is used")

    cwd = args.cwd.resolve()
    arteries_root = args.arteries_root.resolve()
    ctx = Context(
        cwd=cwd,
        arteries_root=arteries_root,
        project_name=args.project or cwd.name,
        cli_name=args.cli or args.provider,
        capillaries_root=(args.capillaries_root or _default_capillaries_root(arteries_root)).resolve(),
    )

    action = "check" if args.check else "remove" if args.remove else "install"
    result = RECIPES[args.provider][action](ctx)
    print(("OK: " if result.success else "ERROR: ") + result.message)
    return 0 if result.success else 1


def _default_arteries_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_cwd() -> Path:
    return Path(os.getenv("ARTERIES_CALLER_CWD") or Path.cwd())


def _default_capillaries_root(arteries_root: Path) -> Path:
    return arteries_root.parent / "capillaries"


def _agent_id(project_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in project_name.lower())
    return f"{cleaned}-hook"


def _arteries_dir(ctx: Context) -> Path:
    return ctx.cwd / ".arteries"


def _runtime_env(ctx: Context, cli_name: str) -> str:
    return f'''ARTERIES_ROOT="${{ARTERIES_ROOT:-{ctx.arteries_root}}}"
CAPILLARIES_ROOT="${{CAPILLARIES_ROOT:-{ctx.capillaries_root}}}"
PROJECT_ROOT="${{PROJECT_ROOT:-{ctx.cwd}}}"
export PYTHONPATH="$ARTERIES_ROOT/src:$CAPILLARIES_ROOT/src:$PROJECT_ROOT/src:${{PYTHONPATH:-}}"
export ARTERIES_PROJECT="${{ARTERIES_PROJECT:-{ctx.project_name}}}"
export ARTERIES_AGENT_ID="${{ARTERIES_AGENT_ID:-{_agent_id(ctx.project_name)}}}"
export ARTERIES_CLI="${{ARTERIES_CLI:-{cli_name}}}"
export ARTERIES_REPO="${{ARTERIES_REPO:-$PROJECT_ROOT}}"
'''


def _ensure_runtime(ctx: Context, cli_name: str) -> None:
    hooks = _arteries_dir(ctx) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    config = {
        "arteries_root": str(ctx.arteries_root),
        "project_root": str(ctx.cwd),
        "project": ctx.project_name,
        "agent_id": _agent_id(ctx.project_name),
        "cli": cli_name,
        "capillaries_root": str(ctx.capillaries_root),
    }
    (_arteries_dir(ctx) / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    env = _runtime_env(ctx, cli_name)
    observe = f'''#!/usr/bin/env bash
set -euo pipefail

{env}
if [[ $# -gt 0 ]]; then
  prompt="$*"
else
  prompt="$(cat)"
fi

python3 -m arteries.eval "$prompt"
'''
    generic = '''#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
if [[ $# -gt 0 ]]; then
  prompt="$*"
else
  prompt="$(cat)"
fi

result="$($script_dir/observe.sh "$prompt")"
if [[ -n "$result" ]]; then
  printf 'ARTERIES RETRIEVED PROMPT - use this to guide your response:\n\n%s\n' "$result"
fi
'''
    activate = f'''#!/usr/bin/env bash
set -euo pipefail

{env}
python3 -m arteries.runs start --project "$ARTERIES_PROJECT" --agent "$ARTERIES_AGENT_ID" --cli "$ARTERIES_CLI" --repo "$ARTERIES_REPO" >/dev/null 2>&1 || true

cat <<'EOF'
ARTERIES MEMORY SYSTEM ACTIVE.

This repo is connected to arteries project `{ctx.project_name}`.
Arteries observes turns, builds ephemeral/persistent/evergreen memory, and may surface retrieved prompts as visible context.
EOF
'''
    compact = f'''#!/usr/bin/env bash
set -euo pipefail

{env}
format="${{ARTERIES_PACKET_FORMAT:-markdown}}"
message="${{1:-context-pressure}}"
python3 -m arteries.packet --format "$format" --message "$message" --budget "${{ARTERIES_PACKET_BUDGET:-6000}}"
'''
    pi_compact = f'''#!/usr/bin/env bash
set -euo pipefail

{_runtime_env(ctx, "pi")}
python3 -m arteries.packet --format pi-compaction-json --stdin-json --budget "${{ARTERIES_PACKET_BUDGET:-6000}}"
'''
    smoke = '''#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
prompt="${1:-thanks}"

echo '== observe =='
out="$(bash "$script_dir/hooks/generic-observe.sh" "$prompt")"
if [[ -n "$out" ]]; then
  printf '%s\n' "$out"
else
  echo '(no context)'
fi

echo '== compact packet =='
bash "$script_dir/hooks/compact-packet.sh" smoke

echo '== activate =='
bash "$script_dir/hooks/activate.sh"
'''
    files = {
        hooks / "observe.sh": observe,
        hooks / "generic-observe.sh": generic,
        hooks / "activate.sh": activate,
        hooks / "compact-packet.sh": compact,
        hooks / "pi-compact-json.sh": pi_compact,
        _arteries_dir(ctx) / "smoke.sh": smoke,
    }
    for path, body in files.items():
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)


def _remove_runtime(ctx: Context) -> None:
    path = _arteries_dir(ctx)
    if path.exists():
        shutil.rmtree(path)


def _runtime_ok(ctx: Context) -> bool:
    base = _arteries_dir(ctx)
    return all((base / rel).exists() for rel in [
        "config.json",
        "hooks/observe.sh",
        "hooks/generic-observe.sh",
        "hooks/activate.sh",
        "hooks/compact-packet.sh",
        "hooks/pi-compact-json.sh",
        "smoke.sh",
    ])


def _claude_settings_path(ctx: Context) -> Path:
    return ctx.cwd / ".claude" / "settings.local.json"


def _claude_hooks() -> dict:
    return {
        "SessionStart": [{
            "matcher": "startup|resume|clear|compact",
            "hooks": [{
                "type": "command",
                "command": "ARTERIES_CLI=claude bash .arteries/hooks/activate.sh",
                "timeout": 5,
                "statusMessage": "Activating arteries memory...",
            }],
        }],
        "UserPromptSubmit": [{
            "hooks": [{
                "type": "command",
                "command": "ARTERIES_CLI=claude bash .arteries/hooks/generic-observe.sh \"$CLAUDE_USER_PROMPT\"",
                "timeout": 10,
                "statusMessage": "arteries",
            }],
        }],
        "PreCompact": [{
            "matcher": "manual|auto",
            "hooks": [{
                "type": "command",
                "command": "ARTERIES_CLI=claude bash .arteries/hooks/compact-packet.sh claude-precompact",
                "timeout": 10,
                "statusMessage": "Building arteries continuity packet...",
            }],
        }],
        "PostCompact": [{
            "matcher": "manual|auto",
            "hooks": [{
                "type": "command",
                "command": "ARTERIES_CLI=claude bash .arteries/hooks/compact-packet.sh claude-postcompact",
                "timeout": 10,
                "statusMessage": "Recording arteries compact continuity...",
            }],
        }],
    }


def _hook_group_has_command(groups: list[dict], command: str) -> bool:
    for group in groups:
        for hook in group.get("hooks", []):
            if hook.get("command") == command:
                return True
    return False


def _install_claude(ctx: Context) -> Result:
    _ensure_runtime(ctx, ctx.cli_name)
    path = _claude_settings_path(ctx)
    settings = _read_json(path)
    hooks = settings.setdefault("hooks", {})
    wanted = _claude_hooks()
    for event, groups in wanted.items():
        existing = hooks.setdefault(event, [])
        for group in groups:
            command = group["hooks"][0]["command"]
            if not _hook_group_has_command(existing, command):
                existing.append(group)
    _write_json(path, settings)
    return Result(True, "Installed Claude arteries hooks in .claude/settings.local.json.")


def _check_claude(ctx: Context) -> Result:
    if not _runtime_ok(ctx):
        return Result(False, "Missing .arteries runtime files.")
    path = _claude_settings_path(ctx)
    if not path.exists():
        return Result(False, ".claude/settings.local.json not found.")
    settings = _read_json(path)
    hooks = settings.get("hooks", {})
    for event, groups in _claude_hooks().items():
        existing = hooks.get(event, [])
        for group in groups:
            command = group["hooks"][0]["command"]
            if not _hook_group_has_command(existing, command):
                return Result(False, f"Missing Claude hook for {event}: {command}")
    return Result(True, "Claude arteries hooks are installed.")


def _remove_claude(ctx: Context) -> Result:
    path = _claude_settings_path(ctx)
    if path.exists():
        settings = _read_json(path)
        hooks = settings.get("hooks", {})
        commands = {group["hooks"][0]["command"] for groups in _claude_hooks().values() for group in groups}
        for event in list(hooks.keys()):
            hooks[event] = [
                group for group in hooks[event]
                if not any(hook.get("command") in commands for hook in group.get("hooks", []))
            ]
            if not hooks[event]:
                del hooks[event]
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks", None)
        _write_json(path, settings)
    _remove_runtime(ctx)
    return Result(True, "Removed Claude arteries hooks and .arteries runtime.")


def _agents_path(ctx: Context) -> Path:
    return ctx.cwd / "AGENTS.md"


def _codex_config_path(ctx: Context) -> Path:
    return ctx.cwd / ".codex" / "config.toml"


def _codex_agents_section() -> str:
    return f'''{MARKER_START}
## Arteries Memory

At session start, run `ARTERIES_CLI=codex bash .arteries/hooks/activate.sh` to start a new arteries run and load context.
On each user prompt, run `ARTERIES_CLI=codex bash .arteries/hooks/generic-observe.sh "<prompt>"` and use any returned text as additional context.
When context pressure or compaction happens, run `ARTERIES_CLI=codex bash .arteries/hooks/compact-packet.sh codex-compact` and preserve the packet as continuity context.

Arteries observes turns, builds memory, may surface retrieved prompts, and produces compact continuity packets.
{MARKER_END}'''


def _codex_toml_block() -> str:
    return f'''{CODEX_MARKER_START}
experimental_compact_prompt_file = "../.arteries/codex/compact_prompt.txt"

[features]
hooks = true

[[hooks.SessionStart]]
matcher = "startup|resume|clear|compact"

[[hooks.SessionStart.hooks]]
type = "command"
command = "ARTERIES_CLI=codex bash .arteries/hooks/activate.sh"
statusMessage = "Activating arteries memory"

[[hooks.PreCompact]]
matcher = "manual|auto"

[[hooks.PreCompact.hooks]]
type = "command"
command = "ARTERIES_CLI=codex bash .arteries/hooks/compact-packet.sh codex-precompact"
statusMessage = "Building arteries continuity packet"

[[hooks.PostCompact]]
matcher = "manual|auto"

[[hooks.PostCompact.hooks]]
type = "command"
command = "ARTERIES_CLI=codex bash .arteries/hooks/compact-packet.sh codex-postcompact"
statusMessage = "Recording arteries compact continuity"
{CODEX_MARKER_END}'''


def _codex_compact_prompt() -> str:
    return """When compacting this coding session, preserve continuity for Arteries.

Include:
- current user intent and unresolved task state
- recent decisions and constraints
- files read, files modified, commands run, and validation status
- blockers, open questions, and next steps
- any Arteries continuity packet already present

Do not let older memory override explicit current user instructions, developer instructions, system instructions, or repo instructions. Keep the result concise and operational.
"""


def _install_codex(ctx: Context) -> Result:
    _ensure_runtime(ctx, ctx.cli_name)
    _append_marker_block(_agents_path(ctx), _codex_agents_section(), MARKER_START)
    prompt_path = ctx.cwd / ".arteries" / "codex" / "compact_prompt.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(_codex_compact_prompt(), encoding="utf-8")
    _append_marker_block(_codex_config_path(ctx), _codex_toml_block(), CODEX_MARKER_START)
    return Result(True, "Installed Codex arteries integration in AGENTS.md and .codex/config.toml.")


def _check_codex(ctx: Context) -> Result:
    if not _runtime_ok(ctx):
        return Result(False, "Missing .arteries runtime files.")
    agents = _agents_path(ctx)
    if not agents.exists() or MARKER_START not in agents.read_text(encoding="utf-8"):
        return Result(False, "AGENTS.md has no arteries section.")
    toml = _codex_config_path(ctx)
    if not toml.exists() or CODEX_MARKER_START not in toml.read_text(encoding="utf-8"):
        return Result(False, ".codex/config.toml has no arteries hook block.")
    if not (ctx.cwd / ".arteries" / "codex" / "compact_prompt.txt").exists():
        return Result(False, "Missing Codex compact prompt file.")
    return Result(True, "Codex arteries integration is installed.")


def _remove_codex(ctx: Context) -> Result:
    _remove_marker_block(_agents_path(ctx), MARKER_START, MARKER_END)
    _remove_marker_block(_codex_config_path(ctx), CODEX_MARKER_START, CODEX_MARKER_END)
    _remove_runtime(ctx)
    return Result(True, "Removed Codex arteries integration and .arteries runtime.")


def _pi_extension_path(ctx: Context) -> Path:
    return ctx.cwd / ".pi" / "extensions" / "arteries.ts"


def _pi_extension() -> str:
    return '''// Arteries Pi extension scaffold.
// Add this extension to Pi, or copy the handler into your Pi extension bundle.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFileSync } from "node:child_process";

export default function arteries(pi: ExtensionAPI) {
  pi.on("session_before_compact", async (event) => {
    const result = execFileSync("bash", [".arteries/hooks/pi-compact-json.sh"], {
      input: JSON.stringify(event),
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
    });
    const packet = JSON.parse(result);
    return {
      compaction: {
        summary: packet.summary,
        firstKeptEntryId: event.preparation.firstKeptEntryId,
        tokensBefore: event.preparation.tokensBefore,
        details: packet.details,
      },
    };
  });
}
'''


def _install_pi(ctx: Context) -> Result:
    _ensure_runtime(ctx, ctx.cli_name)
    path = _pi_extension_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_pi_extension(), encoding="utf-8")
    return Result(True, "Installed Pi arteries compaction extension in .pi/extensions/arteries.ts.")


def _check_pi(ctx: Context) -> Result:
    if not _runtime_ok(ctx):
        return Result(False, "Missing .arteries runtime files.")
    if not _pi_extension_path(ctx).exists():
        return Result(False, "Missing Pi arteries extension.")
    return Result(True, "Pi arteries compaction extension is installed.")


def _remove_pi(ctx: Context) -> Result:
    extension = _pi_extension_path(ctx)
    if extension.exists():
        extension.unlink()
    for directory in (ctx.cwd / ".pi" / "extensions", ctx.cwd / ".pi"):
        try:
            directory.rmdir()
        except OSError:
            pass
    _remove_runtime(ctx)
    return Result(True, "Removed Pi arteries integration and .arteries runtime.")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _append_marker_block(path: Path, block: str, start: str) -> None:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    if start in current:
        end = MARKER_END if start == MARKER_START else CODEX_MARKER_END
        start_idx = current.find(start)
        end_idx = current.find(end, start_idx)
        if end_idx != -1:
            end_idx += len(end)
            updated = current[:start_idx].rstrip() + "\n\n" + block + current[end_idx:]
            path.write_text(updated.strip() + "\n", encoding="utf-8")
            return
    sep = "\n\n" if current.strip() else ""
    path.write_text(current.rstrip() + sep + block + "\n", encoding="utf-8")


def _remove_marker_block(path: Path, start: str, end: str) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    start_idx = text.find(start)
    if start_idx == -1:
        return
    end_idx = text.find(end, start_idx)
    if end_idx == -1:
        return
    end_idx += len(end)
    after_newline = text.find("\n", end_idx)
    if after_newline != -1:
        end_idx = after_newline + 1
    cleaned = (text[:start_idx] + text[end_idx:]).replace("\n\n\n", "\n\n").rstrip()
    path.write_text((cleaned + "\n") if cleaned else "", encoding="utf-8")


RECIPES = {
    "pi": {"install": _install_pi, "check": _check_pi, "remove": _remove_pi},
    "codex": {"install": _install_codex, "check": _check_codex, "remove": _remove_codex},
    "claude": {"install": _install_claude, "check": _check_claude, "remove": _remove_claude},
}


if __name__ == "__main__":
    raise SystemExit(main())
