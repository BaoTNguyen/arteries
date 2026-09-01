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

GENERATE_URL = os.getenv("GENERATE_URL", "http://127.0.0.1:8001/v1/chat/completions")
COMPILE_MODEL = os.getenv("ARTERIES_COMPILE_MODEL", "qwen3.6-27b")

# Optional vision endpoint for describing images at ingest time. Defaults to the
# generation server, which only answers if it was started with --mmproj; without
# one, image ingestion asks for a description instead of guessing at the picture.
VISION_URL = os.getenv("ARTERIES_VISION_URL", GENERATE_URL)
VISION_MODEL = os.getenv("ARTERIES_VISION_MODEL", COMPILE_MODEL)

# Images embedded in a document are described by a frontier model. They are rare
# -- a handful per plan -- and the description becomes permanent memory, so this
# is the one place where paying for accuracy beats keeping everything local.
# Set to "" to disable and fall back to the local endpoint or a manual caption.
FRONTIER_VISION_MODEL = os.getenv("ARTERIES_FRONTIER_VISION_MODEL", "claude-opus-5")

# The embedding config is a shared contract, not an arteries setting: both
# projects write vectors into the same Postgres and read each other's columns.
# Prefer capillaries' values so the two ends can't drift -- the same reason
# extract.py prefers its DOMAIN_KEYWORDS. They already drifted once, silently:
# capillaries moved to Qwen3-Embedding-0.6B at 1024 dims while arteries stayed
# on arctic-embed at 768, so every embedding write would have failed on a
# dimension mismatch the moment compilation started working.
try:
    from capillaries.config.paths import (  # noqa: F401
        EMBED_DIM,
        EMBED_MODEL,
        EMBED_URL,
        QUERY_PREFIX,
    )
except Exception:  # capillaries not installed / not importable
    EMBED_URL = os.getenv("EMBED_URL", "http://127.0.0.1:8003/v1/embeddings")
    EMBED_MODEL = os.getenv("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
    EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))
    QUERY_PREFIX = os.getenv("EMBED_QUERY_PREFIX", "")

# Falls back to the repo directory name, never the string "default".
# runlog._project() already resolves that way, so an unset ARTERIES_PROJECT
# split a single turn across two identities: agent_events under "arteries" and
# every memory write under "default". The event log looked healthy throughout,
# which is why it went unnoticed -- 46 ephemeral rows, 11 edges and 19
# retrievals landed under a project nobody registered.
PROJECT_ID = os.getenv("ARTERIES_PROJECT") or os.path.basename(
    os.getenv("ARTERIES_REPO") or os.getcwd()) or "default"
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
# Cosine floor for a persistent memory to be eligible at all. The old 0.3 was
# inherited from capillaries' SINGLE_THRESHOLD, which capillaries itself retired
# in favour of a cross-encoder -- and it was never calibrated here, because zero
# rows have ever carried an embedding. It is also model-specific, and the model
# changed. 0.0 admits everything; packet.py now does the real filtering by
# score. Set this once a few weeks of real memories show a usable distribution.
RELEVANCE_THRESHOLD = float(os.getenv("ARTERIES_RELEVANCE_THRESHOLD", "0.0"))

# Minimum rerank confidence before a retrieved prompt is injected into the
# agent's context. Measured against the reembedded corpus: planner-style turns
# score ~0.98, while implementation turns land in 0.01-0.56 with no correlation
# to usefulness -- at 0.3 an episode writing a 20-line string function was
# served "Delegate Like a Parallel Coworker" at 0.53. 0.6 keeps the former and
# drops the latter. Lower it once the corpus covers implementation work.
RETRIEVAL_MIN_CONFIDENCE = float(os.getenv("ARTERIES_RETRIEVAL_MIN_CONFIDENCE", "0.6"))
