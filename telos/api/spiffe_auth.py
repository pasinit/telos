"""JWT-SVID validation against a SPIRE trust domain's JWT bundle.

The bundle (a JWKS document per trust domain) comes from the local SPIRE agent's
SPIFFE Workload API, ``FetchJWTBundles``. py-spiffe would be the natural client,
but every release requires protobuf>=6 while pontifex's google-generativeai pins
protobuf<6, so this module speaks the one gRPC method it needs directly via
grpcio's generic stubs and decodes the single-map response by hand. Signature,
audience and expiry checks are PyJWT's.

The caller's SPIRE agent only answers ``FetchJWTBundles`` for a registered
workload (or with ``allow_unauthenticated_verifiers = true`` in its config).
"""
import contextlib
import json
import logging
import threading
import time
from collections.abc import Iterator
from typing import Protocol

import grpc
import jwt

# Asymmetric algorithms the JWT-SVID specification allows.
SVID_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"})
_FETCH_JWT_BUNDLES = "/SpiffeWorkloadAPI/FetchJWTBundles"

log = logging.getLogger("telos.spiffe_auth")


class BundleUnavailableError(RuntimeError):
    """The trust domain's JWT bundle could not be obtained; callers answer 503."""


class NotReadyError(BundleUnavailableError):
    """No bundle has been fetched yet (cold start, SPIRE agent not up); callers answer 503 fast."""


