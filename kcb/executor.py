"""Remote SSH executor using asyncssh."""

from pathlib import Path

import asyncssh


def _colored(text: str, ansi_code: int) -> str:
    """Wrap text in an ANSI color escape sequence."""
    return f"\033[{ansi_code}m{text}\033[0m"


class RemoteExecutor:
    """Holds a persistent SSH connection to an ephemeral VPS.

    Usage:
        executor = RemoteExecutor(host="1.2.3.4", key_path=Path("~/.ssh/id_rsa"))
        await executor.connect()
        await executor.run("uname -a")
        await executor.disconnect()
    """

    def __init__(
        self,
        host: str,
        key_path: Path,
        username: str = "root",
        port: int = 22,
    ) -> None:
        self.host = host
        self.key_path = key_path
        self.username = username
        self.port = port
        self._conn: asyncssh.SSHClientConnection | None = None

    async def connect(self) -> None:
        """Open SSH connection. known_hosts=None for ephemeral VMs."""
        self._conn = await asyncssh.connect(
            self.host,
            port=self.port,
            username=self.username,
            client_keys=[str(self.key_path)],
            known_hosts=None,
            keepalive_interval=30,
            keepalive_count_max=6,
        )

    async def run(self, cmd: str, *, log_prefix: str = "", check: bool = True) -> int:
        """Run a remote command, streaming stdout+stderr line-by-line with an ANSI-colored prefix.

        Each output line is printed with a cyan prefix: ``[log_prefix] `` when
        log_prefix is provided, or ``[remote] `` otherwise.

        Returns the exit code. Raises RuntimeError if check=True and exit code != 0.
        """
        if self._conn is None:
            raise RuntimeError("Not connected. Call connect() before run().")

        prefix_text = f"[{log_prefix}]" if log_prefix else "[remote]"
        prefix = _colored(prefix_text, 36)  # 36 = cyan

        async with self._conn.create_process(cmd, stderr=asyncssh.STDOUT) as proc:
            # asyncssh process stdout is an asyncio StreamReader when
            # stderr is redirected to stdout via asyncssh.STDOUT.
            async for line in proc.stdout:
                # Lines arrive with a trailing newline from the remote shell;
                # strip it so we control formatting uniformly.
                print(f"{prefix} {line.rstrip()}", flush=True)

        rc = proc.exit_status
        # exit_status may be None if the process was killed by a signal;
        # treat that as a non-zero exit.
        if rc is None:
            rc = -1

        if check and rc != 0:
            raise RuntimeError(f"Command failed with exit code {rc}: {cmd}")

        return rc

    async def upload_file(self, local: Path, remote: str) -> None:
        """Upload a local file to the remote host via asyncssh.scp()."""
        if self._conn is None:
            raise RuntimeError("Not connected. Call connect() before upload_file().")

        await asyncssh.scp(str(local), (self._conn, remote))

    async def disconnect(self) -> None:
        """Close the SSH connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
