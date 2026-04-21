"""End-to-end integration tests for kcb.__main__.run()."""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kcb.__main__ import run
from kcb.config import BuildConfig, DockerConfig, HetznerConfig, LocalVMConfig
from kcb.hetzner import ServerHandle


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
    return BuildConfig(provider=HetznerConfig(api_token="test-token"), **kwargs)


def _make_mock_executor() -> MagicMock:
    executor = MagicMock()
    executor.connect = AsyncMock()
    executor.run = AsyncMock()
    executor.disconnect = AsyncMock()
    return executor


def _make_mock_provider(
    *,
    target: str = _FAKE_IP,
    provision_side_effect=None,
    teardown_side_effect=None,
    list_managed_return=None,
) -> MagicMock:
    provider = MagicMock()
    provider.host_arch = "x86_64"
    provider.ssh_key_path = _FAKE_SSH_KEY
    provider.provision = AsyncMock(return_value=target, side_effect=provision_side_effect)
    provider.teardown = AsyncMock(side_effect=teardown_side_effect)
    provider.list_managed = AsyncMock(return_value=[] if list_managed_return is None else list_managed_return)
    provider.download_artifacts = AsyncMock()
    provider.make_executor = MagicMock(return_value=_make_mock_executor())
    return provider


def _make_build_ns(
    *,
    bootstrap_side_effect=None,
    build_kernel_arch_side_effect=None,
    build_rootfs_arch_side_effect=None,
    build_syzkaller_side_effect=None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        bootstrap=AsyncMock(side_effect=bootstrap_side_effect),
        prepare_kernel_source=AsyncMock(),
        apply_kernel_patch=AsyncMock(),
        build_kernel_arch=AsyncMock(
            return_value=_KERNEL_ARTIFACTS,
            side_effect=build_kernel_arch_side_effect,
        ),
        prepare_rootfs_source=AsyncMock(),
        build_rootfs_arch=AsyncMock(
            return_value=_ROOTFS_ARTIFACTS,
            side_effect=build_rootfs_arch_side_effect,
        ),
        build_syzkaller=AsyncMock(
            return_value=_SYZKALLER_ARTIFACTS,
            side_effect=build_syzkaller_side_effect,
        ),
    )


async def test_full_build_success(tmp_path: Path) -> None:
    config = _make_config(output_dir=tmp_path)
    build_ns = _make_build_ns()
    provider = _make_mock_provider()

    with (
        patch("kcb.__main__.make_provider", return_value=provider),
        patch("kcb.__main__._build", new=build_ns),
    ):
        await run(config)

    executor = provider.make_executor.return_value
    provider.provision.assert_awaited_once()
    provider.make_executor.assert_called_once_with(_FAKE_IP)
    executor.connect.assert_awaited_once()
    executor.disconnect.assert_awaited_once()

    build_ns.bootstrap.assert_awaited_once()
    build_ns.prepare_kernel_source.assert_awaited_once()
    build_ns.build_kernel_arch.assert_awaited_once()
    build_ns.prepare_rootfs_source.assert_awaited_once()
    build_ns.build_rootfs_arch.assert_awaited_once()
    build_ns.build_syzkaller.assert_awaited_once()

    assert provider.download_artifacts.await_count == 3
    executor.run.assert_any_call("make -C /root/linux clean", log_prefix="kernel-clean-x86_64")
    executor.run.assert_any_call("rm -rf /root/buildroot-output-x86_64", log_prefix="rootfs-clean-x86_64")
    provider.teardown.assert_awaited_once_with(True, _FAKE_IP)
    provider.list_managed.assert_awaited_once()


async def test_build_failure_keep_off_destroys_target(tmp_path: Path) -> None:
    config = _make_config(keep_on_failure=False, output_dir=tmp_path)
    build_ns = _make_build_ns(build_kernel_arch_side_effect=RuntimeError("kernel exploded"))
    provider = _make_mock_provider()

    with (
        patch("kcb.__main__.make_provider", return_value=provider),
        patch("kcb.__main__._build", new=build_ns),
    ):
        with pytest.raises(RuntimeError, match="kernel exploded"):
            await run(config)

    provider.teardown.assert_awaited_once_with(False, _FAKE_IP)
    provider.download_artifacts.assert_not_awaited()
    build_ns.build_rootfs_arch.assert_not_awaited()
    build_ns.build_syzkaller.assert_not_awaited()
    provider.make_executor.return_value.disconnect.assert_awaited_once()


async def test_build_failure_keep_on_reports_remaining_resource(
    tmp_path: Path, capsys
) -> None:
    config = _make_config(keep_on_failure=True, output_dir=tmp_path)
    build_ns = _make_build_ns(build_kernel_arch_side_effect=RuntimeError("kernel exploded"))

    async def _fake_teardown(success: bool, host: str) -> None:
        if not success:
            print(f"[kcb] Server kept alive at {host}")
            print(f"[kcb] Destroy: kcb cleanup {_FAKE_HANDLE.server_id}")

    provider = _make_mock_provider(
        teardown_side_effect=_fake_teardown,
        list_managed_return=[_FAKE_HANDLE],
    )

    with (
        patch("kcb.__main__.make_provider", return_value=provider),
        patch("kcb.__main__._build", new=build_ns),
    ):
        with pytest.raises(RuntimeError, match="kernel exploded"):
            await run(config)

    captured = capsys.readouterr()
    assert _FAKE_IP in captured.out
    assert _FAKE_HANDLE.server_id in captured.out
    assert "Remaining kcb-managed resources" in captured.out


