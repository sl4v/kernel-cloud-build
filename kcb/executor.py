"""Command executors for SSH and local Docker containers."""

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

import asyncssh


def _colored(text: str, ansi_code: int) -> str:
    """Wrap text in an ANSI color escape sequence."""
    return f"\033[{ansi_code}m{text}\033[0m"


@runtime_checkable
class CommandExecutor(Protocol):
    """Minimal executor interface used by the build steps."""

    async def connect(self) -> None:
        ...

    async def run(self, cmd: str, *, log_prefix: str = "", check: bool = True) -> int:
        ...

    async def upload_file(self, local: Path, remote: str) -> None:
        ...

    async def disconnect(self) -> None:
        ...


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


class DockerExecutor:
    """Executes build commands inside a local Docker container."""

    def __init__(self, container_name: str) -> None:
        self.container_name = container_name

    async def connect(self) -> None:
        """Docker exec is stateless; container startup is handled by the provider."""
        return None

    async def run(self, cmd: str, *, log_prefix: str = "", check: bool = True) -> int:
        """Run a shell command in the container and stream its output."""
        prefix_text = f"[{log_prefix}]" if log_prefix else "[docker]"
        prefix = _colored(prefix_text, 33)  # 33 = yellow
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            self.container_name,
            "bash",
            "-lc",
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _stream(stream: asyncio.StreamReader) -> None:
            async for line in stream:
                print(f"{prefix} {line.decode().rstrip()}", flush=True)

        await asyncio.gather(_stream(proc.stdout), _stream(proc.stderr))
        rc = await proc.wait()
        if check and rc != 0:
            raise RuntimeError(f"Command failed with exit code {rc}: {cmd}")
        return rc

    async def upload_file(self, local: Path, remote: str) -> None:
        """Copy a local file into the container."""
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "cp",
            str(local),
            f"{self.container_name}:{remote}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip()
            raise RuntimeError(f"docker cp failed with exit code {proc.returncode}: {err}")

    async def disconnect(self) -> None:
        """No-op: docker exec does not maintain a persistent session."""
        return None
