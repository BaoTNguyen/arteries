"""Decision/action ledger: what was available, what was chosen, what it cost.

Events (runlog) record what happened. Decisions record the counterfactual —
the available actions, the choice, and its cost — which is what RL credit
assignment needs. Postgres when available, repo-local JSONL fallback
(.arteries/decisions/), always teed to the heart event spine.

Episode/task identity arrives via env (set by heart per episode):
    ARTERIES_EPISODE_ID
    ARTERIES_TASK_ID
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

from arteries import runlog
from arteries.config import AGENT_PROCESS_ID, DB_CONFIG, PROJECT_ID
from arteries.journal import journal_append


def episode_id() -> str | None:
    return os.getenv("ARTERIES_EPISODE_ID") or None


def task_id() -> str | None:
    return os.getenv("ARTERIES_TASK_ID") or None


def log_decision(
    decision_type: str,
    chosen_action: str,
    available_actions: list[str],
    *,
    observation: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    turn_id: str | None = None,
    repo_path: str | Path | None = None,
) -> dict[str, Any]:
    run = _run(repo_path)
    record = {
        "id": str(uuid.uuid4()),
        "episode_id": episode_id(),
        "run_id": run["run_id"],
        "turn_id": turn_id,
        "project_id": run["project_id"],
        "agent_id": run["agent_id"],
        "decision_type": decision_type,
        "observation": observation or {},
        "available_actions": available_actions,
        "chosen_action": chosen_action,
        "cost": cost or {},
        "metadata": {**(metadata or {}), **({"task_id": task_id()} if task_id() else {})},
        "created_at": _now_iso(),
    }
    store = _persist(record, "decision", run, repo_path)
    journal_append(
        "arteries",
        f"decision.{decision_type}",
        turn_id=turn_id,
        chosen=chosen_action,
        available=available_actions,
        store=store,
        **({"cost": cost} if cost else {}),
    )
    return record


def log_reward(
    reward_type: str,
    value: float,
    *,
    components: dict[str, Any] | None = None,
    source: str = "arteries",
    decision_id: str | None = None,
    turn_id: str | None = None,
    repo_path: str | Path | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    run = _run(repo_path)
    record = {
        "id": str(uuid.uuid4()),
        "episode_id": episode_id(),
        "decision_id": decision_id,
        "run_id": run["run_id"],
        "project_id": run["project_id"],
        "reward_type": reward_type,
        "value": float(value),
        "components": components or {},
        "source": source,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "created_at": _now_iso(),
    }
    store = _persist(record, "reward", run, repo_path)
    journal_append(
        "arteries", f"reward.{reward_type}", turn_id=turn_id,
        value=value, reward_source=source, store=store,
    )
    return record


def recent_decisions(
    project_id: str | None = None,
    limit: int = 25,
    episode: str | None = None,
    repo_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    project = project_id or os.getenv("ARTERIES_PROJECT") or PROJECT_ID
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            where, params = "project_id = %s", [project]
            if episode:
                where, params = "episode_id = %s", [episode]
            cur.execute(
                f"""
                SELECT id, episode_id, run_id, turn_id, project_id, decision_type,
                       observation, available_actions, chosen_action, cost, metadata, created_at
                FROM arteries.decisions
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (*params, limit),
            )
            return [runlog._json_ready(dict(row)) for row in cur.fetchall()]
    except Exception:
        return _recent_jsonl(project if not episode else None, episode, limit, repo_path)


def _corpus_feedback(episode: dict) -> None:
    """Tell capillaries how the prompt it suggested turned out.

    Here rather than in heart for the same reason the gate is: the direction is
    capillaries -> arteries -> heart, and heart reaching back around arteries to
    report on a component it should not know about inverts that. This function
    already has the episode's outcome and reward in hand.

    Arteries had the whole loop for its own memory -- situation out, reward back
    through this ingest, joined on episode_id. Capillaries heard the question
    and never the answer, so its relevance signal could not exist at all. It is
    deliberately not the same signal as the episode reward (they are joined,
    neither replaces the other), but it needs the outcome to be computed from.

    Best-effort: retrieval feedback must never be what fails a finished episode.
    """
    outcome = episode.get("outcome")
    total = (episode.get("reward") or {}).get("total")
    for packet in episode.get("context_packets") or []:
        trace_id = (packet.get("corpus") or {}).get("trace_id")
        if not trace_id or not outcome:
            continue
        body = {"trace_id": trace_id, "outcome": outcome,
                "notes": f"heart role={packet.get('role')}"}
        if total is not None:
            body["quality_score"] = max(0.0, min(1.0, float(total)))
        _corpus_feedback_post(body)


