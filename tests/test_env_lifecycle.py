from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from rl_web_agent.env import WebAgentEnv


class EnvironmentLifecycleTest(IsolatedAsyncioTestCase):
    async def test_db_mode_never_installs_legacy_browser_proxy(self):
        env = WebAgentEnv.__new__(WebAgentEnv)
        env.isolation_mode = "db"
        env.config = SimpleNamespace(
            proxy=SimpleNamespace(enabled=True, server="http://localhost:8080")
        )

        self.assertIsNone(env._browser_proxy_server())

        env.isolation_mode = "direct"
        self.assertEqual(
            env._browser_proxy_server(), "http://localhost:8080"
        )

    async def test_db_route_rewrites_only_reviewed_site_authority(self):
        env = WebAgentEnv.__new__(WebAgentEnv)
        env.isolation_mode = "db"
        env.db_agent_session = SimpleNamespace(
            headers={"X-Agent-Route": "signed-token"}
        )
        env.config = SimpleNamespace(
            sites={"shopping": "metis.lti.cs.cmu.edu:7770"}
        )
        env.server_ips = {"shopping": "127.0.0.1:7772"}
        env.context = Mock()
        env.context.route = AsyncMock()
        env.logger = Mock()

        await env._install_db_route_header_injection(["shopping"])
        handler = env.context.route.await_args.args[1]

        route = Mock()
        route.continue_ = AsyncMock()
        request = SimpleNamespace(
            url="http://metis.lti.cs.cmu.edu:7770/product.html?q=1",
            headers={"accept": "text/html"},
        )
        await handler(route, request)
        call = route.continue_.await_args.kwargs
        self.assertEqual(
            call["url"], "http://127.0.0.1:7772/product.html?q=1"
        )
        self.assertEqual(call["headers"]["X-Agent-Route"], "signed-token")

        local_route = Mock()
        local_route.continue_ = AsyncMock()
        local_request = SimpleNamespace(
            url="http://shopping.local/product.html?q=1",
            headers={"accept": "text/html"},
        )
        await handler(local_route, local_request)
        self.assertEqual(
            local_route.continue_.await_args.kwargs["url"],
            "http://127.0.0.1:7772/product.html?q=1",
        )

        outside_route = Mock()
        outside_route.continue_ = AsyncMock()
        outside = SimpleNamespace(
            url="https://example.com/", headers={"accept": "text/html"}
        )
        await handler(outside_route, outside)
        outside_route.continue_.assert_awaited_once_with()

    async def test_browser_stops_before_db_route_cleanup(self):
        events = []
        env = WebAgentEnv.__new__(WebAgentEnv)
        env.logger = Mock()
        env.trace_file_path = None
        env.context = Mock()
        env.context.close = AsyncMock(side_effect=lambda: events.append("context"))
        env.page = Mock()
        env.browser = Mock()
        env.browser.is_connected.return_value = True
        env.browser.close = AsyncMock(side_effect=lambda: events.append("browser"))
        env.isolation_mode = "db"
        env.db_agent_session = SimpleNamespace(agent_id="agent")
        env.db_isolation_manager = Mock()
        env.db_isolation_manager.cleanup.side_effect = lambda session: events.append("database")
        env._playwright_acquired = False
        env.context_manager = None

        await env.close()

        self.assertEqual(events, ["context", "browser", "database"])
        self.assertIsNone(env.context)
        self.assertIsNone(env.browser)
        self.assertIsNone(env.page)

    async def test_close_is_idempotent_for_playwright_reference(self):
        env = WebAgentEnv.__new__(WebAgentEnv)
        env.logger = Mock()
        env.trace_file_path = None
        env.context = None
        env.page = None
        env.browser = None
        env.isolation_mode = "db"
        env.db_agent_session = None
        env.db_isolation_manager = None
        env._playwright_acquired = True
        env.context_manager = object()
        env._cleanup_playwright = AsyncMock()

        await env.close()
        await env.close()

        env._cleanup_playwright.assert_awaited_once()
