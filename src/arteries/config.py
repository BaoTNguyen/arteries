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

EPHEMERAL_MODE = os.getenv("ARTERIES_EPHEMERAL", "compile")
PERSISTENT_READ = os.getenv("ARTERIES_PERSISTENT_READ", "relevance")
RELEVANCE_THRESHOLD = float(os.getenv("ARTERIES_RELEVANCE_THRESHOLD", "0.3"))

# Minimum rerank confidence before a retrieved prompt is injected into the
# agent's context. Measured against the reembedded corpus: planner-style turns
# score ~0.98, while implementation turns land in 0.01-0.56 with no correlation
# to usefulness -- at 0.3 an episode writing a 20-line string function was
# served "Delegate Like a Parallel Coworker" at 0.53. 0.6 keeps the former and
# drops the latter. Lower it once the corpus covers implementation work.
RETRIEVAL_MIN_CONFIDENCE = float(os.getenv("ARTERIES_RETRIEVAL_MIN_CONFIDENCE", "0.6"))
