"""
Utility functions for the RL Web Agent project.
"""

import json
from typing import Any, Union


def format_llm_observation(observation: dict[str, Any]) -> str:
    """
    Format observation data for LLM consumption.

    Based on REPL observation formatting but without emojis, HTML formatting removed,
    tab URLs added, and evaluation/score removed.

    Args:
        observation: Current page observation data

    Returns:
        Formatted observation string for LLM
    """
    if observation is None:
        return "No observation data available"

    obs_parts = []

    # Basic page info from current tab
    active_tab = next((tab for tab in observation["tabs"] if tab["is_active"]), observation["tabs"][0])
    obs_parts.append(f"URL: {active_tab['url']}")
    obs_parts.append(f"Title: {active_tab['title']}")
    obs_parts.append("")

    # HTML - Raw HTML without formatting
    obs_parts.append("HTML:")
    obs_parts.append(observation["html"])
    obs_parts.append("")

    # Clickable elements
    obs_parts.append(f"CLICKABLE ELEMENTS ({len(observation['clickable_elements'])})")
    obs_parts.append("-" * 40)
    for i, elem_id in enumerate(observation["clickable_elements"], 1):
        obs_parts.append(f"  {i:2d}. {elem_id}")
    obs_parts.append("")

    # Input elements with detailed info
    obs_parts.append(f"INPUT ELEMENTS ({len(observation['input_elements'])})")
    obs_parts.append("-" * 40)
    for i, inp in enumerate(observation["input_elements"], 1):
        elem_id = inp["id"]
        elem_type = inp["type"]
        value = inp["value"]
        can_edit = inp["canEdit"]
        is_focused = inp["isFocused"]

        status_parts = []
        if is_focused:
            status_parts.append("focused")
        if not can_edit:
            status_parts.append("read-only")

        status = " " + " ".join(status_parts) if status_parts else ""

        obs_parts.append(f"  {i:2d}. {elem_id} [{elem_type}]{status}")
        if value:
            obs_parts.append(f"      Value: '{value}'")
    obs_parts.append("")

    # Tabs with URLs
    obs_parts.append(f"TABS ({len(observation['tabs'])})")
    obs_parts.append("-" * 40)
    for tab in observation["tabs"]:
        active = "ACTIVE" if tab["is_active"] else "inactive"
        tab_title = tab["title"]
        tab_url = tab["url"]
        tab_id = tab["id"]
        obs_parts.append(f"  {tab_id:2d}. {active} - {tab_title} - {tab_url}")
    obs_parts.append("")

    return "\n".join(obs_parts)


def json_dumps_truncated(obj: Any, max_str_length: int = 200, indent: Union[int, str, None] = None) -> str:
    """
    Custom JSON serializer that truncates long strings while preserving structure.

    Args:
        obj: Object to serialize
        max_str_length: Maximum length for string values before truncation
        indent: JSON indentation (same as json.dumps)

    Returns:
        JSON string with long strings truncated
    """

    def truncate_strings(item: Any) -> Any:
        """Recursively truncate strings in nested data structures."""
        if isinstance(item, str):
            if len(item) > max_str_length:
                return item[:max_str_length] + "... [TRUNCATED]"
            return item
        elif isinstance(item, dict):
            return {key: truncate_strings(value) for key, value in item.items()}
        elif isinstance(item, list | tuple):
            return [truncate_strings(element) for element in item]
        else:
            return item

    truncated_obj = truncate_strings(obj)
    return json.dumps(truncated_obj, indent=indent, ensure_ascii=False)
