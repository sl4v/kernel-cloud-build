# test_hetzner_mock.py — Mock-based tests for kcb/hetzner.py
"""
All tests use unittest.mock to mock hcloud.Client so no real API calls are made.
pytest-asyncio is configured in auto mode (see pyproject.toml), so async test
functions are collected automatically without needing @pytest.mark.asyncio.
"""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kcb.config import ProviderConfig
from kcb.hetzner import ServerHandle, create_server, destroy_server, list_servers, wait_ready

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_TOKEN = "fake-token"
_FAKE_KEY_PATH = Path("/tmp/test_kcb_id_rsa")


def _make_config(**kwargs) -> ProviderConfig:
    return ProviderConfig(
        api_token=_FAKE_TOKEN,
        server_type="cx23",
        location="nbg1",
        ssh_key_path=_FAKE_KEY_PATH,
        **kwargs,
    )


def _make_mock_server(
    server_id: int = 42,
    name: str = "kcb-abcdef01",
    created: datetime.datetime | None = None,
    ipv4: str = "1.2.3.4",
) -> MagicMock:
    """Build a mock BoundServer with the minimal attributes our code touches."""
    if created is None:
        created = datetime.datetime(2024, 1, 1, 12, 0, 0)
    server = MagicMock()
    server.id = server_id
    server.name = name
    server.created = created
    server.public_net.ipv4.ip = ipv4
    return server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fake_pubkey(tmp_path, monkeypatch):
    """Write a minimal fake RSA public key that the fingerprint code can parse."""
    import base64
    import struct

    # Build a minimal SSH RSA public key blob (just enough bytes for base64)
    key_type = b"ssh-rsa"

    def _encode_str(s: bytes) -> bytes:
        return struct.pack(">I", len(s)) + s

    # e and n are tiny — only for fingerprint hashing, not real crypto
    e = (65537).to_bytes(3, "big")
    n = (0xDEADBEEF).to_bytes(4, "big")
    blob = _encode_str(key_type) + _encode_str(b"\x00" + e) + _encode_str(b"\x00" + n)
    b64 = base64.b64encode(blob).decode()
    pub_key_content = f"ssh-rsa {b64} test-key"

    pub_path = tmp_path / "id_rsa.pub"
    pub_path.write_text(pub_key_content)

    # Patch _load_public_key to return our fake key data, and track the path
    monkeypatch.setattr(
        "kcb.hetzner._load_public_key",
        lambda _path: pub_key_content,
    )


# ---------------------------------------------------------------------------
# Test 1 — create_server: new SSH key uploaded, server created
# ---------------------------------------------------------------------------

async def test_create_server_uploads_new_ssh_key():
    """When no key with the matching fingerprint exists, a new one is created."""
    config = _make_config()
    mock_server = _make_mock_server()
    mock_response = MagicMock()
    mock_response.server = mock_server

    with patch("kcb.hetzner.hcloud.Client") as MockClient:
        client_instance = MockClient.return_value
        # No existing key with this fingerprint
        client_instance.ssh_keys.get_by_fingerprint.return_value = None
        mock_ssh_key = MagicMock()
        mock_ssh_key.id = 99
        client_instance.ssh_keys.create.return_value = mock_ssh_key
        client_instance.servers.create.return_value = mock_response

        handle = await create_server(config)

    # SSH key should have been uploaded
    client_instance.ssh_keys.create.assert_called_once()
    create_kwargs = client_instance.ssh_keys.create.call_args

    # Server must have been created with correct image, type, location, labels
    client_instance.servers.create.assert_called_once()
    server_kwargs = client_instance.servers.create.call_args[1]
    assert server_kwargs["image"].name == "ubuntu-24.04"
    assert server_kwargs["server_type"].name == "cx23"
    assert server_kwargs["location"].name == "nbg1"
    assert server_kwargs["labels"] == {"managed-by": "kcb"}

    # Returned handle must be populated
    assert handle.server_id == "42"
    assert handle.label == "kcb-abcdef01"
    assert handle.created_at != ""


# ---------------------------------------------------------------------------
# Test 2 — create_server: existing SSH key reused
# ---------------------------------------------------------------------------

async def test_create_server_reuses_existing_ssh_key():
    """When a key with the matching fingerprint already exists, no upload occurs."""
    config = _make_config()
    mock_server = _make_mock_server()
    mock_response = MagicMock()
    mock_response.server = mock_server

    with patch("kcb.hetzner.hcloud.Client") as MockClient:
        client_instance = MockClient.return_value
        existing_key = MagicMock()
        existing_key.id = 77
        existing_key.name = "my-existing-key"
        # Fingerprint lookup finds an existing key
        client_instance.ssh_keys.get_by_fingerprint.return_value = existing_key
        client_instance.servers.create.return_value = mock_response

        handle = await create_server(config)

    # No new key should have been created
    client_instance.ssh_keys.create.assert_not_called()

    # The existing key should have been passed to server creation
    server_kwargs = client_instance.servers.create.call_args[1]
    ssh_keys_arg = server_kwargs["ssh_keys"]
    assert len(ssh_keys_arg) == 1
    assert ssh_keys_arg[0].id == 77

    assert handle.server_id == "42"


# ---------------------------------------------------------------------------
# Test 3 — wait_ready: TCP connects immediately
# ---------------------------------------------------------------------------