def _corpus_feedback_post(body: dict) -> None:
    """Post to the daemon rather than calling FeedbackHandler directly.

    The handler wants `mode`, `prompt_id` and `skill_id` -- internals the API
    layer resolves from the trace. POST /agent/feedback needs only trace_id and
    outcome, so it is the contract that does not break when those internals
    move.
    """
    import urllib.request

    url = os.getenv("CAPILLARIES_URL", "http://127.0.0.1:8000") + "/agent/feedback"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).close()
    except Exception:
        pass


def ingest_episodes(source: str | Path | None = None, repo_path: str | Path | None = None) -> int:
    """Backfill the rewards table from episode records.

    This is the credit-assignment bridge: heart and marrow are stdlib-only and
    never talk to Postgres, so decisions get their episode reward here.

    `source` may be a JSONL path, a directory of `*/episode.json`, or None to
    read JSONL from stdin. **Prefer stdin.** Arteries defines the record shape
    and the sender pipes it in; reaching into another repo's directory layout
    couples this to whichever of them happens to own the filesystem this week,
    and RL traffic is moving to marrow.

    An episode whose reward total is null is skipped, not scored zero. Heart
    writes null on purpose for `blocked`, `unverified` and `scope_denied`, and a
    zero would assert those episodes did badly rather than that nothing measured
    them. Each skip is journalled as `reward.unscored` and counted on stderr, so
    a run that scores nothing says so instead of looking like a quiet success.
    """
    if source is None:
        episodes = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
    else:
        path = Path(source)
        if path.is_dir():
            episodes = [json.loads(p.read_text()) for p in sorted(path.glob("*/episode.json"))]
        else:
            episodes = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    ingested: set = set()
    recorded: set = set()
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT episode_id FROM arteries.rewards WHERE reward_type = 'episode'")
            ingested = {row[0] for row in cur.fetchall()}
            # one query rather than an exists-check per episode: a runs
            # directory is hundreds of episodes and this pass reads all of them
            cur.execute("""SELECT episode_id FROM arteries.persistent
                            WHERE kind = 'episode' AND episode_id IS NOT NULL
                              AND valid_until IS NULL""")
            recorded = {row[0] for row in cur.fetchall()}
    except Exception:
        pass  # ponytail: no dedup in jsonl-fallback mode; re-ingest after db is back

    count = skipped = 0
    unscored: dict[str, int] = {}
    _closed: list[tuple[str, str | None, float | None]] = []
    saved = {k: os.environ.get(k) for k in ("ARTERIES_EPISODE_ID", "ARTERIES_TASK_ID")}
    try:
        for ep in episodes:
            if not ep.get("episode_id"):
                continue
            # Before the reward skip, not after. An episode whose reward is
            # already ingested still needs its record written once -- otherwise
            # only episodes new to this pass ever got one, and the entire
            # history stayed invisible to memory.
            if ep["episode_id"] not in recorded:
                record_episode(ep)
                recorded.add(ep["episode_id"])
            if ep["episode_id"] in ingested:
                continue
            os.environ["ARTERIES_EPISODE_ID"] = ep["episode_id"]
            os.environ["ARTERIES_TASK_ID"] = ep.get("task_id") or ""
            usage = ep.get("usage") or {}
            reward = ep.get("reward") or {}
            total = reward.get("total")
            if total is None:
                # Unscored is not zero, and the difference is the whole reason
                # heart writes null here: `blocked` means the agent declined to
                # guess, `unverified` means nothing ran a check, `scope_denied`
                # means the sandbox refused writes the spec allowed. Recording
                # 0.0 for any of them teaches the model a failure that never
                # happened -- and blaming it for a mount table drawn too tight
                # is the worst of the three.
                #
                # It is also not something to swallow. `.get("total", 0.0)` used
                # to default only on a MISSING key, so a present null reached
                # float() and raised -- which aborted the loop, leaving every
                # later episode in the directory permanently unread.
                unscored[ep.get("outcome") or "unknown"] = 1 + unscored.get(
                    ep.get("outcome") or "unknown", 0)
                skipped += 1
                _closed.append((ep["episode_id"], ep.get("outcome"), None))
                _corpus_feedback(ep)  # capillaries still wants the outcome
                continue
            log_reward(
                "episode",
                total,
                components={**(reward.get("components") or {}),
                            "outcome": ep.get("outcome")},
                source="heart",
                repo_path=repo_path,
                tokens_in=usage.get("tokens_in"),
                tokens_out=usage.get("tokens_out"),
                cost_usd=usage.get("cost_usd"),
            )
            _corpus_feedback(ep)
            _closed.append((ep["episode_id"], ep.get("outcome"), total))
            count += 1
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    close_episodes(_closed)
    if skipped:
        # one line, because silence here is how you fail to notice that most of
        # a week's episodes carried no score at all
        print(f"skipped {skipped} unscored episode(s)", file=sys.stderr)
        # One event for the run, not one per episode. It used to be per episode,
        # and an unscored episode can never become scored -- so a backlog of 29
        # re-journalled itself on every invocation and grew the day's journal to
        # 344 `reward.unscored` events out of ~700. O(runs x backlog) for a fact
        # that does not change. The tally keeps what the event was for.
        journal_append("arteries", "reward.unscored", reward_source="heart",
                       episodes=skipped, outcomes=unscored)
    return count


