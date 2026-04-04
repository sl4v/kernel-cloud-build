"""End-to-end integration tests for kcb.__main__.run().

All external I/O is mocked: make_provider (returns a mock Provider), RemoteExecutor,
and asyncio.create_subprocess_exec (used internally by rsync_artifacts).
Tests cover the full orchestration path and keep_on_failure semantics.

Implementation note on patching ``_build``:
  Inside ``kcb/__main__.py`` the build module is imported as ``_build``
  (``from kcb import build as _build``) to avoid the name collision with
  the Click ``@main.command()`` function named ``build``.  All call sites
  in ``run()`` use ``_build.<func>``, so the correct patch target is
  ``kcb.__main__._build``.

Implementation note on patching provider:
  ``run()`` calls ``make_provider(config)`` which is imported from
  ``kcb.providers``.  We patch ``kcb.__main__.make_provider`` so that the
  mock provider is returned instead of a real HetznerProvider.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kcb.__main__ import run
from kcb.config import BuildConfig, HetznerConfig, LocalVMConfig
from kcb.hetzner import ServerHandle


# ---------------------------------------------------------------------------
# Constants / shared fixtures
# ---------------------------------------------------------------------------

_FAKE_HANDLE = ServerHandle(server_id="99", label="kcb-fake", created_at="2026-01-01")
_FAKE_IP = "1.2.3.4"
_FAKE_SSH_KEY = Path("/tmp/id_rsa")

_KERNEL_ARTIFACTS = {
    "bzImage": "/root/linux/arch/x86/boot/bzImage",
    "vmlinux": "/root/linux/vmlinux",
}
_ROOTFS_ARTIFACTS = {"rootfs": "/root/buildroot-output-x86_64/images/rootfs.ext4"}
_SYZKALLER_ARTIFACTS = {
    "x86_64": {"linux": "/root/syzkaller/bin/linux_amd64", "host": "/root/syzkaller/bin/host_amd64"},
}


def _make_config(**kwargs) -> BuildConfig:
    """Return a minimal BuildConfig with a fake API token (Hetzner provider)."""
    return BuildConfig(
        provider=HetznerConfig(api_token="test-token"),
        **kwargs,
    )


def _make_mock_provider(
    *,
    provision_side_effect=None,
    teardown_side_effect=None,
    list_managed_return=None,
) -> MagicMock:
    """Build a mock Provider with configurable behaviours.

    Returned object satisfies the Provider protocol:
      - provision: AsyncMock returning _FAKE_IP
      - teardown: AsyncMock (no-op unless side_effect given)
      - list_managed: AsyncMock returning [] by default
      - username, ssh_key_path, host_arch set to sane defaults
    """
    provider = MagicMock()
    provider.username = "root"
    provider.ssh_key_path = _FAKE_SSH_KEY
    provider.host_arch = "x86_64"
    provider.port = 22
    provider.provision = AsyncMock(
        return_value=_FAKE_IP,
        side_effect=provision_side_effect,
    )
    provider.teardown = AsyncMock(side_effect=teardown_side_effect)
    provider.list_managed = AsyncMock(
        return_value=[] if list_managed_return is None else list_managed_return
    )
    return provider


def _make_build_ns(
    *,
    bootstrap_side_effect=None,
    build_kernel_arch_side_effect=None,
    build_rootfs_arch_side_effect=None,
    build_syzkaller_side_effect=None,
) -> types.SimpleNamespace:
    """Return a SimpleNamespace that mimics the ``kcb.build`` module interface.

    Patched in as ``kcb.__main__._build`` so that ``run()``'s calls to
    ``_build.bootstrap``, ``_build.prepare_kernel_source``, etc. resolve to
    the mocks.  Individual side_effect overrides allow failure injection per test.
    """
    bootstrap = AsyncMock(side_effect=bootstrap_side_effect)
    prepare_kernel_source = AsyncMock()
    apply_kernel_patch = AsyncMock()
    build_kernel_arch = AsyncMock(
        return_value=_KERNEL_ARTIFACTS,
        side_effect=build_kernel_arch_side_effect,
    )
    prepare_rootfs_source = AsyncMock()
    build_rootfs_arch = AsyncMock(
        return_value=_ROOTFS_ARTIFACTS,
        side_effect=build_rootfs_arch_side_effect,
    )
    build_syzkaller = AsyncMock(
        return_value=_SYZKALLER_ARTIFACTS,
        side_effect=build_syzkaller_side_effect,
    )
    rsync_artifacts = AsyncMock()
    return types.SimpleNamespace(
        bootstrap=bootstrap,
        prepare_kernel_source=prepare_kernel_source,
        apply_kernel_patch=apply_kernel_patch,
        build_kernel_arch=build_kernel_arch,
        prepare_rootfs_source=prepare_rootfs_source,
        build_rootfs_arch=build_rootfs_arch,
        build_syzkaller=build_syzkaller,
        rsync_artifacts=rsync_artifacts,
    )


def _make_executor_cls() -> MagicMock:
    """Return a MagicMock that acts as the RemoteExecutor class.

    Calling the mock (i.e. instantiating the class) returns an object whose
    ``connect`` and ``run`` methods are AsyncMocks.
    """
    instance = MagicMock()
    instance.connect = AsyncMock()
    instance.run = AsyncMock()
    cls = MagicMock(return_value=instance)
    return cls


# ---------------------------------------------------------------------------
# 1. Full build success — all three components
# ---------------------------------------------------------------------------


async def test_full_build_success(tmp_path: Path) -> None:
    """Happy path: all three components build, teardown called at end."""
    config = _make_config(output_dir=tmp_path)
    build_ns = _make_build_ns()
    executor_cls = _make_executor_cls()
    mock_provider = _make_mock_provider()

    with (
        patch("kcb.__main__.make_provider", return_value=mock_provider),
        patch("kcb.__main__.RemoteExecutor", new=executor_cls),
        patch("kcb.__main__._build", new=build_ns),
    ):
        await run(config)

    # Provisioning lifecycle
    mock_provider.provision.assert_called_once()

    # Executor was instantiated with the correct host, username, key path, and port
    executor_cls.assert_called_once_with(
        host=_FAKE_IP,
        username="root",
        key_path=_FAKE_SSH_KEY,
        port=22,
    )
    executor_cls.return_value.connect.assert_called_once()

    # Bootstrap ran
    build_ns.bootstrap.assert_called_once()

    # All three build steps issued
    build_ns.prepare_kernel_source.assert_called_once()
    build_ns.build_kernel_arch.assert_called_once()
    build_ns.prepare_rootfs_source.assert_called_once()
    build_ns.build_rootfs_arch.assert_called_once()
    build_ns.build_syzkaller.assert_called_once()

    # rsync called once per component: kernel, rootfs, syzkaller (per-arch)
    assert build_ns.rsync_artifacts.call_count == 3

    # disk cleanup: kernel clean and rootfs dir removal called once each (one arch each)
    executor = executor_cls.return_value
    executor.run.assert_any_call("make -C /root/linux clean", log_prefix="kernel-clean-x86_64")
    executor.run.assert_any_call("rm -rf /root/buildroot-output-x86_64", log_prefix="rootfs-clean-x86_64")
    assert executor.run.call_count == 2

    # teardown called with success=True
    mock_provider.teardown.assert_called_once_with(True, _FAKE_IP)

    # list_managed called to report remaining servers
    mock_provider.list_managed.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Build failure, keep_on_failure=False — teardown receives success=False
# ---------------------------------------------------------------------------


async def test_build_failure_keep_off_destroys_server(tmp_path: Path) -> None:
    """When build_kernel raises and keep_on_failure=False, teardown is called with success=False."""
    config = _make_config(keep_on_failure=False, output_dir=tmp_path)
    build_ns = _make_build_ns(build_kernel_arch_side_effect=RuntimeError("kernel exploded"))
    executor_cls = _make_executor_cls()
    mock_provider = _make_mock_provider()

    with (
        patch("kcb.__main__.make_provider", return_value=mock_provider),
        patch("kcb.__main__.RemoteExecutor", new=executor_cls),
        patch("kcb.__main__._build", new=build_ns),
    ):
        with pytest.raises(RuntimeError, match="kernel exploded"):
            await run(config)

    # teardown must be called with success=False
    mock_provider.teardown.assert_called_once_with(False, _FAKE_IP)

    # rootfs/syzkaller/rsync should not have been reached after kernel failure
    build_ns.build_rootfs_arch.assert_not_called()
    build_ns.build_syzkaller.assert_not_called()
    build_ns.rsync_artifacts.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Build failure, keep_on_failure=True — HetznerProvider keeps server alive
# ---------------------------------------------------------------------------


async def test_build_failure_keep_on_does_not_destroy(tmp_path: Path, capsys) -> None:
    """When build_kernel raises and keep_on_failure=True, provider receives success=False.

    The HetznerProvider is responsible for printing the keep-alive message; here we
    verify that teardown is called with success=False and the provider prints the IP.
    """
    config = _make_config(keep_on_failure=True, output_dir=tmp_path)
    build_ns = _make_build_ns(build_kernel_arch_side_effect=RuntimeError("kernel exploded"))
    executor_cls = _make_executor_cls()

    # Simulate provider printing a keep-alive message (as HetznerProvider does)
    async def _fake_teardown(success: bool, host: str) -> None:
        if not success:
            print(f"[kcb] Server kept alive at {host}")
            print(f"[kcb] Destroy: kcb cleanup {_FAKE_HANDLE.server_id}")

    mock_provider = _make_mock_provider(
        list_managed_return=[_FAKE_HANDLE],
        teardown_side_effect=_fake_teardown,
    )

    with (
        patch("kcb.__main__.make_provider", return_value=mock_provider),
        patch("kcb.__main__.RemoteExecutor", new=executor_cls),
        patch("kcb.__main__._build", new=build_ns),
    ):
        with pytest.raises(RuntimeError, match="kernel exploded"):
            await run(config)

    # teardown received success=False
    mock_provider.teardown.assert_called_once_with(False, _FAKE_IP)

    # A "kept alive" message should have been printed with the IP and server ID
    captured = capsys.readouterr()
    assert _FAKE_IP in captured.out
    assert _FAKE_HANDLE.server_id in captured.out


# ---------------------------------------------------------------------------
# 4. Subset build: kernel only — rootfs/syzkaller NOT called
# ---------------------------------------------------------------------------


async def test_subset_build_kernel_only(tmp_path: Path) -> None:
    """config with components=['kernel'] only calls build_kernel, not rootfs/syzkaller."""
    config = _make_config(components=["kernel"], output_dir=tmp_path)
    build_ns = _make_build_ns()
    executor_cls = _make_executor_cls()
    mock_provider = _make_mock_provider()

    with (
        patch("kcb.__main__.make_provider", return_value=mock_provider),
        patch("kcb.__main__.RemoteExecutor", new=executor_cls),
        patch("kcb.__main__._build", new=build_ns),
    ):
        await run(config)

    build_ns.prepare_kernel_source.assert_called_once()
    build_ns.build_kernel_arch.assert_called_once()
    build_ns.prepare_rootfs_source.assert_not_called()
    build_ns.build_rootfs_arch.assert_not_called()
    build_ns.build_syzkaller.assert_not_called()

    # rsync still runs (only kernel artifacts are passed)
    build_ns.rsync_artifacts.assert_called_once()

    # disk cleanup: only kernel clean, no rootfs clean
    executor = executor_cls.return_value
    executor.run.assert_called_once_with("make -C /root/linux clean", log_prefix="kernel-clean-x86_64")

    # teardown still called on success
    mock_provider.teardown.assert_called_once_with(True, _FAKE_IP)


# ---------------------------------------------------------------------------
# 5. KeyboardInterrupt during provision — finally block still calls teardown
# ---------------------------------------------------------------------------


async def test_keyboard_interrupt_destroys_server(tmp_path: Path) -> None:
    """KeyboardInterrupt propagates out of run() but the finally block fires first."""
    config = _make_config(output_dir=tmp_path)
    build_ns = _make_build_ns()
    executor_cls = _make_executor_cls()
    mock_provider = _make_mock_provider(
        provision_side_effect=KeyboardInterrupt,
    )

    with (
        patch("kcb.__main__.make_provider", return_value=mock_provider),
        patch("kcb.__main__.RemoteExecutor", new=executor_cls),
        patch("kcb.__main__._build", new=build_ns),
    ):
        with pytest.raises(KeyboardInterrupt):
            await run(config)

    # finally block must have run — teardown called with success=False
    mock_provider.teardown.assert_called_once_with(False, "")

    # No build steps should have been attempted
    build_ns.bootstrap.assert_not_called()
    build_ns.prepare_kernel_source.assert_not_called()
    build_ns.build_kernel_arch.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Cleanup command prints "not applicable" for local provider
# ---------------------------------------------------------------------------


def test_cleanup_not_applicable_for_local_provider() -> None:
    """cleanup command exits cleanly with a message for local provider configs."""
    from click.testing import CliRunner
    from kcb.__main__ import main

    runner = CliRunner()
    # Use a minimal local provider config written to a temp file via mix_stderr=False
    import tempfile, os

    local_config_yaml = """\
