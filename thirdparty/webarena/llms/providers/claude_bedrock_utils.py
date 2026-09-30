"""Tools to generate from Claude via AWS Bedrock Converse API.
Uses aioboto3 for async operations."""

import asyncio
import logging
from typing import Any

import aioboto3
import aiolimiter
from botocore.exceptions import ClientError
from tqdm.asyncio import tqdm_asyncio


async def _throttled_bedrock_converse_acreate(
    model_id: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    top_p: float,
    limiter: aiolimiter.AsyncLimiter,
    region_name: str = "us-east-1",
) -> dict[str, Any]:
    """Async function to call Bedrock Converse API with throttling."""
    async with limiter:
        session = aioboto3.Session()
        async with session.client("bedrock-runtime", region_name=region_name) as bedrock:
            # Convert messages to Bedrock format
            bedrock_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    # System messages are handled separately in Bedrock
                    continue
                bedrock_messages.append({"role": msg["role"], "content": [{"text": msg["content"]}]})

            # Extract system message if exists
            system_messages = [msg["content"] for msg in messages if msg["role"] == "system"]

            inference_config = {
                "temperature": temperature,
                "topP": top_p,
                "maxTokens": max_tokens,
            }

            request_body = {
                "modelId": model_id,
                "messages": bedrock_messages,
                "inferenceConfig": inference_config,
            }

            if system_messages:
                request_body["system"] = [{"text": system_messages[0]}]

            for attempt in range(3):
                try:
                    response = await bedrock.converse(**request_body)
                    return response
                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code")
                    if error_code in ["ThrottlingException", "ModelTimeoutException"]:
                        logging.warning(f"Bedrock API error: {error_code}. Sleeping for 10 seconds.")
                        await asyncio.sleep(10)
                    else:
                        logging.warning(f"Bedrock API error: {e}")
                        break
                except Exception as e:
                    logging.warning(f"Unexpected error: {e}")
                    break

            # Return empty response on failure
            return {"output": {"message": {"role": "assistant", "content": [{"text": ""}]}}}


async def agenerate_from_bedrock_claude_chat(
    messages_list: list[list[dict[str, str]]],
    model_id: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    context_length: int,
    requests_per_minute: int = 100,
    region_name: str = "us-east-1",
) -> list[str]:
    """Generate from Bedrock Claude Chat API.

    Args:
        messages_list: list of message list
        model_id: The Claude model ID (e.g., 'anthropic.claude-3-sonnet-20240229-v1:0')
        temperature: Temperature to use.
        max_tokens: Maximum number of tokens to generate.
        top_p: Top p to use.
        context_length: Length of context to use.
        requests_per_minute: Number of requests per minute to allow.
        region_name: AWS region name.

    Returns:
        List of generated responses.
    """

    limiter = aiolimiter.AsyncLimiter(requests_per_minute)
    async_responses = [
        _throttled_bedrock_converse_acreate(
            model_id=model_id,
            messages=message,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            limiter=limiter,
            region_name=region_name,
        )
        for message in messages_list
    ]
    responses = await tqdm_asyncio.gather(*async_responses)
    return [x["output"]["message"]["content"][0]["text"] for x in responses]


# @retry_with_exponential_backoff
def generate_from_bedrock_claude_chat(
    messages: list[dict[str, str]],
    model_id: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    context_length: int,
    region_name: str = "us-east-1",
    stop_token: str | None = None,
) -> str:
    """Generate from Bedrock Claude Chat API (synchronous).

    Args:
        messages: List of message dictionaries with 'role' and 'content' keys
        model_id: The Claude model ID (e.g., 'anthropic.claude-3-sonnet-20240229-v1:0')
        temperature: Temperature to use.
        max_tokens: Maximum number of tokens to generate.
        top_p: Top p to use.
        context_length: Length of context to use.
        region_name: AWS region name.
        stop_token: Stop token (not directly supported by Bedrock Converse API).

    Returns:
        Generated response text.
    """
    import boto3

    bedrock = boto3.client("bedrock-runtime", region_name=region_name)

    # Convert messages to Bedrock format
    bedrock_messages = []
    system_messages = []

    for msg in messages:
        if msg["role"] == "system":
            system_messages.append(msg["content"])
        else:
            bedrock_messages.append({"role": msg["role"], "content": [{"text": msg["content"]}]})

    inference_config = {
        "temperature": temperature,
        "topP": top_p,
        "maxTokens": max_tokens,
    }

    request_body = {
        "modelId": model_id,
        "messages": bedrock_messages,
        "inferenceConfig": inference_config,
    }

    if system_messages:
        request_body["system"] = [{"text": system_messages[0]}]

    # Add stop sequences if provided
    if stop_token:
        request_body["inferenceConfig"]["stopSequences"] = [stop_token]

    response = bedrock.converse(**request_body)
    answer: str = response["output"]["message"]["content"][0]["text"]
    return answer


def generate_from_bedrock_claude_completion(
    prompt: str,
    model_id: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    context_length: int,
    region_name: str = "us-east-1",
    stop_token: str | None = None,
) -> str:
    """Generate from Bedrock Claude using completion format.

    This converts a completion-style prompt to chat format for Claude.
    """
    # Convert completion prompt to chat format
    messages = [{"role": "user", "content": prompt}]

    return generate_from_bedrock_claude_chat(
        messages=messages,
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        context_length=context_length,
        region_name=region_name,
        stop_token=stop_token,
    )
