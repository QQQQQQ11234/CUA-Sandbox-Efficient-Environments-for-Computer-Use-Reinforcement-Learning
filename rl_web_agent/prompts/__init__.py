"""Prompt loading utilities"""

from pathlib import Path


def load_prompt(prompt_name: str) -> str:
    """Load a prompt from the prompts directory

    Args:
        prompt_name: Name of the prompt file (without .txt extension)

    Returns:
        str: The prompt content

    Raises:
        FileNotFoundError: If the prompt file doesn't exist
    """
    prompt_path = Path(__file__).parent / f"{prompt_name}.txt"

    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    with open(prompt_path, encoding="utf-8") as f:
        return f.read().strip()
