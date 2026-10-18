"""Request settings for supported Chat Completions model contracts."""


def chat_generation(model: str, max_tokens: int) -> dict:
    # Luna's reasoning budget uses max_completion_tokens. Disabling reasoning keeps
    # bounded extraction budgets for visible output and supports structured tool callers.
    if model.rsplit("/", 1)[-1].startswith("gpt-6-luna"):
        return {"max_completion_tokens": max_tokens, "reasoning_effort": "none"}
    return {"temperature": 0, "max_tokens": max_tokens}
