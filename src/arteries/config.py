"""Arteries configuration. Shares the capillaries Postgres instance."""

from __future__ import annotations

import os

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "/var/run/postgresql"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "database": os.getenv("DB_NAME", "capillaries"),
    "user":     os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
}

EMBED_URL = os.getenv("EMBED_URL", "http://127.0.0.1:8003/v1/embeddings")
EMBED_MODEL = os.getenv("EMBED_MODEL", "snowflake-arctic-embed-m-v2.0")
GENERATE_URL = os.getenv("GENERATE_URL", "http://127.0.0.1:8001/v1/chat/completions")
COMPILE_MODEL = os.getenv("ARTERIES_COMPILE_MODEL", "qwen3.6-27b")
EMBED_DIM = 768

PROJECT_ID = os.getenv("ARTERIES_PROJECT", "default")
AGENT_PROCESS_ID = os.getenv("ARTERIES_AGENT_ID", str(os.getpid()))
# both names accepted: cli_normalize/hooks set ARTERIES_PARENT_AGENT_ID
PARENT_AGENT_ID = os.getenv("ARTERIES_PARENT_AGENT_ID") or os.getenv("ARTERIES_PARENT_AGENT") or None

# --- Memory isolation presets ---
# subagent: writes ephemeral tagged with parent, compiled at higher bar
_PRESETS = {
    "readonly": {"ARTERIES_PERSISTENT_READ": "relevance", "ARTERIES_EPHEMERAL": "discard"},
    "clean":    {"ARTERIES_PERSISTENT_READ": "none",      "ARTERIES_EPHEMERAL": "discard"},
    "subagent": {"ARTERIES_PERSISTENT_READ": "relevance", "ARTERIES_EPHEMERAL": "compile"},
}

_preset = os.getenv("ARTERIES_MEMORY", "").lower()
if _preset in _PRESETS:
    for _k, _v in _PRESETS[_preset].items():
        os.environ.setdefault(_k, _v)

# compile  write ephemeral, promote to persistent in the background (default)
# keep     write ephemeral, never promote — `art remember` and `art compile` are
#          the deliberate paths up. For interactive sessions, where compiling
#          every assistant reply turned six days of work into 178 permanent
#          "memories" that were mostly narration of what had just been said.
# discard  never write ephemeral at all; an in-process buffer serves this turn only
_EPHEMERAL_MODES = ("compile", "keep", "discard")
EPHEMERAL_MODE = os.getenv("ARTERIES_EPHEMERAL", "compile").strip().lower()
if EPHEMERAL_MODE not in _EPHEMERAL_MODES:
    # an unrecognised value must not silently mean "stop remembering"
    EPHEMERAL_MODE = "compile"
PERSISTENT_READ = os.getenv("ARTERIES_PERSISTENT_READ", "relevance")
RELEVANCE_THRESHOLD = float(os.getenv("ARTERIES_RELEVANCE_THRESHOLD", "0.3"))
