# kcb — Kernel Cloud Build

`kcb` is a Python CLI that provisions an ephemeral Hetzner VPS (or connects to a local VM), builds Linux kernel images, a Buildroot rootfs, and Syzkaller binaries on it, downloads the artifacts via rsync, then destroys the VPS. The full build runs remotely.

---

## Install

Requires Python 3.10+ and `rsync` installed locally.

```bash
pip install .
# or
uv pip install .
```

You also need a Hetzner Cloud API token (unless using a local VM). Set it as an environment variable:

```bash
export KCB_HETZNER_TOKEN=your_token_here
```

---

## Quickstart

```bash
# Set your API token
export KCB_HETZNER_TOKEN=your_token_here

# Run a full build (kernel + rootfs + syzkaller) with defaults
kcb build

# Artifacts land in ./kcb-artifacts/ by default
ls ./kcb-artifacts/
```

To use a config file instead:

```bash
# For Hetzner cloud builds:
cp configs/example-cloud.yaml ~/.kcb/config.yaml
# For local VM builds:
cp configs/example-local.yaml ~/.kcb/config.yaml
# Edit ~/.kcb/config.yaml to set your preferences
kcb build --config ~/.kcb/config.yaml
```

---

## Configuration

`kcb` reads a YAML config file. All fields are optional except `provider.api_token` (Hetzner provider), which can be supplied via the `KCB_HETZNER_TOKEN` environment variable instead.

### Full config reference

#### Hetzner provider (cloud VPS)

```yaml
provider:
  type: hetzner                          # Default when type is omitted
  api_token: "${KCB_HETZNER_TOKEN}"      # Required. ${VAR} expansion is supported.
  server_type: cx43                      # Hetzner server type.
  location: fsn1                         # Hetzner datacenter location. Default: fsn1
                                         # Available: fsn1 (Falkenstein, DE), nbg1 (Nuremberg, DE),
                                         #            hel1 (Helsinki, FI), ash (Ashburn, US),
                                         #            hil (Hillsboro, US), sin (Singapore)
  ssh_key_path: ~/.ssh/id_rsa            # Local SSH private key for VPS access. Default: ~/.ssh/id_rsa

components:                              # Which components to build. Default: [kernel, rootfs, syzkaller]
  - kernel
  - rootfs
  - syzkaller

kernel:
  git_url: https://github.com/torvalds/linux.git  # Kernel git URL. Default: mainline Linux
  branch: master                                  # Git branch. Default: master
  # tarball_url: https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.9.tar.xz
  #   Download a tarball instead of cloning. Mutually exclusive with branch.

  targets:                                    # Architectures to build. Default: [x86_64]
    - x86_64                                  #   produces bzImage + vmlinux
    - arm64                                   #   produces Image + vmlinux (cross-compiled)

  config_overlays:                            # Optional kernel config fragments. Default: []
    - configs/kernel-overlays/fuzzing.config  # Applied in order after defconfig; later entries win.
    - configs/kernel-overlays/ksmbd.config

rootfs:
  version: "2024.02"  # Buildroot release version. Default: "2024.02"
  targets:            # Architectures to build rootfs for. Default: [x86_64]
    - x86_64
    - arm64

  config_fragments:                             # Optional Buildroot config fragments. Default: []
    - configs/rootfs-fragments/ksmbd.config     # Applied in order after defconfig; later entries win.

syzkaller:
  targets:            # Syzkaller target OS/arch pairs. Default: [amd64]
    - amd64
    - arm64

  host_os:            # OS(es) to compile host tools (syz-manager etc.) for. Default: [linux]
    - linux           #   HOSTOS=linux binaries → host_{arch}/
    - macos           #   HOSTOS=darwin cross-compiled binaries → host_darwin_{arch}/

output_dir: ~/kernel-builds  # Local directory to download artifacts into. Default: ./kcb-artifacts

keep_on_failure: false       # Keep the VPS alive if the build fails. Default: false
```

#### Local VM provider

Use an existing machine instead of provisioning a cloud VPS. `kcb` will SSH in, run the build, rsync the artifacts, and then leave the machine untouched.

```yaml
provider:
  type: local
  host: 192.168.64.10       # IP or hostname of the build machine
  port: 22                  # SSH port. Default: 22
  username: root            # SSH username. Default: root
  ssh_key_path: ~/.ssh/id_rsa
  arch: x86_64              # Native architecture of the build machine: x86_64 or arm64.
                            # Controls cross-toolchain selection. Default: x86_64
```

With the local provider:
- No API token is required.
- `keep_on_failure` has no effect — the machine is never destroyed.
- `kcb cleanup` is not applicable and will exit with a message.

