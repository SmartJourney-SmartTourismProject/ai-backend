# tests/test_llm.py
# app/core/llm.py's get_llm() - provider chain construction and ordering.
# No real network calls: inspects the constructed RunnableWithFallbacks'
# own .runnable/.fallbacks attributes rather than invoking anything.

import pytest

from app.config.settings import settings
from app.core.llm import get_llm


@pytest.fixture(autouse=True)
def _clear_llm_cache():
    """get_llm is @lru_cache'd per purpose - a test that monkeypatches
    settings and expects a fresh construction needs a clean cache, both
    before and after (so later test files don't inherit a stale build)."""
    get_llm.cache_clear()
    yield
    get_llm.cache_clear()


def _spec_of(model) -> str:
    """Reconstructs the "<provider>:<model>" spec a constructed chat model
    came from, for assertions - reading the real client attributes rather
    than assuming construction order."""
    cls_name = type(model).__name__
    if cls_name == "ChatGoogleGenerativeAI":
        return f"gemini:{model.model.replace('models/', '')}"
    if cls_name == "ChatGroq":
        return f"groq:{model.model_name}"
    raise AssertionError(f"unexpected model class {cls_name}")


def _chain_specs(purpose: str) -> list[str]:
    llm = get_llm(purpose)
    if hasattr(llm, "runnable"):
        return [_spec_of(llm.runnable), *(_spec_of(f) for f in llm.fallbacks)]
    return [_spec_of(llm)]   # no fallbacks configured - a single model


def test_recommend_purpose_tries_groq_first(monkeypatch):
    # Live-confirmed 2026-09-03 (see settings.py's own comment): Gemini
    # reliably fails RecommendationOutput's schema regardless of API key -
    # reproduced across two different Gemini accounts. Groq succeeds a real
    # fraction of the time on the same payload, so it goes first here.
    monkeypatch.setattr(settings, "llm_provider_chain_groq_first_purposes", "recommend,plan")
    specs = _chain_specs("recommend")
    assert specs[0].startswith("groq:")


def test_plan_purpose_tries_groq_first(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider_chain_groq_first_purposes", "recommend,plan")
    specs = _chain_specs("plan")
    assert specs[0].startswith("groq:")


def test_groq_first_reorder_preserves_relative_order_within_each_group(monkeypatch):
    # The setting's own default is now "" (empty) - Gemini's schema-400 that
    # justified Groq-first for recommend/plan was fixed at the schema level
    # instead (app/models/schemas.py, see settings.py's own comment), so this
    # test sets the setting explicitly rather than relying on the old
    # default, to keep testing the reorder MECHANISM independent of that.
    monkeypatch.setattr(settings, "llm_provider_chain_groq_first_purposes", "recommend,plan")
    specs = _chain_specs("recommend")
    groq_specs = [s for s in specs if s.startswith("groq:")]
    other_specs = [s for s in specs if not s.startswith("groq:")]
    # Exactly the groq entries, moved to the front, with everything else's
    # relative order (gemini-3.5-flash-lite before gemini-3.6-flash) intact.
    assert specs == [*groq_specs, *other_specs]
    assert other_specs == ["gemini:gemini-3.5-flash-lite", "gemini:gemini-3.6-flash"]


def test_slots_and_respond_purposes_are_unaffected_by_the_groq_first_setting():
    for purpose in ("slots", "respond"):
        specs = _chain_specs(purpose)
        assert specs[0].startswith("gemini:")


def test_empty_groq_first_setting_disables_reordering_entirely(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider_chain_groq_first_purposes", "")
    specs = _chain_specs("recommend")
    assert specs[0].startswith("gemini:")
