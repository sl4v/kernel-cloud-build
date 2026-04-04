"""Build functions: bootstrap, kernel, rootfs, syzkaller, and rsync."""

import asyncio
import base64
from pathlib import Path

from kcb.config import BuildConfig
from kcb.executor import RemoteExecutor

# Path to bootstrap.sh relative to this file: ../../scripts/bootstrap.sh
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


async def bootstrap(
    executor: RemoteExecutor,
    config: BuildConfig,
    host_arch: str = "x86_64",
) -> None:
    """Upload and run scripts/bootstrap.sh to install build dependencies.

    host_arch is accepted for forward-compatibility; bootstrap.sh detects the
    host architecture itself via ``uname -m``.
    """
    await executor.run(
        "df -BG / | awk 'NR==2{gsub(\"G\",\"\",$4); free=$4+0; if(free < 5) {print \"[kcb] FATAL: Only \" free \"GB free on remote /; need at least 5GB\" > \"/dev/stderr\"; exit 1} else if(free < 30) print \"[kcb] WARNING: Only \" free \"GB free on remote /; builds may fail\"}'",
        log_prefix="disk-check",
    )
    bootstrap_sh = _SCRIPTS_DIR / "bootstrap.sh"
    await executor.upload_file(bootstrap_sh, "/root/bootstrap.sh")
    await executor.run(
        "chmod +x /root/bootstrap.sh && /root/bootstrap.sh",
        log_prefix="bootstrap",
    )


async def _apply_config_overlays(
    executor: RemoteExecutor, config: BuildConfig, arch: str
) -> None:
    """Upload each config overlay and merge it into /root/linux/.config in order."""
    for i, overlay in enumerate(config.kernel.config_overlays):
        remote_path = f"/root/linux/.config-overlay-{i}"
        await executor.upload_file(overlay, remote_path)
        await executor.run(
            f"cd /root/linux && scripts/kconfig/merge_config.sh -m .config .config-overlay-{i}",
            log_prefix=f"kernel-overlay-{arch}-{i}",
        )


async def prepare_kernel_source(executor: RemoteExecutor, config: BuildConfig) -> None:
    """Download/clone the kernel source tree to /root/linux (once, arch-independent)."""
    if config.kernel.tarball_url:
        tarball_url = config.kernel.tarball_url
        await executor.run(
            f"test -f /root/linux/Makefile || "
            f"(wget --progress=dot:mega {tarball_url} -O /root/linux.tar"
            f" && mkdir -p /root/linux"
            f" && tar -xf /root/linux.tar -C /root/linux --strip-components=1)",
            log_prefix="kernel-download",
        )
        await executor.run("rm -f /root/linux.tar", log_prefix="kernel-cleanup")
    else:
        url = config.kernel.git_url
        branch = config.kernel.branch
        await executor.run(
            f"test -d /root/linux/.git || "
            f"git clone --depth=1 --branch {branch} --progress {url} /root/linux",
            log_prefix="kernel-clone",
        )


async def apply_kernel_patch(executor: RemoteExecutor, config: BuildConfig) -> None:
    """Upload and apply config.kernel.patch to /root/linux with patch -p1.

    No-op when config.kernel.patch is None.  Raises RuntimeError (via
    executor.run check=True) if patch fails, which surfaces reject hunks or
    compilation issues as a clear error before the build starts.
    """
    if config.kernel.patch is None:
        return
    await executor.upload_file(config.kernel.patch, "/root/kernel.patch")
    await executor.run(
        "patch -p1 -d /root/linux < /root/kernel.patch",
        log_prefix="kernel-patch",
    )


def _kernel_cross_compile(host_arch: str, target_arch: str) -> str:
    """Return the CROSS_COMPILE prefix for a given host/target arch pair.

    Returns an empty string when no cross-compilation is needed (native build).
    """
    if host_arch == target_arch:
        return ""
    if host_arch == "x86_64" and target_arch == "arm64":
        return "aarch64-linux-gnu-"
    if host_arch == "arm64" and target_arch == "x86_64":
        return "x86_64-linux-gnu-"
    raise ValueError(f"Unsupported host/target arch combination: {host_arch}/{target_arch}")


