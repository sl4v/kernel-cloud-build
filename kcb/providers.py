"""Provider protocol and implementations for kcb."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from kcb import hetzner
from kcb.build import docker_cp_artifacts, rsync_artifacts
from kcb.config import BuildConfig, DockerConfig, HetznerConfig, LocalVMConfig
from kcb.executor import CommandExecutor, DockerExecutor, RemoteExecutor
from kcb.hetzner import ServerHandle


async def _run_docker_command(*args: str) -> tuple[int, str, str]:
    """Run a docker CLI command and return (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def _docker_list_managed() -> list[ServerHandle]:
    """List local Docker containers created by kcb."""
    rc, stdout, stderr = await _run_docker_command(
        "ps",
        "-a",
        "--filter",
        "label=managed-by=kcb",
        "--format",
        "{{.ID}}\t{{.Names}}\t{{.CreatedAt}}",
    )
    if rc != 0:
        raise RuntimeError(f"docker ps failed with exit code {rc}: {stderr.strip()}")

    handles: list[ServerHandle] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        container_id, name, created_at = line.split("\t", 2)
        handles.append(ServerHandle(server_id=container_id, label=name, created_at=created_at))
    return handles


@runtime_checkable
class Provider(Protocol):
    """Protocol defining the interface all provider implementations must satisfy."""

    ssh_key_path: Path
    host_arch: str  # "x86_64" or "arm64" — build host's native arch

    async def provision(self) -> str:
        """Provision the build host and return its address or identifier."""
        ...

    def make_executor(self, target: str) -> CommandExecutor:
        """Create the executor used to run build commands on the target."""
        ...

    async def download_artifacts(
        self,
        target: str,
        artifacts: dict[str, str],
        local_dest: Path,
    ) -> None:
        """Download artifacts produced on the target to local_dest."""
        ...

    async def teardown(self, success: bool, host: str) -> None:
        """Tear down the build host."""
        ...

    async def list_managed(self) -> list[ServerHandle]:
        """Return all resources managed by this provider."""
        ...


class HetznerProvider:
    """Wraps hetzner.py functions to implement the Provider protocol."""

    username: str = "root"
    host_arch: str = "x86_64"
    port: int = 22

    def __init__(self, config: HetznerConfig, keep_on_failure: bool = False) -> None:
        self._config = config
        self._keep_on_failure = keep_on_failure
        self.ssh_key_path = config.ssh_key_path
        self._handle: ServerHandle | None = None

    async def provision(self) -> str:
        """Create a Hetzner VPS and wait until SSH is reachable; returns public IP."""
        self._handle = await hetzner.create_server(self._config)
        return await hetzner.wait_ready(self._handle, self._config)

    def make_executor(self, target: str) -> CommandExecutor:
        return RemoteExecutor(
            host=target,
            username=self.username,
            key_path=self.ssh_key_path,
            port=self.port,
        )

    async def download_artifacts(
        self,
        target: str,
        artifacts: dict[str, str],
        local_dest: Path,
    ) -> None:
        await rsync_artifacts(
            target,
            self.ssh_key_path,
            artifacts,
            local_dest,
            username=self.username,
            port=self.port,
        )

    async def teardown(self, success: bool, host: str) -> None:
        """Destroy the VPS unless keep_on_failure is True and the build failed."""
        if self._handle is None:
            return
        if success or not self._keep_on_failure:
            await hetzner.destroy_server(self._handle, self._config)
        else:
            print(f"[kcb] Server kept alive at {host}")
            print(f"[kcb] Destroy: kcb cleanup {self._handle.server_id}")

    async def list_managed(self) -> list[ServerHandle]:
        """List all Hetzner VPS instances tagged managed-by=kcb."""
        return await hetzner.list_servers(self._config)


class LocalVMProvider:
    """Local VM provider — no provisioning or teardown required."""

    username: str
    ssh_key_path: Path
    port: int

    def __init__(self, config: LocalVMConfig) -> None:
        self._config = config
        self.host_arch = config.arch
        self.username = config.username
        self.ssh_key_path = config.ssh_key_path
        self.port = config.port

    async def provision(self) -> str:
        """Return the pre-configured host directly; no provisioning is performed."""
        return self._config.host

    def make_executor(self, target: str) -> CommandExecutor:
        return RemoteExecutor(
            host=target,
            username=self.username,
            key_path=self.ssh_key_path,
            port=self.port,
        )

    async def download_artifacts(
        self,
        target: str,
        artifacts: dict[str, str],
        local_dest: Path,
    ) -> None:
        await rsync_artifacts(
            target,
            self.ssh_key_path,
            artifacts,
            local_dest,
            username=self.username,
            port=self.port,
        )

    async def teardown(self, success: bool, host: str) -> None:
        """No-op: local VMs are never destroyed by kcb."""
        return None

    async def list_managed(self) -> list[ServerHandle]:
        """Local provider manages no resources; always returns an empty list."""
        return []


class DockerProvider:
    """Local Docker provider — starts a container and runs the build inside it."""

    host_arch: str
    ssh_key_path: Path

    def __init__(self, config: DockerConfig, keep_on_failure: bool = False) -> None:
        self._config = config
        self._keep_on_failure = keep_on_failure
        self.host_arch = config.arch
        self.ssh_key_path = config.ssh_key_path
        self._container_name: str | None = None

    async def provision(self) -> str:
        """Start a detached container and return its name."""
        container_name = self._config.container_name or f"kcb-build-{uuid.uuid4().hex[:8]}"
        rc, _, stderr = await _run_docker_command(
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "--hostname",
            container_name,
            "--label",
            "managed-by=kcb",
            "--label",
            "provider=docker",
            self._config.image,
            "sleep",
            "infinity",
        )
        if rc != 0:
            raise RuntimeError(f"docker run failed with exit code {rc}: {stderr.strip()}")
        self._container_name = container_name
        return container_name

    def make_executor(self, target: str) -> CommandExecutor:
        return DockerExecutor(target)

    async def download_artifacts(
        self,
        target: str,
        artifacts: dict[str, str],
        local_dest: Path,
    ) -> None:
        await docker_cp_artifacts(target, artifacts, local_dest)

    async def teardown(self, success: bool, host: str) -> None:
        """Remove the container unless keep_on_failure is enabled for a failed build."""
        if self._container_name is None:
            return
        if success or not self._keep_on_failure:
            rc, _, stderr = await _run_docker_command("rm", "-f", self._container_name)
            if rc != 0:
                raise RuntimeError(
                    f"docker rm failed with exit code {rc}: {stderr.strip()}"
                )
            self._container_name = None
        else:
            print(f"[kcb] Container kept alive as {host}")
            print(f"[kcb] Shell: docker exec -it {host} bash")

    async def list_managed(self) -> list[ServerHandle]:
        """List local Docker containers created by kcb."""
        return await _docker_list_managed()


def make_provider(config: BuildConfig) -> Provider:
    """Factory: select the right Provider implementation from BuildConfig."""
    if isinstance(config.provider, HetznerConfig):
        return HetznerProvider(config.provider, keep_on_failure=config.keep_on_failure)
    if isinstance(config.provider, LocalVMConfig):
        return LocalVMProvider(config.provider)
    if isinstance(config.provider, DockerConfig):
        return DockerProvider(config.provider, keep_on_failure=config.keep_on_failure)
    raise ValueError(f"Unknown provider type: {type(config.provider)}")
