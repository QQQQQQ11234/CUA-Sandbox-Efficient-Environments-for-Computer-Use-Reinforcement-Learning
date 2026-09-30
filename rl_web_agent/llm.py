"""
Async LLM interface supporting OpenAI and AWS Bedrock providers.
Unified interface: input is OpenAI message format, output is string content.
Also supports function calling with structured responses.
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

import aioboto3
import openai
from omegaconf import DictConfig

from rl_web_agent.config_store import ConfigStore
from rl_web_agent.utils import json_dumps_truncated


class LLMProtocolError(RuntimeError):
    """The upstream endpoint returned a non OpenAI-compatible response."""


def _first_openai_choice(response: Any, operation: str) -> Any:
    """Return the first choice with a useful error instead of leaking NoneType."""
    choices = getattr(response, "choices", None)
    if not choices:
        response_id = getattr(response, "id", None)
        detail = f" (response id: {response_id})" if response_id else ""
        raise LLMProtocolError(
            f"OpenAI-compatible endpoint returned no choices during {operation}{detail}"
        )
    choice = choices[0]
    if getattr(choice, "message", None) is None:
        raise LLMProtocolError(
            f"OpenAI-compatible endpoint returned a choice without a message during {operation}"
        )
    return choice


class LLMSession(ABC):
    """Abstract base class for LLM conversation sessions"""

    def __init__(self, provider: "LLMProvider", generation_defaults: dict, system_prompt: str):
        self.provider = provider
        self.generation_defaults = generation_defaults
        self.system_prompt = system_prompt
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Each session maintains conversation history in its provider's native format
        self._init_conversation_history()

    @abstractmethod
    def _init_conversation_history(self):
        """Initialize conversation history in provider's native format"""
        pass

    @abstractmethod
    def reset_turn_history(self) -> None:
        """Discard prior browser turns while retaining the system prompt."""
        pass

    @abstractmethod
    async def chat_complete(self, user_message: str = None, tool_response: dict = None, **kwargs) -> dict[str, Any]:
        """
        Add a user message or tool response and get model response.

        Args:
            user_message: User message to add (mutually exclusive with tool_response)
            tool_response: Tool response to add (mutually exclusive with user_message)
            **kwargs: Additional generation parameters

        Returns:
            Dict with content, reasoning_content, and tool_calls
        """
        pass

    @abstractmethod
    async def chat_complete_with_tools(self, user_message: str = None, tool_response: dict = None, tools: list[dict[str, Any]] = None, **kwargs) -> dict[str, Any]:
        """
        Add a user message or tool response and get model response with tool calling support.

        Args:
            user_message: User message to add (mutually exclusive with tool_response)
            tool_response: Tool response to add (mutually exclusive with user_message)
            tools: Available tools for function calling
            **kwargs: Additional generation parameters

        Returns:
            Structured response with content and tool calls
        """
        pass

    @abstractmethod
    def to_json(self) -> dict[str, Any]:
        """
        Export session conversation history to JSON format.

        Returns:
            Dictionary containing the conversation history and metadata
        """
        pass


class LLMProvider(ABC):
    """Abstract base class for LLM providers"""

    def __init__(self, config: DictConfig, semaphore: asyncio.Semaphore):
        self.config = config
        self.semaphore = semaphore
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    async def complete(self, messages: list[dict[str, str]], **kwargs) -> str:
        """Generate completion from OpenAI format messages, return content string"""
        pass

    @abstractmethod
    async def complete_with_tools(self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs) -> dict[str, Any]:
        """Generate completion with function calling support, return structured response"""
        pass

    @abstractmethod
    async def create_session(self, system_prompt: str) -> LLMSession:
        """Create a new conversation session with system prompt"""
        pass

    @abstractmethod
    async def close(self):
        """Clean up resources"""
        pass


