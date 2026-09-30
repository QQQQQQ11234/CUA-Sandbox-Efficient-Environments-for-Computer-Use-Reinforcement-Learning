"""WebArena evaluator - ported to async and simplified for RL Web Agent"""

import asyncio
import collections
import html
import logging
import urllib
from typing import Any

from beartype import beartype
from nltk.tokenize import word_tokenize

logger = logging.getLogger(__name__)


class StringEvaluator:
    """Check whether the answer is correct with:
    exact match
    must include
    fuzzy match, using LLM judge (simplified for now)
    """

    @staticmethod
    @beartype
    def clean_answer(answer):
        answer = answer.strip()
        if answer.startswith("'") and answer.endswith("'"):
            answer = answer[1:-1]
        elif answer.startswith('"') and answer.endswith('"'):
            answer = answer[1:-1]
        return answer.lower()

    @staticmethod
    @beartype
    def exact_match(ref, pred):
        return float(StringEvaluator.clean_answer(pred) == StringEvaluator.clean_answer(ref))

    @staticmethod
    @beartype
    def must_include(ref, pred, tokenize):
        clean_ref = StringEvaluator.clean_answer(ref)
        clean_pred = StringEvaluator.clean_answer(pred)
        # tokenize the answer if the ref is a single word
        # prevent false positive (e.g, 0)
        if tokenize and len(clean_ref) == 1 and len(word_tokenize(clean_ref, preserve_line=True)) == 1:
            tok_pred = word_tokenize(clean_pred, preserve_line=True)
            return float(clean_ref in tok_pred)
        else:
            return float(clean_ref in clean_pred)

    @staticmethod
    @beartype
    async def fuzzy_match(ref, pred, intent, config=None, extra_headers=None):
        from rl_web_agent.helper_functions import get_helper_functions

        helper = get_helper_functions(config, extra_headers or {})
        return await helper.llm_fuzzy_match(pred, ref, intent)

    @staticmethod
    @beartype
    async def ua_match(ref, pred, intent, config=None, extra_headers=None):
        from rl_web_agent.helper_functions import get_helper_functions

        helper = get_helper_functions(config, extra_headers or {})
        return await helper.llm_ua_match(pred, ref, intent)

    async def evaluate(self, answer: str, page, task_config: dict[str, Any], env_config=None, extra_headers=None) -> float:
        """Evaluate answer against reference answers in config"""
        pred = self.clean_answer(answer)
        logger.debug(f"answer: {answer}")
        logger.debug(f"model answer: {pred}")
        logger.debug(f"task_config: {task_config}")

        score = 1.0
        for approach, value in task_config["eval"]["reference_answers"].items():
            match approach:
                case "exact_match":
                    score *= self.exact_match(ref=value, pred=pred)
                    logger.debug(f"exact_match score: {score}")

                case "must_include":
                    assert isinstance(value, list)
                    for must_value in value:
                        score *= self.must_include(
                            ref=must_value,
                            pred=pred,
                            tokenize=(len(value) == 1),
                        )
                        logger.debug(f"must_include score: {score}")
                        logger.debug(f"must_value: {must_value}")
                        logger.debug(f"pred: {pred}")
                case "fuzzy_match":
                    intent = task_config["intent"]
                    if value == "N/A":
                        # if the instruction only asks the model to generate N/A when encountering an unachievable task
                        # without more concrete reasons
                        score *= self.exact_match(ref=value, pred=pred)
                        logger.debug(f"fuzzy_match score for N/A: {score}")
                        # if the instruction also asks the model to generate the reason why the task is unachievable
                        # this should be the default as it will prevent false positive N/A`
                        if score != 1:
                            score = 1.0 * await self.ua_match(intent=task_config["intent"], ref=task_config["eval"]["string_note"], pred=pred, config=env_config, extra_headers=extra_headers)
                            logger.debug(f"fuzzy_match score for N/A with ua_match: {score}")
                    else:
                        assert isinstance(value, list)
                        for reference in value:
                            score *= await self.fuzzy_match(ref=reference, pred=pred, intent=intent, config=env_config, extra_headers=extra_headers)
                            logger.debug(f"fuzzy_match score for {reference}: {score}")
        return score


