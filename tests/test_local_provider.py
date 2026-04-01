"""Tests for LocalVMProvider and make_provider factory in kcb/providers.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kcb.config import BuildConfig, HetznerConfig, LocalVMConfig
from kcb.providers import HetznerProvider, LocalVMProvider, Provider, make_provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_TOKEN = "fake-token"


def _make_local_config(
    host: str = "192.168.1.10",
    port: int = 22,
    username: str = "root",
    arch: str = "x86_64",
    ssh_key_path: Path = Path("~/.ssh/id_rsa"),
) -> LocalVMConfig:
    return LocalVMConfig(
        host=host,
        port=port,
        username=username,
        arch=arch,  # type: ignore[arg-type]
        ssh_key_path=ssh_key_path,
    )


def _make_build_config_local(**kwargs) -> BuildConfig:
    local_cfg = _make_local_config(**kwargs)
    return BuildConfig(provider=local_cfg)


def _make_build_config_hetzner(**kwargs) -> BuildConfig:
    hetzner_cfg = HetznerConfig(api_token=_FAKE_TOKEN, **kwargs)
    return BuildConfig(provider=hetzner_cfg)


# ---------------------------------------------------------------------------
# LocalVMProvider tests
# ---------------------------------------------------------------------------


async def test_local_provision_returns_host() -> None:
    """provision() returns the configured host without any side effects."""
    config = _make_local_config(host="10.0.0.5")
    provider = LocalVMProvider(config)
    result = await provider.provision()
    assert result == "10.0.0.5"


async def test_local_teardown_is_noop() -> None:
    """teardown() completes without error and returns None."""
    config = _make_local_config()
    provider = LocalVMProvider(config)
    result = await provider.teardown(success=True, host="10.0.0.5")
    assert result is None

    result = await provider.teardown(success=False, host="10.0.0.5")
    assert result is None


async def test_local_list_managed_empty() -> None:
    """list_managed() always returns an empty list."""
    config = _make_local_config()
    provider = LocalVMProvider(config)
    handles = await provider.list_managed()
    assert handles == []


def test_local_provider_host_arch_x86_64() -> None:
    """host_arch matches the arch configured in LocalVMConfig (x86_64)."""
    config = _make_local_config(arch="x86_64")
    provider = LocalVMProvider(config)
    assert provider.host_arch == "x86_64"


def test_local_provider_host_arch_arm64() -> None:
    """host_arch matches the arch configured in LocalVMConfig (arm64)."""
    config = _make_local_config(arch="arm64")
    provider = LocalVMProvider(config)
    assert provider.host_arch == "arm64"


def test_local_provider_host_arch() -> None:
    """host_arch is propagated correctly from config (parametrized via separate tests)."""
    for arch in ("x86_64", "arm64"):
        config = _make_local_config(arch=arch)  # type: ignore[arg-type]
        provider = LocalVMProvider(config)
        assert provider.host_arch == arch


def test_local_provider_ssh_key_path() -> None:
    """ssh_key_path is taken from config."""
    key = Path("/home/user/.ssh/vm_rsa")
    config = _make_local_config(ssh_key_path=key)
    provider = LocalVMProvider(config)
    assert provider.ssh_key_path == key


def test_local_provider_username() -> None:
    """username is taken from config."""
    config = _make_local_config(username="ubuntu")
    provider = LocalVMProvider(config)
    assert provider.username == "ubuntu"


def test_local_provider_satisfies_protocol() -> None:
    """LocalVMProvider structurally satisfies the Provider protocol."""
    config = _make_local_config()
    provider = LocalVMProvider(config)
    assert isinstance(provider, Provider)


# ---------------------------------------------------------------------------
# make_provider factory tests
# ---------------------------------------------------------------------------


def test_make_provider_returns_hetzner_provider() -> None:
    """make_provider returns a HetznerProvider when the config uses HetznerConfig."""
    config = _make_build_config_hetzner()
    provider = make_provider(config)
    assert isinstance(provider, HetznerProvider)


def test_make_provider_returns_local_provider() -> None:
    """make_provider returns a LocalVMProvider when the config uses LocalVMConfig."""
    config = _make_build_config_local()
    provider = make_provider(config)
    assert isinstance(provider, LocalVMProvider)


def test_make_provider_hetzner_passes_keep_on_failure() -> None:
    """make_provider passes keep_on_failure from BuildConfig to HetznerProvider."""
    config = _make_build_config_hetzner()
    config = config.model_copy(update={"keep_on_failure": True})
    provider = make_provider(config)
    assert isinstance(provider, HetznerProvider)
    assert provider._keep_on_failure is True


def test_local_provider_port_default() -> None:
    config = _make_local_config(port=22)
    provider = LocalVMProvider(config)
    assert provider.port == 22


def test_local_provider_port_custom() -> None:
    config = _make_local_config(port=2222)
    provider = LocalVMProvider(config)
    assert provider.port == 2222


def test_hetzner_provider_port_is_22() -> None:
    config = HetznerConfig(api_token=_FAKE_TOKEN)
    provider = HetznerProvider(config)
    assert provider.port == 22


def test_make_provider_local_satisfies_protocol() -> None:
    """The provider returned by make_provider for local config satisfies the Protocol."""
    config = _make_build_config_local()
    provider = make_provider(config)
    assert isinstance(provider, Provider)


def test_make_provider_hetzner_satisfies_protocol() -> None:
    """The provider returned by make_provider for Hetzner config satisfies the Protocol."""
    config = _make_build_config_hetzner()
    provider = make_provider(config)
    assert isinstance(provider, Provider)