def episode_fact(ep: dict) -> str:
    """One line of what an episode did. Mechanical only.

    Every clause is read off episode.json -- outcome, reward, which verifiers
    ran and what they said, how big the diff was, which agent. No summary of
    the agent's reasoning and no judgment about whether the work was good: the
    reward already scores that, and a model's account of its own failed run is
    the last thing that should harden into memory.
    """
    reward = ep.get("reward") or {}
    total = reward.get("total")
    parts = [f"heart episode {ep.get('episode_id')}"]
    if task := ep.get("task_id"):
        parts.append(f"({task})")
    if repo := ep.get("repo_path"):
        parts.append(f"on {Path(repo).name}")
    if base := ep.get("base_commit"):
        parts.append(f"@{base[:12]}")
    line = " ".join(parts) + f": {ep.get('outcome') or 'unknown'}"
    line += f", reward {total:.4g}" if isinstance(total, (int, float)) else ", unscored"
    if agent := ep.get("agent"):
        line += f"; agent {agent}"
    if (lines := ep.get("diff_lines")) is not None:
        line += f"; {lines} diff lines"
    verdicts = [f"{name} {'passed' if r.get('passed') else 'failed'}"
                for name, r in (ep.get("verifier_results") or {}).items()]
    if verdicts:
        line += "; verifiers: " + ", ".join(sorted(verdicts))
    if refused := ep.get("scope_refused_paths"):
        line += f"; refused {len(refused)} path(s)"
    return line


