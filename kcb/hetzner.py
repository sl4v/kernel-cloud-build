"""Hetzner Cloud VPS lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import hcloud
from hcloud.images import Image
from hcloud.locations import Location
from hcloud.server_types import ServerType
from hcloud.ssh_keys import SSHKey

from kcb.config import ProviderConfig

log = logging.getLogger(__name__)

_KCB_LABEL = "managed-by"
_KCB_LABEL_VALUE = "kcb"
_KCB_LABEL_SELECTOR = f"{_KCB_LABEL}={_KCB_LABEL_VALUE}"


@dataclass(frozen=True)
class ServerHandle:
    server_id: str
    label: str
    created_at: str


def _make_client(config: ProviderConfig) -> hcloud.Client:
    return hcloud.Client(token=config.api_token)


def _load_public_key(ssh_key_path: Path) -> str:
    """Read the SSH public key from the given path.

    If the path does not have a .pub suffix, one is appended automatically.
    """
    path = ssh_key_path.expanduser()
    if path.suffix != ".pub":
        path = path.with_suffix(path.suffix + ".pub")
    return path.read_text().strip()


async def create_server(config: ProviderConfig) -> ServerHandle:
    """Provision a new VPS tagged with managed-by=kcb."""
    client = _make_client(config)

    # Load and potentially upload the SSH public key (idempotent by fingerprint).
    public_key_data = _load_public_key(config.ssh_key_path)

    # Compute fingerprint via the hcloud SDK helper by creating a temporary key
    # object and letting the API look up by fingerprint after upload attempt.
    # Strategy: attempt to create; if it already exists by fingerprint, fetch it.
    import hashlib
    import base64

    # Parse the key to compute its MD5 fingerprint (matching Hetzner's format).
    parts = public_key_data.split()
    key_b64 = parts[1] if len(parts) >= 2 else parts[0]
    key_bytes = base64.b64decode(key_b64)
    md5_digest = hashlib.md5(key_bytes).digest()
    fingerprint = ":".join(f"{b:02x}" for b in md5_digest)

    # Check if SSH key already registered with this fingerprint.
    ssh_key = client.ssh_keys.get_by_fingerprint(fingerprint)
    if ssh_key is None:
        key_name = f"kcb-{uuid.uuid4().hex[:8]}"
        log.debug("Uploading SSH key as %s", key_name)
        ssh_key = client.ssh_keys.create(name=key_name, public_key=public_key_data)
    else:
        log.debug("SSH key already registered: %s (fingerprint %s)", ssh_key.name, fingerprint)

    # Build a unique server name.
    server_name = f"kcb-{uuid.uuid4().hex[:8]}"

    log.info("Creating server %s (type=%s, location=%s)", server_name, config.server_type, config.location)

    response = client.servers.create(
        name=server_name,
        server_type=ServerType(name=config.server_type),
        image=Image(name="ubuntu-24.04"),
        location=Location(name=config.location),
        ssh_keys=[SSHKey(id=ssh_key.id)],
        labels={_KCB_LABEL: _KCB_LABEL_VALUE},
    )

    server = response.server
    return ServerHandle(
        server_id=str(server.id),
        label=server.name,
        created_at=str(server.created),
    )


async def wait_ready(
    handle: ServerHandle,
    config: ProviderConfig,
    timeout: int = 300,
) -> str:
    """Poll TCP port 22 every 5s until reachable; return public IPv4.

    Retrieves the server's IP via the Hetzner API, then attempts TCP connections
    on port 22 at 5-second intervals until the server responds or *timeout*
    seconds elapse.

    Raises:
        TimeoutError: if the server does not respond within *timeout* seconds.
    """
    client = _make_client(config)
    server = client.servers.get_by_id(int(handle.server_id))
    ip: str = server.public_net.ipv4.ip

    log.info("Waiting for SSH on %s (server %s, timeout=%ds)", ip, handle.server_id, timeout)
    print(f"[kcb] Polling SSH on {ip} (timeout={timeout}s)...", flush=True)

    start = time.monotonic()
    deadline = start + timeout
    last_progress = start
    while True:
        try:
            reader, writer = await asyncio.open_connection(ip, 22)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.info("Server %s is ready at %s", handle.server_id, ip)
            return ip
        except OSError:
            pass

        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise TimeoutError(
                f"Server {handle.server_id} did not become ready within {timeout}s"
            )

        if now - last_progress >= 15:
            print(f"[kcb] Still waiting for SSH on {ip} ({int(now - start)}s elapsed)...", flush=True)
            last_progress = now

        wait = min(5.0, remaining)
        await asyncio.sleep(wait)


async def destroy_server(handle: ServerHandle, config: ProviderConfig) -> None:
    """Terminate and delete a VPS by its handle.

    Not-found errors are silently ignored so that the function is safe to call
    on already-deleted servers.
    """
    client = _make_client(config)
    try:
        server = client.servers.get_by_id(int(handle.server_id))
        client.servers.delete(server)
        log.info("Deleted server %s", handle.server_id)
    except hcloud.APIException as exc:
        if exc.code == "not_found":
            log.debug("Server %s already gone (not_found), ignoring", handle.server_id)
        else:
            raise


async def list_servers(config: ProviderConfig) -> list[ServerHandle]:
    """List all VPS instances tagged with managed-by=kcb (orphan detection)."""
    client = _make_client(config)
    servers = client.servers.get_all(label_selector=_KCB_LABEL_SELECTOR)
    return [
        ServerHandle(
            server_id=str(s.id),
            label=s.name,
            created_at=str(s.created),
        )
        for s in servers
    ]
