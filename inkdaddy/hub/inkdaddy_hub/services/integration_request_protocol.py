"""The small, shared wire contract for sending and reviewing integration requests."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

REQUEST_PATH = "/v1/integration-requests"
MAX_NAME_LENGTH = 80
MAX_REQUEST_BYTES = 768
MAX_ACK_BYTES = 256
MAX_REVIEW_BYTES = 256 * 1024
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{32,256}")
INTEGRATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_PUBLIC_HOST = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_PATH = re.compile(r"(?:/[A-Za-z0-9_.~-]+)+")
_GLOBAL_IPV6 = ipaddress.ip_network("2000::/3")


@dataclass(frozen=True)
class RequestData:
    integration_id: str
    integration_name: str

    def payload(self) -> dict[str, str]:
        return {"integration_id": self.integration_id, "integration_name": self.integration_name}


def request_data(name: object) -> RequestData:
    if not isinstance(name, str) or len(name) > MAX_NAME_LENGTH * 2:
        raise ValueError("Enter an integration name of up to 80 characters.")
    normalized = unicodedata.normalize("NFKC", name).strip(" ")
    normalized = " ".join(part for part in normalized.split(" ") if part)
    if (
        not 2 <= len(normalized) <= MAX_NAME_LENGTH
        or not normalized[0].isalnum()
        or any(
            unicodedata.category(character)[0] not in "LNM" and character not in " ._+-&()/"
            for character in normalized
        )
    ):
        raise ValueError("Enter the integration name using letters, numbers, spaces, or common name punctuation.")
    identifier = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()[:32]
    return RequestData(identifier, normalized)


def parse_request(payload: object) -> RequestData:
    if not isinstance(payload, dict) or set(payload) != {"integration_id", "integration_name"}:
        raise ValueError("Invalid integration request.")
    request = request_data(payload["integration_name"])
    if payload != request.payload():
        raise ValueError("Invalid integration request.")
    return request


@dataclass(frozen=True)
class Relay:
    host: str
    path: str
    token: str = field(repr=False)


def configured_relay(url: str, token: str) -> Relay:
    if (
        len(url) > 1024 or not url.isascii()
        or any(ord(character) <= 32 or ord(character) >= 127 for character in url)
        or any(character in url for character in "\\?#")
        or TOKEN_PATTERN.fullmatch(token) is None
    ):
        raise ValueError("Invalid integration relay configuration.")
    endpoint = urlsplit(url)
    host = endpoint.hostname or ""
    if (
        endpoint.scheme != "https"
        or len(host) > 253
        or _PUBLIC_HOST.fullmatch(host) is None
        or host.endswith((".local", ".localhost", ".internal", ".home.arpa"))
        or endpoint.netloc.lower() not in (host, f"{host}:443")
        or _PATH.fullmatch(endpoint.path) is None
        or any(part in (".", "..") for part in endpoint.path.split("/"))
    ):
        raise ValueError("Invalid integration relay configuration.")
    return Relay(host, endpoint.path, token)


def _relay_addresses(host: str) -> list[str]:
    addresses: list[str] = []
    for _family, _kind, _protocol, _name, address in socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
    ):
        ip = ipaddress.ip_address(address[0])
        if (
            not ip.is_global or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            or "%" in address[0]
            or isinstance(ip, ipaddress.IPv6Address) and (
                ip not in _GLOBAL_IPV6 or ip.sixtofour is not None or ip.teredo is not None
            )
        ):
            raise ValueError("Integration relay addresses must be public.")
        if address[0] not in addresses:
            addresses.append(address[0])
    if not addresses:
        raise OSError("The integration relay did not resolve.")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, 443, timeout=6, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        connection = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


def relay_request(method: str, relay: Relay, body: bytes | None = None) -> tuple[int, bytes]:
    # Resolve once. Every address must be public and the socket uses the selected IP directly.
    address = _relay_addresses(relay.host)[0]
    connection = _PinnedHTTPSConnection(relay.host, address)
    try:
        headers = {"Authorization": f"Bearer {relay.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, relay.path, body=body, headers=headers)
        response = connection.getresponse()
        # Deliberately never follow redirects or read error bodies.
        if method == "POST" and response.status in (200, 201):
            limit = MAX_ACK_BYTES
        elif method == "GET" and response.status == 200:
            limit = MAX_REVIEW_BYTES
        else:
            return response.status, b""
        contents = response.read(limit + 1)
        if len(contents) > limit:
            raise ValueError("Integration relay response exceeds its size limit.")
        return response.status, contents
    finally:
        connection.close()


def request_body(request: RequestData) -> bytes:
    # UTF-8 keeps the maximum sized valid Unicode name within MAX_REQUEST_BYTES.
    return json.dumps(request.payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def accepted_payload(request: RequestData) -> dict[str, str]:
    return {"status": "accepted", "integration_id": request.integration_id}
