"""CLI entry point and orchestrator for kcb."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import click

from kcb import build as _build
from kcb import hetzner
from kcb.config import BuildConfig, LocalVMConfig, load_config
from kcb.executor import RemoteExecutor
from kcb.providers import make_provider


async def run(config: BuildConfig) -> None:
    """Main orchestration flow: provision -> setup -> build -> download -> destroy."""
    provider = make_provider(config)
    host = ""
    success = False
    try:
        host = await provider.provision()
        executor = RemoteExecutor(host=host, username=provider.username, key_path=provider.ssh_key_path, port=provider.port)
        await executor.connect()
        await _build.bootstrap(executor, config, provider.host_arch)

        if "kernel" in config.components:
            await _build.prepare_kernel_source(executor, config)
            await _build.apply_kernel_patch(executor, config)
            for arch in config.kernel.targets:
                kernel_artifacts = await _build.build_kernel_arch(executor, config, arch, provider.host_arch)
                await _build.rsync_artifacts(host, provider.ssh_key_path, kernel_artifacts, config.output_dir / arch, username=provider.username, port=provider.port)
                await executor.run("make -C /root/linux clean", log_prefix=f"kernel-clean-{arch}")

        if "rootfs" in config.components:
            await _build.prepare_rootfs_source(executor, config)
            for arch in config.rootfs.targets:
                rootfs_artifacts = await _build.build_rootfs_arch(executor, config, arch, provider.host_arch)
                await _build.rsync_artifacts(host, provider.ssh_key_path, rootfs_artifacts, config.output_dir / arch, username=provider.username, port=provider.port)
                await executor.run(f"rm -rf /root/buildroot-output-{arch}", log_prefix=f"rootfs-clean-{arch}")

        if "syzkaller" in config.components:
            syz_artifacts = await _build.build_syzkaller(executor, config)
            for local_arch, arch_artifacts in syz_artifacts.items():
                await _build.rsync_artifacts(host, provider.ssh_key_path, arch_artifacts, config.output_dir / local_arch, username=provider.username, port=provider.port)

        success = True
    finally:
        await provider.teardown(success, host)
        remaining = await provider.list_managed()
        if remaining:
            print(f"[kcb] Remaining kcb-managed servers ({len(remaining)}):")
            for s in remaining:
                print(f"  id={s.server_id}  name={s.label}  created={s.created_at}")
        else:
            print("[kcb] No kcb-managed servers remaining.")


@click.group()
def main() -> None:
    """kcb — Kernel Cloud Build CLI."""


@main.command()
@click.option("--config", "config_path", type=click.Path(), default=None,
              help="Path to YAML config file.")
@click.option("--kernel/--no-kernel", "build_kernel_flag", default=None,
              help="Include kernel in components.")
@click.option("--rootfs/--no-rootfs", "build_rootfs_flag", default=None,
              help="Include rootfs in components.")
@click.option("--syzkaller/--no-syzkaller", "build_syzkaller_flag", default=None,
              help="Include syzkaller in components.")
@click.option("--keep-on-failure", is_flag=True, default=False,
              help="Keep VPS alive on build failure.")
@click.option("--server-type", default=None, help="Hetzner server type (e.g. cx43).")
@click.option("--kernel-url", default=None, help="Kernel git URL.")
@click.option("--kernel-branch", default=None, help="Kernel git branch.")
@click.option("--kernel-arch", "kernel_archs", multiple=True,
              type=click.Choice(["x86_64", "arm64"]),
              help="Kernel target architecture (may be repeated).")
@click.option("--kernel-config", "kernel_config_paths", type=click.Path(), multiple=True,
              help="Path to kernel config overlay file (may be repeated; applied in order).")
@click.option("--kernel-patch", "kernel_patch_path", type=click.Path(), default=None,
              help="Path to a unified diff patch applied to the kernel source before building.")
@click.option("--kernel-headers/--no-kernel-headers", "kernel_download_headers", default=None,
              help="Run headers_install after kernel build and download headers as artifacts.")
@click.option("--rootfs-arch", "rootfs_archs", multiple=True,
              type=click.Choice(["x86_64", "arm64"]),
              help="Rootfs target architecture (may be repeated).")
@click.option("--output-dir", type=click.Path(), default=None,
              help="Local directory to download artifacts into.")
def build(
    config_path: Optional[str],
    build_kernel_flag: Optional[bool],
    build_rootfs_flag: Optional[bool],
    build_syzkaller_flag: Optional[bool],
    keep_on_failure: bool,
    server_type: Optional[str],
    kernel_url: Optional[str],
    kernel_branch: Optional[str],
    kernel_archs: tuple[str, ...],
    kernel_config_paths: tuple[str, ...],
    kernel_patch_path: Optional[str],
    kernel_download_headers: Optional[bool],
    rootfs_archs: tuple[str, ...],
    output_dir: Optional[str],
) -> None:
    """Provision a VPS, build kernel/rootfs/syzkaller, download artifacts."""
    # Build overrides dict for load_config — only include non-None values.
    overrides: dict = {}

    if server_type is not None:
        overrides["provider.server_type"] = server_type
    if kernel_url is not None:
        overrides["kernel.git_url"] = kernel_url
    if kernel_branch is not None:
        overrides["kernel.branch"] = kernel_branch
    if kernel_archs:
        overrides["kernel.targets"] = list(kernel_archs)
    if kernel_config_paths:
        overrides["kernel.config_overlays"] = [Path(p) for p in kernel_config_paths]
    if kernel_patch_path is not None:
        overrides["kernel.patch"] = Path(kernel_patch_path)
    if kernel_download_headers is not None:
        overrides["kernel.download_headers"] = kernel_download_headers
    if rootfs_archs:
        overrides["rootfs.targets"] = list(rootfs_archs)
    if output_dir is not None:
        overrides["output_dir"] = Path(output_dir)
    if keep_on_failure:
        overrides["keep_on_failure"] = True

    yaml_path = Path(config_path) if config_path else None

    try:
        config = load_config(yaml_path, **overrides)
    except Exception as exc:
        raise click.ClickException(f"Failed to load config: {exc}") from exc

    # Build the components list from flags if any flag was explicitly passed.
    # If none were specified, keep the config default.
    explicit_flags = {
        "kernel": build_kernel_flag,
        "rootfs": build_rootfs_flag,
        "syzkaller": build_syzkaller_flag,
    }
    if any(v is not None for v in explicit_flags.values()):
        components = [
            name for name, flag in explicit_flags.items()
            if flag is True  # include only those explicitly enabled
        ]
        # Preserve config default for flags left at None (not passed by user).
        # A flag of None means "not specified"; True means "include"; False means "exclude".
        not_specified = [
            name for name, flag in explicit_flags.items()
            if flag is None
        ]
        # Add unspecified components only if they were in the original config default.
        for name in not_specified:
            if name in config.components:
                components.append(name)
        config = config.model_copy(update={"components": components})

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        print("\n[kcb] Interrupted, destroying server...")
        # The finally block inside run() handles the actual destroy.
        sys.exit(1)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.argument("server_id", required=False)
@click.option("--list", "list_servers_flag", is_flag=True,
              help="List kcb-managed servers.")
@click.option("--all", "destroy_all", is_flag=True,
              help="Destroy all kcb-managed servers (prompts for confirmation).")
@click.option("--config", "config_path", type=click.Path(), default=None,
              help="Path to YAML config file.")
@click.option("--token", default=None, envvar="KCB_HETZNER_TOKEN",
              help="Hetzner API token (overrides config).")
def cleanup(
    server_id: Optional[str],
    list_servers_flag: bool,
    destroy_all: bool,
    config_path: Optional[str],
    token: Optional[str],
) -> None:
    """List or destroy kcb-managed servers.

    \b
    Examples:
      kcb cleanup --list               List all kcb-managed servers
      kcb cleanup --all                Destroy all kcb-managed servers
      kcb cleanup <server-id>          Destroy a specific server by ID
    """
    overrides: dict = {}
    if token is not None:
        overrides["provider.api_token"] = token

    yaml_path = Path(config_path) if config_path else None

    try:
        config = load_config(yaml_path, **overrides)
    except Exception as exc:
        raise click.ClickException(f"Failed to load config: {exc}") from exc

    if isinstance(config.provider, LocalVMConfig):
        click.echo("cleanup is not applicable for local provider")
        return

    if list_servers_flag:
        _run_list_servers(config)
    elif destroy_all:
        _run_destroy_all(config)
    elif server_id:
        _run_destroy_server(server_id, config)
    else:
        raise click.UsageError(
            "Specify a server ID, --list, or --all.\n"
            "Run 'kcb cleanup --help' for usage."
        )


def _run_list_servers(config: BuildConfig) -> None:
    """List all kcb-managed servers and print their details."""
    servers = asyncio.run(hetzner.list_servers(config.provider))
    if not servers:
        click.echo("[kcb] No kcb-managed servers found.")
        return
    click.echo(f"[kcb] Found {len(servers)} kcb-managed server(s):")
    for s in servers:
        click.echo(f"  id={s.server_id}  name={s.label}  created={s.created_at}")


def _run_destroy_all(config: BuildConfig) -> None:
    """Destroy all kcb-managed servers after user confirmation."""
    servers = asyncio.run(hetzner.list_servers(config.provider))
    if not servers:
        click.echo("[kcb] No kcb-managed servers to destroy.")
        return
    click.echo(f"[kcb] About to destroy {len(servers)} server(s):")
    for s in servers:
        click.echo(f"  id={s.server_id}  name={s.label}  created={s.created_at}")
    click.confirm("[kcb] Confirm destroy all?", abort=True)
    for s in servers:
        click.echo(f"[kcb] Destroying server {s.server_id} ({s.label})...")
        asyncio.run(hetzner.destroy_server(s, config.provider))
        click.echo(f"[kcb] Destroyed {s.server_id}.")


def _run_destroy_server(server_id: str, config: BuildConfig) -> None:
    """Destroy a single server by ID."""
    from kcb.hetzner import ServerHandle
    # Build a minimal handle; label and created_at are only informational.
    handle = ServerHandle(server_id=server_id, label="", created_at="")
    click.echo(f"[kcb] Destroying server {server_id}...")
    asyncio.run(hetzner.destroy_server(handle, config.provider))
    click.echo(f"[kcb] Server {server_id} destroyed.")


if __name__ == "__main__":
    main()
