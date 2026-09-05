"""
Tests for the real-engine LLM client wiring (GeminiClient specifically).

These mock the underlying SDK's network call (google.genai.Client), so they run
offline and don't need a real GEMINI_API_KEY -- what they verify is that this
codebase builds the request correctly (forces a function call rather than free
text, passes the tool's JSON schema through untouched) and parses the response
correctly, not that Gemini itself behaves a certain way.

AnthropicClient isn't covered here since it long predates this file and every
other test in this suite already exercises the rest of the pipeline against
FakeLLMClient instead.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from llm_client import GeminiClient

SAMPLE_TOOL = {
    "name": "record_job_listings",
    "description": "Record extracted job listings.",
    "input_schema": {
        "type": "object",
        "properties": {"listings": {"type": "array"}},
        "required": ["listings"],
    },
}


def test_gemini_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiClient()


@patch("google.genai.Client")
def test_gemini_forced_tool_call_forces_a_function_and_parses_args(mock_client_cls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    fake_call = MagicMock()
    fake_call.args = {"listings": [{"title": "Product Manager"}]}
    fake_resp = MagicMock()
    fake_resp.function_calls = [fake_call]
    mock_instance = MagicMock()
    mock_instance.models.generate_content.return_value = fake_resp
    mock_client_cls.return_value = mock_instance

    client = GeminiClient()
    result = client.forced_tool_call(system="sys prompt", user="user prompt", tool=SAMPLE_TOOL)

    assert result == {"listings": [{"title": "Product Manager"}]}

    _, kwargs = mock_instance.models.generate_content.call_args
    assert kwargs["model"] == client.model
    assert kwargs["contents"] == "user prompt"

    config = kwargs["config"]
    assert config.system_instruction == "sys prompt"
    # Forced, not free-text: mode ANY with exactly the one declared function.
    assert str(config.tool_config.function_calling_config.mode).endswith("ANY")
    declared = config.tools[0].function_declarations[0]
    assert declared.name == "record_job_listings"
    assert declared.parameters_json_schema == SAMPLE_TOOL["input_schema"]


@patch("google.genai.Client")
def test_gemini_forced_tool_call_returns_empty_dict_if_model_returns_no_call(mock_client_cls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    fake_resp = MagicMock()
    fake_resp.function_calls = None
    mock_client_cls.return_value.models.generate_content.return_value = fake_resp

    client = GeminiClient()
    assert client.forced_tool_call(system="s", user="u", tool=SAMPLE_TOOL) == {}


def test_gemini_model_defaults_and_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    with patch("google.genai.Client"):
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        assert GeminiClient().model == "gemini-3.5-flash-lite"
        monkeypatch.setenv("GEMINI_MODEL", "gemini-custom-test-model")
        assert GeminiClient().model == "gemini-custom-test-model"


# --- Timeout + retry (added after two live incidents: a hard connection reset,
# then an indefinite hang, both against the real Gemini API with a client that had
# no timeout or retry configured at all) --------------------------------------

@patch("google.genai.Client")
def test_gemini_client_is_built_with_a_timeout_and_retry_options(mock_client_cls, monkeypatch):
    """A plain genai.Client(api_key=...) with no http_options can hang forever or
    crash outright on a network hiccup -- both actually happened live. This checks
    the fix is really wired in, not just documented in a comment."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    GeminiClient()

    _, kwargs = mock_client_cls.call_args
    http_options = kwargs["http_options"]
    assert http_options.timeout == 90_000
    assert http_options.retry_options.attempts == 3


