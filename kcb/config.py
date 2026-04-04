"""Configuration models for kcb using Pydantic v2."""

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class HetznerConfig(BaseModel):
    type: Literal["hetzner"] = "hetzner"
    api_token: str  # env: KCB_HETZNER_TOKEN
    server_type: str = "cx23"
    location: str = "nbg1"
    ssh_key_path: Path = Path("~/.ssh/id_rsa")

    @model_validator(mode="after")
    def expand_ssh_key_path(self) -> "HetznerConfig":
        self.ssh_key_path = self.ssh_key_path.expanduser()
        return self

    @model_validator(mode="after")
    def resolve_api_token(self) -> "HetznerConfig":
        """Expand ${VAR} references and fall back to KCB_HETZNER_TOKEN env var."""
        token = self.api_token

        # Expand ${VAR_NAME} references in the token value
        def _expand(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))

        token = re.sub(r"\$\{([^}]+)\}", _expand, token)

        # If the token is still a placeholder or empty, fall back to env var
        if not token or token.startswith("${"):
            token = os.environ.get("KCB_HETZNER_TOKEN", token)

        self.api_token = token
        return self


# Backwards-compat alias — existing code (hetzner.py, __main__.py) imports ProviderConfig
ProviderConfig = HetznerConfig


class LocalVMConfig(BaseModel):
    type: Literal["local"] = "local"
    host: str
    port: int = 22
    username: str = "root"
    ssh_key_path: Path = Path("~/.ssh/id_rsa")
    arch: Literal["x86_64", "arm64"] = "x86_64"

    @model_validator(mode="after")
    def expand_ssh_key_path(self) -> "LocalVMConfig":
        self.ssh_key_path = self.ssh_key_path.expanduser()
        return self


class KernelConfig(BaseModel):
    git_url: str = "https://github.com/torvalds/linux.git"
    branch: str = "master"
    tarball_url: str | None = None
    patch: Path | None = None
    config_overlays: list[Path] = []
    targets: list[Literal["x86_64", "arm64"]] = ["x86_64"]

    @model_validator(mode="before")
    @classmethod
    def reject_tarball_with_branch(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("tarball_url") and data.get("branch"):
            raise ValueError(
                "branch is only used with git_url; remove branch when tarball_url is set"
            )
        return data


class RootfsFile(BaseModel):
    src: Path   # local path to the file
    dest: str   # absolute path inside the rootfs, e.g. "/etc/ksmbd/ksmbd.conf"


class BuildrootConfig(BaseModel):
    version: str = "2024.02"
    targets: list[Literal["x86_64", "arm64"]] = ["x86_64"]
    config_fragments: list[Path] = []
    extra_space_mb: int = 0
    boot_commands: list[str] = []
    extra_files: list[RootfsFile] = []


class SyzkallerConfig(BaseModel):
    targets: list[Literal["amd64", "arm64"]] = ["amd64"]
    host_os: list[Literal["linux", "macos"]] = ["linux"]


class BuildConfig(BaseModel):
    provider: Annotated[HetznerConfig | LocalVMConfig, Field(discriminator="type")]
    kernel: KernelConfig = Field(default_factory=KernelConfig)
    rootfs: BuildrootConfig = Field(default_factory=BuildrootConfig)
    syzkaller: SyzkallerConfig = Field(default_factory=SyzkallerConfig)
    components: list[Literal["kernel", "rootfs", "syzkaller"]] = [
        "kernel",
        "rootfs",
        "syzkaller",
    ]
    output_dir: Path = Path("./kcb-artifacts")
    keep_on_failure: bool = False


def _set_nested(d: dict, dotted_key: str, value: Any) -> None:
    """Set a value in a nested dict using a dotted key path.

    Example: _set_nested(d, "provider.server_type", "cx43") sets
    d["provider"]["server_type"] = "cx43", creating intermediate dicts.
    """
    parts = dotted_key.split(".", 1)
    if len(parts) == 1:
        d[dotted_key] = value
    else:
        head, tail = parts
        if head not in d or not isinstance(d[head], dict):
            d[head] = {}
        _set_nested(d[head], tail, value)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict.

    Nested dicts are merged; all other types are overwritten by override.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(
    yaml_path: Path | None = None,
    **cli_overrides: Any,
) -> BuildConfig:
    """Load BuildConfig from a YAML file and apply CLI overrides on top.

    Two-stage loading:
    1. Parse the YAML file (if provided) as the base configuration.
    2. Merge cli_overrides on top — CLI always wins.

    cli_overrides accepts dotted keys (e.g. provider__server_type="cx72" or
    as a flat dict passed via **) that map onto the nested config structure.
    Callers may also pass pre-nested dicts as values.

    The api_token field (Hetzner provider) additionally supports:
    - Literal "${KCB_HETZNER_TOKEN}" in YAML (expanded at model validation time)
    - KCB_HETZNER_TOKEN env var as a fallback when api_token is absent from YAML
    """
    # Stage 1: load YAML base
    raw: dict[str, Any] = {}
    if yaml_path is not None:
        yaml_path = Path(yaml_path)
        with yaml_path.open() as fh:
            loaded = yaml.safe_load(fh)
        if loaded is not None:
            raw = loaded

    # Stage 2: apply CLI overrides (dotted keys, e.g. "provider.server_type")
    override_dict: dict[str, Any] = {}
    for key, value in cli_overrides.items():
        # Accept both dot-separated and double-underscore-separated keys
        normalized = key.replace("__", ".")
        _set_nested(override_dict, normalized, value)

    merged = _deep_merge(raw, override_dict)

    # Inject type: hetzner when absent from provider block for backwards compat.
    # The discriminated union requires the type field to be present.
    provider_raw = merged.setdefault("provider", {})
    if isinstance(provider_raw, dict) and not provider_raw.get("type"):
        provider_raw["type"] = "hetzner"

    # Inject env-var token fallback only for Hetzner provider.
    # Local provider does not use an API token, so injecting would cause
    # unexpected HetznerConfig fields on a LocalVMConfig.
    provider_type = provider_raw.get("type") if isinstance(provider_raw, dict) else None
    if provider_type == "hetzner":
        env_token = os.environ.get("KCB_HETZNER_TOKEN")
        if env_token:
            if not isinstance(provider_raw, dict):
                merged["provider"] = {"type": "hetzner", "api_token": env_token}
            elif not provider_raw.get("api_token"):
                provider_raw["api_token"] = env_token

    return BuildConfig.model_validate(merged)