async def test_wait_ready_succeeds_immediately():
    """If TCP port 22 is open on first try, the public IP is returned."""
    config = _make_config()
    handle = ServerHandle(server_id="42", label="kcb-test", created_at="2024-01-01")

    mock_server = _make_mock_server(ipv4="10.0.0.1")

    with patch("kcb.hetzner.hcloud.Client") as MockClient:
        client_instance = MockClient.return_value
        client_instance.servers.get_by_id.return_value = mock_server

        # asyncio.open_connection succeeds immediately
        mock_reader = MagicMock()
        mock_writer = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch("asyncio.open_connection", new=AsyncMock(return_value=(mock_reader, mock_writer))):
            ip = await wait_ready(handle, config, timeout=30)

    assert ip == "10.0.0.1"


# ---------------------------------------------------------------------------
# Test 4 — wait_ready: TCP fails then succeeds
# ---------------------------------------------------------------------------

async def test_wait_ready_retries_then_succeeds():
    """If TCP connection fails once and succeeds on the second attempt, IP is returned."""
    config = _make_config()
    handle = ServerHandle(server_id="42", label="kcb-test", created_at="2024-01-01")

    mock_server = _make_mock_server(ipv4="10.0.0.2")

    mock_reader = MagicMock()
    mock_writer = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    call_count = 0

    async def _open_connection(ip, port):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("Connection refused")
        return mock_reader, mock_writer

    with patch("kcb.hetzner.hcloud.Client") as MockClient:
        client_instance = MockClient.return_value
        client_instance.servers.get_by_id.return_value = mock_server

        with patch("asyncio.open_connection", side_effect=_open_connection):
            with patch("asyncio.sleep", new=AsyncMock()):
                ip = await wait_ready(handle, config, timeout=30)

    assert ip == "10.0.0.2"
    assert call_count == 2


# ---------------------------------------------------------------------------
# Test 5 — wait_ready: always fails, TimeoutError raised
# ---------------------------------------------------------------------------

async def test_wait_ready_timeout():
    """If TCP connection always fails, TimeoutError is raised after timeout."""
    config = _make_config()
    handle = ServerHandle(server_id="42", label="kcb-test", created_at="2024-01-01")

    mock_server = _make_mock_server(ipv4="10.0.0.3")

    with patch("kcb.hetzner.hcloud.Client") as MockClient:
        client_instance = MockClient.return_value
        client_instance.servers.get_by_id.return_value = mock_server

        # time.monotonic advances quickly so the loop exits after a single iteration
        import time as _time
        _start = _time.monotonic()
        call_count = 0

        def _monotonic():
            nonlocal call_count
            call_count += 1
            # First call: return start time; subsequent calls: past deadline
            if call_count == 1:
                return _start
            return _start + 9999

        with patch("asyncio.open_connection", new=AsyncMock(side_effect=OSError("refused"))):
            with patch("asyncio.sleep", new=AsyncMock()):
                with patch("kcb.hetzner.time.monotonic", side_effect=_monotonic):
                    with pytest.raises(TimeoutError):
                        await wait_ready(handle, config, timeout=1)


# ---------------------------------------------------------------------------
# Test 6 — destroy_server: client.servers.delete called
# ---------------------------------------------------------------------------

async def test_destroy_server_calls_delete():
    """destroy_server fetches the server and calls delete on it."""
    config = _make_config()
    handle = ServerHandle(server_id="42", label="kcb-test", created_at="2024-01-01")
    mock_server = _make_mock_server()

    with patch("kcb.hetzner.hcloud.Client") as MockClient:
        client_instance = MockClient.return_value
        client_instance.servers.get_by_id.return_value = mock_server

        await destroy_server(handle, config)

    client_instance.servers.get_by_id.assert_called_once_with(42)
    client_instance.servers.delete.assert_called_once_with(mock_server)


# ---------------------------------------------------------------------------
# Test 7 — destroy_server: not_found error is ignored
# ---------------------------------------------------------------------------

async def test_destroy_server_ignores_not_found():
    """If the server has already been deleted, destroy_server completes silently."""
    import hcloud as _hcloud

    config = _make_config()
    handle = ServerHandle(server_id="99", label="kcb-gone", created_at="2024-01-01")

    with patch("kcb.hetzner.hcloud.Client") as MockClient:
        client_instance = MockClient.return_value
        client_instance.servers.get_by_id.side_effect = _hcloud.APIException(
            code="not_found",
            message="server not found",
            details=None,
        )

        # Should NOT raise
        await destroy_server(handle, config)

    client_instance.servers.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8 — list_servers: returns ServerHandles for all kcb-tagged servers
# ---------------------------------------------------------------------------

async def test_list_servers_returns_kcb_tagged_servers():
    """list_servers queries with managed-by=kcb label selector and maps results."""
    config = _make_config()

    server_a = _make_mock_server(server_id=1, name="kcb-aaa", ipv4="1.1.1.1",
                                  created=datetime.datetime(2024, 2, 1))
    server_b = _make_mock_server(server_id=2, name="kcb-bbb", ipv4="2.2.2.2",
                                  created=datetime.datetime(2024, 2, 2))

    with patch("kcb.hetzner.hcloud.Client") as MockClient:
        client_instance = MockClient.return_value
        client_instance.servers.get_all.return_value = [server_a, server_b]

        handles = await list_servers(config)

    # get_all must be called with the correct label selector
    client_instance.servers.get_all.assert_called_once_with(label_selector="managed-by=kcb")

    assert len(handles) == 2
    ids = {h.server_id for h in handles}
    labels = {h.label for h in handles}
    assert ids == {"1", "2"}
    assert labels == {"kcb-aaa", "kcb-bbb"}