class OpenAISession(LLMSession):
    """OpenAI conversation session - stores messages in OpenAI format"""

    def __init__(self, provider: "OpenAIProvider", generation_defaults: dict, system_prompt: str):
        super().__init__(provider, generation_defaults, system_prompt)

    def _init_conversation_history(self):
        """Initialize conversation history in OpenAI format"""
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def reset_turn_history(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

    async def chat_complete(self, user_message: str = None, tool_response: dict = None, **kwargs) -> str:
        """Add user message or tool response and get model response"""
        if user_message and tool_response:
            raise ValueError("Cannot provide both user_message and tool_response")
        if not user_message and not tool_response:
            raise ValueError("Must provide either user_message or tool_response")

        # Add message to conversation history in OpenAI format
        if user_message:
            self.messages.append({"role": "user", "content": user_message})
        else:
            # Add tool response message
            self.messages.append({"role": "tool", "content": tool_response["content"], "tool_call_id": tool_response["tool_call_id"]})

        # Merge generation defaults with provided kwargs
        merged_kwargs = dict(self.generation_defaults)
        merged_kwargs.update(kwargs)

        # Convert Hydra ListConfig to regular list for JSON serialization
        if "stop" in merged_kwargs and merged_kwargs["stop"] is not None:
            merged_kwargs["stop"] = list(merged_kwargs["stop"]) if merged_kwargs["stop"] else None

        # Call OpenAI API directly
        async with self.provider.semaphore:
            self.logger.debug("🔵 OpenAI INPUT MESSAGES:\n%s", json_dumps_truncated(self.messages, indent=2))
            request_kwargs = {
                "model": merged_kwargs.get("model", self.provider.model),
                "messages": self.messages,
                "temperature": merged_kwargs.get("temperature"),
                "max_completion_tokens": merged_kwargs.get("max_tokens"),
                "top_p": merged_kwargs.get("top_p"),
                "frequency_penalty": merged_kwargs.get("frequency_penalty"),
                "presence_penalty": merged_kwargs.get("presence_penalty"),
                "stop": merged_kwargs.get("stop"),
                "stream": False,
            }
            if self.provider.config.thinking_enabled:
                del request_kwargs["top_p"]

            # drop None values
            request_kwargs = {k: v for k, v in request_kwargs.items() if v is not None}

            response = await self.provider.client.chat.completions.create(**request_kwargs)

            # Store the full response in native format
            message = _first_openai_choice(response, "chat completion").message
            self.messages.append({k: v for k, v in message.model_dump().items() if v})

            # Format response for return
            result = {"content": message.content or "", "reasoning_content": "", "tool_calls": []}

            self.logger.debug("🔵 OpenAI RESPONSE:\n%s", result["content"])
            self.logger.debug(f"OpenAI response: {result}")

        return result

    async def chat_complete_with_tools(self, user_message: str = None, tool_response: dict = None, tools: list[dict[str, Any]] = None, **kwargs) -> dict[str, Any]:
        """Add user message or tool response and get model response with tool calling"""
        if user_message and tool_response:
            raise ValueError("Cannot provide both user_message and tool_response")
        if not user_message and not tool_response:
            raise ValueError("Must provide either user_message or tool_response")

        # Add message to conversation history in OpenAI format
        if user_message:
            self.messages.append({"role": "user", "content": user_message})
        else:
            # Add tool response message
            self.messages.append({"role": "tool", "content": tool_response["content"], "tool_call_id": tool_response["tool_call_id"]})

        # Merge generation defaults with provided kwargs
        merged_kwargs = dict(self.generation_defaults)
        merged_kwargs.update(kwargs)

        # Convert Hydra ListConfig to regular list for JSON serialization
        if "stop" in merged_kwargs and merged_kwargs["stop"] is not None:
            merged_kwargs["stop"] = list(merged_kwargs["stop"]) if merged_kwargs["stop"] else None

        # Call OpenAI API directly
        async with self.provider.semaphore:
            self.logger.debug("🔵 OpenAI TOOLS INPUT MESSAGES:\n%s", json_dumps_truncated(self.messages, indent=2))
            self.logger.debug("🔵 OpenAI TOOLS:\n%s", json_dumps_truncated(tools, indent=2))

            # Prepare request with tools
            request_kwargs = {
                "model": merged_kwargs.get("model", self.provider.model),
                "messages": self.messages,
                "tools": tools,
                "temperature": merged_kwargs.get("temperature"),
                "max_completion_tokens": merged_kwargs.get("max_tokens"),
                "top_p": merged_kwargs.get("top_p"),
                "frequency_penalty": merged_kwargs.get("frequency_penalty"),
                "presence_penalty": merged_kwargs.get("presence_penalty"),
                "stop": merged_kwargs.get("stop"),
                "stream": False,
            }
            if self.provider.config.thinking_enabled:
                del request_kwargs["top_p"]

            # Add tool_choice if specified
            if "tool_choice" in merged_kwargs:
                request_kwargs["tool_choice"] = merged_kwargs["tool_choice"]

            # drop None values
            request_kwargs = {k: v for k, v in request_kwargs.items() if v is not None}

            response = await self.provider.client.chat.completions.create(**request_kwargs)

            choice = _first_openai_choice(response, "tool chat completion")
            message = choice.message

            # Store the full response in native format
            self.messages.append({k: v for k, v in message.model_dump().items() if v})

            # Format response for return (convert to standard format)
            result = {"content": message.content or "", "tool_calls": [], "finish_reason": choice.finish_reason}

            # Process tool calls if present
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    result["tool_calls"].append({"id": tool_call.id, "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments}, "type": tool_call.type})

            self.logger.debug("🔵 OpenAI TOOLS RESPONSE:\n%s", json_dumps_truncated(result, indent=2))

        return result

    def to_json(self) -> dict[str, Any]:
        """Export OpenAI session conversation history to JSON format"""
        return {"provider": "openai", "system_prompt": self.system_prompt, "conversation_history": self.messages, "total_messages": len(self.messages)}


class OpenAIProvider(LLMProvider):
    """OpenAI API provider using official async SDK"""

    def __init__(self, config: DictConfig, semaphore: asyncio.Semaphore):
        super().__init__(config, semaphore)
        self.client = openai.AsyncOpenAI(api_key=config.api_key, base_url=config.get("base_url"), timeout=config.get("timeout", 60), max_retries=config.get("max_retries", 2))
        self.model = config.get("model", "gpt-4")

    async def complete(self, messages: list[dict[str, str]], **kwargs) -> str:
        """Generate completion using OpenAI API"""
        async with self.semaphore:
            self.logger.debug("🔵 OpenAI INPUT MESSAGES:\n%s", json_dumps_truncated(messages, indent=2))
            request_kwargs = {
                "model": kwargs.get("model", self.model),
                "messages": messages,
                "temperature": kwargs.get("temperature"),
                "max_completion_tokens": kwargs.get("max_tokens"),
                "top_p": kwargs.get("top_p"),
                "frequency_penalty": kwargs.get("frequency_penalty"),
                "presence_penalty": kwargs.get("presence_penalty"),
            }
            if self.config.thinking_enabled:
                del request_kwargs["top_p"]

            # drop None values
            request_kwargs = {k: v for k, v in request_kwargs.items() if v is not None}

            response = await self.client.chat.completions.create(
                **request_kwargs,
                stream=False,
            )

            content = _first_openai_choice(response, "completion").message.content
            self.logger.debug("🔵 OpenAI RESPONSE:\n%s", content)
            self.logger.debug(f"OpenAI response: {content}")
            return content

    async def complete_with_tools(self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs) -> dict[str, Any]:
        """Generate completion with function calling using OpenAI API"""
        async with self.semaphore:
            self.logger.debug("🔵 OpenAI TOOLS INPUT MESSAGES:\n%s", json_dumps_truncated(messages, indent=2))
            self.logger.debug("🔵 OpenAI TOOLS:\n%s", json_dumps_truncated(tools, indent=2))

            # Prepare request with tools
            request_kwargs = {
                "model": kwargs.get("model", self.model),
                "messages": messages,
                "tools": tools,
                "temperature": kwargs.get("temperature"),
                "max_completion_tokens": kwargs.get("max_tokens"),
                "top_p": kwargs.get("top_p"),
                "frequency_penalty": kwargs.get("frequency_penalty"),
                "presence_penalty": kwargs.get("presence_penalty"),
                "stop": kwargs.get("stop"),
                "stream": False,
            }

            # Add tool_choice if specified
            if "tool_choice" in kwargs:
                request_kwargs["tool_choice"] = kwargs["tool_choice"]
            if self.config.thinking_enabled:
                del request_kwargs["top_p"]

            # drop None values
            request_kwargs = {k: v for k, v in request_kwargs.items() if v is not None}

            response = await self.client.chat.completions.create(**request_kwargs)

            choice = _first_openai_choice(response, "tool completion")
            message = choice.message

            # Format response consistently
            result = {"content": message.content or "", "tool_calls": [], "finish_reason": choice.finish_reason}

            # Process tool calls if present
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    result["tool_calls"].append({"id": tool_call.id, "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments}, "type": tool_call.type})

            self.logger.debug("🔵 OpenAI TOOLS RESPONSE:\n%s", json_dumps_truncated(result, indent=2))
            self.logger.debug(f"OpenAI function calling response: {result}")
            return result

    async def create_session(self, system_prompt: str) -> LLMSession:
        """Create a new conversation session"""
        # This method should not be called directly on providers
        raise NotImplementedError("Use LLMClient.create_session() instead")

    async def close(self):
        """Close OpenAI client"""
        await self.client.close()


class BedrockSession(LLMSession):
    """Bedrock conversation session - stores messages in Bedrock format"""

    def __init__(self, provider: "BedrockProvider", generation_defaults: dict, system_prompt: str):
        super().__init__(provider, generation_defaults, system_prompt)

    def _init_conversation_history(self):
        """Initialize conversation history in Bedrock format"""
        self.system_messages = [{"text": self.system_prompt}]
        self.converse_messages = []

    def reset_turn_history(self) -> None:
        self.converse_messages = []

    async def chat_complete(self, user_message: str = None, tool_response: dict = None, **kwargs) -> str:
        """Add user message or tool response and get model response"""
        if user_message and tool_response:
            raise ValueError("Cannot provide both user_message and tool_response")
        if not user_message and not tool_response:
            raise ValueError("Must provide either user_message or tool_response")

        # Add message to conversation history in Bedrock format
        if user_message:
            self.converse_messages.append({"role": "user", "content": [{"text": user_message}]})
        else:
            # Add tool response message in Bedrock format
            tool_result_content = tool_response["content"]
            tool_call_id = tool_response["tool_call_id"]

            # Parse the tool result content
            try:
                result_data = json.loads(tool_result_content)
                content = [{"json": result_data}]
            except (json.JSONDecodeError, TypeError):
                # If not valid JSON, treat as text
                content = [{"text": str(tool_result_content)}]

            self.converse_messages.append({"role": "user", "content": [{"toolResult": {"toolUseId": tool_call_id, "content": content}}]})

        # Merge generation defaults with provided kwargs
        merged_kwargs = dict(self.generation_defaults)
        merged_kwargs.update(kwargs)

        # Convert Hydra ListConfig to regular list for JSON serialization
        if "stop" in merged_kwargs and merged_kwargs["stop"] is not None:
            merged_kwargs["stop"] = list(merged_kwargs["stop"]) if merged_kwargs["stop"] else None

        # Call Bedrock API directly
        async with self.provider.semaphore:
            self.logger.debug("🟠 Bedrock INPUT MESSAGES:\n%s", json_dumps_truncated(self.converse_messages, indent=2))

            client = await self.provider._get_client()

            # Prepare inference config
            inference_config = {
                "maxTokens": merged_kwargs.get("max_tokens"),
                "temperature": merged_kwargs.get("temperature"),
                "topP": merged_kwargs.get("top_p"),
            }
            # drop None values
            inference_config = {k: v for k, v in inference_config.items() if v is not None}

            # Add stop sequences if provided
            stop_sequences = merged_kwargs.get("stop_sequences", merged_kwargs.get("stop"))
            if stop_sequences:
                inference_config["stopSequences"] = stop_sequences if isinstance(stop_sequences, list) else [stop_sequences]

            # Use Converse API
            converse_kwargs = {"modelId": merged_kwargs.get("model_id", self.provider.model_id), "messages": self.converse_messages, "inferenceConfig": inference_config, "system": self.system_messages}

            # Merge additionalModelRequestFields from provider defaults and per-call overrides
            additional_fields: dict = dict(self.provider.additional_model_request_fields)
            if "additionalModelRequestFields" in merged_kwargs:
                override_fields = merged_kwargs["additionalModelRequestFields"]
                if isinstance(override_fields, dict):
                    additional_fields.update(override_fields)

            # Add thinking configuration if enabled (merge, do not overwrite user-provided reasoning_config)
            if self.provider.thinking_enabled:
                if "reasoning_config" not in additional_fields:
                    additional_fields["reasoning_config"] = {"type": "enabled", "budget_tokens": self.provider.thinking_budget_tokens}
                inference_config["maxTokens"] += self.provider.thinking_budget_tokens

            if additional_fields:
                converse_kwargs["additionalModelRequestFields"] = additional_fields

            async with client as bedrock_client:
                response = await bedrock_client.converse(**converse_kwargs)

            # Store the full response in native format
            output = response["output"]["message"]
            """
              {
    "role": "assistant",
    "content": [
      {
        "text": "\n\nTHOUGHT: The objective is to find reviewers who mention \"under water photo\". The product page has a \"12 Reviews\" link (semantic ID \"reviews_12\") which likely contains customer reviews. I need to cli... [TRUNCATED]"
      },
      {
        "reasoningContent": {
          "reasoningText": {
            "text": "Okay, let's see. The user wants a list of reviewers who mention \"under water photo\" in their reviews. The current page is the product page for a Fujifilm camera. I need to check the reviews section to... [TRUNCATED]"
          }
        }
      }
    ]
  },
  """
            if "deepseek" in self.provider.model_id:
                # strip reasoningcontent
                output["content"] = [content for content in output["content"] if "reasoningContent" not in content]

            self.converse_messages.append(output)

            # Format response for return
            result = {"content": "", "reasoning_content": "", "tool_calls": []}

            if "content" in output:
                for content_block in output["content"]:
                    if "text" in content_block:
                        result["content"] += content_block["text"]
                    elif "thinking" in content_block and self.provider.thinking_enabled:
                        # Handle thinking content block as per Anthropic docs
                        thinking_data = content_block["thinking"]
                        if isinstance(thinking_data, dict):
                            result["reasoning_content"] = thinking_data.get("thinking", "")
                        else:
                            result["reasoning_content"] = str(thinking_data)
                    elif "toolUse" in content_block:
                        tool_use = content_block["toolUse"]
                        result["tool_calls"].append({"id": tool_use["toolUseId"], "function": {"name": tool_use["name"], "arguments": json.dumps(tool_use["input"])}, "type": "function"})

            # Log reasoning content if present
            if result["reasoning_content"]:
                self.logger.debug(f"Bedrock thinking content: {result['reasoning_content']}")

            self.logger.debug("🟠 Bedrock RESPONSE:\n%s", result["content"])
            self.logger.debug(f"Bedrock response: {result}")

        return result

    async def chat_complete_with_tools(self, user_message: str = None, tool_response: dict = None, tools: list[dict[str, Any]] = None, **kwargs) -> dict[str, Any]:
        """Add user message or tool response and get model response with tool calling"""
        if user_message and tool_response:
            raise ValueError("Cannot provide both user_message and tool_response")
        if not user_message and not tool_response:
            raise ValueError("Must provide either user_message or tool_response")

        # Add message to conversation history in Bedrock format
        if user_message:
            self.converse_messages.append({"role": "user", "content": [{"text": user_message}]})
        else:
            # Add tool response message in Bedrock format
            tool_result_content = tool_response["content"]
            tool_call_id = tool_response["tool_call_id"]

            # Parse the tool result content
            try:
                result_data = json.loads(tool_result_content)
                content = [{"json": result_data}]
            except (json.JSONDecodeError, TypeError):
                # If not valid JSON, treat as text
                content = [{"text": str(tool_result_content)}]

            self.converse_messages.append({"role": "user", "content": [{"toolResult": {"toolUseId": tool_call_id, "content": content}}]})
        # Merge generation defaults with provided kwargs
        merged_kwargs = dict(self.generation_defaults)
        merged_kwargs.update(kwargs)

        # Convert Hydra ListConfig to regular list for JSON serialization
        if "stop" in merged_kwargs and merged_kwargs["stop"] is not None:
            merged_kwargs["stop"] = list(merged_kwargs["stop"]) if merged_kwargs["stop"] else None

        # Call Bedrock API directly
        async with self.provider.semaphore:
            self.logger.debug("🟠 Bedrock TOOLS INPUT MESSAGES:\n%s", json_dumps_truncated(self.converse_messages, indent=2))

            client = await self.provider._get_client()

            # Convert OpenAI tools format to Bedrock format
            tool_config = None
            if tools:
                tool_config = {"tools": []}

                for tool in tools:
                    if tool["type"] == "function":
                        func = tool["function"]
                        bedrock_tool = {"toolSpec": {"name": func["name"], "description": func["description"], "inputSchema": {"json": func["parameters"]}}}
                        tool_config["tools"].append(bedrock_tool)

            # Prepare inference config
            inference_config = {
                "maxTokens": merged_kwargs.get("max_tokens"),
                "temperature": merged_kwargs.get("temperature"),
                "topP": merged_kwargs.get("top_p"),
            }
            # drop None values
            inference_config = {k: v for k, v in inference_config.items() if v is not None}

            # Add stop sequences if provided
            stop_sequences = merged_kwargs.get("stop_sequences", merged_kwargs.get("stop"))
            if stop_sequences:
                inference_config["stopSequences"] = stop_sequences if isinstance(stop_sequences, list) else [stop_sequences]

            # Use Converse API with tools
            converse_kwargs = {"modelId": merged_kwargs.get("model_id", self.provider.model_id), "messages": self.converse_messages, "inferenceConfig": inference_config, "system": self.system_messages}

            # Add tool configuration if present
            if tool_config:
                converse_kwargs["toolConfig"] = tool_config

            # Merge additionalModelRequestFields from provider defaults and per-call overrides
            additional_fields: dict = dict(self.provider.additional_model_request_fields)
            if "additionalModelRequestFields" in merged_kwargs:
                override_fields = merged_kwargs["additionalModelRequestFields"]
                if isinstance(override_fields, dict):
                    additional_fields.update(override_fields)

            # Add thinking configuration if enabled (merge, do not overwrite user-provided reasoning_config)
            if self.provider.thinking_enabled:
                if "reasoning_config" not in additional_fields:
                    additional_fields["reasoning_config"] = {"type": "enabled", "budget_tokens": self.provider.thinking_budget_tokens}
                inference_config["maxTokens"] += self.provider.thinking_budget_tokens

            if additional_fields:
                converse_kwargs["additionalModelRequestFields"] = additional_fields

            async with client as bedrock_client:
                response = await bedrock_client.converse(**converse_kwargs)

            # Store the full response in native format
            output = response["output"]["message"]
            self.converse_messages.append(output)

            # Extract response and format for return (convert to standard format)
            result = {"content": "", "reasoning_content": "", "tool_calls": [], "finish_reason": response["stopReason"]}

            # Process content
            for content_block in output["content"]:
                if "text" in content_block:
                    result["content"] += content_block["text"]
                elif "toolUse" in content_block:
                    # Convert Bedrock tool use to OpenAI format
                    tool_use = content_block["toolUse"]
                    result["tool_calls"].append({"id": tool_use["toolUseId"], "function": {"name": tool_use["name"], "arguments": json.dumps(tool_use["input"])}, "type": "function"})
                elif "thinking" in content_block and self.provider.thinking_enabled:
                    # Handle thinking content block as per Anthropic docs
                    thinking_data = content_block["thinking"]
                    if isinstance(thinking_data, dict):
                        result["reasoning_content"] = thinking_data.get("thinking", "")
                    else:
                        result["reasoning_content"] = str(thinking_data)

                        # Log reasoning content if present
            if result["reasoning_content"]:
                self.logger.debug(f"Bedrock thinking content: {result['reasoning_content']}")

            self.logger.debug("🟠 Bedrock TOOLS RESPONSE:\n%s", json_dumps_truncated(result, indent=2))
            self.logger.debug(f"Bedrock function calling response: {result}")

        return result

    def to_json(self) -> dict[str, Any]:
        """Export Bedrock session conversation history to JSON format"""
        return {"provider": "bedrock", "system_prompt": self.system_prompt, "system_messages": self.system_messages, "conversation_history": self.converse_messages, "total_messages": len(self.converse_messages)}


class BedrockProvider(LLMProvider):
    """AWS Bedrock provider using official boto3 SDK with Converse API"""

    def __init__(self, config: DictConfig, semaphore: asyncio.Semaphore):
        super().__init__(config, semaphore)
        self.region = config.get("region", "us-east-1")
        self.model_id = config.get("model_id", "anthropic.claude-3-sonnet-20240229-v1:0")
        self.session = None
        self.client = None

        # Thinking configuration
        self.thinking_enabled = config.get("thinking", {}).get("enabled", False)
        self.thinking_budget_tokens = config.get("thinking", {}).get("budget_tokens", 32000)
        self.additional_model_request_fields = config.get("additionalModelRequestFields", {})

    async def _get_client(self):
        """Get or create Bedrock client"""
        # Always create a fresh session and client to avoid reuse issues
        self.session = aioboto3.Session()
        self.client = self.session.client("bedrock-runtime", region_name=self.region)
        return self.client

    async def complete(self, messages: list[dict[str, str]], **kwargs) -> str:
        """Generate completion using Bedrock Converse API"""
        async with self.semaphore:
            self.logger.debug("🟠 Bedrock INPUT MESSAGES:\n%s", json_dumps_truncated(messages, indent=2))

            client = await self._get_client()

            # Convert OpenAI messages to Converse API format
            converse_messages = []
            system_messages = []

            for msg in messages:
                if msg["role"] == "system":
                    system_messages.append({"text": msg["content"]})
                else:
                    converse_messages.append({"role": msg["role"], "content": [{"text": msg["content"]}]})

            # Prepare inference config
            inference_config = {
                "maxTokens": kwargs.get("max_tokens"),
                "temperature": kwargs.get("temperature"),
                "topP": kwargs.get("top_p"),
            }
            # drop None values
            inference_config = {k: v for k, v in inference_config.items() if v is not None}

            # Add stop sequences if provided
            stop_sequences = kwargs.get("stop_sequences", kwargs.get("stop"))
            if stop_sequences:
                inference_config["stopSequences"] = stop_sequences if isinstance(stop_sequences, list) else [stop_sequences]

            # Use Converse API
            converse_kwargs = {"modelId": kwargs.get("model_id", self.model_id), "messages": converse_messages, "inferenceConfig": inference_config}

            # Add system messages if present
            if system_messages:
                converse_kwargs["system"] = system_messages

            # Merge additionalModelRequestFields from provider defaults and per-call overrides
            additional_fields: dict = dict(self.additional_model_request_fields)
            if "additionalModelRequestFields" in kwargs:
                override_fields = kwargs["additionalModelRequestFields"]
                if isinstance(override_fields, dict):
                    additional_fields.update(override_fields)

            # Add thinking configuration if enabled (merge, do not overwrite user-provided reasoning_config)
            if self.thinking_enabled:
                if "reasoning_config" not in additional_fields:
                    additional_fields["reasoning_config"] = {"type": "enabled", "budget_tokens": self.thinking_budget_tokens}
                inference_config["maxTokens"] += self.thinking_budget_tokens

            if additional_fields:
                converse_kwargs["additionalModelRequestFields"] = additional_fields

            async with client as bedrock_client:
                response = await bedrock_client.converse(**converse_kwargs)

            # Extract content string
            output = response["output"]["message"]
            content = ""
            # Process content blocks
            if "content" in output:
                for content_block in output["content"]:
                    if "text" in content_block:
                        # if content_block["type"] == "text":
                        content += content_block["text"]

            # Log thinking content if present (when thinking is enabled)
            if self.thinking_enabled and "reasoning" in output:
                reasoning_content = output["reasoning"].get("content", "")
                self.logger.debug(f"Bedrock thinking content: {reasoning_content}")

            self.logger.debug("🟠 Bedrock RESPONSE:\n%s", content)
            self.logger.debug(f"Bedrock response: {content}")
            return content

    async def complete_with_tools(self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs) -> dict[str, Any]:
        """Generate completion with function calling using Bedrock Converse API"""
        async with self.semaphore:
            self.logger.debug("🟠 Bedrock TOOLS INPUT MESSAGES:\n%s", json_dumps_truncated(messages, indent=2))

            client = await self._get_client()

            # Convert OpenAI messages to Converse API format
            converse_messages = []
            system_messages = []

            for msg in messages:
                if msg["role"] == "system":
                    system_messages.append({"text": msg["content"]})
                elif msg["role"] == "tool":
                    # Handle tool response messages - format as toolResult
                    tool_result_content = msg["content"]
                    tool_call_id = msg["tool_call_id"]

                    # Parse the tool result content
                    try:
                        result_data = json.loads(tool_result_content)
                        content = [{"json": result_data}]
                    except (json.JSONDecodeError, TypeError):
                        # If not valid JSON, treat as text
                        content = [{"text": str(tool_result_content)}]

                    converse_messages.append({"role": "user", "content": [{"toolResult": {"toolUseId": tool_call_id, "content": content}}]})
                elif msg["role"] == "assistant":
                    # Handle assistant messages (with or without tool calls)
                    content_blocks = []

                    # When thinking is enabled, assistant messages MUST start with a thinking block
                    if self.thinking_enabled:
                        thinking_content = msg["reasoning_content"]
                        content_blocks.append({"thinking": {"content": thinking_content}})

                    # Add text content if present
                    if msg.get("content"):
                        content_blocks.append({"text": msg["content"]})

                    # Convert tool calls to Bedrock toolUse format if present
                    if msg.get("tool_calls"):
                        for tool_call in msg["tool_calls"]:
                            tool_use_block = {"toolUse": {"toolUseId": tool_call["id"], "name": tool_call["function"]["name"], "input": json.loads(tool_call["function"]["arguments"])}}
                            content_blocks.append(tool_use_block)

                    converse_messages.append({"role": "assistant", "content": content_blocks})
                else:
                    # Regular user messages
                    converse_messages.append({"role": msg["role"], "content": [{"text": msg["content"]}]})

            # Convert OpenAI tools format to Bedrock format
            tool_config = None
            if tools:
                tool_config = {"tools": []}

                for tool in tools:
                    if tool["type"] == "function":
                        func = tool["function"]
                        bedrock_tool = {"toolSpec": {"name": func["name"], "description": func["description"], "inputSchema": {"json": func["parameters"]}}}
                        tool_config["tools"].append(bedrock_tool)

            # Prepare inference config
            inference_config = {
                "maxTokens": kwargs.get("max_tokens"),
                "temperature": kwargs.get("temperature"),
                "topP": kwargs.get("top_p"),
            }
            # drop None values
            inference_config = {k: v for k, v in inference_config.items() if v is not None}

            # Add stop sequences if provided
            stop_sequences = kwargs.get("stop_sequences", kwargs.get("stop"))
            if stop_sequences:
                inference_config["stopSequences"] = stop_sequences if isinstance(stop_sequences, list) else [stop_sequences]

            # Use Converse API with tools
            converse_kwargs = {"modelId": kwargs.get("model_id", self.model_id), "messages": converse_messages, "inferenceConfig": inference_config}

            # Add system messages if present
            if system_messages:
                converse_kwargs["system"] = system_messages

            # Add tool configuration if present
            if tool_config:
                converse_kwargs["toolConfig"] = tool_config

            # Merge additionalModelRequestFields from provider defaults and per-call overrides
            additional_fields: dict = dict(self.additional_model_request_fields)
            if "additionalModelRequestFields" in kwargs:
                override_fields = kwargs["additionalModelRequestFields"]
                if isinstance(override_fields, dict):
                    additional_fields.update(override_fields)

            # Add thinking configuration if enabled (merge, do not overwrite user-provided reasoning_config)
            if self.thinking_enabled:
                if "reasoning_config" not in additional_fields:
                    additional_fields["reasoning_config"] = {"type": "enabled", "budget_tokens": self.thinking_budget_tokens}
                inference_config["maxTokens"] += self.thinking_budget_tokens

            if additional_fields:
                converse_kwargs["additionalModelRequestFields"] = additional_fields

            async with client as bedrock_client:
                response = await bedrock_client.converse(**converse_kwargs)

            # Extract response and format consistently
            output = response["output"]["message"]
            result = {"content": "", "tool_calls": [], "finish_reason": response.get("stopReason", "stop")}

            # Process content
            if "content" in output:
                for content_block in output["content"]:
                    if "text" in content_block:
                        result["content"] += content_block["text"]
                    elif "toolUse" in content_block:
                        # Convert Bedrock tool use to OpenAI format
                        tool_use = content_block["toolUse"]
                        result["tool_calls"].append({"id": tool_use["toolUseId"], "function": {"name": tool_use["name"], "arguments": json.dumps(tool_use["input"])}, "type": "function"})
                    elif "thinking" in content_block and self.thinking_enabled:
                        # Extract thinking content from content blocks
                        thinking_content = content_block["thinking"].get("content", "")
                        result["reasoning_content"] = thinking_content

            # Also check for reasoning in output root (legacy support)
            if self.thinking_enabled and "reasoning" in output:
                reasoning_content = output["reasoning"].get("content", "")
                if "reasoning_content" not in result:  # Don't overwrite if already set from content blocks
                    result["reasoning_content"] = reasoning_content
                self.logger.debug(f"Bedrock thinking content: {reasoning_content}")

            self.logger.debug("🟠 Bedrock TOOLS RESPONSE:\n%s", json_dumps_truncated(result, indent=2))
            self.logger.debug(f"Bedrock function calling response: {result}")
            return result

    async def create_session(self, system_prompt: str) -> LLMSession:
        """Create a new conversation session"""
        # This method should not be called directly on providers
        raise NotImplementedError("Use LLMClient.create_session() instead")

    async def close(self):
        """Close Bedrock client"""
        # aioboto3 session cleanup is handled automatically
        pass


class LLMClient:
    """Main LLM client with provider abstraction and concurrency control"""

    def __init__(self, config: DictConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)

        # Set up concurrency control
        max_concurrent = config.get("max_concurrent", 5)
        self.semaphore = asyncio.Semaphore(max_concurrent)

        # Store generation defaults from config
        self.generation_defaults = config.get("generation", {})

        # Initialize provider
        provider_name = config.get("provider", "openai").lower()
        if provider_name == "openai":
            self.provider = OpenAIProvider(config.openai, self.semaphore)
        elif provider_name == "bedrock":
            self.provider = BedrockProvider(config.bedrock, self.semaphore)
        else:
            raise ValueError(f"Unsupported LLM provider: {provider_name}")

        self.logger.info(f"Initialized LLM client with {provider_name} provider, max_concurrent={max_concurrent}")

    async def complete(self, messages: list[dict[str, str]], **kwargs) -> str:
        """Generate completion from OpenAI format messages, return content string"""
        # Merge config defaults with provided kwargs
        merged_kwargs = dict(self.generation_defaults)
        merged_kwargs.update(kwargs)

        # Convert Hydra ListConfig to regular list for JSON serialization
        if "stop" in merged_kwargs and merged_kwargs["stop"] is not None:
            merged_kwargs["stop"] = list(merged_kwargs["stop"]) if merged_kwargs["stop"] else None

        return await self.provider.complete(messages, **merged_kwargs)

    async def complete_with_tools(self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs) -> dict[str, Any]:
        """Generate completion with function calling support, return structured response"""
        # Merge config defaults with provided kwargs
        merged_kwargs = dict(self.generation_defaults)
        merged_kwargs.update(kwargs)

        # Convert Hydra ListConfig to regular list for JSON serialization
        if "stop" in merged_kwargs and merged_kwargs["stop"] is not None:
            merged_kwargs["stop"] = list(merged_kwargs["stop"]) if merged_kwargs["stop"] else None

        return await self.provider.complete_with_tools(messages, tools, **merged_kwargs)

    async def complete_many(self, requests: list[dict[str, Any]]) -> list[str]:
        """Generate multiple completions concurrently, return list of content strings"""
        tasks = []
        for request in requests:
            messages = request.get("messages")
            if not messages:
                raise ValueError("Each request must have 'messages' field")
            kwargs = {k: v for k, v in request.items() if k != "messages"}
            task = self.complete(messages, **kwargs)
            tasks.append(task)

        return await asyncio.gather(*tasks)

    async def create_session(self, system_prompt: str) -> LLMSession:
        """Create a new conversation session with system prompt"""
        if isinstance(self.provider, OpenAIProvider):
            return OpenAISession(self.provider, self.generation_defaults, system_prompt)
        elif isinstance(self.provider, BedrockProvider):
            return BedrockSession(self.provider, self.generation_defaults, system_prompt)
        else:
            raise ValueError(f"Unsupported provider type: {type(self.provider)}")

    async def close(self):
        """Clean up resources"""
        await self.provider.close()

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        _ = exc_type, exc_val, exc_tb  # Unused parameters
        await self.close()


# Global singleton instance
_llm_client_instance = None


def get_llm_client() -> LLMClient:
    """Get singleton LLM client instance from global config store"""
    global _llm_client_instance

    if _llm_client_instance is None:
        try:
            cfg = ConfigStore.get()
            llm_config = cfg.llm
            _llm_client_instance = LLMClient(llm_config)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize LLM client from config store: {e}") from e

    return _llm_client_instance


def reset_llm_client() -> None:
    """Reset the singleton LLM client instance (useful for testing)"""
    global _llm_client_instance
    _llm_client_instance = None
    ConfigStore.reset()
