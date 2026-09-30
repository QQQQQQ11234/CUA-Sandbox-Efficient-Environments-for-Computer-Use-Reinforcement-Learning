from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from experiments.shopping.run_official_storefront_db_eval import (
    RoutedEvaluatorRequests,
    SHOPPING_AUTHORITY,
    assert_authenticated_storefront_page,
    make_task_configs,
)


def fake_page(*, url: str, content: str, login_count: int, logout_count: int):
    page = Mock()
    page.url = url
    page.content.return_value = content

    def locator(selector: str):
        result = Mock()
        result.count.return_value = {
            "#login-form": login_count,
            'a[href*="customer/account/logout"]': logout_count,
        }[selector]
        return result

    page.locator.side_effect = locator
    return page


class StorefrontOfficialDBEvalTest(TestCase):
    def test_evaluator_requests_include_active_task_route(self):
        response = Mock(status_code=200)
        response.raise_for_status = Mock()
        delegate = Mock()
        delegate.post.return_value = response
        environment_type = type("Environment", (), {})
        environment_type.active_instance = Mock(
            db_session=Mock(
                headers={"X-Agent-Route": "signed-task-route"},
                shared_site_hosts={"shopping": SHOPPING_AUTHORITY},
            )
        )
        requests = RoutedEvaluatorRequests(
            delegate,
            environment_type,
            retry_interval_seconds=0,
        )

        self.assertIs(
            requests.post(
                f"http://{SHOPPING_AUTHORITY}/rest/default/V1/token",
                headers={"content-type": "application/json"},
            ),
            response,
        )

        delegate.post.assert_called_once_with(
            f"http://{SHOPPING_AUTHORITY}/rest/default/V1/token",
            headers={
                "content-type": "application/json",
                "X-Agent-Route": "signed-task-route",
            },
            timeout=60,
        )
        response.raise_for_status.assert_called_once_with()

    def test_evaluator_requests_require_an_active_task(self):
        environment_type = type("Environment", (), {"active_instance": None})
        requests = RoutedEvaluatorRequests(Mock(), environment_type)

        with self.assertRaisesRegex(RuntimeError, "no active DB-isolated task"):
            requests.get(f"http://{SHOPPING_AUTHORITY}/rest/V1/orders")

    def test_authenticated_product_page_requires_logout_link(self):
        page = fake_page(
            url=f"http://{SHOPPING_AUTHORITY}/product.html",
            content="<a href='/customer/account/logout/'>Sign Out</a>",
            login_count=0,
            logout_count=1,
        )
        assert_authenticated_storefront_page(page, "test")

    def test_login_page_is_rejected(self):
        page = fake_page(
            url=f"http://{SHOPPING_AUTHORITY}/customer/account/login/",
            content="Customer Login",
            login_count=1,
            logout_count=0,
        )
        with self.assertRaisesRegex(RuntimeError, "still on the login page"):
            assert_authenticated_storefront_page(page, "test")

    def test_wrong_authority_is_rejected(self):
        page = fake_page(
            url="http://127.0.0.1:7772/product.html",
            content="Sign Out",
            login_count=0,
            logout_count=1,
        )
        with self.assertRaisesRegex(RuntimeError, "changed authority"):
            assert_authenticated_storefront_page(page, "test")

    def test_task_configs_use_official_authority_and_routed_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks"
            tasks.mkdir()
            (tasks / "21.json").write_text(
                json.dumps(
                    {
                        "task_id": 21,
                        "sites": ["shopping"],
                        "storage_state": "./.auth/shopping_state.json",
                        "start_url": "http://metis.lti.cs.cmu.edu:7770/product.html",
                        "eval": {
                            "reference_url": "http://localhost:7770/customer/account/"
                        },
                    }
                )
            )
            ids = root / "ids.txt"
            ids.write_text("21\n")
            [config_path] = make_task_configs(ids, tasks)
            config = json.loads(Path(config_path).read_text())

        self.assertEqual(
            config["start_url"], f"http://{SHOPPING_AUTHORITY}/product.html"
        )
        self.assertEqual(
            config["eval"]["reference_url"],
            f"http://{SHOPPING_AUTHORITY}/customer/account/",
        )
        self.assertIsNone(config["storage_state"])


if __name__ == "__main__":
    import unittest

    unittest.main()
