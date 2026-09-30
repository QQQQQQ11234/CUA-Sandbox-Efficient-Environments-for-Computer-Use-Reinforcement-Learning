from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Callable


class InvalidRouteToken(ValueError):
    pass


@dataclass(frozen=True)
class RouteTokenClaims:
    agent_id: str
    expires_at: int


class RouteTokenSigner:
    def __init__(
        self,
        secret: str | bytes,
        ttl_seconds: int = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        secret_bytes = secret.encode() if isinstance(secret, str) else secret
        if len(secret_bytes) < 32:
            raise ValueError("route token secret must contain at least 32 bytes")
        if ttl_seconds <= 0:
            raise ValueError("route token TTL must be positive")
        self.secret = secret_bytes
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def issue(self, agent_id: str) -> str:
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        payload = {
            "agent_id": agent_id,
            "exp": int(self.clock()) + self.ttl_seconds,
            "v": 1,
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self.secret, payload_bytes, hashlib.sha256).digest()
        return f"{self._encode(payload_bytes)}.{self._encode(signature)}"

    def verify(self, token: str) -> RouteTokenClaims:
        try:
            payload_part, signature_part = token.split(".", 1)
            payload_bytes = self._decode(payload_part)
            supplied_signature = self._decode(signature_part)
        except (TypeError, ValueError) as exc:
            raise InvalidRouteToken("malformed route token") from exc

        expected_signature = hmac.new(self.secret, payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidRouteToken("invalid route token signature")

        try:
            payload = json.loads(payload_bytes)
            agent_id = str(payload["agent_id"])
            expires_at = int(payload["exp"])
            version = int(payload["v"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidRouteToken("invalid route token payload") from exc

        if version != 1 or not agent_id:
            raise InvalidRouteToken("unsupported route token claims")
        if expires_at < int(self.clock()):
            raise InvalidRouteToken("route token has expired")
        return RouteTokenClaims(agent_id=agent_id, expires_at=expires_at)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    @staticmethod
    def _decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)
