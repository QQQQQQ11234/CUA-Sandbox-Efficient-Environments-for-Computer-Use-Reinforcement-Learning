from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from .route_registry import RouteRecord, SQLiteRouteRegistry
from .route_token import RouteTokenSigner

_CURRENT_ROUTE: ContextVar[RouteRecord | None] = ContextVar(
    "cua_sandbox_current_route", default=None
)


def _normalize_headers(headers: Iterable[tuple[Any, Any]]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in headers:
        key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        value = raw_value.decode() if isinstance(raw_value, bytes) else str(raw_value)
        normalized[key.lower()] = value
    return normalized


class TrustedDBRouter:
    def __init__(
        self,
        registry: SQLiteRouteRegistry,
        token_signer: RouteTokenSigner,
        route_token_header: str = "X-Agent-Route",
    ) -> None:
        self.registry = registry
        self.token_signer = token_signer
        self.route_token_header = route_token_header.lower()

    def resolve(self, headers: dict[str, str]) -> tuple[str, RouteRecord]:
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        try:
            route_token = normalized[self.route_token_header]
        except KeyError as exc:
            raise KeyError(f"missing signed route header: {self.route_token_header}") from exc
        agent_id = self.token_signer.verify(route_token).agent_id
        return agent_id, self.registry.resolve_route(agent_id)

    @contextmanager
    def request_scope(self, headers: dict[str, str]) -> Iterator[tuple[str, RouteRecord]]:
        agent_id, _ = self.resolve(headers)
        route = self.registry.acquire(agent_id)
        token = _CURRENT_ROUTE.set(route)
        try:
            yield agent_id, route
        finally:
            _CURRENT_ROUTE.reset(token)
            self.registry.release(agent_id)

    @staticmethod
    def current_db() -> str | None:
        route = _CURRENT_ROUTE.get()
        return route.db_name if route else None

    @staticmethod
    def current_route() -> RouteRecord | None:
        return _CURRENT_ROUTE.get()


class ASGITrustedDBRouterMiddleware:
    def __init__(self, app: Callable[..., Awaitable[Any]], router: TrustedDBRouter) -> None:
        self.app = app
        self.router = router

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> Any:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = _normalize_headers(scope.get("headers", []))
        with self.router.request_scope(headers) as (agent_id, route):
            blocked_headers = {b"x-agent-id", b"x-agent-db", b"x-agent-route"}
            scope["headers"] = [
                (key, value)
                for key, value in scope.get("headers", [])
                if key.lower() not in blocked_headers
            ]
            scope["web_agent_agent_id"] = agent_id
            scope["web_agent_db"] = route.db_name
            scope["web_agent_db_engine"] = route.db_engine
            scope["web_agent_db_host"] = route.db_host
            scope["web_agent_db_port"] = route.db_port
            scope["web_agent_environment"] = route.to_dict()
            return await self.app(scope, receive, send)


class WSGITrustedDBRouterMiddleware:
    def __init__(self, app: Callable[..., Any], router: TrustedDBRouter) -> None:
        self.app = app
        self.router = router

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Any:
        headers = {
            key[5:].replace("_", "-").lower(): str(value)
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }
        with self.router.request_scope(headers) as (agent_id, route):
            environ.pop("HTTP_X_AGENT_ID", None)
            environ.pop("HTTP_X_AGENT_DB", None)
            environ.pop("HTTP_X_AGENT_ROUTE", None)
            environ["web_agent.agent_id"] = agent_id
            environ["web_agent.db_name"] = route.db_name
            environ["web_agent.db_engine"] = route.db_engine
            environ["web_agent.db_host"] = route.db_host
            environ["web_agent.db_port"] = route.db_port
            environ["web_agent.environment"] = route.to_dict()
            return self.app(environ, start_response)


class RoutedPsycopgConnector:
    def __init__(self, base_dsn: str) -> None:
        self.base_dsn = base_dsn

    def connect(self, **kwargs: Any) -> Any:
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict, make_conninfo
        except ImportError as exc:
            raise RuntimeError("RoutedPsycopgConnector requires psycopg") from exc
        db_name = TrustedDBRouter.current_db()
        if not db_name:
            raise RuntimeError("no trusted database route is active")
        route = TrustedDBRouter.current_route()
        if route is not None and route.db_engine not in {"postgres", "postgresql"}:
            raise RuntimeError(
                f"active route uses {route.db_engine}, not PostgreSQL"
            )
        parts = conninfo_to_dict(self.base_dsn)
        parts["dbname"] = db_name
        return psycopg.connect(make_conninfo(**parts), **kwargs)


class RoutedMySQLConnector:
    """Create a MySQL connection from the trusted active route.

    The application adapter supplies credentials and chooses a driver.  The
    route owns host, port, and database; callers cannot override those values
    with request-provided input.  Both ``mysql-connector-python`` and PyMySQL
    are supported when installed, keeping this package free of a mandatory
    MySQL client dependency.
    """

    def __init__(
        self,
        *,
        user: str,
        password: str = "",
        driver: str = "auto",
        connect_kwargs: dict[str, Any] | None = None,
    ) -> None:
        if not user:
            raise ValueError("RoutedMySQLConnector requires a database user")
        if driver not in {"auto", "mysql.connector", "pymysql"}:
            raise ValueError(f"unsupported MySQL driver: {driver}")
        self.user = user
        self.password = password
        self.driver = driver
        self.connect_kwargs = dict(connect_kwargs or {})

    def _route_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        route = TrustedDBRouter.current_route()
        if route is None:
            raise RuntimeError("no trusted database route is active")
        if route.db_engine != "mysql":
            raise RuntimeError(f"active route uses {route.db_engine}, not MySQL")
        parameters = dict(self.connect_kwargs)
        parameters.update(kwargs)
        # These are deliberately assigned after caller kwargs.  The signed
        # route, not an HTTP/database request parameter, selects the tenant.
        parameters.update(
            {
                "host": route.db_host,
                "port": route.db_port,
                "user": self.user,
                "password": self.password,
                "database": route.db_name,
            }
        )
        return parameters

    def connect(self, **kwargs: Any) -> Any:
        parameters = self._route_kwargs(kwargs)
        drivers = (
            [self.driver]
            if self.driver != "auto"
            else ["mysql.connector", "pymysql"]
        )
        missing: list[str] = []
        for driver in drivers:
            try:
                if driver == "mysql.connector":
                    import mysql.connector  # type: ignore[import-not-found]

                    return mysql.connector.connect(**parameters)
                import pymysql  # type: ignore[import-not-found]

                return pymysql.connect(**parameters)
            except ImportError:
                missing.append(driver)
        raise RuntimeError(
            "RoutedMySQLConnector requires mysql-connector-python or PyMySQL; "
            f"tried {', '.join(missing)}"
        )
