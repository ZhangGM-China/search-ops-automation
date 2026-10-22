import socket
from functools import wraps
from urllib.parse import urlparse

import httpx
import requests
import urllib3


class TestNetworkBlockedError(RuntimeError):
    __test__ = False


def _is_business_hostname(hostname: object) -> bool:
    if not hostname:
        return False
    if isinstance(hostname, bytes):
        hostname = hostname.decode("ascii", errors="ignore")
    normalized = str(hostname).rstrip(".").lower()
    return normalized == "example.test" or normalized.endswith(".example.test")


def should_block_business_url(url: object) -> bool:
    parsed_url = url if isinstance(url, (str, bytes)) else str(url)
    hostname = urlparse(parsed_url).hostname
    return _is_business_hostname(hostname)


def _raise_blocked(method: object, target: object) -> None:
    raise TestNetworkBlockedError(
        "TestNetworkBlockedError: pytest blocked a test-environment "
        f"business API request before send: {method} {target}. "
        "Mock the request; pytest never permits business API access."
    )


def install_test_network_guard() -> None:
    current_request = requests.sessions.Session.request
    if getattr(current_request, "_test_network_guard", False):
        return

    @wraps(current_request)
    def guarded_request(session, method, url, *args, **kwargs):
        if should_block_business_url(url):
            _raise_blocked(method, url)
        return current_request(session, method, url, *args, **kwargs)

    guarded_request._test_network_guard = True
    requests.sessions.Session.request = guarded_request

    current_session_send = requests.sessions.Session.send

    @wraps(current_session_send)
    def guarded_session_send(session, request, *args, **kwargs):
        if should_block_business_url(request.url):
            _raise_blocked(request.method, request.url)
        return current_session_send(session, request, *args, **kwargs)

    guarded_session_send._test_network_guard = True
    requests.sessions.Session.send = guarded_session_send

    current_adapter_send = requests.adapters.HTTPAdapter.send

    @wraps(current_adapter_send)
    def guarded_adapter_send(adapter, request, *args, **kwargs):
        if should_block_business_url(request.url):
            _raise_blocked(request.method, request.url)
        return current_adapter_send(adapter, request, *args, **kwargs)

    guarded_adapter_send._test_network_guard = True
    requests.adapters.HTTPAdapter.send = guarded_adapter_send

    current_httpx_request = httpx.Client.request

    @wraps(current_httpx_request)
    def guarded_httpx_request(client, method, url, *args, **kwargs):
        if should_block_business_url(url):
            _raise_blocked(method, url)
        return current_httpx_request(client, method, url, *args, **kwargs)

    guarded_httpx_request._test_network_guard = True
    httpx.Client.request = guarded_httpx_request

    current_httpx_send = httpx.Client.send

    @wraps(current_httpx_send)
    def guarded_httpx_send(client, request, *args, **kwargs):
        if should_block_business_url(request.url):
            _raise_blocked(request.method, request.url)
        return current_httpx_send(client, request, *args, **kwargs)

    guarded_httpx_send._test_network_guard = True
    httpx.Client.send = guarded_httpx_send

    current_async_httpx_request = httpx.AsyncClient.request

    @wraps(current_async_httpx_request)
    async def guarded_async_httpx_request(client, method, url, *args, **kwargs):
        if should_block_business_url(url):
            _raise_blocked(method, url)
        return await current_async_httpx_request(client, method, url, *args, **kwargs)

    guarded_async_httpx_request._test_network_guard = True
    httpx.AsyncClient.request = guarded_async_httpx_request

    current_async_httpx_send = httpx.AsyncClient.send

    @wraps(current_async_httpx_send)
    async def guarded_async_httpx_send(client, request, *args, **kwargs):
        if should_block_business_url(request.url):
            _raise_blocked(request.method, request.url)
        return await current_async_httpx_send(client, request, *args, **kwargs)

    guarded_async_httpx_send._test_network_guard = True
    httpx.AsyncClient.send = guarded_async_httpx_send

    current_pool_request = urllib3.PoolManager.request

    @wraps(current_pool_request)
    def guarded_pool_request(pool, method, url, *args, **kwargs):
        if should_block_business_url(url):
            _raise_blocked(method, url)
        return current_pool_request(pool, method, url, *args, **kwargs)

    guarded_pool_request._test_network_guard = True
    urllib3.PoolManager.request = guarded_pool_request

    current_pool_urlopen = urllib3.connectionpool.HTTPConnectionPool.urlopen

    @wraps(current_pool_urlopen)
    def guarded_pool_urlopen(pool, method, url, *args, **kwargs):
        if should_block_business_url(url) or _is_business_hostname(pool.host):
            _raise_blocked(method, url)
        return current_pool_urlopen(pool, method, url, *args, **kwargs)

    guarded_pool_urlopen._test_network_guard = True
    urllib3.connectionpool.HTTPConnectionPool.urlopen = guarded_pool_urlopen

    current_create_connection = socket.create_connection

    @wraps(current_create_connection)
    def guarded_create_connection(address, *args, **kwargs):
        host = address[0]
        if _is_business_hostname(host):
            _raise_blocked("CONNECT", host)
        return current_create_connection(address, *args, **kwargs)

    guarded_create_connection._test_network_guard = True
    socket.create_connection = guarded_create_connection

    current_getaddrinfo = socket.getaddrinfo

    @wraps(current_getaddrinfo)
    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if _is_business_hostname(host):
            _raise_blocked("DNS", host)
        return current_getaddrinfo(host, port, *args, **kwargs)

    guarded_getaddrinfo._test_network_guard = True
    socket.getaddrinfo = guarded_getaddrinfo

    current_socket_connect = socket.socket.connect

    @wraps(current_socket_connect)
    def guarded_socket_connect(sock, address):
        host = address[0] if isinstance(address, tuple) and address else None
        if _is_business_hostname(host):
            _raise_blocked("CONNECT", host)
        return current_socket_connect(sock, address)

    guarded_socket_connect._test_network_guard = True
    socket.socket.connect = guarded_socket_connect

    current_socket_connect_ex = socket.socket.connect_ex

    @wraps(current_socket_connect_ex)
    def guarded_socket_connect_ex(sock, address):
        host = address[0] if isinstance(address, tuple) and address else None
        if _is_business_hostname(host):
            _raise_blocked("CONNECT", host)
        return current_socket_connect_ex(sock, address)

    guarded_socket_connect_ex._test_network_guard = True
    socket.socket.connect_ex = guarded_socket_connect_ex