async def build_kernel_arch(
    executor: RemoteExecutor,
    config: BuildConfig,
    arch: str,
    host_arch: str = "x86_64",
) -> dict[str, str]:
    """Build the Linux kernel for a single target arch.

    Assumes the source tree is already present at /root/linux (call
    prepare_kernel_source first).  Returns a mapping of artifact names to
    remote paths, always including "vmlinux".

    host_arch controls whether cross-compilation is needed:
      - (x86_64, x86_64) and (arm64, arm64): native build, no CROSS_COMPILE
      - (x86_64, arm64): CROSS_COMPILE=aarch64-linux-gnu-
      - (arm64, x86_64): CROSS_COMPILE=x86_64-linux-gnu-
    """
    cross = _kernel_cross_compile(host_arch, arch)
    artifacts: dict[str, str] = {}

    if arch == "x86_64":
        cross_flags = f" CROSS_COMPILE={cross}" if cross else ""
        await executor.run(
            f"make -C /root/linux ARCH=x86_64{cross_flags} defconfig",
            log_prefix="kernel-defconfig-x86_64",
        )
        await _apply_config_overlays(executor, config, arch)
        await executor.run(
            f"make -C /root/linux ARCH=x86_64{cross_flags} olddefconfig",
            log_prefix="kernel-olddefconfig-x86_64",
        )
        await executor.run(
            f"make -C /root/linux -j$(nproc) ARCH=x86_64{cross_flags} bzImage",
            log_prefix="kernel-x86_64",
        )
        artifacts["bzImage"] = "/root/linux/arch/x86/boot/bzImage"
        artifacts["vmlinux"] = "/root/linux/vmlinux"

    elif arch == "arm64":
        cross_flags = f" CROSS_COMPILE={cross}" if cross else ""
        await executor.run(
            f"make -C /root/linux ARCH=arm64{cross_flags} defconfig",
            log_prefix="kernel-defconfig-arm64",
        )
        await _apply_config_overlays(executor, config, arch)
        await executor.run(
            f"make -C /root/linux ARCH=arm64{cross_flags} olddefconfig",
            log_prefix="kernel-olddefconfig-arm64",
        )
        await executor.run(
            f"make -C /root/linux -j$(nproc) ARCH=arm64{cross_flags} Image",
            log_prefix="kernel-arm64",
        )
        artifacts["Image"] = "/root/linux/arch/arm64/boot/Image"
        artifacts["vmlinux"] = "/root/linux/vmlinux"

    else:
        raise ValueError(f"Unsupported kernel arch: {arch}")

    return artifacts


_S05DEBUGFS_SCRIPT = """\
#!/bin/sh
case "$1" in
  start)
    mount -t debugfs none /sys/kernel/debug
    ;;
  stop|restart|reload) ;;
  *) echo "Usage: $0 {start|stop}"; exit 1 ;;
esac
"""

_S40NETWORK_SCRIPT = """\
#!/bin/sh

case "$1" in
  start)
    ip link set eth0 up
    for i in 1 2 3; do
      sleep 1
      ip link show eth0 | grep -q 'state UP' && break
    done
    udhcpc -n -q -t 5 -A 2 -i eth0
    ;;
  stop)
    ip link set eth0 down
    ;;
  restart|reload)
    "$0" stop
    "$0" start
    ;;
  *)
    echo "Usage: $0 {start|stop|restart}"
    exit 1
esac
"""


async def _apply_rootfs_config_fragments(
    executor: RemoteExecutor,
    config: BuildConfig,
    arch: str,
    output_dir: str,
    buildroot_dir: str,
) -> None:
    """Upload each rootfs config fragment and merge it into the Buildroot .config."""
    for i, fragment in enumerate(config.rootfs.config_fragments):
        remote_path = f"/root/buildroot-fragment-{arch}-{i}"
        await executor.upload_file(fragment, remote_path)
        await executor.run(
            f"cd {output_dir} && {buildroot_dir}/support/kconfig/merge_config.sh -m .config {remote_path}",
            log_prefix=f"rootfs-fragment-{arch}-{i}",
        )


async def prepare_rootfs_source(executor: RemoteExecutor, config: BuildConfig) -> None:
    """Download and extract the Buildroot tarball to /root/buildroot-<version>."""
    version = config.rootfs.version
    tarball_url = f"https://buildroot.org/downloads/buildroot-{version}.tar.gz"

    await executor.run(
        f"test -f /root/buildroot-{version}/Makefile || "
        f"(wget --progress=dot:mega {tarball_url} -O /root/buildroot.tar.gz"
        f" && tar -xf /root/buildroot.tar.gz -C /root/)",
        log_prefix="rootfs-download",
    )
    await executor.run("rm -f /root/buildroot.tar.gz", log_prefix="rootfs-cleanup")


