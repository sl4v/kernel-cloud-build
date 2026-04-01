"""Unit tests for kcb/build.py with a mocked RemoteExecutor."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from kcb.build import (
    _SCRIPTS_DIR,
    bootstrap,
    build_kernel_arch,
    build_rootfs_arch,
    build_syzkaller,
    prepare_kernel_source,
    prepare_rootfs_source,
    rsync_artifacts,
)
from kcb.config import (
    BuildConfig,
    BuildrootConfig,
    KernelConfig,
    ProviderConfig,
    SyzkallerConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor() -> AsyncMock:
    """Return a fully-mocked RemoteExecutor with async run/upload_file methods."""
    executor = AsyncMock()
    executor.run = AsyncMock(return_value=0)
    executor.upload_file = AsyncMock(return_value=None)
    return executor


def _make_config(**kwargs) -> BuildConfig:
    """Return a minimal BuildConfig with a dummy API token."""
    return BuildConfig(
        provider=ProviderConfig(api_token="dummy-token"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. bootstrap()
# ---------------------------------------------------------------------------


async def test_bootstrap_uploads_and_runs_script() -> None:
    """bootstrap() should check disk space, upload bootstrap.sh, then run it via chmod + exec."""
    executor = _make_executor()
    config = _make_config()

    await bootstrap(executor, config)

    expected_local = _SCRIPTS_DIR / "bootstrap.sh"
    executor.upload_file.assert_called_once_with(expected_local, "/root/bootstrap.sh")

    run_calls = executor.run.call_args_list
    assert len(run_calls) == 2, f"Expected 2 run() calls (disk-check + bootstrap), got {len(run_calls)}"

    # First call: disk space check
    first_cmd, first_kwargs = run_calls[0].args[0], run_calls[0].kwargs
    assert "df -BG" in first_cmd, "Expected df -BG disk-check as first run() call"
    assert "exit 1" in first_cmd, "Expected exit 1 in disk-check command for fatal low-space case"
    assert first_kwargs.get("log_prefix") == "disk-check"

    # Second call: run bootstrap.sh
    second_cmd, second_kwargs = run_calls[1].args[0], run_calls[1].kwargs
    assert second_cmd == "chmod +x /root/bootstrap.sh && /root/bootstrap.sh"
    assert second_kwargs.get("log_prefix") == "bootstrap"


async def test_bootstrap_disk_check_aborts_on_low_space() -> None:
    """bootstrap() propagates RuntimeError when the disk-check command exits non-zero."""
    executor = _make_executor()
    # Simulate the remote awk script exiting 1 (< 5 GB free)
    executor.run = AsyncMock(side_effect=RuntimeError("remote command exited with code 1"))
    config = _make_config()

    with pytest.raises(RuntimeError):
        await bootstrap(executor, config)


# ---------------------------------------------------------------------------
# 2. prepare_kernel_source() + build_kernel_arch() — x86_64 only
# ---------------------------------------------------------------------------


async def test_build_kernel_x86_64_only() -> None:
    """prepare_kernel_source + build_kernel_arch x86_64: clone, defconfig, bzImage."""
    executor = _make_executor()
    config = _make_config(kernel=KernelConfig(targets=["x86_64"]))

    await prepare_kernel_source(executor, config)
    result = await build_kernel_arch(executor, config, "x86_64")

    calls = [c.args[0] for c in executor.run.call_args_list]

    # Git clone must be present (from prepare_kernel_source)
    assert any("git clone" in c and "/root/linux" in c for c in calls), (
        "Expected git clone command"
    )
    # x86_64 make: defconfig and bzImage are separate commands
    assert any("defconfig" in c and "bzImage" not in c for c in calls), (
        "Expected separate defconfig command"
    )
    assert any("bzImage" in c for c in calls), "Expected bzImage make command"
    # No arm64
    assert not any("arm64" in c for c in calls), "Unexpected arm64 command"

    assert "bzImage" in result
    assert result["bzImage"] == "/root/linux/arch/x86/boot/bzImage"
    assert "vmlinux" in result
    assert result["vmlinux"] == "/root/linux/vmlinux"
    assert "Image" not in result


# ---------------------------------------------------------------------------
# 3. prepare_kernel_source() + build_kernel_arch() — both arches
# ---------------------------------------------------------------------------


async def test_build_kernel_both_arches() -> None:
    """prepare_kernel_source + build_kernel_arch for x86_64 and arm64."""
    executor = _make_executor()
    config = _make_config(kernel=KernelConfig(targets=["x86_64", "arm64"]))

    await prepare_kernel_source(executor, config)
    result_x86 = await build_kernel_arch(executor, config, "x86_64")
    result_arm64 = await build_kernel_arch(executor, config, "arm64")

    calls = [c.args[0] for c in executor.run.call_args_list]

    assert any("bzImage" in c for c in calls), "Expected x86_64 bzImage make"
    assert any("ARCH=arm64" in c and "defconfig" in c for c in calls), "Expected arm64 defconfig"
    assert any("ARCH=arm64" in c and "Image" in c and "defconfig" not in c for c in calls), (
        "Expected arm64 Image make"
    )

    assert "bzImage" in result_x86
    assert "Image" in result_arm64
    assert result_arm64["Image"] == "/root/linux/arch/arm64/boot/Image"
    assert "vmlinux" in result_arm64
    assert result_arm64["vmlinux"] == "/root/linux/vmlinux"


# ---------------------------------------------------------------------------
# 4. prepare_kernel_source() — tarball source
# ---------------------------------------------------------------------------


async def test_build_kernel_from_tarball() -> None:
    """prepare_kernel_source with tarball_url uses wget+tar instead of git clone, then cleans up."""
    executor = _make_executor()
    tarball = "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.13.tar.xz"
    config = _make_config(
        kernel=KernelConfig(targets=["x86_64"], tarball_url=tarball)
    )

    await prepare_kernel_source(executor, config)
    result = await build_kernel_arch(executor, config, "x86_64")

    calls = [c.args[0] for c in executor.run.call_args_list]
    prefixes = [c.kwargs.get("log_prefix") for c in executor.run.call_args_list]

    # Should download the tarball
    assert any("wget" in c and tarball in c for c in calls), "Expected wget with tarball URL"
    # Should extract with --strip-components=1
    assert any("--strip-components=1" in c and "/root/linux" in c for c in calls), (
        "Expected tar --strip-components=1 into /root/linux"
    )
    # Must NOT git clone
    assert not any("git clone" in c for c in calls), "Unexpected git clone when tarball_url is set"
    # Tarball cleanup must be issued
    assert any("rm -f /root/linux.tar" in c for c in calls), (
        "Expected rm -f /root/linux.tar cleanup after extraction"
    )
    assert "kernel-cleanup" in prefixes, "Expected log_prefix='kernel-cleanup' for tarball rm"

    assert result["bzImage"] == "/root/linux/arch/x86/boot/bzImage"
    assert result["vmlinux"] == "/root/linux/vmlinux"


# ---------------------------------------------------------------------------
# 4b. build_kernel_arch() — cross-compile matrix
# ---------------------------------------------------------------------------


async def test_build_kernel_arm64_on_arm64_host() -> None:
    """arm64 target on arm64 host: native build — no CROSS_COMPILE in make commands."""
    executor = _make_executor()
    config = _make_config(kernel=KernelConfig(targets=["arm64"]))

    result = await build_kernel_arch(executor, config, "arm64", host_arch="arm64")

    calls = [c.args[0] for c in executor.run.call_args_list]
    assert any("ARCH=arm64" in c and "defconfig" in c for c in calls), (
        "Expected arm64 defconfig"
    )
    assert not any("CROSS_COMPILE" in c for c in calls), (
        "Native arm64 build must not set CROSS_COMPILE"
    )
    assert "Image" in result
    assert result["Image"] == "/root/linux/arch/arm64/boot/Image"


async def test_build_kernel_x86_64_native() -> None:
    """x86_64 target on x86_64 host: ARCH=x86_64 set, no CROSS_COMPILE."""
    executor = _make_executor()
    config = _make_config(kernel=KernelConfig(targets=["x86_64"]))

    result = await build_kernel_arch(executor, config, "x86_64", host_arch="x86_64")

    calls = [c.args[0] for c in executor.run.call_args_list]
    assert any("ARCH=x86_64" in c and "defconfig" in c for c in calls), (
        "Expected ARCH=x86_64 in defconfig make command"
    )
    assert any("ARCH=x86_64" in c and "bzImage" in c for c in calls), (
        "Expected ARCH=x86_64 in bzImage make command"
    )
    assert not any("CROSS_COMPILE" in c for c in calls), (
        "Native x86_64 build must not set CROSS_COMPILE"
    )
    assert "bzImage" in result
    assert result["bzImage"] == "/root/linux/arch/x86/boot/bzImage"


async def test_build_kernel_x86_64_on_arm64_host() -> None:
    """x86_64 target on arm64 host: ARCH=x86_64 and CROSS_COMPILE=x86_64-linux-gnu-."""
    executor = _make_executor()
    config = _make_config(kernel=KernelConfig(targets=["x86_64"]))

    result = await build_kernel_arch(executor, config, "x86_64", host_arch="arm64")

    calls = [c.args[0] for c in executor.run.call_args_list]
    assert any("ARCH=x86_64" in c and "defconfig" in c for c in calls), (
        "Expected ARCH=x86_64 in defconfig make command"
    )
    assert any("ARCH=x86_64" in c and "bzImage" in c for c in calls), (
        "Expected ARCH=x86_64 in bzImage make command"
    )
    assert any("CROSS_COMPILE=x86_64-linux-gnu-" in c for c in calls), (
        "Expected CROSS_COMPILE=x86_64-linux-gnu- when cross-compiling x86_64 on arm64 host"
    )
    assert "bzImage" in result
    assert result["bzImage"] == "/root/linux/arch/x86/boot/bzImage"


async def test_build_kernel_arm64_on_x86_64_host() -> None:
    """arm64 target on x86_64 host: cross-compile to aarch64-linux-gnu- (regression guard)."""
    executor = _make_executor()
    config = _make_config(kernel=KernelConfig(targets=["arm64"]))

    result = await build_kernel_arch(executor, config, "arm64", host_arch="x86_64")

    calls = [c.args[0] for c in executor.run.call_args_list]
    assert any("CROSS_COMPILE=aarch64-linux-gnu-" in c for c in calls), (
        "Expected CROSS_COMPILE=aarch64-linux-gnu- for arm64 target on x86_64 host"
    )
    assert any("ARCH=arm64" in c for c in calls), "Expected ARCH=arm64"
    assert "Image" in result


# ---------------------------------------------------------------------------
# 5. build_kernel_arch() — with config_overlay
# ---------------------------------------------------------------------------


async def test_build_kernel_with_config_overlay(tmp_path: Path) -> None:
    """build_kernel_arch uploads overlay and runs merge_config.sh when config_overlay is set."""
    overlay = tmp_path / "my.config"
    overlay.write_text("CONFIG_FOO=y\n")

    executor = _make_executor()
    config = _make_config(
        kernel=KernelConfig(targets=["x86_64"], config_overlays=[overlay])
    )

    await prepare_kernel_source(executor, config)
    await build_kernel_arch(executor, config, "x86_64")

    upload_calls = executor.upload_file.call_args_list
    run_calls = [c.args[0] for c in executor.run.call_args_list]

    # Overlay file should have been uploaded
    assert any(
        call_args.args[0] == overlay for call_args in upload_calls
    ), "Expected overlay file upload"

    # merge_config.sh should have been invoked
    assert any("merge_config.sh" in c for c in run_calls), (
        "Expected merge_config.sh invocation"
    )


# ---------------------------------------------------------------------------
# 5. prepare_rootfs_source() + build_rootfs_arch()
# ---------------------------------------------------------------------------


async def test_prepare_rootfs_source() -> None:
    """prepare_rootfs_source() issues wget, tar, and cleanup commands."""
    executor = _make_executor()
    version = "2024.02"
    config = _make_config(rootfs=BuildrootConfig(version=version))

    await prepare_rootfs_source(executor, config)

    calls = [c.args[0] for c in executor.run.call_args_list]
    prefixes = [c.kwargs.get("log_prefix") for c in executor.run.call_args_list]

    assert any(f"buildroot-{version}.tar.gz" in c and "wget" in c for c in calls), (
        "Expected wget download command"
    )
    assert any("tar -xf" in c for c in calls), "Expected tar extract command"
    assert any("rm -f /root/buildroot.tar.gz" in c for c in calls), (
        "Expected rm -f /root/buildroot.tar.gz cleanup after extraction"
    )
    assert "rootfs-cleanup" in prefixes, "Expected log_prefix='rootfs-cleanup' for tarball rm"


async def test_build_rootfs_arch_x86_64() -> None:
    """build_rootfs_arch x86_64: defconfig, ext4 config merge, build; no arm64 options."""
    executor = _make_executor()
    version = "2024.02"
    config = _make_config(rootfs=BuildrootConfig(version=version))

    result = await build_rootfs_arch(executor, config, "x86_64")

    calls = [c.args[0] for c in executor.run.call_args_list]
    assert any(f"buildroot-{version}" in c and "defconfig" in c for c in calls), (
        "Expected make defconfig command"
    )
    assert any("BR2_TARGET_ROOTFS_EXT2=y" in c for c in calls), "Expected ext4 config"
    assert not any("BR2_aarch64" in c for c in calls), "Unexpected arm64 config for x86_64"
    assert any("buildroot-output-x86_64" in c and "-j$(nproc)" in c for c in calls), (
        "Expected make build command with x86_64 output dir"
    )
    assert result == {"rootfs": "/root/buildroot-output-x86_64/images/rootfs.ext4"}


async def test_build_rootfs_arch_arm64() -> None:
    """build_rootfs_arch arm64: defconfig, ext4+arm64 config merge, build."""
    executor = _make_executor()
    version = "2024.02"
    config = _make_config(rootfs=BuildrootConfig(version=version))

    result = await build_rootfs_arch(executor, config, "arm64")

    calls = [c.args[0] for c in executor.run.call_args_list]
    assert any(f"buildroot-{version}" in c and "defconfig" in c for c in calls), (
        "Expected make defconfig command"
    )
    assert any("BR2_aarch64=y" in c for c in calls), "Expected BR2_aarch64=y for arm64"
    assert any("BR2_TARGET_ROOTFS_EXT2=y" in c for c in calls), "Expected ext4 config"
    assert any("buildroot-output-arm64" in c and "-j$(nproc)" in c for c in calls), (
        "Expected make build command with arm64 output dir"
    )
    assert result == {"rootfs": "/root/buildroot-output-arm64/images/rootfs.ext4"}


# ---------------------------------------------------------------------------
# 6. build_syzkaller()
# ---------------------------------------------------------------------------


async def test_build_rootfs_arch_with_config_fragment(tmp_path: Path) -> None:
    """build_rootfs_arch uploads config fragment and runs merge_config.sh when config_fragments is set."""
    fragment = tmp_path / "ksmbd.config"
    fragment.write_text("BR2_PACKAGE_KSMBD_TOOLS=y\n")

    executor = _make_executor()
    config = _make_config(rootfs=BuildrootConfig(config_fragments=[fragment]))

    await build_rootfs_arch(executor, config, "x86_64")

    upload_calls = executor.upload_file.call_args_list
    run_calls = [c.args[0] for c in executor.run.call_args_list]

    assert any(
        call_args.args[0] == fragment for call_args in upload_calls
    ), "Expected fragment file upload"
    assert any("merge_config.sh" in c and "buildroot-fragment-x86_64-0" in c for c in run_calls), (
        "Expected merge_config.sh invocation for rootfs fragment"
    )
    # olddefconfig must run after the fragment merge
    merge_idx = next(i for i, c in enumerate(run_calls) if "buildroot-fragment-x86_64-0" in c)
    old_idx = next(i for i, c in enumerate(run_calls) if "olddefconfig" in c)
    assert merge_idx < old_idx, "olddefconfig must run after fragment merge"


async def test_build_syzkaller() -> None:
    """build_syzkaller() issues git clone and per-arch make; returns syzkaller_bin."""
    executor = _make_executor()
    config = _make_config(syzkaller=SyzkallerConfig(targets=["amd64"]))

    result = await build_syzkaller(executor, config)

    calls = [c.args[0] for c in executor.run.call_args_list]

    assert any(
        "git clone" in c and "syzkaller" in c for c in calls
    ), "Expected syzkaller git clone"
    assert any(
        "HOSTOS=linux" in c and "HOSTARCH=amd64" in c and "TARGETARCH=amd64" in c for c in calls
    ), "Expected syzkaller make for amd64 with HOSTOS/HOSTARCH"
    assert any(
        "host_amd64" in c for c in calls
    ), "Expected host bin staging for amd64"

    assert result == {
        "x86_64": {"linux": "/root/syzkaller/bin/linux_amd64", "host": "/root/syzkaller/bin/host_amd64"},
    }


async def test_build_syzkaller_macos() -> None:
    """build_syzkaller() also builds darwin host bins when host_os includes macos."""
    executor = _make_executor()
    config = _make_config(syzkaller=SyzkallerConfig(targets=["amd64"], host_os=["linux", "macos"]))

    result = await build_syzkaller(executor, config)

    calls = [c.args[0] for c in executor.run.call_args_list]

    assert any(
        "HOSTOS=darwin" in c and "HOSTARCH=amd64" in c for c in calls
    ), "Expected syzkaller make with HOSTOS=darwin"
    assert any(
        "host_darwin_amd64" in c for c in calls
    ), "Expected host_darwin staging for amd64"

    assert result == {
        "x86_64": {
            "linux": "/root/syzkaller/bin/linux_amd64",
            "host": "/root/syzkaller/bin/host_amd64",
            "host_darwin": "/root/syzkaller/bin/host_darwin_amd64",
        },
    }


# ---------------------------------------------------------------------------
# 7. rsync_artifacts()
# ---------------------------------------------------------------------------


async def test_rsync_artifacts(tmp_path: Path) -> None:
    """rsync_artifacts() calls rsync with correct args and creates local_dest."""
    host = "1.2.3.4"
    key_path = Path("/tmp/id_rsa")
    artifacts = {
        "kernel_x86_64": "/root/linux/arch/x86/boot/bzImage",
        "vmlinux": "/root/linux/vmlinux",
    }
    local_dest = tmp_path / "output"

    # Build a fake process that exits 0
    fake_proc = MagicMock()
    fake_proc.stdout = _AsyncLineReader([b"sending incremental file list\n"])
    fake_proc.stderr = _AsyncLineReader([])
    fake_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)) as mock_exec:
        await rsync_artifacts(host, key_path, artifacts, local_dest)

    assert local_dest.exists(), "local_dest should be created"

    mock_exec.assert_called_once()
    cmd_args = mock_exec.call_args.args

    # First arg is the executable
    assert cmd_args[0] == "rsync"
    assert "-avzL" in cmd_args
    assert "--progress" in cmd_args
    assert "-e" in cmd_args

    # SSH identity flag in -e arg
    e_idx = list(cmd_args).index("-e")
    ssh_arg = cmd_args[e_idx + 1]
    assert str(key_path) in ssh_arg
    assert "-p 22" in ssh_arg
    assert "StrictHostKeyChecking=no" in ssh_arg

    # Remote paths present
    assert f"root@{host}:/root/linux/arch/x86/boot/bzImage" in cmd_args
    assert f"root@{host}:/root/linux/vmlinux" in cmd_args

    # local_dest trailing slash
    assert f"{local_dest}/" in cmd_args


async def test_rsync_artifacts_custom_username(tmp_path: Path) -> None:
    """rsync_artifacts() uses the provided username in remote source paths."""
    host = "1.2.3.4"
    key_path = Path("/tmp/id_rsa")
    artifacts = {"vmlinux": "/root/linux/vmlinux"}
    local_dest = tmp_path / "output"

    fake_proc = MagicMock()
    fake_proc.stdout = _AsyncLineReader([])
    fake_proc.stderr = _AsyncLineReader([])
    fake_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)) as mock_exec:
        await rsync_artifacts(host, key_path, artifacts, local_dest, username="ubuntu")

    cmd_args = mock_exec.call_args.args
    assert f"ubuntu@{host}:/root/linux/vmlinux" in cmd_args


async def test_rsync_artifacts_custom_port(tmp_path: Path) -> None:
    """rsync_artifacts() includes -p <port> in the SSH -e argument."""
    host = "1.2.3.4"
    key_path = Path("/tmp/id_rsa")
    artifacts = {"vmlinux": "/root/linux/vmlinux"}
    local_dest = tmp_path / "output"

    fake_proc = MagicMock()
    fake_proc.stdout = _AsyncLineReader([])
    fake_proc.stderr = _AsyncLineReader([])
    fake_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)) as mock_exec:
        await rsync_artifacts(host, key_path, artifacts, local_dest, port=2222)

    cmd_args = mock_exec.call_args.args
    e_idx = list(cmd_args).index("-e")
    ssh_arg = cmd_args[e_idx + 1]
    assert "-p 2222" in ssh_arg


async def test_rsync_artifacts_raises_on_failure(tmp_path: Path) -> None:
    """rsync_artifacts() raises RuntimeError when rsync exits non-zero."""
    fake_proc = MagicMock()
    fake_proc.stdout = _AsyncLineReader([])
    fake_proc.stderr = _AsyncLineReader([b"error\n"])
    fake_proc.wait = AsyncMock(return_value=1)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
        with pytest.raises(RuntimeError, match="rsync exited with code 1"):
            await rsync_artifacts(
                "1.2.3.4",
                Path("/tmp/id_rsa"),
                {"vmlinux": "/root/linux/vmlinux"},
                tmp_path / "out",
            )


# ---------------------------------------------------------------------------
# Helpers for async stream mocking
# ---------------------------------------------------------------------------


class _AsyncLineReader:
    """Minimal async iterator that yields pre-set byte lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = iter(lines)

    def __aiter__(self) -> "_AsyncLineReader":
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._lines)
        except StopIteration:
            raise StopAsyncIteration
