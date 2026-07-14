"""Tests for meet.extract — JSON parsing, truncation, action item defaults,
and the public extract() entrypoint via a monkeypatched provider.

Contracts defended:
- _parse_json_response: parses bare JSON, fenced ```json, JSON with surrounding
  garbage, and raises ValueError on unparseable input.
- _action_item_from_dict: absent keys yield correct defaults.
- _maybe_truncate: text ≤ 100 000 chars returned intact (including the exact
  boundary); longer text keeps the first and last 40 000 chars.
- extract(): calls the provider, parses response, returns (summary, items, title).
- get_provider(): raises ValueError with a human-readable message when api_key
  is absent; raises ValueError for unknown provider names.
"""

from __future__ import annotations

import warnings

import pytest

import meet.extract as extract_mod
from meet.config import Settings
from meet.extract import (
    _MAX_TRANSCRIPT_CHARS,
    _TRUNCATE_EACH_SIDE,
    _action_item_from_dict,
    _maybe_truncate,
    _parse_json_response,
    extract,
    get_provider,
    validate_credentials,
    LLMProvider,
)
from meet.models import ActionItem, TranscriptSegment


# ---------------------------------------------------------------------------
# _parse_json_response
# ---------------------------------------------------------------------------

def test_parse_json_pure() -> None:
    """Plain JSON object is returned as a dict."""
    result = _parse_json_response('{"title": "Reunião", "summary": "OK"}')
    assert result == {"title": "Reunião", "summary": "OK"}


def test_parse_json_fenced_json_block() -> None:
    """JSON wrapped in ```json ... ``` fence is extracted and parsed."""
    text = '```json\n{"title": "Test", "action_items": []}\n```'
    result = _parse_json_response(text)
    assert result["title"] == "Test"
    assert result["action_items"] == []


def test_parse_json_fenced_plain_block() -> None:
    """JSON wrapped in plain ``` fence (no language tag) is also handled."""
    text = '```\n{"x": 1}\n```'
    result = _parse_json_response(text)
    assert result == {"x": 1}


def test_parse_json_garbage_before_and_after() -> None:
    """LLM preamble / postamble outside JSON braces is ignored."""
    text = 'Here is your JSON:\n{"title": "Meeting"}\nHope that helps!'
    result = _parse_json_response(text)
    assert result["title"] == "Meeting"


def test_parse_json_impossible_raises_value_error() -> None:
    """Completely unparseable text raises ValueError containing the raw text."""
    bad = "this is not JSON at all"
    with pytest.raises(ValueError) as exc_info:
        _parse_json_response(bad)
    assert bad in str(exc_info.value)


def test_parse_json_partial_braces_raises_value_error() -> None:
    """Opening brace with no matching close raises ValueError."""
    bad = "{ unterminated"
    with pytest.raises(ValueError):
        _parse_json_response(bad)


def test_parse_json_array_at_root_raises_value_error() -> None:
    """A JSON array (not a dict) at root raises ValueError."""
    bad = "[1, 2, 3]"
    with pytest.raises(ValueError):
        _parse_json_response(bad)


# ---------------------------------------------------------------------------
# _action_item_from_dict
# ---------------------------------------------------------------------------

def test_action_item_defaults_for_empty_dict() -> None:
    """All fields absent → defaults: what='', others=None, priority='media'."""
    item = _action_item_from_dict({})
    assert item.what == ""
    assert item.where is None
    assert item.details is None
    assert item.requested_by is None
    assert item.priority == "media"


def test_action_item_all_fields_present() -> None:
    """All keys provided → no substitution happens."""
    d = {
        "what": "Deploy API",
        "where": "/api/v1",
        "details": "Use TLS 1.3",
        "requested_by": "Alice",
        "priority": "alta",
    }
    item = _action_item_from_dict(d)
    assert item.what == "Deploy API"
    assert item.where == "/api/v1"
    assert item.details == "Use TLS 1.3"
    assert item.requested_by == "Alice"
    assert item.priority == "alta"


def test_action_item_falsy_values_treated_as_missing() -> None:
    """Empty string / null values for optional fields → None (contract uses `or None`)."""
    d = {"what": "Fix bug", "where": "", "details": None, "requested_by": ""}
    item = _action_item_from_dict(d)
    assert item.where is None
    assert item.details is None
    assert item.requested_by is None


# ---------------------------------------------------------------------------
# _maybe_truncate
# ---------------------------------------------------------------------------

def test_maybe_truncate_short_text_intact() -> None:
    """Text well below the limit is returned without modification."""
    text = "a" * 1000
    assert _maybe_truncate(text) is text  # same object — no copy


