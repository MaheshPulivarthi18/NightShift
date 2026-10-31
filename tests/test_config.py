import os

from nightshift.config import AgentModel, model_id


def test_old_claude_names_become_openrouter_ids() -> None:
    assert model_id("claude-opus-5-5") == "anthropic/claude-opus-5.5"
    assert model_id("claude-haiku-5-5") == "anthropic/claude-haiku-5.5"
    assert model_id("claude-opus-5") == "anthropic/claude-opus-5"


def test_any_openrouter_id_is_kept() -> None:
    assert model_id("deepseek/deepseek-v4.1-flash") == "deepseek/deepseek-v4.1-flash"
    assert AgentModel(model="claude-sonnet-5-5").model == "anthropic/claude-sonnet-5.5"
    assert AgentModel(model="openai/gpt-5.6-terra").model == "openai/gpt-5.6-terra"


def test_tests_never_use_the_dev_database() -> None:
    """conftest points every test, and the app under test, at their own database."""
    from nightshift.config import get_settings
    from nightshift.db import get_engine

    url = get_settings().database_url
    assert url.endswith("/nightshift_test") or os.environ.get("NIGHTSHIFT_TEST_DATABASE_URL") == url
    assert get_engine().url.database == url.rsplit("/", 1)[1]
