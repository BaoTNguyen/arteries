"""Concurrency at the generation boundary.

The database was already safe. Intake resolves in `idx_eph_dedupe`, claims use
FOR UPDATE SKIP LOCKED plus a lease, promotion selects from its parent in the
same statement that inserts, migrations hold an advisory lock. Nothing there can
corrupt.

The model server was not, and then was bounded twice. arteries capped itself at
2 and heart capped itself at 2, which is 4 requests for a server with 2 slots --
the same overload with more bookkeeping. One pool, shared by every process on the
box, is the fix.
"""

import contextlib
import os
import unittest
from unittest.mock import patch

from arteries import compile as compile_mod
from arteries import slots

ENDPOINT = "http://127.0.0.1:8001/v1"


class PoolIdentityTests(unittest.TestCase):
    def test_the_pool_is_keyed_by_host_and_port(self):
        """Two servers are two queues and get two pools. One tensor-parallel
        server spanning both GPUs is one port and one pool, which is also
        right."""
        self.assertNotEqual(slots.pool_for("http://127.0.0.1:8001/v1"),
                            slots.pool_for("http://127.0.0.1:8002/v1"))
        self.assertEqual(slots.pool_for("http://127.0.0.1:8001/v1"),
                         slots.pool_for("http://127.0.0.1:8001/chat"))

    def test_the_path_is_not_named_after_one_repo(self):
        """The convention is the contract, and heart flocks the same directory.
        A heart-specific name would invite a second pool next to it."""
        self.assertIn("model-slots", str(slots.base_dir()))
        self.assertNotIn("heart", str(slots.base_dir()))

    def test_no_endpoint_is_not_our_business(self):
        """A paid API has no local queue to protect."""
        with slots.hold(None) as got:
            self.assertTrue(got)


class SlotCountTests(unittest.TestCase):
    def setUp(self):
        slots._slots_cache.clear()

    def tearDown(self):
        slots._slots_cache.clear()

    def test_an_explicit_override_wins(self):
        with patch.dict(os.environ, {"ARTERIES_MODEL_SLOTS": "7"}):
            self.assertEqual(slots.slot_count(ENDPOINT), 7)

    def test_the_server_is_asked(self):
        """llama.cpp returns one object per slot on /slots. /health only says
        "ok", which is why this reads /slots."""
        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return b'[{"id":0},{"id":1},{"id":2}]'

        with patch("urllib.request.urlopen", return_value=_Resp()):
            self.assertEqual(slots.slot_count(ENDPOINT), 3)

    def test_an_unreachable_server_falls_back(self):
        with patch("urllib.request.urlopen", side_effect=OSError("no route")):
            self.assertEqual(slots.slot_count(ENDPOINT), slots.DEFAULT_SLOTS)

    def test_the_server_is_asked_once_per_endpoint(self):
        with patch("urllib.request.urlopen", side_effect=OSError("no route")) as get:
            slots.slot_count(ENDPOINT)
            slots.slot_count(ENDPOINT)
        self.assertEqual(get.call_count, 1)


class HoldingTests(unittest.TestCase):
    def setUp(self):
        slots._slots_cache.clear()

    def tearDown(self):
        slots._slots_cache.clear()

    def test_exactly_n_holders_at_once(self):
        with patch.dict(os.environ, {"ARTERIES_MODEL_SLOTS": "2"}), \
             contextlib.ExitStack() as stack:
            got = [stack.enter_context(slots.hold(ENDPOINT)) for _ in range(4)]
        self.assertEqual(got, [True, True, False, False])

    def test_leaving_the_block_frees_a_slot(self):
        with patch.dict(os.environ, {"ARTERIES_MODEL_SLOTS": "1"}):
            with slots.hold(ENDPOINT) as first:
                self.assertTrue(first)
                with slots.hold(ENDPOINT) as second:
                    self.assertFalse(second)
            with slots.hold(ENDPOINT) as third:
                self.assertTrue(third)

    def test_a_dead_holder_does_not_wedge_a_slot(self):
        """The kernel drops an flock the instant its holder dies, which no
        application-level counter can promise. This is why it is a lock file.

        A subprocess rather than multiprocessing: `os._exit` skips the flush of
        an mp.Queue, so the first version of this test raced its own signal. It
        also matches what the convention is for -- any process, any language.
        """
        import subprocess
        import sys

        with patch.dict(os.environ, {"ARTERIES_MODEL_SLOTS": "1"}):
            pool = slots.pool_for(ENDPOINT)
            pool.mkdir(parents=True, exist_ok=True)
            target = pool / "slot0"
            done = pool / "took-it"
            done.unlink(missing_ok=True)

            # Takes the lock, says so on disk (synchronous), dies without
            # unwinding.
            subprocess.run([sys.executable, "-c", (
                "import fcntl, os, sys\n"
                f"h = open({str(target)!r}, 'w')\n"
                "fcntl.flock(h, fcntl.LOCK_EX)\n"
                f"open({str(done)!r}, 'w').write('1')\n"
                "os._exit(0)\n")], timeout=10, check=True)

            self.assertTrue(done.is_file(), "the child never took the lock")
            done.unlink()
            with slots.hold(ENDPOINT) as got:
                self.assertTrue(got)


class BackPressureTests(unittest.TestCase):
    """A refused pass must not lose work."""

    def test_no_slot_claims_nothing(self):
        import asyncio

        @contextlib.contextmanager
        def _busy(_endpoint, wait=False):
            yield False

        with patch.object(compile_mod, "_generator_reachable", return_value=True), \
             patch.object(compile_mod.slots, "hold", _busy), \
             patch.object(compile_mod, "_claim_ephemeral",
                          side_effect=AssertionError("claimed without a slot")):
            result = asyncio.run(compile_mod.compile_once())
        self.assertEqual(result["status"], "no_generation_slot")
        self.assertEqual(result["claimed"], 0)

    def test_a_pass_that_gets_a_slot_proceeds(self):
        import asyncio

        @contextlib.contextmanager
        def _free(_endpoint, wait=False):
            yield True

        with patch.object(compile_mod, "_generator_reachable", return_value=True), \
             patch.object(compile_mod.slots, "hold", _free), \
             patch.object(compile_mod.psycopg2, "connect"), \
             patch.object(compile_mod, "_release_stale_claims"), \
             patch.object(compile_mod, "_claim_ephemeral", return_value=[]):
            result = asyncio.run(compile_mod.compile_once())
        self.assertEqual(result["status"], "nothing_to_compile")
