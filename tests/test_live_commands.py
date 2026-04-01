"""Live integration tests against a real Hetzner CX22 instance.

Provisions an actual server, runs every shell command that kcb uses
(wget --progress=dot:mega, tar --strip-components, $(nproc), ENVVAR=val cmd,
rsync via rsync_artifacts), then destroys the server.

Skipped unless KCB_HETZNER_TOKEN is set in the environment.

Run with:
    KCB_HETZNER_TOKEN=<token> uv run pytest tests/test_live_commands.py -v -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kcb import hetzner
from kcb.build import bootstrap, rsync_artifacts
from kcb.config import BuildConfig, ProviderConfig
from kcb.executor import RemoteExecutor


@pytest.fixture
def provider() -> ProviderConfig:
    token = os.environ.get("KCB_HETZNER_TOKEN", "")
    if not token:
        pytest.skip("KCB_HETZNER_TOKEN not set — skipping live tests")
    return ProviderConfig(api_token=token, server_type="cx23", location="nbg1")


async def test_live_shell_commands(provider: ProviderConfig, tmp_path: Path) -> None:
    """Provision CX22, validate every kcb shell command, rsync a mock artifact, then destroy."""
    print(f"\n[live] Creating server (cx23, nbg1)...")
    handle = await hetzner.create_server(provider)
    print(f"[live] Server {handle.label} ({handle.server_id}) created")
    try:
        ip = await hetzner.wait_ready(handle, provider, timeout=300)
        print(f"[live] Server ready at {ip}")

        executor = RemoteExecutor(host=ip, key_path=provider.ssh_key_path)
        await executor.connect()

        # 1. $(nproc) shell expansion — used in every make -j$(nproc) invocation
        await executor.run("echo nproc=$(nproc)", log_prefix="nproc")

        # 2. ENVVAR=val cmd syntax — used in syzkaller build
        await executor.run(
            "TARGETOS=linux TARGETARCH=amd64 bash -c "
            "'echo TARGETOS=$TARGETOS TARGETARCH=$TARGETARCH'",
            log_prefix="env-expand",
        )

        # 3. wget --progress=dot:mega — used for kernel tarball and buildroot download
        await executor.run(
            "wget --progress=dot:mega -O /tmp/wget-test.bin https://httpbin.org/bytes/4096",
            log_prefix="wget",
        )

        # 4. tar with --strip-components=1 — used for kernel tarball extraction
        await executor.run(
            "mkdir -p /tmp/src/subdir && echo content > /tmp/src/subdir/file.txt"
            " && tar -czf /tmp/test.tar.gz -C /tmp src"
            " && mkdir -p /tmp/dst"
            " && tar -xf /tmp/test.tar.gz -C /tmp/dst --strip-components=1",
            log_prefix="tar-strip",
        )
        await executor.run("test -f /tmp/dst/subdir/file.txt", log_prefix="tar-verify")

        # 5. make -C ... -j$(nproc) syntax — bootstrap installs make (mirrors real kcb workflow)
        await bootstrap(executor, BuildConfig(provider=provider))
        await executor.run(
            "mkdir -p /tmp/maketest"
            " && printf 'all:\\n\\techo make-ok nproc=$(shell nproc)\\n' > /tmp/maketest/Makefile"
            " && make -C /tmp/maketest -j$(nproc)",
            log_prefix="make-syntax",
        )

        # 6. Create a mock artifact and download it via rsync_artifacts (the real function)
        await executor.run(
            "dd if=/dev/urandom bs=1K count=16 of=/tmp/mock-artifact.bin 2>&1",
            log_prefix="mock-artifact",
        )

        await rsync_artifacts(
            host=ip,
            key_path=provider.ssh_key_path,
            artifacts={"mock_artifact": "/tmp/mock-artifact.bin"},
            local_dest=tmp_path,
        )
        downloaded = tmp_path / "mock-artifact.bin"
        assert downloaded.exists(), "rsync_artifacts did not download mock-artifact.bin"
        assert downloaded.stat().st_size == 16 * 1024, (
            f"Downloaded file size {downloaded.stat().st_size} != {16 * 1024}"
        )

        await executor.disconnect()
        print("[live] All commands passed.")
    finally:
        print(f"[live] Destroying server {handle.server_id}...")
        await hetzner.destroy_server(handle, provider)
        print("[live] Server destroyed.")
