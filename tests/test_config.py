from nightshift.config import openrouter_model


def test_openrouter_model_names() -> None:
    assert openrouter_model("claude-opus-5-5") == "anthropic/claude-opus-5.5"
    assert openrouter_model("claude-haiku-5-5") == "anthropic/claude-haiku-5.5"
    assert openrouter_model("claude-opus-5") == "anthropic/claude-opus-5"