@patch("time.sleep")
@patch("google.genai.Client")
def test_forced_tool_call_retries_past_a_connection_drop_and_still_succeeds(
    mock_client_cls, mock_sleep, monkeypatch
):
    """Simulates exactly the live failure: the connection gets aborted mid-request
    (a bare OSError, the shape of Windows' "[WinError 10053]"), then a later attempt
    succeeds. The caller should never see the error at all."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    fake_call = MagicMock()
    fake_call.args = {"listings": []}
    fake_resp = MagicMock()
    fake_resp.function_calls = [fake_call]

    mock_instance = MagicMock()
    mock_instance.models.generate_content.side_effect = [
        ConnectionError("[WinError 10053] An established connection was aborted"),
        fake_resp,
    ]
    mock_client_cls.return_value = mock_instance

    client = GeminiClient()
    result = client.forced_tool_call(system="s", user="u", tool=SAMPLE_TOOL)

    assert result == {"listings": []}
    assert mock_instance.models.generate_content.call_count == 2
    mock_sleep.assert_called_once()  # backed off once between the two attempts


@patch("time.sleep")
@patch("google.genai.Client")
def test_forced_tool_call_gives_up_after_repeated_connection_drops(mock_client_cls, mock_sleep, monkeypatch):
    """If the connection never recovers, this should still raise -- not swallow the
    error and silently return an empty result, which would look like "no jobs found"
    instead of "the run failed"."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    mock_instance = MagicMock()
    mock_instance.models.generate_content.side_effect = OSError("[WinError 10053] connection aborted")
    mock_client_cls.return_value = mock_instance

    client = GeminiClient()
    with pytest.raises(OSError):
        client.forced_tool_call(system="s", user="u", tool=SAMPLE_TOOL)

    assert mock_instance.models.generate_content.call_count == GeminiClient._CONNECTION_RETRY_ATTEMPTS


@patch("time.sleep")
@patch("google.genai.Client")
def test_forced_tool_call_retries_past_an_httpx_read_error_and_still_succeeds(
    mock_client_cls, mock_sleep, monkeypatch
):
    """A second real live incident, distinct from the OSError/ConnectionError one above:
    a connection reset *while reading the response* (Windows' "[WinError 10054] An
    existing connection was forcibly closed by the remote host") surfaces as
    httpx.ReadError, not OSError/ConnectionError -- confirmed NOT a subclass of either
    (httpx has its own exception hierarchy). The original fix's `except (OSError,
    ConnectionError)` let this one crash the whole discover run live before this test
    and the broader `httpx.TransportError` catch were added."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    fake_call = MagicMock()
    fake_call.args = {"listings": []}
    fake_resp = MagicMock()
    fake_resp.function_calls = [fake_call]

    mock_instance = MagicMock()
    mock_instance.models.generate_content.side_effect = [
        httpx.ReadError("[WinError 10054] An existing connection was forcibly closed by the remote host"),
        fake_resp,
    ]
    mock_client_cls.return_value = mock_instance

    client = GeminiClient()
    result = client.forced_tool_call(system="s", user="u", tool=SAMPLE_TOOL)

    assert result == {"listings": []}
    assert mock_instance.models.generate_content.call_count == 2
    mock_sleep.assert_called_once()


@patch("time.sleep")
@patch("google.genai.Client")
def test_forced_tool_call_gives_up_after_repeated_httpx_transport_errors(mock_client_cls, mock_sleep, monkeypatch):
    """Same as the OSError give-up test above, but for the httpx.TransportError family --
    confirms this doesn't retry forever and still raises if the connection never recovers."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    mock_instance = MagicMock()
    mock_instance.models.generate_content.side_effect = httpx.ReadError("[WinError 10054] connection reset")
    mock_client_cls.return_value = mock_instance

    client = GeminiClient()
    with pytest.raises(httpx.ReadError):
        client.forced_tool_call(system="s", user="u", tool=SAMPLE_TOOL)

    assert mock_instance.models.generate_content.call_count == GeminiClient._CONNECTION_RETRY_ATTEMPTS


@patch("time.sleep")
@patch("google.genai.Client")
def test_forced_tool_call_does_not_retry_non_connection_errors(mock_client_cls, mock_sleep, monkeypatch):
    """A real API error (bad request, unknown model, etc.) should surface immediately
    -- retrying something that will never succeed just delays the real failure."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    mock_instance = MagicMock()
    mock_instance.models.generate_content.side_effect = ValueError("not a connection problem")
    mock_client_cls.return_value = mock_instance

    client = GeminiClient()
    with pytest.raises(ValueError):
        client.forced_tool_call(system="s", user="u", tool=SAMPLE_TOOL)

    assert mock_instance.models.generate_content.call_count == 1
    mock_sleep.assert_not_called()