class InvalidSvidError(ValueError):
    """The bearer token is not a valid JWT-SVID for this audience and trust domain."""


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _length_delimited(buf: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield ``(field_number, payload)`` for each length-delimited protobuf field; skip the rest."""
    pos = 0
    while pos < len(buf):
        tag, pos = _varint(buf, pos)
        number, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            _, pos = _varint(buf, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        elif wire_type == 2:
            size, pos = _varint(buf, pos)
            if pos + size > len(buf):
                raise ValueError("truncated field")
            yield number, buf[pos:pos + size]
            pos += size
        else:
            raise ValueError(f"unsupported wire type {wire_type}")


def decode_jwt_bundles(data: bytes) -> dict[str, bytes]:
    """Decode a ``JWTBundlesResponse`` (``map<string, bytes> bundles = 1``) into trust domain ID -> JWKS bytes."""
    bundles = {}
    for number, entry in _length_delimited(data):
        if number != 1:
            continue
        fields = dict(_length_delimited(entry))
        bundles[fields.get(1, b"").decode()] = fields.get(2, b"")
    return bundles


class KeySource(Protocol):
    def signing_key(self, kid: str) -> jwt.PyJWK | None: ...


class WorkloadApiKeys:
    """Signing keys of one trust domain, read from the SPIRE agent and cached.

    The bundle is re-read after ``ttl`` seconds, or sooner when a token names an
    unknown key (SPIRE rotated its signing key), at most every ``min_refresh``
    seconds. A cached bundle keeps serving if a refresh fails, until it is twice
    ``ttl`` old. After a failed fetch the next one waits ``retry_after`` seconds;
    meanwhile, and while another thread is fetching, callers get the cache or an
    immediate error instead of queueing behind ``timeout``-long fetches.
    """

    def __init__(self, socket: str, trust_domain: str, *, ttl: float = 300.0, min_refresh: float = 30.0, timeout: float = 5.0,
                 retry_after: float = 2.0) -> None:
        self._socket = socket
        self._trust_domain = trust_domain
        self._ttl = ttl
        self._min_refresh = min_refresh
        self._timeout = timeout
        self._retry_after = retry_after
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = self._attempted_at = float("-inf")
        self._last_error = ""
        self._lock = threading.Lock()

    def _fetch(self) -> dict[str, bytes]:
        with grpc.insecure_channel(self._socket) as channel:
            fetch = channel.unary_stream(_FETCH_JWT_BUNDLES, request_serializer=lambda _: b"", response_deserializer=decode_jwt_bundles)
            stream = fetch(None, metadata=(("workload.spiffe.io", "true"),), timeout=self._timeout)
            try:
                return next(iter(stream))
            finally:
                stream.cancel()

    def _refresh(self) -> None:
        self._attempted_at = time.monotonic()
        try:
            self._load()
            self._last_error = ""
        except BundleUnavailableError as exc:
            self._last_error = str(exc)
            raise

    def _load(self) -> None:
        try:
            bundles = self._fetch()
        except (grpc.RpcError, StopIteration, ValueError) as exc:
            raise BundleUnavailableError(f"SPIRE Workload API FetchJWTBundles failed: {exc}") from exc
        # SPIRE keys the map by trust domain ID (spiffe://<td>); accept the bare name too.
        bundle = bundles.get(f"spiffe://{self._trust_domain}", bundles.get(self._trust_domain))
        if bundle is None:
            raise BundleUnavailableError(f"SPIRE agent has no JWT bundle for trust domain {self._trust_domain} (has: {sorted(bundles)})")
        try:
            keys = jwt.PyJWKSet.from_dict(json.loads(bundle)).keys
        except (ValueError, jwt.PyJWTError) as exc:
            raise BundleUnavailableError(f"unreadable JWT bundle for {self._trust_domain}: {exc}") from exc
        self._keys = {key.key_id: key for key in keys if key.key_id}
        self._fetched_at = self._attempted_at

    @property
    def ready(self) -> bool:
        """A bundle has been fetched at least once."""
        return self._fetched_at > float("-inf")

    def warm(self) -> bool:
        """Fetch the bundle now (blocking; run it off the event loop at startup). False on failure."""
        with self._lock:
            try:
                self._refresh()
            except BundleUnavailableError as exc:
                log.warning("SPIRE JWT bundle not available yet: %s", exc)
                return False
        return True

    def _cached(self, kid: str) -> jwt.PyJWK | None:
        if not self.ready:
            raise NotReadyError(f"no JWT bundle fetched yet: {self._last_error or 'fetch in progress'}")
        if time.monotonic() - self._fetched_at > 2 * self._ttl:
            raise BundleUnavailableError(f"JWT bundle expired: {self._last_error or 'fetch in progress'}")
        return self._keys.get(kid)

    def signing_key(self, kid: str) -> jwt.PyJWK | None:
        # Another thread mid-fetch: answer from the cache rather than queue behind it.
        if not self._lock.acquire(timeout=self._timeout):
            return self._cached(kid)
        try:
            now = time.monotonic()
            stale = now - self._fetched_at > self._ttl
            unknown = kid not in self._keys
            since = now - self._attempted_at
            due = since > self._retry_after if self._last_error else stale or since > self._min_refresh
            if (stale or unknown) and due:
                with contextlib.suppress(BundleUnavailableError):
                    self._refresh()
            return self._cached(kid)
        finally:
            self._lock.release()


class SvidVerifier:
    """Validates JWT-SVIDs for one audience against one trust domain's keys."""

    def __init__(self, keys: KeySource, *, audience: str, trust_domain: str) -> None:
        self._keys = keys
        self._audience = audience
        self._prefix = f"spiffe://{trust_domain}/"

    def verify(self, token: str) -> str:
        """Return the token's SPIFFE ID (its ``sub``).

        Raises:
            InvalidSvidError: malformed token, disallowed algorithm, unknown key,
                bad signature, wrong audience, missing or past expiry, or a
                subject outside the trust domain.
            BundleUnavailableError: the trust domain's keys cannot be obtained.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidSvidError(f"malformed token: {exc}") from exc
        algorithm, kid = header.get("alg"), header.get("kid")
        if algorithm not in SVID_ALGORITHMS:
            raise InvalidSvidError(f"algorithm {algorithm!r} not allowed")
        if not kid:
            raise InvalidSvidError("token has no key id")
        key = self._keys.signing_key(kid)
        if key is None:
            raise InvalidSvidError(f"unknown signing key {kid!r}")
        try:
            claims = jwt.decode(token, key.key, algorithms=[algorithm], audience=self._audience,
                                options={"require": ["exp", "sub", "aud"]})
        except jwt.PyJWTError as exc:
            raise InvalidSvidError(f"{type(exc).__name__}: {exc}") from exc
        subject = str(claims["sub"])
        if not subject.startswith(self._prefix):
            raise InvalidSvidError(f"subject {subject!r} is outside the trust domain")
        return subject
