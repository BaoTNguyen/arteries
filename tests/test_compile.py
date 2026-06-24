import asyncio
import unittest
from unittest.mock import Mock, patch

from arteries import compile as compiler


class CompileOnceTests(unittest.TestCase):
    def test_cancelled_compile_releases_claimed_rows(self):
        conn = Mock()
        claimed = [{"id": "00000000-0000-0000-0000-000000000001"}]

        async def cancelled(_claimed, _persistent):
            raise asyncio.CancelledError()

        with patch.object(compiler.psycopg2, "connect", return_value=conn),              patch.object(compiler, "_release_stale_claims"),              patch.object(compiler, "_claim_ephemeral", return_value=claimed),              patch.object(compiler, "_load_persistent_context", return_value=[]),              patch.object(compiler, "_llm_compile", side_effect=cancelled),              patch.object(compiler, "_release_claimed") as release_claimed:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(compiler.compile_once())

        release_claimed.assert_called_once_with(conn, ["00000000-0000-0000-0000-000000000001"])
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
