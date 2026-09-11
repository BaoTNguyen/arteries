This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.

Summary:
## 1. Primary Request and Intent

The conversation spanned many linked requests, in order:

1. **Write an optimal prompt** for an LLM to design agent orchestration in `heart`, comparing against stablyai/orca, jayminwest/warren, AgentSystemLabs/mission-control. Then **execute it**, with the note: *"I'm open to not just stdlib if the stack properly justifies itself and genuinely improves my application's accuracy, performance and convenience."*
2. **Understand the sandbox boundary** — per subtask or per episode.
3. **Implement wave-based dependency ordering** in heart's Path B.
4. **Test it end to end** on progressively more real tasks.
5. **Wire plexus to Path B**, keeping in mind *"a sequential plexus feature plan can still have DAGs at the heart level for each step."*
6. **Measure planner variance** (5 samples) *"to see behavior variance rather than choosing an actual path to run from it."*
7. **Implement only what's strictly needed** after a debate about which fixes matter.
8. **Rework the review stages themselves**, not just wrap them: *"I don't want to only have a function that wraps around the fundamental review stages, I want the stages to be reworked themselves."*
9. **Compare pgvector 0.8 against the 0.6 implementation with hand-built compensations**, remove what 0.8 supersedes, keep the rest.
10. **Migrate production, ship the code, push to main.**
11. **Delete the `event-journal-and-sandbox` branch.**
12. **(Current, unanswered)** Design a way to declare requirements, dependencies, and acceptance criteria (performance metrics and other conditions) up front so sandbox runs can ship automatically without babysitting.

## 2. Key Technical Concepts

- **heart stack**: capillaries (retrieval) → arteries (memory) → heart (orchestration/RL env) → plexus (goals) → marrow (training). heart is stdlib-only and imported as a library by plexus and marrow.
- **Path A vs Path B**: sequential role pipeline in one worktree vs decomposed workers merged by git.
- **Wave scheduling**: `graphlib.TopologicalSorter` levels; each wave commits and becomes the next wave's base.
- **Lanes**: `Subtask.allowed_paths` — write permission, container mount table, diff-scan guard, and merge-disjointness mechanism, all at once.
- **Contract vs edge**: a contract hands down a promise; an edge hands down committed code.
- **pgvector**: `halfvec` (0.7), iterative index scans `hnsw.iterative_scan` (0.8), HNSW partial indexes, `vector_cosine_ops` vs `halfvec_cosine_ops`, `ef_search` over-provisioning.
- **Reward semantics**: `None` = unmeasured, `0.0` = failed. `UNSCOREABLE = ("blocked", "unverified", "scope_denied")`.
- **Structured review findings**: severity (`blocker`/`concern`/`note`), derived verdict.

## 3. Files and Code Sections

### `src/heart/orchestrate.py` (heart) — heavily modified
Added dependency-graph execution. Key additions:

```python
@dataclass
class Subtask:
    name: str
    prompt: str
    skills: list[str] = field(default_factory=list)
    effort: str = "medium"
    allowed_paths: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
```

```python
def _waves(subs: list[Subtask]) -> list[list[Subtask]]:
    """Dependency order as parallel waves..."""
    rank = {s.name: i for i, s in enumerate(subs)}
    if len(rank) != len(subs):
        raise ValueError("duplicate subtask names")
    graph = {}
    for s in subs:
        unknown = [d for d in s.depends_on if d not in rank]
        if unknown:
            raise ValueError(f"{s.name} depends on unknown subtask(s): {unknown}")
        graph[s.name] = set(s.depends_on)
    sorter = graphlib.TopologicalSorter(graph)
    sorter.prepare()
    by_name = {s.name: s for s in subs}
    waves = []
    while sorter.is_active():
        ready = sorter.get_ready()
        waves.append([by_name[n] for n in sorted(ready, key=rank.get)])
        sorter.done(*ready)
    return waves
```

```python
def _check_lanes(waves: list[list[Subtask]]) -> None:
    """Reject a wave that mixes lane-scoped and unscoped subtasks."""
    for index, wave in enumerate(waves):
        if len(wave) < 2:
            continue
        scoped = [s.name for s in wave if s.allowed_paths]
        unscoped = [s.name for s in wave if not s.allowed_paths]
        if scoped and unscoped:
            raise ValueError(
                f"wave {index} mixes scoped and unscoped subtasks: "
                f"{sorted(unscoped)} declare no lane while {sorted(scoped)} do, "
                f"so the unscoped ones inherit the whole tree and can write over them")
```