### Environment variables

| Variable            | Description                                  |
|---------------------|----------------------------------------------|
| `KCB_HETZNER_TOKEN` | Hetzner API token. Used when `api_token` is absent from the config file or set to `${KCB_HETZNER_TOKEN}`. |

---

## CLI Reference

### `kcb build`

Provision a VPS (or connect to a local VM), run the build, download artifacts, destroy the VPS.

```
kcb build [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--config PATH` | Path to YAML config file. |
| `--kernel / --no-kernel` | Include or exclude kernel from the build. |
| `--rootfs / --no-rootfs` | Include or exclude rootfs from the build. |
| `--syzkaller / --no-syzkaller` | Include or exclude syzkaller from the build. |
| `--keep-on-failure` | Keep the VPS alive if the build fails. Prints the server ID for later cleanup. (Hetzner provider only.) |
| `--server-type TEXT` | Hetzner server type, e.g. `cx43`. Overrides config. |
| `--kernel-url TEXT` | Kernel git URL. Overrides config. |
| `--kernel-branch TEXT` | Kernel git branch. Overrides config. |
| `--kernel-arch [x86_64\|arm64]` | Kernel target architecture. May be repeated. Overrides config. |
| `--kernel-config PATH` | Path to a kernel config overlay fragment. May be repeated; applied in order. Overrides config. |
| `--rootfs-arch [x86_64\|arm64]` | Rootfs target architecture. May be repeated. Overrides config. |
| `--output-dir PATH` | Local directory for downloaded artifacts. Overrides config. |

Rootfs `config_fragments` can only be set via the YAML config file; there is no CLI flag for them.

**Examples:**

```bash
# Full build with defaults
kcb build

# Build only kernel and rootfs, keep VPS on failure
kcb build --kernel --rootfs --keep-on-failure

# Custom kernel, arm64 cross-compile, config overlay
kcb build \
  --kernel-url https://github.com/myorg/linux.git \
  --kernel-branch my-fix-v3 \
  --kernel-arch x86_64 --kernel-arch arm64 \
  --kernel-config ./configs/kernel-overlays/fuzzing.config \
  --output-dir ./build-output

# Build kernel and rootfs for arm64 only
kcb build --kernel --rootfs \
  --kernel-arch arm64 \
  --rootfs-arch arm64

# Use a YAML config file, override branch only
kcb build --config ~/.kcb/config.yaml --kernel-branch test-branch

# Syzkaller only
kcb build --syzkaller

# Larger server for faster build
kcb build --server-type cx43

# Build on a local VM (provider.type: local in config)
kcb build --config ~/.kcb/local.yaml
```

---

### `kcb cleanup`

List or destroy kcb-managed VPS instances. Useful for orphaned servers left behind by `--keep-on-failure` or interrupted runs. Not applicable when using the local provider.

```
kcb cleanup [OPTIONS] [SERVER_ID]
```

| Flag / Argument | Description |
|-----------------|-------------|
| `SERVER_ID` | Destroy a specific server by ID. |
| `--list` | List all kcb-managed servers with their IDs and creation times. |
| `--all` | Destroy all kcb-managed servers (prompts for confirmation). |
| `--config PATH` | Path to YAML config file (for API token). |
| `--token TEXT` | Hetzner API token. Overrides config and `KCB_HETZNER_TOKEN`. |

**Examples:**

```bash
# List orphaned servers
kcb cleanup --list

# Destroy a specific server
kcb cleanup 12345678

# Destroy all kcb-managed servers
kcb cleanup --all
```

---

## Local Artifact Layout

After a successful build, artifacts are organized under `output_dir` by architecture:

```
kcb-artifacts/
├── x86_64/
│   ├── bzImage              (kernel image)
│   ├── vmlinux              (uncompressed kernel)
│   ├── rootfs.ext4          (Buildroot rootfs, if built)
│   ├── linux_amd64/         (syzkaller target binaries)
│   ├── host_amd64/          (syz-manager, syz-repro, syz-mutate, etc.)
│   └── host_darwin_amd64/   (macOS host tools, if host_os: [macos])
└── arm64/                   (if arm64 was a target)
    ├── Image
    ├── vmlinux
    ├── rootfs.ext4
    ├── linux_arm64/
    └── host_arm64/
```

---

## Requirements

- Python 3.10+
- `rsync` installed locally (used to download artifacts from the VPS)
- An SSH key at `~/.ssh/id_rsa` (or configured via `provider.ssh_key_path`)
- A Hetzner Cloud account and API token (Hetzner provider only)
