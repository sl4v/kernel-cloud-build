"""Provider Protocol and implementations for kcb."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from kcb import hetzner
from kcb.config import BuildConfig, HetznerConfig, LocalVMConfig
from kcb.hetzner import ServerHandle


@runtime_checkable
class Provider(Protocol):
    """Protocol defining the interface all provider implementations must satisfy."""

    ssh_key_path: Path
    username: str
    host_arch: str  # "x86_64" or "arm64" — host machine's native arch
    port: int

    async def provision(self) -> str:
        """Provision the build host and return its host/IP address."""
        ...

    async def teardown(self, success: bool, host: str) -> None:
        """Tear down the build host.

        Args:
            success: Whether the build succeeded. Used by some providers to
                     decide whether to keep the server alive for debugging.
            host: The host/IP returned by provision(), for informational messages.
        """
        ...

    async def list_managed(self) -> list[ServerHandle]:
        """Return all servers managed by this provider (for orphan detection)."""
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

    def __init__(self, config: LocalVMConfig) -> None:
        self._config = config
        self.host_arch = config.arch
        self.username = config.username
        self.ssh_key_path = config.ssh_key_path
        self.port = config.port

    async def provision(self) -> str:
        """Return the pre-configured host directly; no provisioning is performed."""
        return self._config.host

    async def teardown(self, success: bool, host: str) -> None:
        """No-op: local VMs are never destroyed by kcb."""
        pass

    async def list_managed(self) -> list[ServerHandle]:
        """Local provider manages no servers; always returns an empty list."""
        return []


def make_provider(config: BuildConfig) -> Provider:
    """Factory: select the right Provider implementation from BuildConfig.

    Args:
        config: The top-level BuildConfig whose ``provider`` field determines
                which concrete Provider is instantiated.

    Returns:
        A Provider instance matching the configured provider type.

    Raises:
        ValueError: If the provider type is unknown (should not occur given
                    Pydantic discriminated union validation).
    """
    if isinstance(config.provider, HetznerConfig):
        return HetznerProvider(config.provider, keep_on_failure=config.keep_on_failure)
    elif isinstance(config.provider, LocalVMConfig):
        return LocalVMProvider(config.provider)
    else:
        raise ValueError(f"Unknown provider type: {type(config.provider)}")