async def build_rootfs_arch(
    executor: RemoteExecutor,
    config: BuildConfig,
    arch: str,
    host_arch: str = "x86_64",
) -> dict[str, str]:
    """Build a Buildroot rootfs ext4 image for a single target arch.

    Assumes the source tree is already present (call prepare_rootfs_source
    first).  Uses a separate O= output directory per arch so multiple arches
    can be built without re-extracting.

    host_arch is accepted for API consistency with build_kernel_arch; Buildroot
    selects the target architecture internally via BR2_ARCH config variables and
    does not require an external CROSS_COMPILE prefix.

    Returns {"rootfs": "<remote_output_dir>/images/rootfs.ext4"}.
    """
    if arch not in ("x86_64", "arm64"):
        raise ValueError(f"Unsupported rootfs arch: {arch}")

    version = config.rootfs.version
    buildroot_dir = f"/root/buildroot-{version}"
    output_dir = f"/root/buildroot-output-{arch}"

    await executor.run(
        f"FORCE_UNSAFE_CONFIGURE=1 make -C {buildroot_dir} O={output_dir} defconfig",
        log_prefix=f"rootfs-defconfig-{arch}",
    )

    # Config fragment: always enable ext4; for arm64 also switch the target arch.
    # merge_config.sh writes .config into the current directory (output_dir),
    # so cd there first and use an absolute path to the buildroot merge script.
    extra = "\\nBR2_aarch64=y" if arch == "arm64" else ""
    await executor.run(
        f"printf 'BR2_TARGET_ROOTFS_EXT2=y\\nBR2_TARGET_ROOTFS_EXT2_4=y\\nBR2_PACKAGE_DROPBEAR=y{extra}\\n'"
        f" > /tmp/rootfs-{arch}.config"
        f" && cd {output_dir}"
        f" && {buildroot_dir}/support/kconfig/merge_config.sh -m .config /tmp/rootfs-{arch}.config",
        log_prefix=f"rootfs-config-{arch}",
    )
    await _apply_rootfs_config_fragments(executor, config, arch, output_dir, buildroot_dir)
    await executor.run(
        f"FORCE_UNSAFE_CONFIGURE=1 make -C {buildroot_dir} O={output_dir} olddefconfig",
        log_prefix=f"rootfs-olddefconfig-{arch}",
    )
    await executor.run(
        f"FORCE_UNSAFE_CONFIGURE=1 make -C {buildroot_dir} O={output_dir} -j$(nproc)",
        log_prefix=f"rootfs-build-{arch}",
    )
    rootfs_image = f"{output_dir}/images/rootfs.ext4"

    # Patch S05debugfs: mount debugfs at /sys/kernel/debug so KCOV is accessible.
    encoded_dbg = base64.b64encode(_S05DEBUGFS_SCRIPT.encode()).decode()
    await executor.run(
        f"echo '{encoded_dbg}' | base64 -d > /tmp/S05debugfs-{arch}",
        log_prefix=f"rootfs-s05debugfs-{arch}",
    )
    await executor.run(
        f"printf 'write /tmp/S05debugfs-{arch} etc/init.d/S05debugfs\\n"
        f"set_inode_field /etc/init.d/S05debugfs i_mode 0100755\\n'"
        f" | debugfs -w {rootfs_image}",
        log_prefix=f"rootfs-patch-debugfs-{arch}",
    )

    # Patch S40network: bring eth0 up explicitly before DHCP so udhcpc doesn't
    # silently fail on virtio-net devices that are link-DOWN at init time.
    encoded = base64.b64encode(_S40NETWORK_SCRIPT.encode()).decode()
    await executor.run(
        f"echo '{encoded}' | base64 -d > /tmp/S40network-{arch}",
        log_prefix=f"rootfs-s40network-{arch}",
    )
    await executor.run(
        f"printf 'rm /etc/init.d/S40network\\nwrite /tmp/S40network-{arch} etc/init.d/S40network\\n"
        f"set_inode_field /etc/init.d/S40network i_mode 0100755\\n'"
        f" | debugfs -w {rootfs_image}",
        log_prefix=f"rootfs-patch-network-{arch}",
    )

    if config.rootfs.extra_space_mb > 0:
        n = config.rootfs.extra_space_mb
        await executor.run(
            f"truncate -s +{n}M {rootfs_image}"
            f" && e2fsck -f -y {rootfs_image}"
            f" && resize2fs {rootfs_image}",
            log_prefix=f"rootfs-resize-{arch}",
        )

    return {"rootfs": rootfs_image}