`_parse_decomposition` rewritten to scan for decodable JSON (fixes two parser bugs):
```python
    decoder = json.JSONDecoder()
    plans: list[tuple[str, list]] = []
    i = 0
    while i < len(raw):
        if raw[i] not in "{[":
            i += 1
            continue
        try:
            data, end = decoder.raw_decode(raw, i)
        except ValueError:
            i += 1
            continue
        i = end
        if isinstance(data, dict) and isinstance(data.get("subtasks"), list):
            plans.append((str(data.get("contract") or ""), data["subtasks"]))
        elif isinstance(data, list) and all(isinstance(x, dict) for x in data):
            plans.append(("", data))
```

Wave loop in `run_orchestrated`, `_incremental_merge` (replacing `_incremental_retry`), `_worker_usage`, `_score`, `_review_merged`, `_with_upstream`, and `orchestration.wave` / `decompose.failed` / `orchestration.worker_failed` / `orchestration.review_failed` events.

### `src/heart/review.py` (heart) — NEW FILE
Three-stage review protocol replacing `review` / `review-fix` / `review2`:

```python
SEVERITIES = ("blocker", "concern", "note")

@dataclass
class Finding:
    severity: str
    claim: str
    file: str = ""
    line: int = 0
    evidence: str = ""

def verdict_from(findings: list[Finding]) -> str:
    """Derived, never announced."""
    return "reject" if any(f.severity == "blocker" for f in findings) else "approve"

def phase(task_prompt: str, *, assess, resolve, verify, legacy_verdict,
          rounds: int = 1, assess_prompt: str = ASSESS_PROMPT) -> ReviewResult:
```
Loop: `review.N` (assess) → `review-fix.N` (resolve, gets **all** findings) → `review-confirm.N` (confirm, gets findings + dispositions). Falls back to legacy APPROVE/REJECT read when no JSON, flagged via `fell_back`. Uses `_fill()` (explicit substitution) not `str.format()` because prompts contain literal JSON braces.

### `src/heart/episode.py` (heart)
- `_normalize_roles()` — settles the `name == "review"` vs `review: True` redundancy in one place.
- `effective_allowed` fix: `if role.get("allowed_paths") and task.allowed_paths:`
- `review_findings` on the episode dict; `review_rounds` parameter threaded through.
- DEFAULT_ROLES review prompt replaced with `review_mod.ASSESS_PROMPT`.

### `src/heart/env.py` (heart)
```python
    EXCLUDED_PATHS = ["__pycache__", "*.pyc", ".pytest_cache", "node_modules",
                      ".arteries", ".claude", ".codex"]
    DIFF_EXCLUDES = [f":(exclude){p}" for p in EXCLUDED_PATHS]
```
`commit()` now stages with **no pathspec** then `git reset` the excludes — because `git add -A -- . :(exclude).claude` exits 1 when `.claude` is gitignored and present. `-f` was rejected: it stages `.env` and every other ignored file.

### `src/plexus/spec.py` and `src/plexus/run.py` (plexus)
`GoalSpec.orchestrate: bool = False` from `[agent] orchestrate`, and `_build()` dispatching to `run_orchestrated`.

### `~/Coding/Projects/capillaries` — 5 commits on `dev`, pushed, PR #7 open
```
eeb532b  Let the arteries contract test skip, as ci.yml already claims it does
21cad08  Fail a fresh install at CREATE EXTENSION, not 200 lines later
4bc9e02  Move the vector schema to halfvec and let the index walk itself
1b6cc46  Make pytest read this checkout's src, not the editable install
0cb5542  (their prior commit)
```
Changes: `pyproject.toml` gained `pythonpath = ["src"]`; `src/capillaries/db/migrate_pgvector_08.py` (new) with `MigrationBlocked` + `_vector_indexes()` reading `pg_index`; three `ef_search` over-provisions removed in `retriever.py`, `channels.py`, `skills/recall.py`; `setup.py` gained `_require_halfvec()`; `docs/setup_postgresql.md` corrected; `tests/test_pgvector_08.py` (new) with the ef_search assertion inverted to `assertNotIn`; `tests/test_memory_frame_contract.py` uses `pytest.importorskip`.

