"""Helper functions for evaluation - adapted from WebArena to use our config system"""

import json
import logging
import os
from urllib.parse import urlparse

import requests


class HelperFunctions:
    """Helper functions for evaluation that use our config system"""

    def __init__(self, config, extra_headers):
        """Initialize with our config containing accounts and site URLs"""
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.extra_headers = extra_headers

        # Set up proxies if proxy is enabled
        self.proxies = None
        if config.proxy.enabled:
            self.proxies = {
                "http": config.proxy.server,
                "https": config.proxy.server,
            }

    def _get_site_url(self, site_name: str) -> str:
        """Get site URL from config"""
        site_host = self.config.sites[site_name]
        return f"http://{site_host}"

    def _get_account_info(self, account_key: str) -> dict:
        """Get account info from config"""
        return self.config.accounts[account_key]

    @staticmethod
    def _evaluator_completion_kwargs(cfg, model: str, messages: list[dict]) -> dict:
        """Build provider-specific LiteLLM arguments for evaluator judges."""
        evaluator_config = cfg.get("evaluator_llm", {})
        provider = str(evaluator_config.get("provider", "")).lower()
        kwargs = {"model": model, "messages": messages}
        if provider == "openai" or model.startswith("openai/"):
            kwargs["api_base"] = str(
                evaluator_config.get("base_url")
                or os.getenv("EVALUATOR_LLM_BASE_URL", "http://127.0.0.1:18000/v1")
            )
            kwargs["api_key"] = str(
                evaluator_config.get("api_key")
                or os.getenv("EVALUATOR_LLM_API_KEY", "local-llama")
            )
            kwargs["temperature"] = float(evaluator_config.get("temperature", 0.0))
            kwargs["max_tokens"] = int(evaluator_config.get("max_tokens", 256))
            kwargs["num_retries"] = int(evaluator_config.get("max_retries", 3))
            kwargs["timeout"] = float(evaluator_config.get("timeout", 300))
        return kwargs

    @staticmethod
    def _evaluator_response_content(response) -> str:
        choices = getattr(response, "choices", None)
        if not choices or getattr(choices[0], "message", None) is None:
            raise RuntimeError("Evaluator endpoint returned no OpenAI-compatible choices")
        content = choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Evaluator endpoint returned an empty response")
        return content

    def shopping_get_auth_token(self) -> str:
        """Get shopping site auth token"""
        shopping_url = self._get_site_url("shopping")
        admin_account = self._get_account_info("shopping_shopping_admin")

        headers = {"content-type": "application/json"}
        headers.update(self.extra_headers)

        self.logger.info(f"Shopping auth request - URL: {shopping_url}/rest/default/V1/integration/admin/token")
        self.logger.info(f"Shopping auth request - Headers: {headers}")
        self.logger.info(f"Shopping auth request - Proxies: {self.proxies}")

        response = requests.post(
            url=f"{shopping_url}/rest/default/V1/integration/admin/token",
            headers=headers,
            data=json.dumps(
                {
                    "username": admin_account["username"],
                    "password": admin_account["password"],
                }
            ),
            proxies=self.proxies,
            timeout=30,
        )
        self.logger.info(f"Shopping auth response status: {response.status_code}")
        response.raise_for_status()
        token: str = response.json()
        return token

    def shopping_get_latest_order_url(self) -> str:
        """Get the latest order url from the shopping website."""
        shopping_url = self._get_site_url("shopping")
        token = self.shopping_get_auth_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        headers.update(self.extra_headers)

        params = {
            "searchCriteria[sortOrders][0][field]": "created_at",
            "searchCriteria[sortOrders][0][direction]": "DESC",
            "searchCriteria[pageSize]": "1",
        }

        response = requests.get(f"{shopping_url}/rest/V1/orders", params=params, headers=headers, proxies=self.proxies, timeout=30)
        response.raise_for_status()

        response_obj = response.json()
        order_item = response_obj["items"][0]
        order_id = int(order_item["increment_id"])
        order_url = f"{shopping_url}/sales/order/view/order_id/{order_id}/"
        return order_url

    def shopping_get_sku_latest_review_author(self, sku: str) -> str:
        """Get the latest review author for a product SKU."""
        shopping_url = self._get_site_url("shopping")
        token = self.shopping_get_auth_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        headers.update(self.extra_headers)

        response = requests.get(f"{shopping_url}/rest/V1/products/{sku}/reviews", headers=headers, proxies=self.proxies, timeout=30)
        response.raise_for_status()

        response_obj = response.json()
        if len(response_obj) == 0:
            return ""
        author: str = response_obj[-1]["nickname"]
        return author

    def shopping_get_sku_latest_review_rating(self, sku: str) -> str:
        """Get the latest review rating for a product SKU."""
        shopping_url = self._get_site_url("shopping")
        token = self.shopping_get_auth_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        headers.update(self.extra_headers)

        response = requests.get(f"{shopping_url}/rest/V1/products/{sku}/reviews", headers=headers, proxies=self.proxies, timeout=30)
        response.raise_for_status()

        response_obj = response.json()
        if len(response_obj) == 0:
            return ""
        assert response_obj[0]["ratings"][0]["rating_name"] == "Rating"
        latest_review = response_obj[-1]
        rating: str = str(latest_review["ratings"][0]["percent"])
        return rating

    def reddit_get_post_url(self, url: str) -> str:
        """Get the post url from a Reddit comment/post URL"""
        # Url is http://domain/f/subreddit/post_id/...
        # get domain, subreddit, post_id
        parsed = urlparse(url)
        domain = parsed.netloc
        tok_url = parsed.path.split("/")

        if len(tok_url) < 4 or tok_url[1] != "f":
            return url

        subreddit = tok_url[2]
        post_id = tok_url[3]
        scheme = parsed.scheme
        post_url = f"{scheme}://{domain}/f/{subreddit}/{post_id}/"
        return post_url

    async def gitlab_get_project_member_role(self, page, account_name: str) -> str:
        """Get project member role from GitLab page (async version)"""
        try:
            account_idx = await page.evaluate(
                f"""(() => {{
                const elements = document.querySelectorAll("td[data-label='Account'] span.gl-avatar-labeled-sublabel");
                let index = -1;  // Default value if not found

                for(let i = 0; i < elements.length; i++) {{
                    if(elements[i].outerText === '@{account_name}') {{
                        index = i;
                        break;
                    }}
                }}

                return index;
            }})()"""
            )
            role: str = await page.evaluate(
                f"""(() => {{
                    return document.querySelectorAll("td.col-max-role span")[{account_idx}].outerText;
                }})()"""
            )
        except Exception:
            role = ""
        return role

    async def gitlab_get_project_memeber_role(self, page, account_name: str) -> str:
        """Backward-compatible alias for WebArena's historical typo."""
        return await self.gitlab_get_project_member_role(page, account_name)

    async def llm_fuzzy_match(self, pred: str, reference: str, question: str) -> float:
        """WebArena fuzzy-match judge, using this project's async transport."""
        import litellm

        from rl_web_agent.config_store import ConfigStore

        # Get evaluator LLM config
        cfg = ConfigStore.get()
        model = cfg.evaluator_llm.model

        message = "Help a teacher to grade the answer of a student given a question. Keep in mind that the student may use different phrasing or wording to answer the question. The goal is to evaluate whether the answer is semantically equivalent to the reference answer.\n"
        message += f"question: {question}\n"
        message += f"reference answer: {reference}\n"
        message += "all the string 'N/A' that you see is a special sequence that means 'not achievable'\n"
        message += f"student answer: {pred}\n"
        message += "Conclude the judgement by correct/incorrect/partially correct."
        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": message},
        ]

        # Call LiteLLM async completion (API keys read from environment automatically)
        response = await litellm.acompletion(**self._evaluator_completion_kwargs(cfg, model, messages))

        content = self._evaluator_response_content(response)
        response_lower = content.lower()
        if "partially correct" in response_lower or "incorrect" in response_lower:
            return 0.0
        assert "correct" in response_lower
        return 1.0

    async def llm_ua_match(self, pred: str, reference: str, question: str) -> float:
        """Use LiteLLM for unachievable task matching"""
        import litellm

        from rl_web_agent.config_store import ConfigStore

        # Get evaluator LLM config
        cfg = ConfigStore.get()
        model = cfg.evaluator_llm.model

        message = f"task: {question}\n"
        message += f"actual unachievable reason: {reference}\n"
        message += f"reported unachievable reason: {pred}\n"
        message += (
            "The task described above is inherently unachievable due to the reason specified under 'actual unachievable reason'. "
            "An individual previously attempted this task and was unable to complete it. They provided a reason for their failure, "
            "which is listed under 'reported unachievable reason'. Your role is to review both the actual and reported reasons. "
            "Determine if the reported reason aligns with the actual reason, even if implicitly. "
            "If the stated reason is in line with the actual reason, respond with 'same'. Otherwise, respond with 'different'."
        )
        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": message},
        ]

        # Call LiteLLM async completion (API keys read from environment automatically)
        response = await litellm.acompletion(**self._evaluator_completion_kwargs(cfg, model, messages))

        content = self._evaluator_response_content(response)
        response_lower = content.lower()
        if "different" in response_lower:
            return 0.0
        assert "same" in response_lower
        return 1.0