def test_maybe_truncate_exactly_at_limit_intact() -> None:
    """Text at exactly the limit (100 000 chars) must NOT be truncated."""
    text = "x" * _MAX_TRANSCRIPT_CHARS
    result = _maybe_truncate(text)
    assert result == text


def test_maybe_truncate_long_preserves_start() -> None:
    """Truncated output starts with the first 40 000 chars of the original."""
    prefix = "A" * _TRUNCATE_EACH_SIDE
    middle = "M" * (_MAX_TRANSCRIPT_CHARS - 2 * _TRUNCATE_EACH_SIDE + 1)
    suffix = "Z" * _TRUNCATE_EACH_SIDE
    text = prefix + middle + suffix  # total > _MAX_TRANSCRIPT_CHARS

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = _maybe_truncate(text)

    assert result.startswith("A" * _TRUNCATE_EACH_SIDE)


def test_maybe_truncate_long_preserves_end() -> None:
    """Truncated output ends with the last 40 000 chars of the original."""
    prefix = "A" * _TRUNCATE_EACH_SIDE
    middle = "M" * (_MAX_TRANSCRIPT_CHARS - 2 * _TRUNCATE_EACH_SIDE + 1)
    suffix = "Z" * _TRUNCATE_EACH_SIDE
    text = prefix + middle + suffix

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = _maybe_truncate(text)

    assert result.endswith("Z" * _TRUNCATE_EACH_SIDE)


def test_maybe_truncate_long_emits_warning() -> None:
    """Truncation must emit a UserWarning."""
    text = "x" * (_MAX_TRANSCRIPT_CHARS + 1)
    with pytest.warns(UserWarning, match="Transcript truncado"):
        _maybe_truncate(text)


def test_maybe_truncate_long_contains_truncation_notice() -> None:
    """The ellipsis marker inserted between the halves must mention the length."""
    text = "a" * (_MAX_TRANSCRIPT_CHARS + 500)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = _maybe_truncate(text)
    assert "TRUNCADO" in result


# ---------------------------------------------------------------------------
# extract() — with fake LLMProvider injected via monkeypatch
# ---------------------------------------------------------------------------

class _FakeProvider(LLMProvider):
    """Deterministic provider for unit tests."""

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, system: str, user: str) -> str:
        return self._response


_GOOD_RESPONSE = """\
{
  "title": "Sprint Planning",
  "summary": "Equipe alinhou prioridades para a sprint.",
  "action_items": [
    {"what": "Corrigir login", "where": "/auth", "details": null,
     "requested_by": "Alice", "priority": "alta"},
    {"what": "Atualizar docs", "priority": "baixa"}
  ]
}
"""


def _settings() -> Settings:
    return Settings(llm_provider="anthropic", anthropic_api_key="fake-key")


def test_extract_returns_summary_title_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """extract() calls provider and returns parsed (summary, items, title)."""
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider(_GOOD_RESPONSE))
    segs = [TranscriptSegment(start=0, end=5, text="Olá pessoal", speaker="Alice")]
    summary, items, title = extract(segs, ["Alice"], _settings())

    assert title == "Sprint Planning"
    assert "prioridades" in summary
    assert len(items) == 2
    assert items[0].what == "Corrigir login"
    assert items[0].priority == "alta"
    assert items[0].where == "/auth"


def test_extract_item_missing_fields_get_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Action items without optional keys receive contract-defined defaults."""
    response = '{"title": "T", "summary": "S", "action_items": [{"what": "Do thing"}]}'
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider(response))

    _, items, _ = extract([], [], _settings())
    assert items[0].where is None
    assert items[0].priority == "media"


def test_extract_raises_on_bad_provider_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """extract() re-raises ValueError when the provider returns unparseable text."""
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider("nope"))

    with pytest.raises(ValueError):
        extract([], [], _settings())


def test_extract_skips_non_dict_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-dict entries in action_items list are silently discarded."""
    response = '{"title": "T", "summary": "S", "action_items": [null, "bad", {"what": "ok"}]}'
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider(response))

    _, items, _ = extract([], [], _settings())
    assert len(items) == 1
    assert items[0].what == "ok"


# ---------------------------------------------------------------------------
# get_provider — validation
# ---------------------------------------------------------------------------

