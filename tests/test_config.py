"""Unit tests for kcb/config.py — load_config() and BuildConfig models."""

import os
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from kcb.config import (
    BuildConfig,
    BuildrootConfig,
    HetznerConfig,
    KernelConfig,
    LocalVMConfig,
    ProviderConfig,
    SyzkallerConfig,
    load_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_yaml(tmp_path: Path, content: str) -> Path:
    """Write indented YAML content to a temp file and return its path."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# 1. Minimal YAML with only provider.api_token set
# ---------------------------------------------------------------------------


def test_minimal_yaml_loads(tmp_path: Path) -> None:
    """A YAML containing only provider.api_token should load with all defaults."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "test-token-123"
        """,
    )

    config = load_config(cfg_file)

    assert isinstance(config, BuildConfig)
    assert config.provider.api_token == "test-token-123"


# ---------------------------------------------------------------------------
# 2. Defaults are applied when fields are absent
# ---------------------------------------------------------------------------


def test_defaults_applied(tmp_path: Path) -> None:
    """Fields not present in YAML should get their documented default values."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "tok"
        """,
    )

    config = load_config(cfg_file)

    # ProviderConfig defaults
    assert config.provider.server_type == "cx23"
    assert config.provider.location == "fsn1"
    assert config.provider.ssh_key_path == Path("~/.ssh/id_rsa").expanduser()

    # KernelConfig defaults
    assert config.kernel.git_url == "https://github.com/torvalds/linux.git"
    assert config.kernel.branch == "master"
    assert config.kernel.config_overlays == []
    assert config.kernel.targets == ["x86_64"]

    # BuildrootConfig defaults
    assert config.rootfs.version == "2024.02"

    # SyzkallerConfig defaults
    assert config.syzkaller.targets == ["amd64"]

    # BuildConfig top-level defaults
    assert config.components == ["kernel", "rootfs", "syzkaller"]
    assert config.output_dir == Path("./kcb-artifacts")
    assert config.keep_on_failure is False


def test_defaults_without_yaml() -> None:
    """load_config() with no YAML path and just a token override should produce defaults."""
    config = load_config(None, **{"provider.api_token": "env-tok"})

    assert config.provider.api_token == "env-tok"
    assert config.provider.server_type == "cx23"
    assert config.kernel.branch == "master"
    assert config.components == ["kernel", "rootfs", "syzkaller"]


# ---------------------------------------------------------------------------
# 3. CLI overrides win over YAML values
# ---------------------------------------------------------------------------


def test_cli_overrides_win(tmp_path: Path) -> None:
    """CLI keyword overrides must take precedence over values in the YAML file."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "yaml-token"
          server_type: cx52
          location: fsn1
        kernel:
          branch: yaml-branch
        output_dir: ./yaml-output
        keep_on_failure: false
        """,
    )

    config = load_config(
        cfg_file,
        **{
            "provider.server_type": "cx72",
            "kernel.branch": "cli-branch",
            "output_dir": "./cli-output",
            "keep_on_failure": True,
        },
    )

    assert config.provider.api_token == "yaml-token"   # YAML value, not overridden
    assert config.provider.server_type == "cx72"        # CLI override
    assert config.kernel.branch == "cli-branch"         # CLI override
    assert config.output_dir == Path("./cli-output")    # CLI override
    assert config.keep_on_failure is True               # CLI override


def test_cli_double_underscore_separator(tmp_path: Path) -> None:
    """CLI overrides using __ as separator (common in shell env) should also work."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "tok"
        """,
    )

    config = load_config(cfg_file, **{"provider__location": "hel1"})

    assert config.provider.location == "hel1"


def test_cli_override_nested_sub_model(tmp_path: Path) -> None:
    """A CLI override of a deeply-nested field should not clobber sibling fields."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "tok"
          server_type: cx52
          location: fsn1
        """,
    )

    config = load_config(cfg_file, **{"provider.location": "nbg1"})

    assert config.provider.location == "nbg1"
    assert config.provider.server_type == "cx52"   # sibling not clobbered


# ---------------------------------------------------------------------------
# 4. KCB_HETZNER_TOKEN env var fallback
# ---------------------------------------------------------------------------


def test_env_var_token_when_missing_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When api_token is absent from YAML, KCB_HETZNER_TOKEN env var is used."""
    monkeypatch.setenv("KCB_HETZNER_TOKEN", "env-secret-token")

    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          server_type: cx72
        """,
    )

    config = load_config(cfg_file)

    assert config.provider.api_token == "env-secret-token"
    assert config.provider.server_type == "cx72"


def test_yaml_token_literal_interpolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ${KCB_HETZNER_TOKEN} literal in YAML is expanded from the env var."""
    monkeypatch.setenv("KCB_HETZNER_TOKEN", "expanded-token")

    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "${KCB_HETZNER_TOKEN}"
        """,
    )

    config = load_config(cfg_file)

    assert config.provider.api_token == "expanded-token"


def test_env_var_not_used_when_yaml_has_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When api_token IS in YAML, the env var should not override it."""
    monkeypatch.setenv("KCB_HETZNER_TOKEN", "should-not-appear")

    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "yaml-wins"
        """,
    )

    config = load_config(cfg_file)

    assert config.provider.api_token == "yaml-wins"


def test_missing_token_raises_without_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When api_token is missing from YAML and env var is unset, ValidationError raised."""
    monkeypatch.delenv("KCB_HETZNER_TOKEN", raising=False)

    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          server_type: cx52
        """,
    )

    with pytest.raises(ValidationError):
        load_config(cfg_file)


# ---------------------------------------------------------------------------
# 5. Invalid values raise ValidationError
# ---------------------------------------------------------------------------


def test_invalid_kernel_target_raises(tmp_path: Path) -> None:
    """A kernel target outside the allowed Literal values must raise ValidationError."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "tok"
        kernel:
          targets:
            - x86_64
            - riscv64
        """,
    )

    with pytest.raises(ValidationError):
        load_config(cfg_file)


def test_invalid_syzkaller_target_raises(tmp_path: Path) -> None:
    """A syzkaller target outside the allowed Literal values must raise ValidationError."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "tok"
        syzkaller:
          targets:
            - amd64
            - riscv
        """,
    )

    with pytest.raises(ValidationError):
        load_config(cfg_file)


def test_invalid_component_raises(tmp_path: Path) -> None:
    """An unknown component name must raise ValidationError."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "tok"
        components:
          - kernel
          - firmware
        """,
    )

    with pytest.raises(ValidationError):
        load_config(cfg_file)


def test_tarball_url_with_branch_raises(tmp_path: Path) -> None:
    """Setting both tarball_url and branch must raise ValidationError."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "tok"
        kernel:
          tarball_url: https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.13.tar.xz
          branch: master
        """,
    )

    with pytest.raises(ValidationError, match="branch is only used with git_url"):
        load_config(cfg_file)


def test_invalid_keep_on_failure_type_raises(tmp_path: Path) -> None:
    """keep_on_failure must be a boolean; a non-bool value should raise ValidationError."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "tok"
        keep_on_failure: "maybe"
        """,
    )

    with pytest.raises(ValidationError):
        load_config(cfg_file)


# ---------------------------------------------------------------------------
# 6. Extra coverage: YAML with all fields set round-trips correctly
# ---------------------------------------------------------------------------


def test_full_yaml_round_trip(tmp_path: Path) -> None:
    """A fully-specified YAML config loads without using any defaults."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "full-token"
          server_type: cx72
          location: hel1
          ssh_key_path: ~/.ssh/id_ed25519

        kernel:
          git_url: https://github.com/myorg/linux.git
          branch: my-branch
          config_overlays:
            - ./my.config
          targets:
            - x86_64
            - arm64

        rootfs:
          version: "2024.05"

        syzkaller:
          targets:
            - amd64
            - arm64

        components:
          - kernel
          - syzkaller

        output_dir: ./my-artifacts
        keep_on_failure: true
        """,
    )

    config = load_config(cfg_file)

    assert config.provider.api_token == "full-token"
    assert config.provider.server_type == "cx72"
    assert config.provider.location == "hel1"
    assert config.provider.ssh_key_path == Path("~/.ssh/id_ed25519").expanduser()

    assert config.kernel.git_url == "https://github.com/myorg/linux.git"
    assert config.kernel.branch == "my-branch"
    assert config.kernel.config_overlays == [Path("./my.config")]
    assert config.kernel.targets == ["x86_64", "arm64"]

    assert config.rootfs.version == "2024.05"

    assert config.syzkaller.targets == ["amd64", "arm64"]

    assert config.components == ["kernel", "syzkaller"]
    assert config.output_dir == Path("./my-artifacts")
    assert config.keep_on_failure is True


# ---------------------------------------------------------------------------
# 7. Discriminated union: HetznerConfig / LocalVMConfig
# ---------------------------------------------------------------------------


def test_backwards_compat_no_type(tmp_path: Path) -> None:
    """YAML without a type field in provider block should load as HetznerConfig."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          api_token: "tok"
          server_type: cx23
        """,
    )

    config = load_config(cfg_file)

    assert isinstance(config.provider, HetznerConfig)
    assert config.provider.type == "hetzner"
    assert config.provider.api_token == "tok"
    assert config.provider.server_type == "cx23"


def test_hetzner_explicit_type(tmp_path: Path) -> None:
    """YAML with type: hetzner loads correctly as HetznerConfig."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          type: hetzner
          api_token: "hetzner-tok"
          server_type: cx52
          location: nbg1
        """,
    )

    config = load_config(cfg_file)

    assert isinstance(config.provider, HetznerConfig)
    assert config.provider.type == "hetzner"
    assert config.provider.api_token == "hetzner-tok"
    assert config.provider.server_type == "cx52"
    assert config.provider.location == "nbg1"


def test_local_provider_yaml_round_trip(tmp_path: Path) -> None:
    """YAML with type: local and host loads as LocalVMConfig."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          type: local
          host: 192.168.64.10
          ssh_key_path: ~/.ssh/id_ed25519
        """,
    )

    config = load_config(cfg_file)

    assert isinstance(config.provider, LocalVMConfig)
    assert config.provider.type == "local"
    assert config.provider.host == "192.168.64.10"
    assert config.provider.ssh_key_path == Path("~/.ssh/id_ed25519").expanduser()


def test_local_provider_defaults(tmp_path: Path) -> None:
    """LocalVMConfig should have correct default values for port, username, and arch."""
    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          type: local
          host: 10.0.0.5
        """,
    )

    config = load_config(cfg_file)

    assert isinstance(config.provider, LocalVMConfig)
    assert config.provider.port == 22
    assert config.provider.username == "root"
    assert config.provider.arch == "x86_64"
    assert config.provider.ssh_key_path == Path("~/.ssh/id_rsa").expanduser()


def test_local_provider_no_token_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local provider should load successfully without api_token and without KCB_HETZNER_TOKEN."""
    monkeypatch.delenv("KCB_HETZNER_TOKEN", raising=False)

    cfg_file = write_yaml(
        tmp_path,
        """\
        provider:
          type: local
          host: 192.168.64.10
        """,
    )

    config = load_config(cfg_file)

    assert isinstance(config.provider, LocalVMConfig)
    assert config.provider.host == "192.168.64.10"


def test_provider_config_alias_is_hetzner_config() -> None:
    """ProviderConfig must remain a backwards-compat alias for HetznerConfig."""
    assert ProviderConfig is HetznerConfig