def record_episode(ep: dict) -> str | None:
    """Write what an episode did straight to persistent. Returns the row id.

    Straight to persistent, with no ephemeral row in between, because the
    compile pass exists to turn a session's loose turns into a durable claim and
    there is nothing loose here: episode.json is already the compiled form. A
    model call to re-say it would add cost, a failure mode at wrap-up, and an
    opportunity to invent.

    Written for every episode, not just sandboxed ones -- the sandbox is a scope
    boundary, not a category of run.

    Idempotent on episode_id, so a re-ingest of the same runs directory does not
    accumulate one row per pass.
    """
    eid = ep.get("episode_id")
    if not eid:
        return None
    try:
        from arteries import storage

        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM arteries.persistent
                            WHERE episode_id = %s AND kind = 'episode'
                              AND valid_until IS NULL LIMIT 1""", (eid,))
            if cur.fetchone():
                return None
        return storage.insert_persistent(
            project_id=os.getenv("ARTERIES_PROJECT") or PROJECT_ID,
            fact=episode_fact(ep),
            domains=["episode"],
            # embedding deferred: `art doctor` backfills null embeddings, and a
            # wrap-up that needs the embedding service up is a wrap-up that
            # fails when it is down
            embedding=None,
            source_meta={"source": "heart", "outcome": ep.get("outcome"),
                         "reward": (ep.get("reward") or {}).get("total"),
                         "components": (ep.get("reward") or {}).get("components") or {},
                         "agent": ep.get("agent"), "repo_path": ep.get("repo_path"),
                         "base_commit": ep.get("base_commit"),
                         "diff_lines": ep.get("diff_lines"),
                         "usage": ep.get("usage") or {}},
            episode_id=eid,
            task_id=ep.get("task_id"),
            kind="episode",
        )
    except Exception:
        # the reward rows and the closed status already landed; a memory row is
        # never worth failing an ingest over
        return None


def close_episodes(closures: list[tuple[str, str | None, float | None]]) -> int:
    """Mark episodes finished. Returns how many rows moved off `running`.

    arteries.episodes was write-only: `_upsert_episode` creates a row the first
    time a decision or reward carries an episode id, and nothing ever updated
    it. 299 of 299 rows read `running`, 287 of them older than an hour, and
    `ended_at` was never set -- so any question of the form "what finished, and
    how" was unanswerable from the database, while retention and activity logic
    read a column with one value in it.

    Here rather than in heart because heart never talks to Postgres. The
    terminal fact already arrives on this side: `art rewards` reads
    episode.json, which carries the outcome and the reward. Closing on ingest
    means the row closes exactly when the fact that closes it is read, scored or
    not -- an episode skipped as unscored is still an episode that ended.

    `WHERE ended_at IS NULL` so a re-ingest cannot rewrite history, the same
    guard runlog.close_run has always used.
    """
    rows = [(outcome or "finished", json.dumps({"reward_total": total}), eid)
            for eid, outcome, total in closures if eid]
    if not rows:
        return 0
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, """
                UPDATE arteries.episodes
                   SET status = %s,
                       ended_at = now(),
                       metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                 WHERE id = %s AND ended_at IS NULL
            """, rows)
            conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except Exception:
        # the reward rows are already written; a closed status is a read-side
        # convenience, never worth failing an ingest over
        return 0


def _run(repo_path: str | Path | None) -> dict[str, Any]:
    try:
        return runlog.current_run(repo_path=repo_path)
    except Exception:
        return {
            "run_id": None,
            "project_id": os.getenv("ARTERIES_PROJECT") or PROJECT_ID,
            "agent_id": str(AGENT_PROCESS_ID),
            "repo_path": str(repo_path or Path.cwd()),
        }


def _persist(record: dict, kind: str, run: dict, repo_path: str | Path | None) -> str:
    """Returns where the write landed: db | jsonl | lost (degradation signal)."""
    try:
        if kind == "decision":
            _db_insert_decision(record)
        else:
            _db_insert_reward(record)
        return "db"
    except Exception:
        try:
            _write_jsonl(run, kind, record, repo_path)
            return "jsonl"
        except Exception:
            return "lost"  # the journal tee still fires; never break the caller


def _db_insert_decision(record: dict) -> None:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        _upsert_episode(cur, record)
        cur.execute(
            """
            INSERT INTO arteries.decisions
                (id, episode_id, run_id, turn_id, project_id, agent_id, decision_type,
                 observation, available_actions, chosen_action, cost, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s::jsonb)
            """,
            (
                record["id"], record["episode_id"], record["run_id"], record["turn_id"],
                record["project_id"], record["agent_id"], record["decision_type"],
                json.dumps(record["observation"], default=str),
                json.dumps(record["available_actions"], default=str),
                record["chosen_action"],
                json.dumps(record["cost"], default=str),
                json.dumps(record["metadata"], default=str),
            ),
        )
        conn.commit()


def _db_insert_reward(record: dict) -> None:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        _upsert_episode(cur, record)
        cur.execute(
            """
            INSERT INTO arteries.rewards
                (id, episode_id, decision_id, run_id, project_id, reward_type,
                 value, components, source, tokens_in, tokens_out, cost_usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            """,
            (
                record["id"], record["episode_id"], record.get("decision_id"),
                record["run_id"], record["project_id"], record["reward_type"],
                record["value"], json.dumps(record["components"], default=str),
                record["source"], record.get("tokens_in"), record.get("tokens_out"),
                record.get("cost_usd"),
            ),
        )
        conn.commit()


def _upsert_episode(cur, record: dict) -> None:
    # the ledger self-populates episode rows: heart never talks to Postgres,
    # so the first decision/reward carrying an episode id creates the episode
    if not record.get("episode_id"):
        return
    cur.execute(
        """
        INSERT INTO arteries.episodes (id, project_id, agent_id, task_id, run_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            record["episode_id"], record["project_id"], record.get("agent_id"),
            task_id(), record.get("run_id"),
        ),
    )


def _write_jsonl(run: dict, kind: str, record: dict, repo_path: str | Path | None) -> None:
    root = Path(
        repo_path or run.get("repo_path") or os.getenv("ARTERIES_REPO") or Path.cwd()
    ) / ".arteries" / "decisions"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, **record}, sort_keys=True, default=str) + "\n")


def _recent_jsonl(
    project_id: str | None, episode: str | None, limit: int, repo_path: str | Path | None
) -> list[dict[str, Any]]:
    root = Path(
        repo_path or os.getenv("ARTERIES_REPO") or Path.cwd()
    ) / ".arteries" / "decisions"
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    for path in sorted(root.glob("*.jsonl"), reverse=True):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") != "decision":
                continue
            if project_id and rec.get("project_id") != project_id:
                continue
            if episode and rec.get("episode_id") != episode:
                continue
            records.append(rec)
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return records[:limit]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# heart still calls this name; marrow will use ingest_episodes directly.
ingest_heart_episodes = ingest_episodes
