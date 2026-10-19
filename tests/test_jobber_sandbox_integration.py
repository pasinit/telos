"""Run only in a disposable Linux container with the deployed sandbox policy."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from sandbox_runtime import SandboxManager

from telos.transport.sandbox import SandboxRunner


@unittest.skipUnless(os.getenv("TELOS_SANDBOX_SMOKE") == "1", "requires disposable nested-sandbox container")
class SandboxIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await SandboxManager.reset()

    async def test_execution_and_isolation(self):
        runner = SandboxRunner(enable_nested_sandbox=True)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = await runner.run("python -c 'print(6 * 7)'", cwd=workspace)
            self.assertEqual(result, ("42\n", "", 0))
            result = await runner.run("printf shell-ok > result.txt && cat result.txt", cwd=workspace)
            self.assertEqual(result, ("shell-ok", "", 0))
            self.assertEqual((workspace / "result.txt").read_text(), "shell-ok")
            result = await runner.run("touch /app/denied-write-probe", cwd=workspace)
            self.assertNotEqual(result[2], 0, result)
            self.assertFalse(Path("/app/denied-write-probe").exists())
            # A listener in the service namespace must not be reachable from code.
            async def close_connection(reader, writer):
                writer.close()
                await writer.wait_closed()
            server = await asyncio.start_server(close_connection, "127.0.0.1", 0)
            async with server:
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                command = f'python -c "import socket; socket.create_connection((\'127.0.0.1\', {port}), timeout=2)"'
                result = await runner.run(command, cwd=workspace)
                self.assertNotEqual(result[2], 0, result)
                self.assertIn("ConnectionRefusedError", result[1])
            result = await asyncio.wait_for(runner.run("sleep 30 & wait", timeout=1, cwd=workspace), timeout=5)
            self.assertEqual(result, ("", "Execution timed out.", 1))


if __name__ == "__main__":
    unittest.main()
