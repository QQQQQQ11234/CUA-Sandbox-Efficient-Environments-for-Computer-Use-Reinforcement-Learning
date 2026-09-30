from __future__ import annotations

import argparse
import hmac
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from rl_web_agent.isolation.route_registry import (
    RouteBusyError,
    RouteFrozenError,
    SQLiteRouteRegistry,
)


def make_handler(
    registry: SQLiteRouteRegistry, bearer_secret: str
) -> type[BaseHTTPRequestHandler]:
    expected_authorization = f"Bearer {bearer_secret}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "WebAgentRouteRegistry/2.0"

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._json(HTTPStatus.OK, {"status": "ok", "version": 2})
                return
            if not self._authorized():
                return
            if path.rstrip("/") == "/v1/routes":
                routes = [
                    route.to_dict()
                    for route in registry.list_route_records().values()
                ]
                self._json(HTTPStatus.OK, {"routes": routes})
                return
            parsed = self._route_action(path)
            if parsed is None or parsed[1] is not None:
                self._json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
                return
            agent_id, _ = parsed
            try:
                route = registry.resolve_route(agent_id)
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"detail": "unknown agent route"})
                return
            self._json(HTTPStatus.OK, route.to_dict())

        def do_POST(self) -> None:
            if not self._authorized():
                return
            parsed = self._route_action(urlparse(self.path).path)
            if parsed is None or parsed[1] not in {
                "acquire",
                "release",
                "enqueue-job",
                "complete-job",
            }:
                self._json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
                return
            agent_id, action = parsed
            try:
                if action == "acquire":
                    route = registry.acquire(agent_id)
                elif action == "release":
                    route = registry.release(agent_id)
                elif action == "enqueue-job":
                    route = registry.enqueue_background_job(agent_id)
                else:
                    route = registry.complete_background_job(agent_id)
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"detail": "unknown agent route"})
                return
            except RouteFrozenError as exc:
                self._json(HTTPStatus.LOCKED, {"detail": str(exc)})
                return
            except RouteBusyError as exc:
                self._json(HTTPStatus.CONFLICT, {"detail": str(exc)})
                return
            self._json(HTTPStatus.OK, route.to_dict())

        def _authorized(self) -> bool:
            authorization = self.headers.get("Authorization", "")
            if hmac.compare_digest(authorization, expected_authorization):
                return True
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"detail": "invalid registry credentials"},
            )
            return False

        @staticmethod
        def _route_action(path: str) -> tuple[str, str | None] | None:
            prefix = "/v1/routes/"
            if not path.startswith(prefix):
                return None
            remainder = path.removeprefix(prefix).rstrip("/")
            if not remainder:
                return None
            parts = remainder.rsplit("/", 1)
            if len(parts) == 2 and parts[1] in {
                "acquire",
                "release",
                "enqueue-job",
                "complete-job",
            }:
                return unquote(parts[0]), parts[1]
            return unquote(remainder), None

        def _json(self, status: HTTPStatus, payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if status == HTTPStatus.LOCKED:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry-path", default="./runtime/db_routes.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config-path")
    args = parser.parse_args()
    if args.config_path:
        config = json.loads(Path(args.config_path).read_text())
        secret = str(config.get("route_registry_secret", ""))
    else:
        secret = os.environ.get("WEB_AGENT_ROUTE_REGISTRY_SECRET", "")
    if len(secret.encode()) < 32:
        raise ValueError("WEB_AGENT_ROUTE_REGISTRY_SECRET must contain at least 32 bytes")
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(SQLiteRouteRegistry(args.registry_path), secret),
    )
    print(f"route registry listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