class URLEvaluator:
    """Check URL matching"""

    async def evaluate(self, answer: str, page, task_config: dict[str, Any], env_config=None, extra_headers=None) -> float:
        """Evaluate current page URL against reference URL"""
        page_url = page.url

        def clean_url(url):
            url = str(url)
            url = url.rstrip("/")
            return url

        def parse_url(url):
            """Parse a URL into its base, path, and query components."""
            parsed_url = urllib.parse.urlparse(url)
            base_path = parsed_url.netloc + parsed_url.path
            query = urllib.parse.parse_qs(parsed_url.query)
            return base_path, query

        def parse_urls(urls):
            """Parse a list of URLs."""
            base_paths = []
            queries = collections.defaultdict(set)
            for url in urls:
                base_path, query = parse_url(url)
                base_paths.append(base_path)
                for k, v in query.items():
                    queries[k].update(v)
            return base_paths, queries

        pred = clean_url(page_url)
        logger.debug(f"pred url: {pred}")
        ref_urls = task_config["eval"]["reference_url"].split(" |OR| ")
        ref_urls = [clean_url(url) for url in ref_urls]
        logger.debug(f"ref urls: {ref_urls}")
        matching_rule = task_config["eval"].get("url_note", "GOLD in PRED")
        if matching_rule == "GOLD in PRED":
            ref_base_paths, ref_queries = parse_urls(ref_urls)
            pred_base_paths, pred_query = parse_url(pred)
            logger.debug(f"pred base paths: {pred_base_paths}")
            logger.debug(f"pred query: {pred_query}")
            logger.debug(f"ref base paths: {ref_base_paths}")
            logger.debug(f"ref queries: {ref_queries}")
            base_score = float(any([ref_base_path in pred_base_paths for ref_base_path in ref_base_paths]))
            query_score = 1.0
            for k, possible_values in ref_queries.items():
                query_score *= float(any(possible_ref_value in pred_query.get(k, []) for possible_ref_value in possible_values))
                logger.debug(f"possible values: {possible_values}")
                logger.debug(f"pred query: {pred_query}")
                logger.debug(f"k: {k}")
                logger.debug(f"query score: {query_score}")
            logger.debug(f"final query score: {query_score}")
            score = base_score * query_score
            logger.debug(f"final score: {score}")

        else:
            raise ValueError(f"Unknown matching rule: {matching_rule}")

        return score


