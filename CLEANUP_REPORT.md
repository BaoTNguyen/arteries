# arteries cleanup: import placement and duplication

Branch `dev`. Two commits: the import audit, then the dedup.

## Counts

| | |
|---|---|
| function-local imports audited | 146 |
| removed from function bodies | 129 |
| new module-scope imports | 85 |
| kept local | 17 |
| kept with a stated reason | 17 of 17 |

The gap between 129 removed and 85 added is not an error. The same module was
often imported inside four or five different functions in one file; hoisting
collapses those to a single line. arteries had the worst of this in the stack
and now has the fewest function-local imports of any repo.

## KEEP categories

| category | count | reason |
|---|---|---|
| optional extra | 3 | `rdflib` in `ontology.py` (the `ontology` extra, used by one hand-run command) and `anthropic` in `ingest.py` (the frontier path, undeclared) |
| intra-package cycle | 6 | `evergreen` ↔ `ingest`, `graph` → `ontology`, `compile` → `evergreen`/`degrade` |
| cost, not correctness | 3 | `ontology` pulls in difflib and a database connection; `gexf` is a CLI errand with no business on the retrieval path |
| `__main__` guard | 2 | `import sys` inside `if __name__ == "__main__"`, which is the ordinary idiom and needs no defence |
| graceful degradation | 3 | inside `try:`/`except ImportError`, where the fallback is the point — `extract.py` falls back to arteries' own `DOMAIN_KEYWORDS` when capillaries is absent |

### On the capillaries edge

`eval.py:46` imports `capillaries.find` at module scope, and that is correct.
The dependency runs one way: arteries hard-requires capillaries, capillaries
tolerates arteries' absence. The cycle the pyproject comments warn about is a
packaging problem — neither name resolves on PyPI — not an import-time one.
The audit left this alone, and the full-package import walk confirms nothing
recurses.

## Within-repo duplication

### Extracted

**`add_event_args()` and `normalize_from_args()` in `cli_normalize.py`** —
`cli_normalize`, `hook_observe` and `assistant` each declared the same four
flags (`--cli`, `--event`, `--project`, `--agent`) with the same three
`ARTERIES_*` env defaults behind them. Only the `--event` default ever
differed, so it became a keyword argument and nothing else changed.

The pair that follows was copied too: `normalize()` with all four values
threaded through, then `apply_event_env()` on the result. That second line is
the interesting one. Forget it and the event normalises fine, returns a valid
object, and nothing downstream ever sees it — a hook that silently observes
nothing. `normalize_from_args` does both or neither.

`tests/test_injection_marker.py` patched the two names separately and now
patches the one that replaced them.

### Left alone

| what | why |
|---|---|
| the two `INSERT INTO arteries.evergreen` statements in `evergreen.py:292` and `ingest.py:256` | same shape, different statements. One is `INSERT ... SELECT` promoting a row out of `persistent`; the other is `INSERT ... VALUES` with `core`, `origin` and `evidence` set. The column lists differ too. Forcing them through one builder would mean a function whose arguments are "which of these two queries" |
| the NDJSON read loop in `journal.py:171` and `runlog.py:632` | six lines of skip-blank, `json.loads`, skip-malformed. It is the shape of reading a journal, not a decision anyone can get wrong differently in two places |

## Verification

| check | result |
|---|---|
| `PYTHONPATH=src pytest -q` | 609 passed, 7 skipped, 7 subtests |
| full package import walk | no new failures; no recursion through capillaries |
| `rdflib` absent after importing `arteries.cli` | passes — the extra stays off the hook path |

## Cross-repo duplication candidates

Reported, not extracted.

| # | logic | files | ~lines | worth it? |
|---|---|---|---|---|
| 1 | `migrate_embed_dim` — the pgvector column resize. Both files are 108 lines running the same algorithm against different table lists. | `arteries/migrate_embed_dim.py` vs `capillaries/db/migrate_embed_dim.py` | ~100 each | **Yes.** The biggest duplication in the stack, and the two already cover different tables — so an embedding-model change migrates one database and half the other. |
| 2 | `tsquery` construction via `ts_parse` with the `tokid != 12` filter. | `arteries/storage.py` vs `capillaries/search/retriever.py` | ~12 | **Yes, and cheap.** arteries already imports capillaries at module scope, so this is one import away. The bare `12` is exactly the constant that gets explained in one copy and not the other. |
| 3 | `DB_CONFIG` from five env vars with identical defaults, arteries defaulting `DB_NAME` to `"capillaries"`. | `arteries/config.py` vs `capillaries/config/paths.py` | ~8 | **Yes — and note it is half done already.** `config.py:38` imports the four embedding settings from capillaries, with a comment about the 768/1024 mismatch that would have failed every write. `DB_CONFIG` is the one that did not get the same treatment, and both point at the same database. |
| 4 | Probing a model server for its slot count and caching the answer. | `arteries/slots.py` vs `heart/runner.py` | ~8 | **No.** heart is not a dependency of arteries and cannot become one without inverting the stack. Eight lines of `json.load` and a dict cache is not worth a shared package. |

## Suspected bugs

None found. The `--event` defaults that differ between `hook_observe`
(`UserPromptSubmit`) and `assistant` (`assistant_response`) looked like drift
at first and are not: they are the two hooks' actual event names.
