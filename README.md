# kcb — Kernel Cloud Build

`kcb` is a Python CLI that provisions an ephemeral Hetzner VPS, connects to a local VM, or starts a local Docker container, then builds Linux kernel images, a Buildroot rootfs, and Syzkaller binaries there and downloads the artifacts. The full build still runs outside the local Python process.

---

## Install

Requires Python 3.10+ plus provider-specific tooling:
- `rsync` for the Hetzner and local VM providers
- Docker for the Docker provider

```bash
pip install .
# or
uv pip install .
```

You also need a Hetzner Cloud API token when using the Hetzner provider. Set it as an environment variable:

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
# For local Docker builds:
cp configs/example-docker.yaml ~/.kcb/config.yaml
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
  location: nbg1                         # Hetzner datacenter location. Default: nbg1
                                         # Available: fsn1 (Falkenstein, DE), nbg1 (Nuremberg, DE),
                                         #            hel1 (Helsinki, FI), ash (Ashburn, US),
                                         #            hil (Hillsboro, US), sin (Singapore)
  ssh_key_path: ~/.ssh/id_rsa            # Local SSH private key for VPS access. Default: ~/.ssh/id_rsa
                                         # The matching .pub file is also injected into
                                         # /root/.ssh/authorized_keys in the built rootfs.

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

  patch: /path/to/kcov_remote.patch           # Optional unified diff patch applied to the kernel
                                              # source tree before building (patch -p1). Use this
                                              # to inject kcov_remote_start/end or other in-tree
                                              # instrumentation. Default: null (no patch applied).

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

  extra_space_mb: 600   # Add free space to the rootfs image after build (truncate + resize2fs).
                        # Useful when pushing large files into the VM (e.g. kernel with symbols,
                        # coverage agent). Default: 0 (no extra space).

  boot_commands:        # Shell commands to run on every boot, injected as /etc/init.d/S99custom.
    - ksmbd.mountd &    # Example: start ksmbd server in background.
    - echo "VM ready"   # Commands run in the order listed under the start) action.

  extra_files:          # Arbitrary local files to inject into the rootfs image. Default: []
    - src: configs/ksmbd.conf       # Local path to the file.
      dest: /etc/ksmbd/ksmbd.conf   # Absolute destination path inside the rootfs.
                        # Parent directories are created automatically. File mode is 0644.

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
  ssh_key_path: ~/.ssh/id_rsa  # The matching .pub file is injected into /root/.ssh/authorized_keys
                               # in the built rootfs.
  arch: x86_64              # Native architecture of the build machine: x86_64 or arm64.
                            # Controls cross-toolchain selection. Default: x86_64
```

With the local provider:
- No API token is required.
- `keep_on_failure` has no effect — the machine is never destroyed.
- `kcb cleanup` is not applicable and will exit with a message.

#### Docker provider

Use a local Docker container instead of a VPS or pre-existing VM. `kcb` starts a detached container, bootstraps it like the cloud image, copies artifacts back with `docker cp`, and removes the container at the end unless `keep_on_failure: true`.

```yaml
provider:
  type: docker
  image: ubuntu:24.04         # Base image used for the build container. Default: ubuntu:24.04
  container_name: kcb-build   # Optional fixed container name. Default: null (auto-generated)
  ssh_key_path: ~/.ssh/id_rsa # The matching .pub file is injected into /root/.ssh/authorized_keys
                              # in the built rootfs.
  arch: x86_64                # Native architecture inside the container: x86_64 or arm64.
                              # Controls cross-toolchain selection. Default: x86_64
```

With the Docker provider:
- No API token is required.
- Docker must be installed locally and the daemon must be running.
- `keep_on_failure: true` keeps the container alive and prints a `docker exec` command for debugging.
- `kcb cleanup` is not applicable and will exit with a message.

### Environment variables

| Variable            | Description                                  |
|---------------------|----------------------------------------------|
| `KCB_HETZNER_TOKEN` | Hetzner API token. Used when `api_token` is absent from the config file or set to `${KCB_HETZNER_TOKEN}`. |

---

## CLI Reference

### `kcb build`

Provision a VPS, connect to a local VM, or start a local Docker container, run the build, and download artifacts.

```
kcb build [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--config PATH` | Path to YAML config file. |
| `--kernel / --no-kernel` | Include or exclude kernel from the build. |
| `--rootfs / --no-rootfs` | Include or exclude rootfs from the build. |
| `--syzkaller / --no-syzkaller` | Include or exclude syzkaller from the build. |
| `--keep-on-failure` | Keep the Hetzner VPS or Docker container alive if the build fails. No effect for the local VM provider. |
| `--server-type TEXT` | Hetzner server type, e.g. `cx43`. Overrides config. |
| `--kernel-url TEXT` | Kernel git URL. Overrides config. |
| `--kernel-branch TEXT` | Kernel git branch. Overrides config. |
| `--kernel-arch [x86_64\|arm64]` | Kernel target architecture. May be repeated. Overrides config. |
| `--kernel-config PATH` | Path to a kernel config overlay fragment. May be repeated; applied in order. Overrides config. |
| `--kernel-patch PATH` | Path to a unified diff patch applied to the kernel source before building (`patch -p1`). Overrides config. |
| `--rootfs-arch [x86_64\|arm64]` | Rootfs target architecture. May be repeated. Overrides config. |
| `--output-dir PATH` | Local directory for downloaded artifacts. Overrides config. |

Rootfs `config_fragments`, `boot_commands`, and `extra_files` can only be set via the YAML config file; there are no CLI flags for them.

**Examples:**

```bash
# Full build with defaults
kcb build

# Build only kernel and rootfs, keep the build target alive on failure
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

# Build in a local Docker container (provider.type: docker in config)
kcb build --config ~/.kcb/docker.yaml
```

---

### `kcb cleanup`

List or destroy kcb-managed VPS instances. Useful for orphaned Hetzner servers left behind by `--keep-on-failure` or interrupted runs. Not applicable when using the local VM or Docker providers.

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
- `rsync` installed locally when using the Hetzner or local VM providers
- Docker installed locally when using the Docker provider
- An SSH key at `~/.ssh/id_rsa` (or configured via `provider.ssh_key_path`)
- A Hetzner Cloud account and API token (Hetzner provider only)

The remote build server (Ubuntu 24.04) requires the following packages, all installed automatically by `bootstrap.sh`:

| Package | Used for |
|---------|----------|
| `e2fsprogs` | `debugfs` (rootfs patching), `e2fsck` + `resize2fs` (`extra_space_mb` resizing) |