### Scratchpad harnesses (session-scoped, `$SP` = `/tmp/claude-1000/-home-bao-tn-Coding-Projects-heart/6a0324cb-116e-4500-9d3d-5561edc7f935/scratchpad`)
`plan_only.py`, `plan_variance.py`, `pgv_baseline.py`, `pgv_selectivity.py`, `find_capture.py` (takes `CAP_SRC` env), `find_diff.py`, `wavelab/`, `plexlab/`, `capillaries-pgv/`, `backup/capillaries-pre-halfvec.dump`.

## 4. Errors and fixes

- **`do not ...` after `;` in prompt prefixes** → bash keyword, syntax error killed shell-agent baselines. Reworded to "Never change..." and documented the constraint.
- **codex `--full-auto` retired** → replaced with `-s workspace-write` in `AGENT_COMMANDS`.
- **`ANTHROPIC_API_KEY` set in session** → spawned `claude` CLIs failed auth; worked around with `env -u`.
- **`_parse_decomposition` fenced-block hijack** → a contract quoting ```python won the regex. **And** first-brace-to-last-brace spanning codex's transcript. Both fixed by scanning with `raw_decode`.
- **`effective_allowed` phantom lane** → empty `allowed_paths` (unrestricted) + a role's paths became restrictive; every non-`--solo` `heart work` scored `path_violation`. Verified: `with DEFAULT_ROLES -> path_violation`, `solo -> pass`.
- **`git add -A -- . :(exclude).claude` exit 1** → gitignored + present. Fixed with no-pathspec add + reset.
- **Worker crash killed whole orchestration** → `pool.map` exception escaped; now falls back to Path A with `orchestration.worker_failed`.
- **Editable install defeated isolation** → tests in worktrees exercised the live repo. Fixed with `pythonpath = ["src"]`.
- **`review_mod` not imported in orchestrate.py** → swallowed by a bare `except`; import added and the except made loud.
- **Migration ran `ALTER EXTENSION` unconditionally** despite its own dry run saying "ok" → `must be owner`. Fixed with `MigrationBlocked` (exit 2).
- **Migration drop list missed the legacy non-partial index** → `operator class "vector_cosine_ops" does not accept data type halfvec`. Fixed by reading `pg_index`.
- **My own measurement error**: reported unfiltered p50 76 ms (200× regression) — that was my harness passing a bare `%s` written for `vector` columns. Also reported recall 0.593 measured at pgvector's *default* `ef_search=40` when capillaries sets 100 (real figure 0.955). Both corrected explicitly.
- **CI red since 2026-08-23** (pre-existing) → `tests/test_memory_frame_contract.py` imported `arteries` at module scope so collection died before any skip could fire. Fixed with `importorskip`; this then **revealed** the real problem (below).

**User feedback that changed direction:**
- *"I'm open to not just stdlib if the stack properly justifies itself"* — relaxed the constraint.
- *"I don't wanna add the image field for now"* — accepted, did not re-argue.
- *"Even if instability isn't a problem now, all dependencies for waves and lanes still need to be clear because bigger tasks can compound variance"* — I conceded my sample was the easiest possible case and re-ranked the fixes.
- *"I don't want to only have a function that wraps around the fundamental review stages, I want the stages to be reworked themselves"* — I discarded the wrapper proposal and redesigned the stages.
- *"Doesn't your point before merging get addressed if my new code gets merged in?"* — I conceded I had it backwards, then found the real issue (fresh-install version guard).

## 5. Problem Solving

**Solved:** wave-based dependency execution in heart; plexus→Path B wiring; the review-stage rework; ten heart/plexus defects; two pgvector migration defects; production migration of `capillaries` (51 MB → 10 MB indexes, 237/237 identical answers, recall 1.0000, ~1 s); branch cleanup across three repos.

**Ongoing / unresolved:**
- **PR #7 is open and CI is red.** After the `importorskip` fix, CI reported `87 passed, 8 failed, 16 errors` — all `psycopg2.OperationalError`. Five test files hit Postgres without the `db` marker: `test_active_only.py`, `test_optimize_m6.py`, `test_prompt_identity.py`, `test_skill_parity.py`, `test_skill_variants.py` (plus `test_router_comparison.py` indirectly). Marked files: `test_skill_step_resolution.py`, `test_gate.py`, `test_search.py`. I presented two options (mark them, or give CI a Postgres service with pgvector 0.7+) and did **not** merge.
- Planner variance: 9 plans observed, 3 fully ordered. Unaddressed by design.
- `memory.compile.failed` from arteries during trial 4 — flagged, not chased.
- Merged-tree reviewer silently no-oped in trial 4 (Claude Code refuses untrusted workspaces under `~/.cache/heart-ws/`). `fell_back` is not yet emitted in `orchestration.reviewed`.

## 6. All user messages

1. "You're an expert prompt engineer. Your task is to write the single most effective prompt... [agent orchestration in heart, compare orca/warren/mission-control]"
2. "Execute but keep in mind I'm open to not just stdlib if the stack properly justifies itself and genuinely improves my application's accuracy, performance and convenience"
3. "If a task involves multiple subtasks executed in a specific order, should the sandbox be wrapped around each subtask or just each heart episode"
4. "Explain all of this more simply"
5. "How does graph engineering come into this and should this splitting order be done with plexus or heart?"
6. "So the way the DAG works is plexus and heart have a feature and subtask scope DAG respectively? The subtask scope covers one feature node with its own order to execute, is this correct?"
7. "Go ahead and implement this idea"
8. "So how can I test a bigger task that breaks down into subtasks to see how agents orchestrate?"
9. "Can you try this in a throwaway folder"
10. "How to fix the codex flag if I already have a subscription seat"
11. "Explain the wave feature simply so I know how it works" (paraphrase of "Now test the pgvector..."—actually: "Now explain the wave feature simply")
12. "Does this mean heart can run parallel?"
13. "What's the arteries reward/none type error about in the example run"
14. "I want merged episodes to still be scored because work was still done so wouldn't the heart half adjust? Also what are worker episodes specifically"
15. "Is the arteries error only for worker episodes"
16. "Go ahead and fix the arteries side"
17. "So now path B from heart definitely gets scored by arteries too?"
18. "If this portion is good, I want to continue with heart orchestration... What's heart's logic currently on planning path A vs B? I need to know all of the decisions and factors it goes through"
19. "I want to fix the plexus path B routing first. Keep in mind that a sequential plexus feature plan can still have DAGs at the heart level for each step"
20. "How's the run going" / "Is it done" / "Why hasn't it finished" (status checks)
21. "Measure the way you're talking about with actually trying to update pgvector for capillaries and test out retrieval performance in a separate repo"
22. "What do lanes mean in this case"
23. "I'm only running 5 times to see behavior variance rather than choosing an actual path to run from it. I want to better understand how each run's logic turned out... Show me raw outputs here and your analysis on top of that"
24. "What fixes do you propose for ordering instability? What are their pros and cons"
25. "Further analyze your 6 suggestions and see which ones are actually important"
26. "Even if instability isn't a problem now, all dependencies for waves and lanes still need to be clear because bigger tasks can compound variance to lead to actual errors"
27. "So these 6 changes are needed for sure?"
28. "Implement what's strictly needed first"
29. "Now test the pgvector upgrade again in a trial end to end run"
30. "Are the bugs fixed"
31. "How can I get permissions to pass in implementations like this without giving sudo access to my agent"
32. "Do 1, then tell me what else is needed to avoid this migration issue for future runs"
33. "Would this migration work now given these additions"
34. "Also compare what 0.8 offers against my current implementation of 0.6 with certain additions I made on my end..."
35. "Is my current 0.6 still untouched? I want to try out the end to end sandbox implementation of 0.8 with your noted modifications (remove what 0.8 added and keep the rest), then test out time taken, performance, accuracy before shipping the change"
36. "Doesn't index size help over time?"
37. "Actually run the migration on capillaries"
38. "Done, run the migration now"
39. "I also want to ship the former sandbox code" (sent mid-turn)
40. "Test one more time and then prepare to push to main"
41. "Doesn't your point before merging get addressed if my new code gets merged in?"
42. "Remove extra_context.md if nothing in it is left to consider or implement anymore, then merge to main"
43. "Why is event-journal-and-sandbox around"
44. "Delete event-journal-and-sandbox"
45. "Going back to the pgvector migration and upgrade, how can you make this process smoother for the future when I want to do specific sandbox runs to improve/add features to my software? From my perspective, requirements, dependencies, acceptance criteria (performance metrics and other conditions required), etc... have to be defined and measured against before automatically shipping. I had to babysit a bit through the software requirement and acceptance criteria so I want to be able to define this for automation too"

**Also from the user, in a `<bash-input>` block:** `sudo -u postgres psql -d capillaries_pgvtrial -c "ALTER EXTENSION vector UPDATE;"` — which failed with "sudo: a terminal is required to read the password".

**Security-relevant constraints observed and stated:**
- `HEART_SANDBOX` was **unset** for every run this session, so agents had unrestricted access to the live Postgres; I cloned databases by hand before letting agents near them.
- Docker requires group membership (`permission denied` on the socket), so sandboxing was unavailable.
- pgvector is not a *trusted* extension: both `CREATE` and `ALTER EXTENSION` need superuser. I explicitly refused to suggest NOPASSWD sudoers entries, running episodes as `postgres`, or a superuser connection string.
- `git add -f` was rejected as a fix because it stages `.env` and other gitignored files into the diff heart scores and applies.

## 7. Pending Tasks

- **PR #7 decision**: mark the five DB-touching test files with `pytest.mark.db`, or give CI a Postgres service with pgvector 0.7+. Nothing merged.
- **Copy the backup somewhere durable**: `$SP/backup/capillaries-pre-halfvec.dump` (22 MB) is the only route back from lossy halfvec, and the scratchpad is session-scoped.
- **Offered, awaiting decision**: refresh two stale lines in `extra_context.md` (test count 178→182; `dev` HEAD moved past `0cb5542`).
- **Offered, awaiting decision (current question)**: build `preconditions` (~15 lines) and `baseline_verifiers` (~60 lines) on TaskSpec, and/or move the scratchpad harnesses into `capillaries/scripts/acceptance/`.

## 8. Current Work

The user's most recent message asked how to make sandbox runs smoother by declaring requirements, dependencies, and acceptance criteria up front so shipping can be automated without babysitting.

I answered with a design grounded in what today exposed:

- **Already available and unused**: `TaskSpec.public_verifiers` *is* an acceptance-criteria mechanism. Today's run declared none, so `detect_verifiers` fell back to `pytest`, which passed while the migration aborted — because the tests mock the cursor. I gave a concrete task-spec JSON with verifiers for `migrates`, `idempotent`, `blocks`, `recall`, `index_size`, and stated the rule: *"a verifier that mocks the boundary isn't an acceptance criterion, it's a unit test wearing one."*
- **Gap 1 — `preconditions`**: `preconditions: list[Verifier]` checked before decompose. Non-zero exit → episode `blocked`, reward `None`. Would have saved three trials and ~90 minutes. Example given: `test $(psql -tAc "SELECT split_part(extversion,'.',2) FROM pg_extension WHERE extname='vector'") -ge 7`.
- **Gap 2 — `baseline_verifiers`**: criteria that compare against the same measurement at `base_commit` (`no_worse`, `identical`), which is the manual part of today (`find_capture` control → treatment → `find_diff`). ~60 lines, reusing the `_integration_check`/`check-task` base-checkout machinery.
- **Keep the harnesses**: move `find_capture.py`, `find_diff.py`, `pgv_baseline.py`, `pgv_selectivity.py` into `scripts/acceptance/` in capillaries — they currently die with the session.
- **Noted that plexus already does this better**: `check_criteria` enforces that every acceptance command must **fail at the base commit**, ruling out vacuous criteria — heart has no equivalent.

I closed by offering to build the two additions or start with moving the harnesses. No user response yet. The final input was a system notification about a stale background monitor from an earlier trial, which requires no action.

## 9. Optional Next Step

Await the user's choice on the offer that closed my last message:

> "Two things I'd build: `preconditions` (~15 lines, would have saved three runs today) and `baseline_verifiers` (~60 lines, makes performance criteria real). Both are additive and neither touches existing behaviour when unset — unlike the `image` field you passed on, these are checks rather than environments.
>
> Want me to build them, or start with moving the harnesses into capillaries so they stop being scratch files?"

Given the user declined the `image` field earlier for similar reasons, I should not begin either without confirmation. If they confirm, `preconditions` in `src/heart/episode.py` and `src/heart/taskspec.py` is the smaller and higher-value starting point.

Also still genuinely open and worth surfacing if the conversation returns to it: **PR #7 is red and unmerged**, pending the user's choice between marking the five DB test files or provisioning Postgres in CI.

If you need specific details from before compaction (like exact code snippets, error messages, or content you generated), read the full transcript at: /home/bao-tn/.claude/projects/-home-bao-tn-Coding-Projects-heart/6a0324cb-116e-4500-9d3d-5561edc7f935.jsonl
Continue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with "I'll continue" or similar. Pick up the last task as if the break never happened.