_SYZKALLER_ARCH_MAP = {"amd64": "x86_64", "arm64": "arm64"}

_SYZKALLER_HOST_BINS = [
    "syz-manager",
    "syz-repro",
    "syz-mutate",
    "syz-prog2c",
    "syz-db",
    "syz-upgrade",
]


async def build_syzkaller(executor: RemoteExecutor, config: BuildConfig) -> dict[str, dict[str, str]]:
    """Clone and build syzkaller for each configured target arch.

    Sets HOSTOS/HOSTARCH so host tools (syz-manager etc.) are also built for
    the target arch, then stages them to bin/host_{arch}/ before the next arch
    overwrites bin/.

    Returns a mapping of local arch name (x86_64/arm64) to a dict of artifact
    names to remote paths, e.g.:
      {"x86_64": {"linux": ".../linux_amd64", "host": ".../host_amd64"}, ...}
    """
    await executor.run(
        "test -d /root/syzkaller/.git || "
        "git clone --depth=1 --progress https://github.com/google/syzkaller.git /root/syzkaller",
        log_prefix="syzkaller-clone",
    )

    for arch in config.syzkaller.targets:
        await executor.run(
            f"HOSTOS=linux HOSTARCH={arch} TARGETOS=linux TARGETARCH={arch} make -C /root/syzkaller -j$(nproc)",
            log_prefix=f"syzkaller-{arch}",
        )
        bins = " ".join(f"/root/syzkaller/bin/{b}" for b in _SYZKALLER_HOST_BINS)
        await executor.run(
            f"mkdir -p /root/syzkaller/bin/host_{arch} && cp {bins} /root/syzkaller/bin/host_{arch}/",
            log_prefix=f"syzkaller-stage-{arch}",
        )

    if "macos" in config.syzkaller.host_os:
        for arch in config.syzkaller.targets:
            await executor.run(
                f"HOSTOS=darwin HOSTARCH={arch} TARGETOS=linux TARGETARCH={arch} make -C /root/syzkaller -j$(nproc)",
                log_prefix=f"syzkaller-darwin-{arch}",
            )
            bins = " ".join(f"/root/syzkaller/bin/{b}" for b in _SYZKALLER_HOST_BINS)
            await executor.run(
                f"mkdir -p /root/syzkaller/bin/host_darwin_{arch} && cp {bins} /root/syzkaller/bin/host_darwin_{arch}/",
                log_prefix=f"syzkaller-stage-darwin-{arch}",
            )

    result: dict[str, dict[str, str]] = {}
    for arch in config.syzkaller.targets:
        local_arch = _SYZKALLER_ARCH_MAP[arch]
        result[local_arch] = {
            "linux": f"/root/syzkaller/bin/linux_{arch}",
            "host": f"/root/syzkaller/bin/host_{arch}",
        }
        if "macos" in config.syzkaller.host_os:
            result[local_arch]["host_darwin"] = f"/root/syzkaller/bin/host_darwin_{arch}"
    return result


async def rsync_artifacts(
    host: str,
    key_path: Path,
    artifacts: dict[str, str],
    local_dest: Path,
    *,
    username: str = "root",
    port: int = 22,
) -> None:
    """Rsync all remote artifact paths to local_dest in a single invocation.

    Uses asyncio.create_subprocess_exec() with rsync -avz --info=progress2.
    Raises RuntimeError if rsync exits non-zero.
    """
    local_dest.mkdir(parents=True, exist_ok=True)

    # Build list of remote source paths: user@host:/path/to/artifact
    remote_sources = [f"{username}@{host}:{path}" for path in artifacts.values()]

    cmd = [
        "rsync",
        "-avzL",
        "--progress",
        "-e",
        f"ssh -i {key_path} -p {port} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        *remote_sources,
        f"{local_dest}/",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Stream stdout and stderr as they arrive
    async def _stream(stream: asyncio.StreamReader, label: str) -> None:
        async for line in stream:
            print(f"[rsync/{label}] {line.decode().rstrip()}", flush=True)

    await asyncio.gather(
        _stream(proc.stdout, "out"),
        _stream(proc.stderr, "err"),
    )

    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=3600)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("rsync timed out after 1 hour")
    if rc != 0:
        raise RuntimeError(f"rsync exited with code {rc}")