def get_helper_functions(config, extra_headers) -> HelperFunctions:
    """Create helper functions instance scoped to current config/headers."""
    return HelperFunctions(config, extra_headers)


def shopping_get_latest_order_url(config=None, extra_headers=None) -> str:
    """Global function for backward compatibility"""
    helper = get_helper_functions(config, extra_headers or {})
    return helper.shopping_get_latest_order_url()


def shopping_get_sku_latest_review_author(sku: str, config=None, extra_headers=None) -> str:
    """Global function for backward compatibility"""
    helper = get_helper_functions(config, extra_headers or {})
    return helper.shopping_get_sku_latest_review_author(sku)


def shopping_get_sku_latest_review_rating(sku: str, config=None, extra_headers=None) -> str:
    """Global function for backward compatibility"""
    helper = get_helper_functions(config, extra_headers or {})
    return helper.shopping_get_sku_latest_review_rating(sku)


def reddit_get_post_url(url: str, config=None, extra_headers=None) -> str:
    """Global function for backward compatibility"""
    helper = get_helper_functions(config, extra_headers or {})
    return helper.reddit_get_post_url(url)


async def gitlab_get_project_member_role(page, account_name: str, config=None, extra_headers=None) -> str:
    """Global function for backward compatibility"""
    helper = get_helper_functions(config, extra_headers or {})
    return await helper.gitlab_get_project_member_role(page, account_name)


async def gitlab_get_project_memeber_role(page, account_name: str, config=None, extra_headers=None) -> str:
    """Backward-compatible alias for WebArena's historical typo."""
    return await gitlab_get_project_member_role(page, account_name, config, extra_headers)