async def test_subset_build_kernel_only(tmp_path: Path) -> None:
    config = _make_config(components=["kernel"], output_dir=tmp_path)
    build_ns = _make_build_ns()
    provider = _make_mock_provider()

    with (
        patch("kcb.__main__.make_provider", return_value=provider),
        patch("kcb.__main__._build", new=build_ns),
    ):
        await run(config)

    build_ns.prepare_kernel_source.assert_awaited_once()
    build_ns.build_kernel_arch.assert_awaited_once()
    build_ns.prepare_rootfs_source.assert_not_awaited()
    build_ns.build_rootfs_arch.assert_not_awaited()
    build_ns.build_syzkaller.assert_not_awaited()
    provider.download_artifacts.assert_awaited_once()
    provider.make_executor.return_value.run.assert_awaited_once_with(
        "make -C /root/linux clean",
        log_prefix="kernel-clean-x86_64",
    )


async def test_keyboard_interrupt_destroys_target(tmp_path: Path) -> None:
    config = _make_config(output_dir=tmp_path)
    build_ns = _make_build_ns()
    provider = _make_mock_provider(provision_side_effect=KeyboardInterrupt)

    with (
        patch("kcb.__main__.make_provider", return_value=provider),
        patch("kcb.__main__._build", new=build_ns),
    ):
        with pytest.raises(KeyboardInterrupt):
            await run(config)

    provider.teardown.assert_awaited_once_with(False, "")
    provider.make_executor.assert_not_called()
    build_ns.bootstrap.assert_not_awaited()


def test_cleanup_not_applicable_for_local_provider() -> None:
    from click.testing import CliRunner
    from kcb.__main__ import main

    runner = CliRunner()
    local_config_yaml = """\
provider:
  type: local
  host: 192.168.1.10
  ssh_key_path: /tmp/id_rsa
"""
    result = runner.invoke(main, ["cleanup", "--list"], input=None, env={}, catch_exceptions=False)
    assert result.exit_code != 0

    with runner.isolated_filesystem():
        Path("config.yaml").write_text(local_config_yaml)
        result = runner.invoke(main, ["cleanup", "--list", "--config", "config.yaml"])

    assert result.exit_code == 0
    assert "only applicable to the Hetzner provider" in result.output


def test_cleanup_not_applicable_for_docker_provider() -> None:
    from click.testing import CliRunner
    from kcb.__main__ import main

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("config.yaml").write_text("provider:\n  type: docker\n")
        result = runner.invoke(main, ["cleanup", "--list", "--config", "config.yaml"])

    assert result.exit_code == 0
    assert "only applicable to the Hetzner provider" in result.output


async def test_local_provider_run_full_build(tmp_path: Path) -> None:
    config = BuildConfig(
        provider=LocalVMConfig(host="192.168.64.10", ssh_key_path=Path("/tmp/id_rsa")),
        output_dir=tmp_path,
    )
    build_ns = _make_build_ns()
    executor = _make_mock_executor()

    with (
        patch("kcb.__main__._build", new=build_ns),
        patch("kcb.providers.RemoteExecutor", return_value=executor) as remote_executor,
        patch("kcb.providers.rsync_artifacts", new=AsyncMock()) as rsync_mock,
    ):
        await run(config)

    remote_executor.assert_called_once_with(
        host="192.168.64.10",
        username="root",
        key_path=Path("/tmp/id_rsa"),
        port=22,
    )
    executor.connect.assert_awaited_once()
    executor.disconnect.assert_awaited_once()
    assert rsync_mock.await_count == 3


async def test_docker_provider_run_full_build(tmp_path: Path) -> None:
    config = BuildConfig(provider=DockerConfig(container_name="kcb-dev"), output_dir=tmp_path)
    build_ns = _make_build_ns()
    executor = _make_mock_executor()

    docker_cmd = AsyncMock(side_effect=[
        (0, "cid\n", ""),
        (0, "", ""),
        (0, "", ""),
    ])

    with (
        patch("kcb.__main__._build", new=build_ns),
        patch("kcb.providers.DockerExecutor", return_value=executor) as docker_executor,
        patch("kcb.providers.docker_cp_artifacts", new=AsyncMock()) as docker_cp,
        patch("kcb.providers._run_docker_command", new=docker_cmd),
    ):
        await run(config)

    docker_executor.assert_called_once_with("kcb-dev")
    executor.connect.assert_awaited_once()
    executor.disconnect.assert_awaited_once()
    assert docker_cp.await_count == 3
