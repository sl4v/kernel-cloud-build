"""Tests for provider implementations and make_provider factory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from kcb.config import BuildConfig, DockerConfig, HetznerConfig, LocalVMConfig
from kcb.executor import DockerExecutor, RemoteExecutor
from kcb.providers import DockerProvider, HetznerProvider, LocalVMProvider, Provider, make_provider


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


def _make_docker_config(
    image: str = "ubuntu:24.04",
    container_name: str | None = "kcb-test",
    arch: str = "x86_64",
    ssh_key_path: Path = Path("~/.ssh/id_rsa"),
) -> DockerConfig:
    return DockerConfig(
        image=image,
        container_name=container_name,
        arch=arch,  # type: ignore[arg-type]
        ssh_key_path=ssh_key_path,
    )


def _make_build_config_local(**kwargs) -> BuildConfig:
    return BuildConfig(provider=_make_local_config(**kwargs))


def _make_build_config_docker(**kwargs) -> BuildConfig:
    return BuildConfig(provider=_make_docker_config(**kwargs))


def _make_build_config_hetzner(**kwargs) -> BuildConfig:
    return BuildConfig(provider=HetznerConfig(api_token=_FAKE_TOKEN, **kwargs))


async def test_local_provision_returns_host() -> None:
    provider = LocalVMProvider(_make_local_config(host="10.0.0.5"))
    assert await provider.provision() == "10.0.0.5"


async def test_local_teardown_is_noop() -> None:
    provider = LocalVMProvider(_make_local_config())
    assert await provider.teardown(success=True, host="10.0.0.5") is None
    assert await provider.teardown(success=False, host="10.0.0.5") is None


async def test_local_list_managed_empty() -> None:
    provider = LocalVMProvider(_make_local_config())
    assert await provider.list_managed() == []


def test_local_provider_host_arch() -> None:
    for arch in ("x86_64", "arm64"):
        provider = LocalVMProvider(_make_local_config(arch=arch))  # type: ignore[arg-type]
        assert provider.host_arch == arch


def test_local_provider_executor_uses_remote_executor() -> None:
    provider = LocalVMProvider(_make_local_config(host="10.0.0.5", username="ubuntu", port=2222))
    executor = provider.make_executor("10.0.0.5")
    assert isinstance(executor, RemoteExecutor)
    assert executor.host == "10.0.0.5"
    assert executor.username == "ubuntu"
    assert executor.port == 2222


async def test_local_provider_download_artifacts_uses_rsync(tmp_path: Path) -> None:
    provider = LocalVMProvider(_make_local_config(host="10.0.0.5", username="ubuntu", port=2222))
    with patch("kcb.providers.rsync_artifacts", new=AsyncMock()) as rsync_mock:
        await provider.download_artifacts("10.0.0.5", {"vmlinux": "/root/linux/vmlinux"}, tmp_path)
    rsync_mock.assert_awaited_once_with(
        "10.0.0.5",
        provider.ssh_key_path,
        {"vmlinux": "/root/linux/vmlinux"},
        tmp_path,
        username="ubuntu",
        port=2222,
    )


async def test_docker_provider_provision_starts_container() -> None:
    provider = DockerProvider(_make_docker_config(container_name="kcb-dev"))
    with patch("kcb.providers._run_docker_command", new=AsyncMock(return_value=(0, "cid\n", ""))) as docker_cmd:
        target = await provider.provision()
    assert target == "kcb-dev"
    docker_cmd.assert_awaited_once_with(
        "run",
        "-d",
        "--rm",
        "--name",
        "kcb-dev",
        "--hostname",
        "kcb-dev",
        "--label",
        "managed-by=kcb",
        "--label",
        "provider=docker",
        "ubuntu:24.04",
        "sleep",
        "infinity",
    )


async def test_docker_provider_generates_container_name() -> None:
    provider = DockerProvider(_make_docker_config(container_name=None))
    with patch("kcb.providers._run_docker_command", new=AsyncMock(return_value=(0, "cid\n", ""))):
        target = await provider.provision()
    assert target.startswith("kcb-build-")


def test_docker_provider_executor_uses_docker_executor() -> None:
    provider = DockerProvider(_make_docker_config(container_name="kcb-dev"))
    executor = provider.make_executor("kcb-dev")
    assert isinstance(executor, DockerExecutor)
    assert executor.container_name == "kcb-dev"


async def test_docker_provider_download_artifacts_uses_docker_cp(tmp_path: Path) -> None:
    provider = DockerProvider(_make_docker_config(container_name="kcb-dev"))
    with patch("kcb.providers.docker_cp_artifacts", new=AsyncMock()) as docker_cp:
        await provider.download_artifacts("kcb-dev", {"vmlinux": "/root/linux/vmlinux"}, tmp_path)
    docker_cp.assert_awaited_once_with(
        "kcb-dev",
        {"vmlinux": "/root/linux/vmlinux"},
        tmp_path,
    )


async def test_docker_provider_teardown_removes_container() -> None:
    provider = DockerProvider(_make_docker_config(container_name="kcb-dev"))
    provider._container_name = "kcb-dev"
    with patch("kcb.providers._run_docker_command", new=AsyncMock(return_value=(0, "", ""))) as docker_cmd:
        await provider.teardown(success=True, host="kcb-dev")
    docker_cmd.assert_awaited_once_with("rm", "-f", "kcb-dev")
    assert provider._container_name is None


async def test_docker_provider_teardown_keeps_container_on_failure(capsys) -> None:
    provider = DockerProvider(_make_docker_config(container_name="kcb-dev"), keep_on_failure=True)
    provider._container_name = "kcb-dev"
    with patch("kcb.providers._run_docker_command", new=AsyncMock()) as docker_cmd:
        await provider.teardown(success=False, host="kcb-dev")
    docker_cmd.assert_not_called()
    captured = capsys.readouterr()
    assert "Container kept alive as kcb-dev" in captured.out
    assert "docker exec -it kcb-dev bash" in captured.out


async def test_docker_provider_list_managed_uses_docker_ps() -> None:
    provider = DockerProvider(_make_docker_config(container_name="kcb-dev"))
    ps_output = "abc123\tkcb-dev\t2026-04-21 12:00:00 +0000 UTC\n"
    with patch("kcb.providers._run_docker_command", new=AsyncMock(return_value=(0, ps_output, ""))):
        handles = await provider.list_managed()
    assert len(handles) == 1
    assert handles[0].server_id == "abc123"
    assert handles[0].label == "kcb-dev"


def test_make_provider_returns_hetzner_provider() -> None:
    assert isinstance(make_provider(_make_build_config_hetzner()), HetznerProvider)


def test_make_provider_returns_local_provider() -> None:
    assert isinstance(make_provider(_make_build_config_local()), LocalVMProvider)


def test_make_provider_returns_docker_provider() -> None:
    assert isinstance(make_provider(_make_build_config_docker()), DockerProvider)


def test_make_provider_hetzner_passes_keep_on_failure() -> None:
    config = _make_build_config_hetzner().model_copy(update={"keep_on_failure": True})
    provider = make_provider(config)
    assert isinstance(provider, HetznerProvider)
    assert provider._keep_on_failure is True


def test_make_provider_docker_passes_keep_on_failure() -> None:
    config = _make_build_config_docker().model_copy(update={"keep_on_failure": True})
    provider = make_provider(config)
    assert isinstance(provider, DockerProvider)
    assert provider._keep_on_failure is True


def test_make_provider_local_satisfies_protocol() -> None:
    assert isinstance(make_provider(_make_build_config_local()), Provider)


def test_make_provider_hetzner_satisfies_protocol() -> None:
    assert isinstance(make_provider(_make_build_config_hetzner()), Provider)


def test_make_provider_docker_satisfies_protocol() -> None:
    assert isinstance(make_provider(_make_build_config_docker()), Provider)
