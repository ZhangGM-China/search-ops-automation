import asyncio
import os
import socket
import subprocess
import sys
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
import requests
import urllib3

from network_guard import should_block_business_url


def test_pytest_blocks_business_api_before_requests_sends():
    response = Mock(status_code=200)

    with patch("requests.sessions.Session.send", return_value=response) as send:
        with pytest.raises(RuntimeError, match="TestNetworkBlockedError"):
            requests.get(
                "http://staging.example.test/api/search/mixed",
                timeout=1,
            )

    send.assert_not_called()


def test_pytest_blocks_business_api_before_httpx_sends():
    with patch("httpx.Client.send") as send:
        with pytest.raises(RuntimeError, match="TestNetworkBlockedError"):
            httpx.get("https://staging.example.test/api/search/mixed")

    send.assert_not_called()


def test_pytest_blocks_business_api_before_urllib3_sends():
    with patch("urllib3.PoolManager.urlopen") as urlopen:
        with pytest.raises(RuntimeError, match="TestNetworkBlockedError"):
            urllib3.PoolManager().request(
                "GET",
                "https://staging.example.test/api/search/mixed",
            )

    urlopen.assert_not_called()


def test_pytest_blocks_business_api_before_socket_dns_or_connect():
    with patch("socket.getaddrinfo") as getaddrinfo:
        with pytest.raises(RuntimeError, match="TestNetworkBlockedError"):
            socket.create_connection(("staging.example.test", 80), timeout=1)

    getaddrinfo.assert_not_called()


def test_localhost_is_not_classified_as_business_api():
    assert should_block_business_url("http://localhost:8000/health") is False
    assert should_block_business_url("http://127.0.0.1:5174/config") is False


def test_mocked_requests_call_is_not_blocked():
    response = Mock(status_code=200)

    with patch("requests.request", return_value=response) as request:
        result = requests.request(
            "GET",
            "https://staging.example.test/api/search/mixed",
        )

    assert result is response
    request.assert_called_once()


def test_mocked_requests_session_send_is_not_blocked():
    prepared = requests.Request(
        "GET",
        "https://staging.example.test/api/search/mixed",
    ).prepare()
    response = requests.Response()
    response.status_code = 200

    with patch("requests.sessions.Session.send", return_value=response) as send:
        result = requests.Session().send(prepared)

    assert result is response
    send.assert_called_once()


def test_mocked_httpx_client_send_is_not_blocked():
    request = httpx.Request(
        "GET",
        "https://staging.example.test/api/search/mixed",
    )
    response = httpx.Response(200, request=request)

    with patch("httpx.Client.send", return_value=response) as send:
        with httpx.Client() as client:
            result = client.send(request)

    assert result is response
    send.assert_called_once()


def test_pytest_blocks_prepared_request_before_session_send_transport():
    prepared = requests.Request(
        "GET",
        "https://staging.example.test/api/search/mixed",
    ).prepare()

    response = requests.Response()
    response.url = prepared.url
    response.history = []
    response.status_code = 200
    response.raw = urllib3.response.HTTPResponse(body=b"", status=200, headers={})
    response._content = b""
    response.request = prepared

    with patch("requests.adapters.HTTPAdapter.send", return_value=response) as adapter_send:
        with pytest.raises(RuntimeError, match="TestNetworkBlockedError"):
            requests.Session().send(prepared)

    adapter_send.assert_not_called()


def test_pytest_blocks_prepared_request_before_http_adapter_transport():
    prepared = requests.Request(
        "GET",
        "https://staging.example.test/api/search/mixed",
    ).prepare()
    connection = Mock()
    connection.urlopen.side_effect = AssertionError("transport called")

    with patch.object(
        requests.adapters.HTTPAdapter,
        "get_connection_with_tls_context",
        return_value=connection,
    ):
        with pytest.raises(RuntimeError, match="TestNetworkBlockedError"):
            requests.adapters.HTTPAdapter().send(prepared)

    connection.urlopen.assert_not_called()


def test_pytest_blocks_httpx_request_before_direct_client_send_transport():
    request = httpx.Request(
        "GET",
        "https://staging.example.test/api/search/mixed",
    )
    response = httpx.Response(200, request=request)

    with patch("httpx.HTTPTransport.handle_request", return_value=response) as transport:
        with pytest.raises(RuntimeError, match="TestNetworkBlockedError"):
            with httpx.Client() as client:
                client.send(request)

    transport.assert_not_called()


def test_pytest_blocks_httpx_request_before_direct_async_send_transport():
    async def send_request():
        request = httpx.Request(
            "GET",
            "https://staging.example.test/api/search/mixed",
        )
        response = httpx.Response(200, request=request)
        transport = AsyncMock(return_value=response)

        with patch("httpx.AsyncHTTPTransport.handle_async_request", transport):
            with pytest.raises(RuntimeError, match="TestNetworkBlockedError"):
                async with httpx.AsyncClient() as client:
                    await client.send(request)

        transport.assert_not_called()

    asyncio.run(send_request())


def test_pytest_blocks_business_host_before_getaddrinfo_dns():
    with patch("_socket.getaddrinfo", return_value=[]) as dns:
        with pytest.raises(RuntimeError, match="TestNetworkBlockedError"):
            socket.getaddrinfo("STAGING.EXAMPLE.TEST.", 443)

    dns.assert_not_called()


def test_getaddrinfo_allows_suffix_confusion_host_to_reach_mocked_dns():
    assert should_block_business_url("https://evilexample.test/path") is False
    if os.environ.get("AIOPS_OFFLINE_TEST") == "1":
        with pytest.raises(RuntimeError, match="OfflineNetworkBlockedError"):
            socket.getaddrinfo("evilexample.test", 443)
        return

    expected = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    with patch("_socket.getaddrinfo", return_value=expected) as dns:
        result = socket.getaddrinfo("evilexample.test", 443)

    assert result == expected
    dns.assert_called_once()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/path",
        "https://STAGING.EXAMPLE.TEST./path",
        b"https://staging.example.test/path",
    ],
)
def test_business_domain_matching_supports_exact_subdomain_case_dot_and_bytes(url):
    assert should_block_business_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://evilexample.test/path",
        "https://example.test.evil/path",
        "https://notexample.test/path",
    ],
)
def test_business_domain_matching_rejects_suffix_confusion(url):
    assert should_block_business_url(url) is False


def test_socket_connect_ex_guard_blocks_before_underlying_call_in_fresh_process():
    script = r'''
import sys
sys.path.insert(0, "tests")
import network_guard

calls = []

class FakeSocket:
    def connect(self, address):
        calls.append(("connect", address))
        return None

    def connect_ex(self, address):
        calls.append(("connect_ex", address))
        return 0

network_guard.socket.socket = FakeSocket
network_guard.install_test_network_guard()

try:
    FakeSocket().connect_ex((b"STAGING.EXAMPLE.TEST.", 443))
except network_guard.TestNetworkBlockedError:
    pass
else:
    raise AssertionError("connect_ex was not blocked")

assert calls == [], calls
'''

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
