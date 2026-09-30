from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from rl_web_agent.isolation.db_routing import ASGITrustedDBRouterMiddleware, TrustedDBRouter
from rl_web_agent.isolation.route_registry import SQLiteRouteRegistry
from rl_web_agent.isolation.route_token import InvalidRouteToken, RouteTokenSigner


async def request(app, route_token: str, spoofed_agent: str | None = None, spoofed_db: str | None = None) -> str:
    messages: list[dict] = []
    headers = [(b"x-agent-route", route_token.encode())]
    if spoofed_agent:
        headers.append((b"x-agent-id", spoofed_agent.encode()))
    if spoofed_db:
        headers.append((b"x-agent-db", spoofed_db.encode()))
    scope = {"type": "http", "headers": headers}

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return messages[-1]["body"].decode()


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        registry_path = str(Path(directory) / "routes.sqlite3")
        orchestrator_registry = SQLiteRouteRegistry(registry_path)
        app_registry = SQLiteRouteRegistry(registry_path)

        orchestrator_registry.set_route("agent_a", "agent_a_db")
        orchestrator_registry.set_route("agent_b", "agent_b_db")

        signer = RouteTokenSigner("test-secret-that-is-longer-than-32-bytes", ttl_seconds=60)
        router = TrustedDBRouter(app_registry, signer)
        token_a = signer.issue("agent_a")
        token_b = signer.issue("agent_b")

        async def shared_app(scope, receive, send) -> None:
            forwarded_headers = {key.lower() for key, _ in scope["headers"]}
            assert b"x-agent-id" not in forwarded_headers
            assert b"x-agent-db" not in forwarded_headers
            assert b"x-agent-route" not in forwarded_headers
            body = scope["web_agent_db"].encode()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": body})

        app = ASGITrustedDBRouterMiddleware(shared_app, router)

        assert await request(app, token_a, spoofed_agent="agent_b", spoofed_db="agent_b_db") == "agent_a_db"
        assert await request(app, token_b) == "agent_b_db"

        orchestrator_registry.set_route("agent_a", "agent_a_branch_left")
        assert await request(app, token_a) == "agent_a_branch_left"
        assert await request(app, token_b) == "agent_b_db"

        tampered_token = token_a[:-1] + ("A" if token_a[-1] != "A" else "B")
        try:
            await request(app, tampered_token)
        except InvalidRouteToken:
            pass
        else:
            raise AssertionError("tampered route token was accepted")

        expired_signer = RouteTokenSigner(
            "test-secret-that-is-longer-than-32-bytes",
            ttl_seconds=1,
            clock=lambda: 100,
        )
        expired_token = expired_signer.issue("agent_a")
        verifier = RouteTokenSigner(
            "test-secret-that-is-longer-than-32-bytes",
            ttl_seconds=1,
            clock=lambda: 102,
        )
        try:
            verifier.verify(expired_token)
        except InvalidRouteToken:
            pass
        else:
            raise AssertionError("expired route token was accepted")

        orchestrator_registry.remove("agent_a")
        try:
            await request(app, token_a)
        except KeyError:
            pass
        else:
            raise AssertionError("removed agent route still resolved")

    print("DB routing MVP verification passed")


if __name__ == "__main__":
    asyncio.run(main())
