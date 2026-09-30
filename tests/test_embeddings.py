# tests/test_embeddings.py
# No real network - the Gemini/OpenAI SDKs are patched at the module level
# app/rag/embeddings.py imports them from (lazy imports inside the builder
# functions, so patching the real module attribute works).

from unittest.mock import MagicMock, patch

import pytest

from app.rag import embeddings


def _set_chain(monkeypatch, chain: str):
    monkeypatch.setattr(embeddings.settings, "embedding_provider_chain", chain)


def test_embed_texts_uses_gemini_when_configured(monkeypatch):
    _set_chain(monkeypatch, "gemini:gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "fake-key")
    monkeypatch.setattr(embeddings.settings, "embedding_dim", 768)

    fake_response = MagicMock()
    fake_response.embeddings = [MagicMock(values=[0.1] * 768), MagicMock(values=[0.2] * 768)]
    fake_client = MagicMock()
    fake_client.models.embed_content.return_value = fake_response

    with patch("google.genai.Client", return_value=fake_client) as ctor:
        result = embeddings.embed_texts(["a", "b"], "RETRIEVAL_DOCUMENT")

    assert result == [[0.1] * 768, [0.2] * 768]
    ctor.assert_called_once_with(api_key="fake-key")
    call = fake_client.models.embed_content.call_args
    assert call.kwargs["model"] == "gemini-embedding-001"
    assert call.kwargs["contents"] == ["a", "b"]
    assert call.kwargs["config"].task_type == "RETRIEVAL_DOCUMENT"
    assert call.kwargs["config"].output_dimensionality == 768


def test_embed_texts_batches_over_the_gemini_limit(monkeypatch):
    _set_chain(monkeypatch, "gemini:gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "fake-key")
    monkeypatch.setattr(embeddings, "_GEMINI_BATCH_SIZE", 2)

    def fake_embed_content(model, contents, config):
        resp = MagicMock()
        resp.embeddings = [MagicMock(values=[float(len(c))]) for c in contents]
        return resp

    fake_client = MagicMock()
    fake_client.models.embed_content.side_effect = fake_embed_content

    with patch("google.genai.Client", return_value=fake_client):
        result = embeddings.embed_texts(["a", "bb", "ccc", "dddd", "e"], "RETRIEVAL_DOCUMENT")

    assert fake_client.models.embed_content.call_count == 3   # batches of 2,2,1
    assert result == [[1.0], [2.0], [3.0], [4.0], [1.0]]


def test_gemini_retries_on_429_and_honors_the_suggested_delay(monkeypatch):
    from google.genai.errors import ClientError

    _set_chain(monkeypatch, "gemini:gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "fake-key")
    monkeypatch.setattr(embeddings, "_GEMINI_MAX_RETRIES", 2)
    sleeps = []
    monkeypatch.setattr(embeddings.time, "sleep", lambda s: sleeps.append(s))

    # Shape captured live 2026-09-30 from a real 429 - the free-text
    # "Please retry in 15.9s." sentence is NOT what's parsed; the
    # structured retryDelay field in `details` is.
    rate_limited = ClientError(429, {"error": {
        "code": 429, "message": "Quota exceeded. Please retry in 15.9s.", "status": "RESOURCE_EXHAUSTED",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12.5s"}],
    }})
    fake_client = MagicMock()
    ok_response = MagicMock()
    ok_response.embeddings = [MagicMock(values=[0.1])]
    fake_client.models.embed_content.side_effect = [rate_limited, ok_response]

    with patch("google.genai.Client", return_value=fake_client):
        result = embeddings.embed_texts(["x"], "RETRIEVAL_DOCUMENT")

    assert result == [[0.1]]
    assert sleeps == [12.5]   # parsed from the structured retryDelay, not the fixed default
    assert fake_client.models.embed_content.call_count == 2


def test_gemini_gives_up_after_max_retries_and_falls_through_the_chain(monkeypatch):
    from google.genai.errors import ClientError

    _set_chain(monkeypatch, "gemini:gemini-embedding-001,openai:text-embedding-3-small")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "fake-key")
    monkeypatch.setattr(embeddings.settings, "openai_api_key", "fake-key")
    monkeypatch.setattr(embeddings, "_GEMINI_MAX_RETRIES", 1)
    monkeypatch.setattr(embeddings.time, "sleep", lambda s: None)

    rate_limited = ClientError(429, {"error": {"message": "quota exceeded"}})
    fake_gemini_client = MagicMock()
    fake_gemini_client.models.embed_content.side_effect = rate_limited

    fake_openai_module = MagicMock()
    fake_response = MagicMock()
    fake_response.data = [MagicMock(index=0, embedding=[0.9])]
    fake_openai_module.OpenAI.return_value.embeddings.create.return_value = fake_response

    with patch("google.genai.Client", return_value=fake_gemini_client), \
         patch.dict("sys.modules", {"openai": fake_openai_module}):
        result = embeddings.embed_texts(["x"], "RETRIEVAL_DOCUMENT")

    assert result == [[0.9]]
    # 1 initial attempt + 1 retry = 2 calls, then gives up on this provider.
    assert fake_gemini_client.models.embed_content.call_count == 2


def test_gemini_non_429_client_error_is_not_retried(monkeypatch):
    from google.genai.errors import ClientError

    _set_chain(monkeypatch, "gemini:gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "fake-key")
    fake_client = MagicMock()
    fake_client.models.embed_content.side_effect = ClientError(400, {"error": {"message": "bad request"}})

    with patch("google.genai.Client", return_value=fake_client):
        with pytest.raises(embeddings.EmbeddingUnavailable):
            embeddings.embed_texts(["x"], "RETRIEVAL_DOCUMENT")
    assert fake_client.models.embed_content.call_count == 1   # no retry on a non-429 error


def test_openai_branch_is_tried_when_configured_and_returns_in_index_order(monkeypatch):
    _set_chain(monkeypatch, "openai:text-embedding-3-small")
    monkeypatch.setattr(embeddings.settings, "openai_api_key", "fake-key")
    monkeypatch.setattr(embeddings.settings, "embedding_dim", 768)

    fake_openai_module = MagicMock()
    fake_response = MagicMock()
    # Deliberately out of order - the real API guarantees order via `index`,
    # not array position, so the code must sort rather than trust order.
    fake_response.data = [
        MagicMock(index=1, embedding=[0.2]),
        MagicMock(index=0, embedding=[0.1]),
    ]
    fake_openai_module.OpenAI.return_value.embeddings.create.return_value = fake_response

    with patch.dict("sys.modules", {"openai": fake_openai_module}):
        result = embeddings.embed_texts(["x", "y"], "RETRIEVAL_DOCUMENT")

    assert result == [[0.1], [0.2]]
    fake_openai_module.OpenAI.assert_called_once_with(api_key="fake-key")


def test_openai_branch_without_the_package_installed_raises_embedding_unavailable(monkeypatch):
    _set_chain(monkeypatch, "openai:text-embedding-3-small")
    monkeypatch.setattr(embeddings.settings, "openai_api_key", "fake-key")

    # The per-provider ImportError is caught inside embed_texts' failover
    # loop and re-raised as the generic "no provider succeeded" error, with
    # the original chained as __cause__ - that's where "pip install openai"
    # actually surfaces (in logs / a debugger), not the top-level message.
    with patch.dict("sys.modules", {"openai": None}):
        with pytest.raises(embeddings.EmbeddingUnavailable) as exc_info:
            embeddings.embed_texts(["x"], "RETRIEVAL_DOCUMENT")
    assert "pip install openai" in str(exc_info.value.__cause__)


def test_falls_through_to_the_next_provider_on_failure(monkeypatch):
    _set_chain(monkeypatch, "gemini:gemini-embedding-001,openai:text-embedding-3-small")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "fake-key")
    monkeypatch.setattr(embeddings.settings, "openai_api_key", "fake-key")

    with patch("google.genai.Client", side_effect=RuntimeError("gemini down")):
        fake_openai_module = MagicMock()
        fake_response = MagicMock()
        fake_response.data = [MagicMock(index=0, embedding=[0.5])]
        fake_openai_module.OpenAI.return_value.embeddings.create.return_value = fake_response
        with patch.dict("sys.modules", {"openai": fake_openai_module}):
            result = embeddings.embed_texts(["x"], "RETRIEVAL_DOCUMENT")

    assert result == [[0.5]]


def test_provider_with_no_key_is_skipped(monkeypatch):
    _set_chain(monkeypatch, "gemini:gemini-embedding-001,openai:text-embedding-3-small")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "")   # no key
    monkeypatch.setattr(embeddings.settings, "openai_api_key", "fake-key")

    fake_openai_module = MagicMock()
    fake_response = MagicMock()
    fake_response.data = [MagicMock(index=0, embedding=[0.9])]
    fake_openai_module.OpenAI.return_value.embeddings.create.return_value = fake_response

    with patch.dict("sys.modules", {"openai": fake_openai_module}):
        result = embeddings.embed_texts(["x"], "RETRIEVAL_DOCUMENT")

    assert result == [[0.9]]


def test_no_provider_configured_raises_embedding_unavailable(monkeypatch):
    _set_chain(monkeypatch, "gemini:gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "")

    with pytest.raises(embeddings.EmbeddingUnavailable):
        embeddings.embed_texts(["x"], "RETRIEVAL_DOCUMENT")


def test_empty_input_short_circuits_with_no_provider_call(monkeypatch):
    _set_chain(monkeypatch, "gemini:gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "")   # would raise if called
    assert embeddings.embed_texts([], "RETRIEVAL_DOCUMENT") == []


def test_active_embedding_model_reflects_the_first_keyed_provider(monkeypatch):
    _set_chain(monkeypatch, "gemini:gemini-embedding-001,openai:text-embedding-3-small")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "")
    monkeypatch.setattr(embeddings.settings, "openai_api_key", "fake-key")
    assert embeddings.active_embedding_model() == "text-embedding-3-small"


def test_active_embedding_model_none_when_nothing_configured(monkeypatch):
    _set_chain(monkeypatch, "gemini:gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "")
    assert embeddings.active_embedding_model() is None


def test_embed_query_returns_a_single_vector(monkeypatch):
    _set_chain(monkeypatch, "gemini:gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "gemini_api_key", "fake-key")

    fake_response = MagicMock()
    fake_response.embeddings = [MagicMock(values=[0.3, 0.4])]
    fake_client = MagicMock()
    fake_client.models.embed_content.return_value = fake_response

    with patch("google.genai.Client", return_value=fake_client):
        assert embeddings.embed_query("how much is the ETA visa?") == [0.3, 0.4]
    call = fake_client.models.embed_content.call_args
    assert call.kwargs["config"].task_type == "RETRIEVAL_QUERY"