provider:
  type: local
  host: 192.168.1.10
  ssh_key_path: /tmp/id_rsa
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(local_config_yaml)
        config_path = f.name

    try:
        result = runner.invoke(main, ["cleanup", "--list", "--config", config_path])
    finally:
        os.unlink(config_path)

    assert result.exit_code == 0
    assert "not applicable" in result.output


# ---------------------------------------------------------------------------
# 7. Local provider run — provision returns host directly, teardown is no-op
# ---------------------------------------------------------------------------


async def test_local_provider_run_full_build(tmp_path: Path) -> None:
    """run() with a LocalVMConfig: provision returns host directly, teardown is no-op."""
    config = BuildConfig(
        provider=LocalVMConfig(host="192.168.64.10", ssh_key_path=Path("/tmp/id_rsa")),
        output_dir=tmp_path,
    )
    build_ns = _make_build_ns()
    executor_cls = _make_executor_cls()
    # Don't mock make_provider — let the real LocalVMProvider run
    with (
        patch("kcb.__main__.RemoteExecutor", new=executor_cls),
        patch("kcb.__main__._build", new=build_ns),
    ):
        await run(config)

    # provision() returns the configured host directly
    executor_cls.assert_called_once_with(
        host="192.168.64.10",
        username="root",
        key_path=Path("/tmp/id_rsa"),
        port=22,
    )
    executor_cls.return_value.connect.assert_called_once()

    # Bootstrap ran
    build_ns.bootstrap.assert_called_once()

    # All three build steps issued
    build_ns.prepare_kernel_source.assert_called_once()
    build_ns.build_kernel_arch.assert_called_once()
    build_ns.prepare_rootfs_source.assert_called_once()
    build_ns.build_rootfs_arch.assert_called_once()
    build_ns.build_syzkaller.assert_called_once()

    # rsync called 3 times (one per component)
    assert build_ns.rsync_artifacts.call_count == 3

    # disk cleanup: kernel clean and rootfs dir removal called once each
    executor = executor_cls.return_value
    executor.run.assert_any_call("make -C /root/linux clean", log_prefix="kernel-clean-x86_64")
    executor.run.assert_any_call("rm -rf /root/buildroot-output-x86_64", log_prefix="rootfs-clean-x86_64")
    assert executor.run.call_count == 2
