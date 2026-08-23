"""Token usage for an interactive CLI turn, read from the session transcript.

Both CLIs already record what every API call cost them; nobody was reading it,
so the whole stack's cost view showed $0.00 while real sessions ran into tens of
dollars. This module turns those transcripts into the one shape the rest of the
stack uses, and reports each call exactly once.

The stack convention, set by heart's runner: `tokens_in` is *uncached* input,
and cache traffic lives in its own buckets priced off the input rate. The two
vendors disagree about this, in opposite directions, so both are converted here:

  Claude    `input_tokens` already excludes cache; the cache counts are separate
            and additive. Pass through — but the transcript writes one record
            per *content block*, so a message with text plus three tool calls
            appears four times carrying the same usage. Dedupe by message id or
            overstate by ~1.8x.

  Codex     `input_tokens` INCLUDES `cached_input_tokens` (their own arithmetic
            proves it: input + output == total_tokens exactly). Carve the subset
            out, or bill cached tokens at ten times their rate.

Reporting once is the other half. Transcripts are append-only and re-read every
turn, so state is kept per file: a byte offset for Claude (only new lines are
parsed) and the last cumulative total for Codex (whose `total_token_usage` is
running, so the delta is the turn). Without that, every turn re-reports the
whole session and the dashboard compounds.

Silent by design: a missing transcript, a partial line mid-write, or an
unrecognised shape yields no usage rather than an exception. Cost telemetry must
never be able to break a turn.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# what a caller gets back; also the payload keys heart and plexus already price
USAGE_KEYS = ("tokens_in", "tokens_out", "cache_read",
              "cache_write_5m", "cache_write_1h")


def reported_usage(source: dict | None = None) -> dict:
    """Counts the caller handed us, via ARTERIES_USAGE_* env vars.

    The escape hatch for CLIs whose transcripts this module cannot read. Values
    that are missing, non-numeric, or negative are dropped rather than coerced —
    a garbled count is worse than no count, because it prices as if it were real.

    ARTERIES_USAGE_MODEL rides along as a string. It is not a count, so it does
    not gate on `> 0` and does not on its own make a turn "measured".
    """
    env = os.environ if source is None else source
    out = {}
    for key in USAGE_KEYS:
        raw = env.get(f"ARTERIES_USAGE_{key.upper()}")
        if raw is None:
            continue
        try:
            value = int(str(raw).strip())
        except ValueError:
            continue
        if value > 0:
            out[key] = value
    for key, limit in (("model", 120), ("speed", 32)):
        value = (env.get(f"ARTERIES_USAGE_{key.upper()}") or "").strip()
        if value and out:
            out[key] = value[:limit]
    return out


def _state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME",
                               Path.home() / ".local" / "state")) / "arteries"
    return base / "usage-offsets.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))
    except OSError:
        pass


def _claude_usage(records: list[dict]) -> dict:
    """Sum Claude assistant-message usage, one entry per API call.

    Also returns the model under the "model" key — a token count nobody can
    price is only half the measurement, and a session can switch models
    mid-flight, so the last one seen in this chunk wins."""
    seen: set[str] = set()
    out = dict.fromkeys(USAGE_KEYS, 0)
    for d in records:
        message = d.get("message") or {}
        usage = message.get("usage") or d.get("usage")
        if not isinstance(usage, dict):
            continue
        if message.get("model"):
            out["model"] = str(message["model"])[:120]
        # Fast mode bills Opus at 2x. It is reported inside `usage`, not on the
        # message, and only ever as a string — so a turn that ran fast is
        # indistinguishable from a standard one unless this is carried through,
        # and the whole session under-reports by half with nothing to show it.
        if usage.get("speed"):
            out["speed"] = str(usage["speed"])[:32]
        # message id first: it is the API call's own identity, so the several
        # transcript lines a single response produces collapse to one
        key = message.get("id") or d.get("requestId") or d.get("uuid")
        if not key or key in seen:
            continue
        seen.add(key)
        out["tokens_in"] += usage.get("input_tokens") or 0
        out["tokens_out"] += usage.get("output_tokens") or 0
        out["cache_read"] += usage.get("cache_read_input_tokens") or 0
        creation = usage.get("cache_creation") or {}
        write_5m = creation.get("ephemeral_5m_input_tokens")
        write_1h = creation.get("ephemeral_1h_input_tokens")
        if write_5m is None and write_1h is None:
            # older CLI reports only the flat total; treat as the 5m tier, which
            # is both the common case and the cheaper of the two
            write_5m = usage.get("cache_creation_input_tokens") or 0
            write_1h = 0
        out["cache_write_5m"] += write_5m or 0
        out["cache_write_1h"] += write_1h or 0
    return out


def _codex_total(records: list[dict]) -> dict | None:
    """The newest cumulative total in a Codex rollout, normalised.

    Cumulative, not per-turn: `token_count` events fire more than once per turn
    with repeating `last_token_usage`, so summing those double-counts. The
    running total is monotonic and the caller differences it instead."""
    latest = None
    for d in records:
        payload = d.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
            latest = info["total_token_usage"]
    if latest is None:
        return None
    cached = latest.get("cached_input_tokens") or 0
    return {
        # the subset carve-out: their input already contains the cached tokens
        "tokens_in": max(0, (latest.get("input_tokens") or 0) - cached),
        "tokens_out": latest.get("output_tokens") or 0,
        "cache_read": cached,
        "cache_write_5m": 0,  # Codex reports no write tier
        "cache_write_1h": 0,
    }


def _codex_model(records: list[dict]) -> str | None:
    """The model Codex is currently running, from `thread_settings_applied`.

    Codex puts no model on its `token_count` records — the only place it appears
    is the settings record, which is re-emitted whenever the setting changes.
    Last one in the chunk wins, and turns that switch model mid-session
    (gpt-5.6-sol -> gpt-5.6-terra happens in real rollouts) price correctly.
    """
    model = None
    for d in records:
        payload = d.get("payload") or {}
        if payload.get("type") != "thread_settings_applied":
            continue
        value = (payload.get("thread_settings") or {}).get("model")
        if value:
            model = str(value)[:120]
    return model


def _codex_rollout(cwd: str | None, limit: int = 40) -> str | None:
    """The Codex rollout file for this working directory, newest first.

    Codex's hook payload carries only the prompt — no transcript path — so
    unlike Claude there is nothing to hand us and the file has to be found.
    Rollouts open with a `session_meta` record naming their `cwd`, which is
    enough to pick the right one when several sessions are open at once.

    ponytail: newest-matching-cwd, scanning the 40 most recent files. Two Codex
    sessions in the *same* directory would both match and the newer wins; the
    state file is keyed by path so nothing double-counts, but the older
    session's turns would be attributed late. Match on session id instead if
    Codex ever puts one in the hook payload."""
    base = Path.home() / ".codex" / "sessions"
    if not base.is_dir():
        return None
    try:
        files = sorted(base.rglob("rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return None
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.readline()
        except OSError:
            continue
        try:
            meta = (json.loads(head).get("payload") or {})
        except json.JSONDecodeError:
            continue
        if meta.get("type") != "session_meta" and "cwd" not in meta:
            continue
        if not cwd or meta.get("cwd") == cwd:
            return str(path)
    return None


def turn_usage(transcript: str | None = None) -> dict:
    """Usage new since the last call, normalised. Empty dict when there is none.

    Empty rather than zeros on purpose: a caller stamping this onto an event
    should record nothing when nothing was measured, so "no data" stays
    distinguishable from "no tokens"."""
    reported = reported_usage()
    if reported:
        # a host that knows its own numbers beats parsing: only claude and codex
        # transcripts are understood here, so every other CLI would otherwise
        # record turns at zero and read as free on the cost panel
        return {**reported, "usage_source": "reported"}
    transcript = transcript or os.environ.get("ARTERIES_TRANSCRIPT")
    if not transcript and os.environ.get("ARTERIES_CLI") == "codex":
        transcript = _codex_rollout(
            os.environ.get("ARTERIES_EVENT_CWD") or os.getcwd())
    if not transcript:
        # "we had no way to measure this turn", which is a different fact from
        # "this turn was free". Without the distinction a CLI with no adapter is
        # indistinguishable on the cost panel from one that genuinely cost
        # nothing, and the gap stays invisible for months.
        return {"usage_source": "unavailable"}
    try:
        path = Path(transcript)
        state = _load_state()
        entry = dict(state.get(str(path)) or {})
        offset = int(entry.get("offset") or 0)
        size = path.stat().st_size
        if size < offset:
            offset = 0  # file replaced or truncated; re-read from the top
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
    except OSError:
        return {"usage_source": "unavailable"}  # named a transcript we can't read

    # a transcript being written right now can end mid-line; leave the remainder
    # unconsumed so the next turn sees it whole rather than dropping a call
    lines = chunk.split("\n")
    if chunk and not chunk.endswith("\n"):
        remainder = lines.pop()
        new_offset -= len(remainder.encode("utf-8", errors="replace"))

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    usage = _claude_usage(records)
    source = "claude_transcript" if usage.get("model") else None
    codex_now = _codex_total(records)
    if codex_now is not None:
        source = "codex_rollout"
        prior = entry.get("codex_total") or {}
        # difference the running total; a lower value means a new session wrote
        # to the same path, so the newest total is itself the delta
        for key in USAGE_KEYS:
            delta = codex_now[key] - int(prior.get(key) or 0)
            usage[key] += delta if delta >= 0 else codex_now[key]
        entry["codex_total"] = codex_now
        usage["model"] = _codex_model(records) or entry.get("model") or ""

    # remember the model across chunks: a turn whose new lines happen to carry
    # no settings/assistant record still ran on the same model as the last one
    if usage.get("model"):
        entry["model"] = usage["model"]
    elif entry.get("model"):
        usage["model"] = entry["model"]

    entry["offset"] = new_offset
    state[str(path)] = entry
    _save_state(state)

    counts = {k: v for k, v in usage.items() if k in USAGE_KEYS and v}
    if not counts:
        # nothing new in the transcript — this hook fired on a turn that made no
        # API call yet. Not "unavailable": the parser works, there is just
        # nothing to report, and a later turn will pick the tokens up.
        return {}
    out = {**counts, "usage_source": source or "transcript"}
    for key in ("model", "speed"):
        if usage.get(key):
            out[key] = usage[key]
    return out