class HTMLContentEvaluator:
    """Check whether the contents appear in the page"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def evaluate(self, answer: str, page, task_config: dict[str, Any], env_config=None, extra_headers=None) -> float:
        """Evaluate page content using WebArena's official navigation semantics."""
        targets = task_config["eval"]["program_html"]
        score = 1.0
        for target in targets:
            target_url = target["url"]
            if target_url.startswith("func"):
                func = target_url.split("func:")[1]
                func = func.replace("__last_url__", page.url)
                from rl_web_agent.helper_functions import get_helper_functions

                helper = get_helper_functions(env_config, extra_headers or {})
                func_context = {
                    "shopping_get_latest_order_url": helper.shopping_get_latest_order_url,
                    "shopping_get_sku_latest_review_author": helper.shopping_get_sku_latest_review_author,
                    "shopping_get_sku_latest_review_rating": helper.shopping_get_sku_latest_review_rating,
                    "reddit_get_post_url": helper.reddit_get_post_url,
                }
                target_url = eval(func, func_context)
            self.logger.debug(f"target_url: {target_url}")
            if target_url != "last":
                await page.goto(target_url)
                await asyncio.sleep(3)

            locator = target["locator"]  # js element locator
            self.logger.debug(f"locator: {locator}")

            # empty, use the full page
            if not locator.strip():
                selected_element = await page.content()
            # use JS to select the element
            elif locator.startswith("document.") or locator.startswith("[...document."):
                if "prep_actions" in target:
                    try:
                        for prep_action in target["prep_actions"]:
                            await page.evaluate(f"() => {prep_action}")
                    except Exception:
                        pass
                try:
                    selected_element = str(await page.evaluate(f"() => {locator}"))
                    if not selected_element:
                        selected_element = ""
                except Exception:
                    selected_element = ""
            # run program to call API
            elif locator.startswith("func:"):  # a helper function
                func = locator.split("func:")[1]
                func = func.replace("__page__", "page")
                from rl_web_agent.helper_functions import get_helper_functions

                helper = get_helper_functions(env_config, extra_headers or {})
                func_context = {
                    "page": page,
                    "shopping_get_sku_latest_review_author": helper.shopping_get_sku_latest_review_author,
                    "shopping_get_sku_latest_review_rating": helper.shopping_get_sku_latest_review_rating,
                    "reddit_get_post_url": helper.reddit_get_post_url,
                    "gitlab_get_project_memeber_role": helper.gitlab_get_project_memeber_role,
                }
                if "gitlab_get_project_memeber_role" in func:
                    import re

                    match = re.search(r'gitlab_get_project_memeber_role\(page,\s*["\']([^"\']+)["\']\)', func)
                    if match is None:
                        raise ValueError(f"Invalid GitLab member-role locator: {locator}")
                    selected_element = await helper.gitlab_get_project_memeber_role(page, match.group(1))
                else:
                    selected_element = eval(func, func_context)
            else:
                raise ValueError(f"Unknown locator: {locator}")

            selected_element = html.unescape("" if selected_element is None else str(selected_element))

            if "exact_match" in target["required_contents"]:
                required_contents = target["required_contents"]["exact_match"]
                cur_score = StringEvaluator.exact_match(ref=required_contents, pred=selected_element)
                score *= float(cur_score)
            elif "must_include" in target["required_contents"]:
                required_contents = target["required_contents"]["must_include"]
                assert isinstance(required_contents, list)
                for content in required_contents:
                    content_or = content.split(" |OR| ")
                    cur_score = any(
                        StringEvaluator.must_include(
                            ref=content,
                            pred=selected_element,
                            tokenize=False,
                        )
                        for content in content_or
                    )
                    score *= float(cur_score)
            else:
                raise ValueError(f"Unknown required_contents: {target['required_contents'].keys()}")
        return score


async def evaluate_task(answer: str, page, task_config: dict[str, Any], env_config=None, extra_headers=None) -> float:
    """
    Evaluate a task using the provided answer, page, and configuration.
    Uses WebArena's exact evaluation logic.

    Args:
        answer: The model's final answer
        page: Playwright page object
        task_config: Task configuration dict
        env_config: Environment configuration dict (optional)
        extra_headers: Additional HTTP headers dict (optional)

    Returns:
        float: Score between 0.0 and 1.0
    """

    eval_types = task_config["eval"]["eval_types"]
    total_score = 1.0

    for eval_type in eval_types:
        if eval_type == "string_match":
            evaluator = StringEvaluator()
            score = await evaluator.evaluate(answer, page, task_config, env_config, extra_headers)
            total_score *= score
        elif eval_type == "url_match":
            evaluator = URLEvaluator()
            score = await evaluator.evaluate(answer, page, task_config, env_config, extra_headers)
            total_score *= score
        elif eval_type == "program_html":
            evaluator = HTMLContentEvaluator()
            score = await evaluator.evaluate(answer, page, task_config, env_config, extra_headers)
            total_score *= score
        else:
            raise ValueError(f"Unknown eval_type: {eval_type}")

    return total_score
