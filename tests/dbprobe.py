"""Is there a Postgres to talk to?

This probe lived in test_ledger_contracts and test_runlog, with a comment
arguing that two copies of eight lines beat a shared helper nothing else would
import. That was true at two. It stopped being true at six: test_evergreen,
test_evict, test_evidence, test_journal and test_prior_attempts all needed the
same answer, and CI -- which has never had a database -- was failing 29 tests
because they asked nobody.

Imported at module scope by the suites that need it, so the connection is
attempted once per process rather than once per test.
"""
import psycopg2

from arteries.config import DB_CONFIG


def db_reachable() -> bool:
    try:
        conn = psycopg2.connect(connect_timeout=2, **DB_CONFIG)
        conn.close()
        return True
    except Exception:
        return False


DB_REACHABLE = db_reachable()
