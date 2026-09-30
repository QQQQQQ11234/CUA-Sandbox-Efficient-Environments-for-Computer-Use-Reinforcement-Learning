from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from rl_web_agent.evaluator import HTMLContentEvaluator


class FakePage:
    def __init__(self, *, url="http://gitlab.test/current", evaluate_result=None, evaluate_error=None):
        self.url = url
        self.evaluate_result = evaluate_result
        self.evaluate_error = evaluate_error
        self.goto_calls = []

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    async def evaluate(self, expression):
        if self.evaluate_error is not None:
            raise self.evaluate_error
        return self.evaluate_result

    async def content(self):
        return "<html></html>"


def task_with_target(target):
    return {"eval": {"program_html": [target]}}


class HTMLContentEvaluatorTest(IsolatedAsyncioTestCase):
    async def test_missing_dom_element_is_empty(self):
        for evaluate_error in (None, RuntimeError("missing node")):
            page = FakePage(evaluate_result=None, evaluate_error=evaluate_error)
            task = task_with_target(
                {
                    "url": "last",
                    "locator": "document.querySelector('.missing').outerText",
                    "required_contents": {"exact_match": "expected"},
                }
            )
            self.assertEqual(await HTMLContentEvaluator().evaluate("", page, task), 0.0)

    async def test_non_last_target_navigates_current_page(self):
        page = FakePage(evaluate_result="ready")
        task = task_with_target(
            {
                "url": "http://gitlab.test/project",
                "locator": "document.querySelector('.status').outerText",
                "required_contents": {"exact_match": "ready"},
            }
        )
        with patch("rl_web_agent.evaluator.asyncio.sleep", return_value=None):
            score = await HTMLContentEvaluator().evaluate("", page, task)
        self.assertEqual(score, 1.0)
        self.assertEqual(page.goto_calls, [("http://gitlab.test/project", {})])

    async def test_accepts_webarena_member_role_typo(self):
        page = FakePage()

        async def member_role(evaluation_page, account_name):
            self.assertIs(evaluation_page, page)
            self.assertEqual(account_name, "byteblaze")
            return "Owner"

        helper = SimpleNamespace(
            gitlab_get_project_memeber_role=member_role,
            shopping_get_sku_latest_review_author=lambda *_: "",
            shopping_get_sku_latest_review_rating=lambda *_: "",
            reddit_get_post_url=lambda *_: "",
        )
        task = task_with_target(
            {
                "url": "last",
                "locator": "func:gitlab_get_project_memeber_role(__page__, 'byteblaze')",
                "required_contents": {"exact_match": "Owner"},
            }
        )
        with patch("rl_web_agent.helper_functions.get_helper_functions", return_value=helper):
            score = await HTMLContentEvaluator().evaluate("", page, task)
        self.assertEqual(score, 1.0)
