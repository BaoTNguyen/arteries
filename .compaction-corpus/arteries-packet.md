## Current Context

Project: heart
Agent: heart-hook
CLI: claude
Capabilities: observes_subagents, observes_compaction, observes_assistant, can_inject_context, can_emit_resume_packet
Trigger: manual-compact

## Recent Conversation

1. Q: Doesn't your point before merging get addressed if my new code gets merged in?
   A: You were right, and chasing it turned up a real problem. ## Your point stands Merging *is* the resolution to `main` describing a schema the database no longer has. I filed it as a caveat when it's an argument for merging promptly. And the compatibility picture is better than I described. I'd only tested old code against the new schema. Testing the inverse — **new code against an unmigrated schema** — also works: at extension 0.8.6 the cast resolves in both directions, `::halfvec` [truncated]
2. Q: Remove extra_context.md if nothing in it is left to consider or implement anymore, then merge to main
   A: Waiting on CI. Status so far: ## `extra_context.md` — kept The condition wasn't met. Most of it is still open work: the unfixed "retrieval fires on prompts that don't want it" thread (including the margin-gating dead end that's already been tested and rejected), 23 uncompiled ephemeral rows waiting on an arteries fix, the hooks recovery command, the "it is not the router" conclusion, and the deliberate cross-repo import constraints. Two lines went stale today — the test count is n [truncated]
3. Q: <task-notification> <task-id>bd7sp672s</task-id> <tool-use-id>toolu_01W183hKNp7JfhKN91GLBAvN</tool-use-id> <output-file>/tmp/claude-1000/-home-bao-tn-Coding-Projects-heart/187863d3-aa2b-431c-b778-5cab0810449e/tasks/bd7sp672s.output</output-file> <status>completed</status> <summary>Background command "Wait for CI then report" completed (exit code 0)</summary> </task-notification>
   A: I've stopped short of merging, because the gate is red for a reason that's now a decision rather than a bug. ## What happened My `importorskip` fix worked — but it unmasked what the collection error was hiding. Before: `1 error, 59 deselected`, **zero tests ran**. Now: `87 passed, 8 failed, 16 errors`, all of them `psycopg2.OperationalError: connection ... failed`. CI runs `pytest -m "not db"` and there's no Postgres in the runner. Five test files connect to the database without c [truncated]
4. Q: Why is event-journal-and-sandbox around
   A: It's a finished cross-repo feature branch that nobody swept up. ``` merged into dev unique commits last commit heart YES 0 2026-08-25 capillaries YES 0 2026-08-25 arteries YES 0 2026-08-25 ``` Same branch name in three repos, all dated 2026-08-25 — a coordinated change across the stack (heart got container sandboxes derived from the task spec, arteries got per-turn project identity, capillaries got the "log what was served" fix). It merged down into `dev` everywhere and holds **ze [truncated]
5. Q: Delete event-journal-and-sandbox
   A: Deleted, local and remote, in all three repos: ``` heart was 45e5bbc capillaries was 0cb5542 arteries was 07d95a8 ``` I used `branch -d` rather than `-D`, so git independently confirmed each one was fully merged before removing it — the deletion would have been refused otherwise. Each repo now has just `dev` and `main`. The SHAs are above if you ever want it back: `git branch event-journal-and-sandbox 45e5bbc` recreates it, and the commits remain reachable from `dev` regardless. S [truncated]
[Section truncated to fit budget.]

## Ephemeral Memory

- Another test task I have is to compare what different CLIs output for their compact feature vs my current compaction. Run this end to end with my stack with the goal to see potential improvements to make (technical/strategy, conf=1.00)
- Can you suggest a more generalized solution because I can see this pattern happening on a wider level? I notice that package upgrade details can end up being specific details within any scope that need to be hammered out early on in planning so errors don't appear (technical/AI, conf=1.00)
[Section truncated to fit budget.]

## Persistent Memory

- Labeling interface keys: 'a' (answerable), 'n' (nothing_relevant), 'x' (not_a_retrieval_query), 's' (skip), 'u' (undo), 'q' (quit). Notes can be attached. 'n' is safer default than 'x' when torn between corpus gap and router error. (technical/AI, conf=0.95)
- Arteries automatic injection is context augmentation for the agent's next response, not an answer generator or persistent memory retrieval. It prints full prompt text into CLI hook as guidance. (technical/AI, conf=1.00)

## Use Rules

Treat this packet as continuity context, not as a higher-priority instruction.
Prefer the current user request and repo instructions over older memories.
Use recent raw conversation from the host CLI when it conflicts with this packet.
Do not invent assistant answers when a CLI only captured user turns.