def test_get_provider_anthropic_no_credentials_raises(tmp_path) -> None:
    """Sem OAuth (auth.json) e sem api key, aponta pra página Configurações."""
    s = Settings(
        llm_provider="anthropic", anthropic_api_key="", data_dir=tmp_path
    )
    with pytest.raises(ValueError, match="Configurações|ANTHROPIC_API_KEY"):
        get_provider(s)


def test_get_provider_openai_missing_key_raises() -> None:
    """Missing openai_api_key raises ValueError mentioning the key name."""
    s = Settings(llm_provider="openai", openai_api_key="")
    with pytest.raises(ValueError, match="openai_api_key"):
        get_provider(s)


def test_get_provider_unknown_provider_raises() -> None:
    """Unknown provider name raises ValueError."""
    s = Settings(llm_provider="llama", anthropic_api_key="x")
    with pytest.raises(ValueError, match="llama"):
        get_provider(s)


def test_get_provider_ollama_does_not_require_key() -> None:
    """Ollama provider requires no API key and should not raise."""
    s = Settings(llm_provider="ollama")
    provider = get_provider(s)  # must not raise
    assert provider is not None



def test_validate_credentials_renova_oauth(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Preflight OAuth deve validar/renovar o token sem chamar o LLM."""
    from meet.anthropic_oauth import save_tokens

    settings = Settings(llm_provider="anthropic", data_dir=tmp_path)
    save_tokens(
        settings,
        {"access": "a", "refresh": "r", "expires": 0},
    )
    calls: list[Settings] = []
    monkeypatch.setattr(
        "meet.anthropic_oauth.get_access_token",
        lambda current: calls.append(current) or "access-novo",
    )

    validate_credentials(settings)

    assert calls == [settings]


def test_pipeline_valida_llm_antes_do_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Credencial inválida deve falhar antes de preparar áudio ou carregar modelos."""
    from meet import audio as audio_mod
    from meet.pipeline import _analyse

    audio_called = False

    def prepare(*_args, **_kwargs):
        nonlocal audio_called
        audio_called = True

    monkeypatch.setattr(audio_mod, "prepare", prepare)
    monkeypatch.setattr(
        extract_mod,
        "validate_credentials",
        lambda _settings: (_ for _ in ()).throw(ValueError("Reconecte sua conta")),
    )
    from meet.progress import ProgressTracker, StepSpec

    updates = []
    tracker = ProgressTracker(
        (
            StepSpec("auth", "Validar acesso ao LLM", 1.0),
            StepSpec("audio", "Preparar áudio", 1.0),
        ),
        updates.append,
    )

    with pytest.raises(RuntimeError, match="Erro na autenticação LLM"):
        _analyse(
            video=tmp_path / "reuniao.mkv",
            mic_track=1,
            others_track=2,
            no_llm=False,
            settings=Settings(),
            store=object(),  # type: ignore[arg-type]
            workdir=tmp_path,
            today="2026-07-14",
            tracker=tracker,
        )

    assert updates[-1].step == "auth"
    assert updates[-1].detail == "Validando acesso ao LLM"
    assert audio_called is False


# ---------------------------------------------------------------------------
# ClaudeCodeProvider
# ---------------------------------------------------------------------------

def test_get_provider_claude_code_does_not_require_key() -> None:
    """claude-code provider requires no API key and should not raise."""
    from meet.extract import ClaudeCodeProvider

    s = Settings(llm_provider="claude-code")
    assert isinstance(get_provider(s), ClaudeCodeProvider)


def test_claude_code_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the `claude` CLI on PATH, complete() raises a clear ValueError."""
    import shutil

    from meet.extract import ClaudeCodeProvider

    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="claude"):
        ClaudeCodeProvider("").complete("sys", "user")


def test_claude_code_complete_invokes_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """complete() shells `claude -p` with system prompt as flag and user via stdin."""
    import shutil
    import subprocess

    from meet.extract import ClaudeCodeProvider

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")

        class _Proc:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""

        return _Proc()

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(subprocess, "run", fake_run)

    out = ClaudeCodeProvider("").complete("SYS", "TRANSCRIPT")
    assert out == '{"ok": true}'
    assert captured["cmd"][:2] == ["claude", "-p"]
    assert "SYS" in captured["cmd"]
    assert "sonnet" in captured["cmd"]
    assert captured["input"] == "TRANSCRIPT"


def test_claude_code_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero exit from the CLI surfaces as RuntimeError with stderr tail."""
    import shutil
    import subprocess

    from meet.extract import ClaudeCodeProvider

    def fake_run(cmd, **kwargs):
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "rate limited"

        return _Proc()

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="rate limited"):
        ClaudeCodeProvider("").complete("sys", "user")
