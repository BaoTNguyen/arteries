# Install arteries integrations into agent CLI projects.

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

MARKER_START = "<!-- arteries:start -->"
MARKER_END = "<!-- arteries:end -->"
CODEX_MARKER_START = "# arteries:start - managed by `art setup codex`"
CODEX_MARKER_END = "# arteries:end"
PROVIDERS = ("generic", "claude", "codex")


@dataclass
class Result:
    success: bool
    message: str


@dataclass
class Context:
    cwd: Path
    arteries_root: Path
    project_name: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install arteries into agent CLI projects.")
    parser.add_argument("provider", nargs="?", choices=PROVIDERS)
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="target repo directory")
    parser.add_argument("--arteries-root", type=Path, default=_default_arteries_root())
    parser.add_argument("--project", help="ARTERIES_PROJECT value; defaults to repo directory name")
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
    ctx = Context(
        cwd=cwd,
        arteries_root=args.arteries_root.resolve(),
        project_name=args.project or cwd.name,
    )

    action = "check" if args.check else "remove" if args.remove else "install"
    result = RECIPES[args.provider][action](ctx)
    print(("OK: " if result.success else "ERROR: ") + result.message)
    return 0 if result.success else 1


def _default_arteries_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _agent_id(project_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in project_name.lower())
    return f"{cleaned}-hook"


def _arteries_dir(ctx: Context) -> Path:
    return ctx.cwd / ".arteries"


def _ensure_runtime(ctx: Context, cli_name: str) -> None:
    hooks = _arteries_dir(ctx) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    config = {
        "arteries_root": str(ctx.arteries_root),
        "project_root": str(ctx.cwd),
        "project": ctx.project_name,
        "agent_id": _agent_id(ctx.project_name),
        "cli": cli_name,
    }
    (_arteries_dir(ctx) / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    observe = f'''#!/usr/bin/env bash
set -euo pipefail

ARTERIES_ROOT="${{ARTERIES_ROOT:-{ctx.arteries_root}}}"
PROJECT_ROOT="${{PROJECT_ROOT:-{ctx.cwd}}}"
export PYTHONPATH="$ARTERIES_ROOT/src:$PROJECT_ROOT/src:${{PYTHONPATH:-}}"
export ARTERIES_PROJECT="${{ARTERIES_PROJECT:-{ctx.project_name}}}"
export ARTERIES_AGENT_ID="${{ARTERIES_AGENT_ID:-{_agent_id(ctx.project_name)}}}"
export ARTERIES_CLI="${{ARTERIES_CLI:-{cli_name}}}"
export ARTERIES_REPO="${{ARTERIES_REPO:-$PROJECT_ROOT}}"

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

ARTERIES_ROOT="${{ARTERIES_ROOT:-{ctx.arteries_root}}}"
PROJECT_ROOT="${{PROJECT_ROOT:-{ctx.cwd}}}"
export PYTHONPATH="$ARTERIES_ROOT/src:$PROJECT_ROOT/src:${{PYTHONPATH:-}}"
export ARTERIES_PROJECT="${{ARTERIES_PROJECT:-{ctx.project_name}}}"
export ARTERIES_AGENT_ID="${{ARTERIES_AGENT_ID:-{_agent_id(ctx.project_name)}}}"
export ARTERIES_CLI="${{ARTERIES_CLI:-{cli_name}}}"
export ARTERIES_REPO="${{ARTERIES_REPO:-$PROJECT_ROOT}}"

python3 -m arteries.runs start --project "$ARTERIES_PROJECT" --agent "$ARTERIES_AGENT_ID" --cli "$ARTERIES_CLI" --repo "$ARTERIES_REPO" >/dev/null 2>&1 || true

cat <<'EOF'
ARTERIES MEMORY SYSTEM ACTIVE.

This repo is connected to arteries project `{ctx.project_name}`.
Arteries observes turns, builds ephemeral/persistent/evergreen memory, and may surface retrieved prompts as visible context.
EOF
'''
    smoke = '#!/usr/bin/env bash\nset -euo pipefail\n\nscript_dir="$(cd "$(dirname "$0")" && pwd)"\nprompt="${1:-thanks}"\n\necho \'== generic observe ==\'\nout="$(bash "$script_dir/hooks/generic-observe.sh" "$prompt")"\nif [[ -n "$out" ]]; then\n  printf \'%s\n\' "$out"\nelse\n  echo \'(no generic context)\'\nfi\n\necho \'== activate ==\'\nbash "$script_dir/hooks/activate.sh"\n'
    files = {
        hooks / "observe.sh": observe,
        hooks / "generic-observe.sh": generic,
        hooks / "activate.sh": activate,
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
        "smoke.sh",
    ])


def _install_generic(ctx: Context) -> Result:
    _ensure_runtime(ctx, "generic")
    return Result(True, "Installed generic arteries runtime in .arteries/.")


def _check_generic(ctx: Context) -> Result:
    if _runtime_ok(ctx):
        return Result(True, "Generic arteries runtime is installed.")
    return Result(False, "Missing .arteries runtime files.")


def _remove_generic(ctx: Context) -> Result:
    _remove_runtime(ctx)
    return Result(True, "Removed .arteries runtime files.")


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
    }


def _hook_group_has_command(groups: list[dict], command: str) -> bool:
    for group in groups:
        for hook in group.get("hooks", []):
            if hook.get("command") == command:
                return True
    return False


def _install_claude(ctx: Context) -> Result:
    _ensure_runtime(ctx, "claude")
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

Arteries observes turns, builds memory, and may surface retrieved prompts.
{MARKER_END}'''


def _codex_toml_block() -> str:
    return f'''{CODEX_MARKER_START}
[features]
codex_hooks = true

[[hooks.SessionStart]]

[[hooks.SessionStart.hooks]]
type = "command"
command = "ARTERIES_CLI=codex bash .arteries/hooks/activate.sh"
statusMessage = "Activating arteries memory"
{CODEX_MARKER_END}'''


def _install_codex(ctx: Context) -> Result:
    _ensure_runtime(ctx, "codex")
    _append_marker_block(_agents_path(ctx), _codex_agents_section(), MARKER_START)
    toml = _codex_config_path(ctx)
    _append_marker_block(toml, _codex_toml_block(), CODEX_MARKER_START)
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
    return Result(True, "Codex arteries integration is installed.")


def _remove_codex(ctx: Context) -> Result:
    _remove_marker_block(_agents_path(ctx), MARKER_START, MARKER_END)
    _remove_marker_block(_codex_config_path(ctx), CODEX_MARKER_START, CODEX_MARKER_END)
    _remove_runtime(ctx)
    return Result(True, "Removed Codex arteries integration and .arteries runtime.")


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
    "generic": {"install": _install_generic, "check": _check_generic, "remove": _remove_generic},
    "claude": {"install": _install_claude, "check": _check_claude, "remove": _remove_claude},
    "codex": {"install": _install_codex, "check": _check_codex, "remove": _remove_codex},
}


if __name__ == "__main__":
    raise SystemExit(main())
