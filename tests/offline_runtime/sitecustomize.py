import ipaddress
import os
import socket
from functools import wraps


class OfflineNetworkBlockedError(RuntimeError):
    pass


def _is_loopback_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    normalized = str(host or "").strip().rstrip(".").lower()
    if normalized in {"localhost", "0.0.0.0", "::", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _host_from_address(address: object) -> object:
    if not isinstance(address, tuple) or not address:
        return None
    return address[0]


def _raise_blocked(operation: str, host: object, port: object = None) -> None:
    target = str(host)
    if port not in {None, ""}:
        target = f"{target}:{port}"
    raise OfflineNetworkBlockedError(
        f"OfflineNetworkBlockedError: offline test blocked {operation} to {target}"
    )


def _install() -> None:
    if os.environ.get("AIOPS_OFFLINE_TEST") != "1":
        return
    if getattr(socket.getaddrinfo, "_aiops_offline_guard", False):
        return

    original_getaddrinfo = socket.getaddrinfo

    @wraps(original_getaddrinfo)
    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if not _is_loopback_host(host):
            _raise_blocked("DNS", host, port)
        return original_getaddrinfo(host, port, *args, **kwargs)

    guarded_getaddrinfo._aiops_offline_guard = True
    socket.getaddrinfo = guarded_getaddrinfo

    original_create_connection = socket.create_connection

    @wraps(original_create_connection)
    def guarded_create_connection(address, *args, **kwargs):
        host = _host_from_address(address)
        if host is not None and not _is_loopback_host(host):
            port = address[1] if len(address) > 1 else None
            _raise_blocked("CONNECT", host, port)
        return original_create_connection(address, *args, **kwargs)

    guarded_create_connection._aiops_offline_guard = True
    socket.create_connection = guarded_create_connection

    original_connect = socket.socket.connect

    @wraps(original_connect)
    def guarded_connect(sock, address):
        host = _host_from_address(address)
        if host is not None and not _is_loopback_host(host):
            port = address[1] if len(address) > 1 else None
            _raise_blocked("CONNECT", host, port)
        return original_connect(sock, address)

    guarded_connect._aiops_offline_guard = True
    socket.socket.connect = guarded_connect

    original_connect_ex = socket.socket.connect_ex

    @wraps(original_connect_ex)
    def guarded_connect_ex(sock, address):
        host = _host_from_address(address)
        if host is not None and not _is_loopback_host(host):
            port = address[1] if len(address) > 1 else None
            _raise_blocked("CONNECT", host, port)
        return original_connect_ex(sock, address)

    guarded_connect_ex._aiops_offline_guard = True
    socket.socket.connect_ex = guarded_connect_ex


_install()
