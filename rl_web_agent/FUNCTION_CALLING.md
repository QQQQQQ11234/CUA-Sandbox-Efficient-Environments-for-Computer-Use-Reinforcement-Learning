# Function Calling with RL Web Agent

This document describes the new function calling functionality implemented in the RL Web Agent project. The implementation is based on the [Qwen function calling documentation](https://qwen.readthedocs.io/en/latest/framework/function_call.html#id2) and provides a more flexible approach to web automation.

## Overview

Function calling allows LLMs to interact with external tools in a structured way. Instead of the previous chain-of-thought approach where the LLM generates actions that are then parsed, the new tool-enabled agent gives the LLM direct access to browser automation functions.

### Key Components

1. **ToolWebAgent** (`rl_web_agent/tool_agent.py`) - Main agent class with function calling support
2. **Extended LLM Providers** (`rl_web_agent/llm.py`) - Updated to support tool calling
3. **Browser Tool** - Single streamlined tool for web interaction
4. **Tool System Prompt** (`rl_web_agent/prompts/tool_system.txt`) - Optimized prompt for tool usage

## Available Tool

### step_browser
Executes browser actions and returns observations.

**Parameters:**
- `action` (required): Dictionary containing browser action with 'action' field and parameters
- `reasoning` (optional): Explanation of why the action is being taken

**Example Actions:**
```python
{"action": "click", "target": "login_button"}
{"action": "type", "target": "username", "text": "john_doe", "enter": True}
{"action": "goto_url", "url": "https://example.com"}
{"action": "terminate", "answer": "Product costs $29.99"}
```

**Returns:**
- `success`: Boolean indicating if action succeeded
- `observation`: Formatted observation of the page state
- `terminated`: Whether the task is complete
- `score`: Current task score
- `error`: Any error message

## Usage Example

```python
import asyncio
from omegaconf import DictConfig
from rl_web_agent.env import WebAgentEnv
from rl_web_agent.tool_agent import create_tool_web_agent

async def example_usage():
    # Setup configuration (assuming Hydra config)
    cfg = DictConfig({
        "environment": {"browser": {"headless": False}},
        "llm": {"provider": "openai", "openai": {"api_key": "your-key"}},
        "agent": {"max_steps": 20}
    })

    # Create environment and agent
    env = WebAgentEnv(cfg.environment)
    agent = await create_tool_web_agent(cfg.llm, cfg.agent)

    try:
        # Setup environment with task
        task_config = {
            "start_url": "https://example.com",
            "intent": "Find the price of a product"
        }
        await env.setup(task_config)

        # Run task with tools
        result = await agent.run_task_with_tools(
            env,
            objective="Find the price of a specific product",
            max_steps=15
        )

        print(f"Task completed: {result['success']}")
        print(f"Answer: {result['answer']}")
        print(f"Score: {result['score']}")

    finally:
        await agent.close()
        await env.close()

# Run the example
asyncio.run(example_usage())
```

## Running the Example

Use the provided example script:

```bash
# Run the tool agent example
python -m rl_web_agent.tool_example

# With configuration overrides
python -m rl_web_agent.tool_example llm.provider=openai environment.browser.headless=false
```

## Advantages Over Chain-of-Thought Approach

1. **Simplicity**: Single tool interface reduces complexity while maintaining full browser functionality
2. **Direct Action**: LLM directly specifies actions as objects rather than parsed strings
3. **Error Handling**: Each tool call returns structured results with error information
4. **Modularity**: Tool can be easily extended without changing prompts
5. **Debugging**: Clear visibility into actions taken and their results

## LLM Provider Support

### OpenAI
Full function calling support using the official OpenAI API:
- Uses native `tools` parameter
- Supports `tool_choice` for forcing specific tools
- Returns structured responses with tool calls

### AWS Bedrock
Function calling support using the Converse API:
- Converts OpenAI tool format to Bedrock `toolConfig`
- Handles tool use responses in Bedrock format
- Converts back to OpenAI-compatible format for consistency

## Implementation Details

### Tool Definition Format
The tool is defined using OpenAI's function calling schema:

```python
{
    "type": "function",
    "function": {
        "name": "step_browser",
        "description": "Execute an action in the web browser environment and get the resulting observation",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "object",
                    "description": "Browser action to execute with 'action' field and required parameters",
                    "properties": {
                        "action": {"type": "string", "description": "Type of action to perform"}
                    },
                    "required": ["action"]
                },
                "reasoning": {"type": "string", "description": "Brief explanation of why this action is being taken"}
            },
            "required": ["action"]
        }
    }
}
```

### Conversation Flow
1. System prompt establishes the tool and strategy
2. User provides initial page state and objective
3. LLM responds with step_browser tool calls
4. Tool results (observations) are added to conversation history
5. Process continues until terminate action is called

### Error Recovery
- Malformed actions are caught and returned as tool errors
- Browser errors (e.g., element not found) are captured
- LLM can see error messages and adjust strategy
- Graceful degradation for unsupported operations

## Configuration

Add tool-specific configuration to your `config.yaml`:

```yaml
agent:
  max_steps: 25
  tool_timeout: 30  # Seconds to wait for tool execution

llm:
  provider: "openai"
  generation:
    temperature: 0.1
    max_tokens: 1500
  openai:
    model: "gpt-4"
    api_key: "${OPENAI_API_KEY}"
```

## Extending the Browser Tool

The single step_browser tool handles all browser interactions. To extend functionality:

1. Add new action types to the environment's `step()` method
2. Update the system prompt to document new actions
3. The tool automatically supports any action the environment can handle

**Example new action:**
```python
# In the environment, add support for:
{"action": "scroll", "direction": "down", "amount": 3}

# The tool will automatically work with it
await agent.step_browser(
    action={"action": "scroll", "direction": "down", "amount": 3},
    reasoning="Need to scroll to see more products"
)
```

## Best Practices

1. **Start with Initial Action**: Take a step_browser action to see the current page state
2. **Provide Reasoning**: Include reasoning in step_browser calls for better debugging
3. **Handle Errors**: Check tool results for errors and adjust strategy
4. **Be Specific**: Use precise action descriptions and target elements
5. **Terminate Explicitly**: Always use terminate action for clear task completion

This simplified function calling approach provides a streamlined and efficient foundation for web automation tasks.
