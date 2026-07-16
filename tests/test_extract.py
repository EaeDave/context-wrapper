"""Contratos da extração LLM: parsing, responsabilidade e reuniões longas."""

from __future__ import annotations

import json

import pytest

import meet.extract as extract_mod
from meet.config import Settings
from meet.extract import (
    _action_item_from_dict,
    _action_item_is_for_owner,
    _deduplicate_action_items,
    _parse_json_response,
    _split_transcript,
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


@pytest.mark.parametrize(
    ("assigned_to", "expected"),
    [
        ("me", True),
        (" ME ", True),
        (None, True),
        (["me", "Alice"], True),
        (["Alice", "Bruno"], False),
        ("me e Alice", True),
        (False, False),
        (0, False),
        ({"name": "me"}, False),
        ("indefinido", True),
        ("Alice", False),
        ("Equipe de infraestrutura", False),
    ],
)
def test_action_item_filtra_responsavel_explicito(
    assigned_to: object, expected: bool
) -> None:
    assert _action_item_is_for_owner({"assigned_to": assigned_to}) is expected


def test_deduplicate_preserva_detalhes_prioridade_e_intervalo() -> None:
    items = [
        {
            "what": "Atualizar endpoint",
            "where": "/api/orders",
            "details": "Aceitar external_id",
            "priority": "media",
            "source_start": "00:42:00",
            "source_end": "00:42:20",
        },
        {
            "what": " atualizar endpoint ",
            "where": "/API/orders",
            "details": "Manter compatibilidade por 30 dias",
            "requested_by": "Alice",
            "assigned_to": ["me", "Bob"],
            "priority": "alta",
            "source_start": "00:41:50",
            "source_end": "00:42:45",
            "evidence_quote": "Vamos atualizar endpoint",
            "explicitness": "explicit",
        },
    ]

    assert _deduplicate_action_items(items) == [
        {
            "what": "Atualizar endpoint",
            "where": "/api/orders",
            "details": "Aceitar external_id\nManter compatibilidade por 30 dias",
            "requested_by": "Alice",
            "assigned_to": ["me", "Bob"],
            "priority": "alta",
            "source_start": "00:41:50",
            "source_end": "00:42:45",
            "evidence_quote": "Vamos atualizar endpoint",
            "explicitness": "explicit",
        }
    ]


# ---------------------------------------------------------------------------
# Chunking temporal
# ---------------------------------------------------------------------------


def _long_segments(count: int = 12, chars: int = 30) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            start=float(i * 60),
            end=float(i * 60 + 45),
            text=f"marcador-{i:02d} " + (chr(65 + i % 26) * chars),
            speaker="me" if i % 2 == 0 else "Alice",
        )
        for i in range(count)
    ]


def test_split_transcript_cobre_todos_os_segmentos_sem_cortar_turnos() -> None:
    segments = _long_segments()
    chunks = _split_transcript(segments, max_chars=180, overlap_chars=50)

    combined = "\n".join(chunk.text for chunk in chunks)
    assert len(chunks) > 1
    for i in range(len(segments)):
        assert f"marcador-{i:02d}" in combined
    assert all(chunk.text.startswith("[") for chunk in chunks)
    assert all("marcador-" in line for chunk in chunks for line in chunk.text.splitlines())


def test_split_transcript_tem_overlap_e_timestamps_absolutos() -> None:
    chunks = _split_transcript(_long_segments(), max_chars=180, overlap_chars=80)

    adjacent_overlap = [
        set(left.text.splitlines()) & set(right.text.splitlines())
        for left, right in zip(chunks, chunks[1:])
    ]
    assert all(overlap for overlap in adjacent_overlap)
    assert "[00:00:00-00:00:45]" in chunks[0].text
    assert "[00:11:00-00:11:45]" in chunks[-1].text


def test_split_transcript_valida_limites() -> None:
    with pytest.raises(ValueError):
        _split_transcript(_long_segments(), max_chars=0)
    with pytest.raises(ValueError):
        _split_transcript(_long_segments(), max_chars=100, overlap_chars=100)

def test_split_transcript_limita_duracao_mesmo_com_texto_curto() -> None:
    chunks = _split_transcript(
        _long_segments(count=20, chars=1),
        max_chars=100_000,
        overlap_chars=0,
        max_seconds=180,
    )

    assert len(chunks) > 1
    assert all(chunk.end - chunk.start <= 180 for chunk in chunks)


def test_split_transcript_rejeita_duracao_invalida() -> None:
    with pytest.raises(ValueError, match="max_seconds"):
        _split_transcript(_long_segments(), max_seconds=0)


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


def test_extract_curto_emite_progresso_indeterminado_e_conclusao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider(_GOOD_RESPONSE))
    segs = [TranscriptSegment(start=0, end=5, text="Olá pessoal", speaker="Alice")]
    progress: list[tuple[float | None, str]] = []

    summary, items, title, facts = extract(
        segs, ["Alice"], _settings(), on_progress=lambda value, detail: progress.append((value, detail))
    )

    assert title == "Sprint Planning"
    assert "prioridades" in summary
    assert len(items) == 2
    assert items[0].what == "Corrigir login"
    assert items[0].priority == "alta"
    assert items[0].where == "/auth"
    assert isinstance(facts, list)
    assert progress == [
        (None, "Gerando resumo e tarefas com LLM"),
        (1.0, "Resumo e tarefas gerados"),
    ]


def test_extract_item_missing_fields_get_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Action items without optional keys receive contract-defined defaults."""
    response = '{"title": "T", "summary": "S", "action_items": [{"what": "Do thing"}]}'
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider(response))

    _, items, _, _ = extract([], [], _settings())
    assert items[0].where is None
    assert items[0].priority == "media"



def test_extract_retorna_todas_as_tarefas_inclusive_terceiros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extract() agora retorna TODAS as tarefas; filtro pessoal fica no store."""
    response = """{
      "title": "T", "summary": "S", "action_items": [
        {"what": "Eu preparo o deploy", "requested_by": "Alice", "assigned_to": "me"},
        {"what": "Alice corrige o login", "assigned_to": "Alice"},
        {"what": "Revisar a documentação", "assigned_to": null},
        {"what": "Compatibilidade antiga sem campo"}
      ]
    }"""
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider(response))

    _, items, _, _ = extract([], ["me", "Alice"], _settings())

    # All 4 items returned — no filtering at extraction level
    assert len(items) == 4
    whats = [i.what for i in items]
    assert "Eu preparo o deploy" in whats
    assert "Alice corrige o login" in whats
    assert "Revisar a documentação" in whats
    assert "Compatibilidade antiga sem campo" in whats
    # assigned_to normalized correctly
    deploy = next(i for i in items if i.what == "Eu preparo o deploy")
    assert deploy.assigned_to == ["me"]
    assert deploy.requested_by == "Alice"
    alice = next(i for i in items if i.what == "Alice corrige o login")
    assert alice.assigned_to == ["Alice"]
    no_owner = next(i for i in items if i.what == "Revisar a documentação")
    assert no_owner.assigned_to is None


def test_extract_longo_analisa_todos_blocos_e_consolida(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class MapReduceProvider(LLMProvider):
        def complete(self, system: str, user: str) -> str:
            calls.append((system, user))
            if "UM BLOCO temporal" in system:
                block_number = sum("UM BLOCO temporal" in call[0] for call in calls)
                first_timestamp = user.split("[", 1)[1].split("]", 1)[0]
                decisions = [
                    {
                        "text": f"Decisão {first_timestamp}",
                        "source_start": first_timestamp.split("-", 1)[0],
                        "source_end": first_timestamp.split("-", 1)[1],
                    }
                ]
                action_items: list[dict] = []
                if block_number == 1:
                    decisions.append(
                        {
                            "text": "Usar fila durável nos embarques",
                            "source_start": "00:07:50",
                            "source_end": "00:08:10",
                            "explicitness": "inferred",
                        }
                    )
                    action_items.append(
                        {
                            "what": "Atualizar /api/orders",
                            "where": "/api/orders",
                            "assigned_to": "me",
                            "source_start": "00:07:50",
                            "source_end": "00:08:10",
                        }
                    )
                elif block_number == 2:
                    decisions.append(
                        {
                            "text": "Usar uma fila durável para os embarques",
                            "source_start": "00:08:00",
                            "source_end": "00:08:20",
                            "evidence_quote": "vamos usar uma fila durável",
                            "explicitness": "explicit",
                        }
                    )
                    action_items.extend(
                        [
                            {
                                "what": " atualizar /API/orders ",
                                "where": "/API/orders",
                                "assigned_to": "me",
                                "details": "Preservar paginação",
                                "source_start": "00:08:00",
                                "source_end": "00:08:20",
                            },
                            {"what": "Alice migra o banco", "assigned_to": "Alice"},
                            {
                                "what": "Definir responsável pelo rollout",
                                "assigned_to": None,
                            },
                        ]
                    )
                return json.dumps(
                    {
                        "chunk_summary": f"Síntese {first_timestamp}",
                        "decisions": decisions,
                        "requirements": [
                            {
                                "text": f"Requisito {first_timestamp}",
                                "source_start": first_timestamp.split("-", 1)[0],
                                "source_end": first_timestamp.split("-", 1)[1],
                            }
                        ],
                        "constraints": [],
                        "open_questions": [],
                        "action_items": action_items,
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "title": "Reunião longa",
                    "summary": "Resumo consolidado com decisões e requisitos.",
                },
                ensure_ascii=False,
            )

    segments = _long_segments(count=130, chars=900)
    provider = MapReduceProvider()
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: provider)
    progress: list[tuple[float | None, str]] = []

    summary, items, title, facts = extract(
        segments,
        ["me", "Alice"],
        _settings(),
        on_progress=lambda value, detail: progress.append((value, detail)),
    )

    chunk_calls = [(system, user) for system, user in calls if "UM BLOCO" in system]
    assert len(_build_call_text := "\n".join(user for _, user in chunk_calls)) > 0
    assert len(chunk_calls) > 1
    assert len(calls) == len(chunk_calls) + 1
    assert "marcador-00" in _build_call_text
    assert "marcador-65" in _build_call_text
    assert "marcador-129" in _build_call_text

    consolidation = json.loads(calls[-1][1])
    assert consolidation["chunk_count"] == len(chunk_calls)
    assert all("analysis" not in chunk for chunk in consolidation["chunks"])
    assert all(chunk["key_points"] for chunk in consolidation["chunks"])
    assert "Não reemita fatos ou tarefas" in calls[-1][0]

    assert title == "Reunião longa"
    assert "decisões e requisitos" in summary
    item_whats = [item.what.strip() for item in items]
    assert item_whats.count("Atualizar /api/orders") == 1
    assert "Definir responsável pelo rollout" in item_whats
    assert "Alice migra o banco" in item_whats
    updated_orders = items[item_whats.index("Atualizar /api/orders")]
    assert updated_orders.details == "Preservar paginação"
    assert updated_orders.source_start == 7 * 60 + 50
    assert updated_orders.source_end == 8 * 60 + 20

    durable_queue = [fact for fact in facts if "fila durável" in fact.text]
    assert len(durable_queue) == 1
    assert durable_queue[0].explicitness == "explicit"
    assert durable_queue[0].evidence_quote == "vamos usar uma fila durável"
    assert any(fact.kind == "requirement" for fact in facts)
    assert len(facts) == len(chunk_calls) * 2 + 1

    block_count = len(chunk_calls)
    assert progress[0] == (0.0, f"Analisando bloco 1 de {block_count}")
    assert progress[-3] == (
        block_count / (block_count + 1),
        f"Bloco {block_count} de {block_count} analisado",
    )
    assert progress[-2] == (None, "Consolidando análises dos blocos")
    assert progress[-1] == (1.0, "Resumo e tarefas consolidados")
    measured = [value for value, _detail in progress if value is not None]
    assert measured == sorted(measured)
    assert [
        detail for _value, detail in progress if detail.startswith("Analisando bloco")
    ] == [f"Analisando bloco {i} de {block_count}" for i in range(1, block_count + 1)]


def test_deduplicate_facts_preserva_fatos_distintos_no_mesmo_trecho() -> None:
    facts = extract_mod._deduplicate_facts(
        [
            {
                "kind": "requirement",
                "text": "Exibir o produto na tela de embarque",
                "source_start": "00:08:00",
                "source_end": "00:08:20",
                "evidence_quote": "listar produto e volume",
            },
            {
                "kind": "requirement",
                "text": "Exibir o volume na tela de embarque",
                "source_start": "00:08:00",
                "source_end": "00:08:20",
                "evidence_quote": "listar produto e volume",
            },
            {
                "kind": "decision",
                "text": "Usar lote como chave principal",
            },
            {
                "kind": "decision",
                "text": "Usar lote como chave principal",
                "explicitness": "explicit",
            },
        ]
    )

    assert [fact["text"] for fact in facts] == [
        "Exibir o produto na tela de embarque",
        "Exibir o volume na tela de embarque",
        "Usar lote como chave principal",
    ]
    assert facts[-1]["explicitness"] == "explicit"


def test_complete_json_repete_uma_vez_apos_truncamento() -> None:
    calls: list[str] = []

    class TruncatedOnceProvider(LLMProvider):
        def complete(self, system: str, user: str) -> str:
            calls.append(system)
            if len(calls) == 1:
                raise extract_mod.LLMOutputTruncated('{"title":"cortado"')
            return '{"title":"Completo","summary":"OK","action_items":[]}'

    data = extract_mod._complete_json(TruncatedOnceProvider(), "sistema", "usuário")

    assert data["title"] == "Completo"
    assert len(calls) == 2
    assert "RETENTATIVA APÓS LIMITE DE SAÍDA" in calls[1]


def test_complete_json_falha_curto_apos_dois_truncamentos() -> None:
    class AlwaysTruncatedProvider(LLMProvider):
        def complete(self, system: str, user: str) -> str:
            raise extract_mod.LLMOutputTruncated("x" * 9_000)

    with pytest.raises(RuntimeError, match="duas vezes") as exc_info:
        extract_mod._complete_json(AlwaysTruncatedProvider(), "sistema", "usuário")

    assert len(str(exc_info.value)) < 200


def test_complete_json_nao_expoe_resposta_invalida_inteira() -> None:
    response = "x" * 9_000

    with pytest.raises(ValueError, match="9000 caracteres") as exc_info:
        extract_mod._complete_json(_FakeProvider(response), "sistema", "usuário")

    assert len(str(exc_info.value)) < 700


def test_extract_divide_reuniao_longa_com_transcript_curto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class DurationProvider(LLMProvider):
        def complete(self, system: str, user: str) -> str:
            calls.append(system)
            if "UM BLOCO temporal" in system:
                return json.dumps({
                    "chunk_summary": "Síntese",
                    "decisions": [],
                    "requirements": [],
                    "constraints": [],
                    "open_questions": [],
                    "action_items": [],
                })
            return '{"title":"Longa","summary":"OK","facts":[],"action_items":[]}'

    monkeypatch.setattr(extract_mod, "get_provider", lambda _: DurationProvider())
    segments = _long_segments(count=12, chars=1)

    extract(segments, ["me", "Alice"], _settings())

    assert sum("UM BLOCO temporal" in system for system in calls) >= 2
    assert "consolida análises temporais" in calls[-1]


@pytest.mark.parametrize("status_code", [429, 500, 520])
def test_anthropic_retry_recupera_http_transitorio(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    import httpx

    calls: list[int] = []

    class Client:
        def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            calls.append(1)
            return httpx.Response(status_code if len(calls) == 1 else 200)

    monkeypatch.setattr(extract_mod, "_HTTP_RETRY_DELAYS", (0.0,))
    response = extract_mod._anthropic_post_with_retry(
        Client(), "https://example.test", payload={}, headers={}
    )

    assert response.status_code == 200
    assert len(calls) == 2


def test_anthropic_retry_nao_repete_http_permanente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    calls: list[int] = []

    class Client:
        def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            calls.append(1)
            return httpx.Response(400)

    monkeypatch.setattr(extract_mod, "_HTTP_RETRY_DELAYS", (0.0,))
    response = extract_mod._anthropic_post_with_retry(
        Client(), "https://example.test", payload={}, headers={}
    )

    assert response.status_code == 400
    assert len(calls) == 1


def test_anthropic_retry_recupera_erro_de_transporte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    calls: list[int] = []

    class Client:
        def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("temporário")
            return httpx.Response(200)

    monkeypatch.setattr(extract_mod, "_HTTP_RETRY_DELAYS", (0.0,))
    response = extract_mod._anthropic_post_with_retry(
        Client(), "https://example.test", payload={}, headers={}
    )

    assert response.status_code == 200
    assert len(calls) == 2


def test_anthropic_oauth_detecta_limite_de_saida(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200
        def json(self) -> dict:
            return {
                "content": [{"type": "text", "text": '{"title":"cortado"'}],
                "stop_reason": "max_tokens",
            }

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def post(self, _url: str, *, json: dict, headers: dict) -> Response:
            assert json["max_tokens"] == extract_mod._MAX_OUTPUT_TOKENS
            assert headers["Authorization"] == "Bearer access"
            return Response()

    import httpx
    import meet.anthropic_oauth as oauth_mod

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(oauth_mod, "get_access_token", lambda _settings: "access")
    monkeypatch.setattr(oauth_mod, "_check_response", lambda _response: None)
    provider = extract_mod.AnthropicOAuthProvider(_settings())

    with pytest.raises(extract_mod.LLMOutputTruncated):
        provider.complete("sistema", "usuário")


def test_openai_api_detecta_finish_reason_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    import openai

    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"title":"cortado"'),
        finish_reason="length",
    )])
    completions = SimpleNamespace(create=lambda **_kwargs: response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(openai, "OpenAI", lambda **_kwargs: client)
    provider = extract_mod.OpenAIProvider("key", "gpt-4o")

    with pytest.raises(extract_mod.LLMOutputTruncated):
        provider.complete("sistema", "usuário")


def test_extract_explica_identidade_e_atribuicao_ao_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class CapturingProvider(LLMProvider):
        def complete(self, system: str, user: str) -> str:
            captured.update(system=system, user=user)
            return '{"title":"T","summary":"S","action_items":[]}'

    monkeypatch.setattr(extract_mod, "get_provider", lambda _: CapturingProvider())
    segments = [
        TranscriptSegment(0, 1, "Eu faço o deploy", speaker="me"),
        TranscriptSegment(1, 2, "Eu corrijo o login", speaker="Alice"),
    ]

    extract(segments, ["me", "Alice"], _settings())

    assert "REGRAS DE RASTREABILIDADE" in captured["system"]
    assert "assigned_to" in captured["system"]
    assert '"requested_by"' in captured["system"]
    assert "[00:00:00-00:00:01] me: Eu faço o deploy" in captured["user"]
    assert "[00:00:01-00:00:02] Alice: Eu corrijo o login" in captured["user"]

def test_extract_raises_on_bad_provider_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """extract() re-raises ValueError when the provider returns unparseable text."""
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider("nope"))

    with pytest.raises(ValueError):
        extract([], [], _settings())


def test_extract_skips_non_dict_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-dict entries in action_items list are silently discarded."""
    response = '{"title": "T", "summary": "S", "action_items": [null, "bad", {"what": "ok"}]}'
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider(response))

    _, items, _, _ = extract([], [], _settings())
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


def test_get_provider_openai_missing_credentials_raises() -> None:
    """Sem OAuth nem API key, aponta para Configurações e variável de ambiente."""
    s = Settings(llm_provider="openai", openai_api_key="")
    with pytest.raises(ValueError, match="Configurações|OPENAI_API_KEY"):
        get_provider(s)


def test_get_provider_openai_oauth_precede_api_key(tmp_path) -> None:
    """OAuth da assinatura tem precedência; API key permanece fallback."""
    from meet.openai_oauth import save_tokens

    settings = Settings(
        llm_provider="openai",
        openai_api_key="sk-api-fallback",
        data_dir=tmp_path,
    )
    save_tokens(
        settings,
        {
            "access": "oauth-access",
            "refresh": "oauth-refresh",
            "expires": 9_999_999_999_000,
            "account_id": "account-123",
        },
    )

    provider = get_provider(settings)

    assert isinstance(provider, extract_mod.OpenAIOAuthProvider)


def test_openai_oauth_provider_consome_stream_responses(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Provider envia wire format Codex e concatena deltas de texto SSE."""
    import httpx

    captured: dict = {}

    catalog_calls: list[dict] = []

    class FakeCatalogResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"models": [
                {"slug": "internal", "priority": 0, "visibility": "hide"},
                {"slug": "unsupported", "priority": 1, "visibility": "list", "supported_in_api": False},
                {"slug": "gpt-5.6-luna", "priority": 3, "visibility": "list", "supported_in_api": True},
                {"slug": "gpt-5.6-terra", "priority": 2, "visibility": "list", "supported_in_api": True},
            ]}

    def fake_get(url: str, **kwargs) -> FakeCatalogResponse:
        catalog_calls.append({"url": url, **kwargs})
        return FakeCatalogResponse()

    class FakeResponse:
        status_code = 200
        is_success = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def iter_lines():
            return iter([
                'data: {"type":"response.output_text.delta","delta":"{\\"title\\":"}',
                'data: {"type":"response.output_text.delta","delta":"\\"Reunião\\"}"}',
                'data: {"type":"response.completed","response":{"id":"resp-1"}}',
                "data: [DONE]",
            ])

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def stream(method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr("meet.openai_oauth.get_access_token", lambda _settings, **_kwargs: "oauth-access")
    monkeypatch.setattr(
        "meet.openai_oauth.load_tokens",
        lambda _settings: {"account_id": "account-123"},
    )
    provider = extract_mod.OpenAIOAuthProvider(
        Settings(llm_provider="openai", data_dir=tmp_path)
    )

    result = provider.complete("system prompt", "transcrição")

    assert result == '{"title":"Reunião"}'
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["ChatGPT-Account-ID"] == "account-123"
    assert captured["json"]["model"] == "gpt-5.6-terra"
    assert captured["headers"]["version"] == extract_mod._CODEX_CLIENT_VERSION
    assert catalog_calls[0]["params"] == {
        "client_version": extract_mod._CODEX_CLIENT_VERSION
    }
    assert captured["json"]["instructions"] == "system prompt"
    assert captured["json"]["input"][0]["content"][0]["text"] == "transcrição"
    assert captured["json"]["stream"] is True


def test_openai_oauth_provider_envia_imagens_no_responses(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Codex OAuth recebe data URLs como input_image na mensagem do usuário."""
    import httpx

    captured: dict = {}
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"jpeg-frame")

    class FakeResponse:
        status_code = 200
        is_success = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def iter_lines():
            return iter([
                'data: {"type":"response.output_text.delta","delta":"{\\"observations\\":[]}"}',
                'data: {"type":"response.completed","response":{"id":"resp-visual"}}',
                "data: [DONE]",
            ])

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def stream(method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(
        "meet.openai_oauth.get_access_token",
        lambda _settings, **_kwargs: "oauth-access",
    )
    monkeypatch.setattr(
        "meet.openai_oauth.load_tokens",
        lambda _settings: {"account_id": "account-123"},
    )
    provider = extract_mod.OpenAIOAuthProvider(
        Settings(
            llm_provider="openai",
            llm_model="gpt-5.6-terra",
            data_dir=tmp_path,
        )
    )

    result = provider.complete_with_images(
        "system visual",
        "descreva",
        [extract_mod.ImageContent(path=image_path, timestamp=65)],
    )

    assert result == '{"observations":[]}'
    content = captured["json"]["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "Timestamp 00:01:05"}
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "low"
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert content[2] == {"type": "input_text", "text": "descreva"}
    assert captured["headers"]["ChatGPT-Account-ID"] == "account-123"


def test_openai_oauth_visual_renova_token_apos_401(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """O envio de frames repete uma vez com token renovado após rejeição."""
    import httpx

    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"jpeg-frame")
    authorizations: list[str] = []
    refresh_calls: list[str | None] = []

    class FakeResponse:
        def __init__(self, unauthorized: bool):
            self.status_code = 401 if unauthorized else 200
            self.is_success = not unauthorized

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def iter_lines():
            return iter([
                'data: {"type":"response.output_text.delta","delta":"ok"}',
                "data: [DONE]",
            ])

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def stream(_method, _url, **kwargs):
            authorization = kwargs["headers"]["Authorization"]
            authorizations.append(authorization)
            return FakeResponse(authorization == "Bearer stale")

    def fake_access(_settings, *, rejected_access=None):
        refresh_calls.append(rejected_access)
        return "fresh" if rejected_access else "stale"

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr("meet.openai_oauth.get_access_token", fake_access)
    monkeypatch.setattr("meet.openai_oauth.load_tokens", lambda _settings: {})
    provider = extract_mod.OpenAIOAuthProvider(
        Settings(llm_provider="openai", llm_model="gpt-5.6-terra", data_dir=tmp_path)
    )

    result = provider.complete_with_images(
        "system",
        "descreva",
        [extract_mod.ImageContent(path=image_path, timestamp=1)],
    )

    assert result == "ok"
    assert authorizations == ["Bearer stale", "Bearer fresh"]
    assert refresh_calls == [None, "stale"]


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


def test_analyse_propaga_callback_da_extracao_ao_tracker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from types import SimpleNamespace

    from meet import audio as audio_mod
    from meet import diarize as diarize_mod
    from meet import merge as merge_mod
    from meet import transcribe as transcribe_mod
    from meet import voicebank as voicebank_mod
    from meet.pipeline import _analyse
    from meet.progress import ProgressTracker, StepSpec

    monkeypatch.setattr(extract_mod, "validate_credentials", lambda _settings: None)
    monkeypatch.setattr(
        audio_mod,
        "prepare",
        lambda *_args, **_kwargs: SimpleNamespace(
            mic=None,
            others=tmp_path / "others.wav",
            mixed=tmp_path / "mixed.wav",
            duration=60.0,
        ),
    )
    segments = [TranscriptSegment(0, 1, "Tarefa", speaker="me")]
    monkeypatch.setattr(transcribe_mod, "transcribe", lambda *_args, **_kwargs: segments)
    monkeypatch.setattr(diarize_mod, "diarize", lambda *_args, **_kwargs: ([], {}))
    monkeypatch.setattr(merge_mod, "assign_speakers", lambda current, _turns: current)
    monkeypatch.setattr(merge_mod, "rename_speakers", lambda current, _mapping: current)
    monkeypatch.setattr(voicebank_mod, "resolve_with_scores", lambda *_args: {})

    def fake_extract(_segments, _participants, _settings, on_progress=None):
        assert on_progress is not None
        on_progress(0.5, "Analisando bloco 2 de 4")
        on_progress(None, "Consolidando análises dos blocos")
        on_progress(1.0, "Resumo e tarefas consolidados")
        return "Resumo", [], "Título", []

    monkeypatch.setattr(extract_mod, "extract", fake_extract)
    updates = []
    tracker = ProgressTracker(
        (
            StepSpec("auth", "Autenticar", 1.0),
            StepSpec("audio", "Áudio", 1.0),
            StepSpec("transcribe", "Transcrever", 1.0),
            StepSpec("diarize", "Diarizar", 1.0),
            StepSpec("speakers", "Vozes", 1.0),
            StepSpec("llm", "LLM", 1.0),
        ),
        updates.append,
    )

    _analyse(
        video=tmp_path / "reuniao.mkv",
        mic_track=1,
        others_track=2,
        no_llm=False,
        settings=_settings(),
        store=object(),  # type: ignore[arg-type]
        workdir=tmp_path,
        today="2026-07-14",
        tracker=tracker,
    )

    llm_updates = [update for update in updates if update.step == "llm"]
    assert [(update.step_percent, update.detail) for update in llm_updates[-3:]] == [
        (50.0, "Analisando bloco 2 de 4"),
        (None, "Consolidando análises dos blocos"),
        (100.0, "Resumo e tarefas consolidados"),
    ]


def test_reextract_propaga_progresso_estruturado(
    monkeypatch: pytest.MonkeyPatch,
    tmp_store,
    tmp_path,
) -> None:
    from meet.models import MeetingResult
    from meet.pipeline import reextract_meeting

    settings = Settings(data_dir=tmp_path, output_dir=tmp_path)
    meeting_id = tmp_store.save_meeting(
        MeetingResult(
            source=str(tmp_path / "reuniao.mkv"),
            date="2026-07-14",
            title="Reunião longa",
            duration=7200.0,
            participants=["me", "Alice"],
            segments=[TranscriptSegment(0, 1, "Tarefa", speaker="me")],
        ),
        tmp_path / "reuniao.md",
    )
    monkeypatch.setattr(extract_mod, "validate_credentials", lambda _settings: None)

    def fake_extract(_segments, _participants, _settings, on_progress=None):
        assert on_progress is not None
        on_progress(0.25, "Bloco 1 de 3 analisado")
        on_progress(None, "Consolidando análises dos blocos")
        on_progress(1.0, "Resumo e tarefas consolidados")
        return "Resumo novo", [], "Título ignorado", []

    monkeypatch.setattr(extract_mod, "extract", fake_extract)
    updates = []

    reextract_meeting(
        meeting_id,
        settings=settings,
        store=tmp_store,
        on_progress=updates.append,
    )

    llm_updates = [update for update in updates if update.step == "llm"]
    assert [(update.step_percent, update.detail) for update in llm_updates[-3:]] == [
        (25.0, "Bloco 1 de 3 analisado"),
        (None, "Consolidando análises dos blocos"),
        (100.0, "Resumo e tarefas consolidados"),
    ]
    assert updates[-1].percent == 100.0
    assert tmp_store.get_meeting(meeting_id).summary == "Resumo novo"


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


# ---------------------------------------------------------------------------
# Traceable extraction: parse_hms, normalize_assigned_to, validate_evidence
# ---------------------------------------------------------------------------

from meet.extract import (
    _fact_from_dict,
    _normalize_assigned_to,
    _parse_hms,
    _validate_evidence,
)


def test_parse_hms_valid() -> None:
    assert _parse_hms("00:00:00") == 0.0
    assert _parse_hms("01:02:03") == 3723.0
    assert _parse_hms("00:42:30") == 2550.0


def test_parse_hms_invalid() -> None:
    assert _parse_hms(None) is None
    assert _parse_hms("bad") is None
    assert _parse_hms("01:02") is None
    assert _parse_hms(123) is None
    assert _parse_hms("00:60:00") is None
    assert _parse_hms("00:00:60") is None
    assert _parse_hms("-01:00:00") is None


def test_normalize_assigned_to_string_me() -> None:
    assert _normalize_assigned_to("me") == ["me"]


def test_normalize_assigned_to_list() -> None:
    assert _normalize_assigned_to(["me", "Alice"]) == ["me", "Alice"]


def test_normalize_assigned_to_null_sentinels() -> None:
    assert _normalize_assigned_to(None) is None
    assert _normalize_assigned_to("") is None
    assert _normalize_assigned_to("null") is None
    assert _normalize_assigned_to([]) is None
    assert _normalize_assigned_to(["null"]) is None


def test_validate_evidence_confirmed() -> None:
    segs = [TranscriptSegment(start=10.0, end=20.0, text="Alice vai fazer o deploy")]
    assert _validate_evidence(segs, 10.0, 20.0, "Alice vai fazer o deploy") is True


def test_validate_evidence_needs_review_bad_interval() -> None:
    segs = [TranscriptSegment(start=10.0, end=20.0, text="Alice vai fazer o deploy")]
    # interval doesn't overlap
    assert _validate_evidence(segs, 25.0, 30.0, "Alice vai fazer o deploy") is False


def test_validate_evidence_needs_review_no_quote() -> None:
    segs = [TranscriptSegment(start=10.0, end=20.0, text="Alice vai fazer o deploy")]
    assert _validate_evidence(segs, 10.0, 20.0, None) is False


def test_validate_evidence_needs_review_wrong_quote() -> None:
    segs = [TranscriptSegment(start=10.0, end=20.0, text="Alice vai fazer o deploy")]
    assert _validate_evidence(segs, 10.0, 20.0, "Bob vai fazer o build") is False


def test_action_item_from_dict_review_status_confirmed() -> None:
    segs = [TranscriptSegment(start=10.0, end=20.0, text="Bob vai fazer o login")]
    d = {
        "what": "corrigir login",
        "assigned_to": ["Bob"],
        "source_start": "00:00:10",
        "source_end": "00:00:20",
        "evidence_quote": "Bob vai fazer o login",
        "explicitness": "explicit",
    }
    from meet.extract import _action_item_from_dict
    item = _action_item_from_dict(d, segs)
    assert item.review_status == "confirmed"
    assert item.explicitness == "explicit"
    assert item.assigned_to == ["Bob"]
    assert item.source_start == 10.0
    assert item.source_end == 20.0


def test_action_item_from_dict_review_status_needs_review_no_quote() -> None:
    from meet.extract import _action_item_from_dict
    d = {"what": "corrigir login", "source_start": "00:00:10", "source_end": "00:00:20"}
    item = _action_item_from_dict(d, [])
    assert item.review_status == "needs_review"


def test_fact_from_dict_confirmed() -> None:
    segs = [TranscriptSegment(start=0.0, end=5.0, text="vamos usar PostgreSQL")]
    d = {
        "kind": "decision",
        "text": "usar PostgreSQL",
        "source_start": "00:00:00",
        "source_end": "00:00:05",
        "evidence_quote": "vamos usar PostgreSQL",
        "explicitness": "explicit",
    }
    fact = _fact_from_dict(d, "decision", segs)
    assert fact.kind == "decision"
    assert fact.review_status == "confirmed"
    assert fact.source_start == 0.0
    assert fact.source_end == 5.0


def test_extract_retorna_fatos(monkeypatch: pytest.MonkeyPatch) -> None:
    """extract() retorna MeetingFact dos 4 kinds quando LLM os fornece."""
    response = json.dumps({
        "title": "T",
        "summary": "S",
        "facts": [
            {"kind": "decision", "text": "usar Redis", "evidence_quote": "vamos usar Redis",
             "source_start": "00:00:01", "source_end": "00:00:03", "explicitness": "explicit"},
            {"kind": "requirement", "text": "latência < 200ms", "evidence_quote": None,
             "source_start": None, "source_end": None, "explicitness": "inferred"},
            {"kind": "constraint", "text": "orçamento fixo", "evidence_quote": None,
             "source_start": None, "source_end": None, "explicitness": "inferred"},
            {"kind": "open_question", "text": "quem faz deploy?", "evidence_quote": None,
             "source_start": None, "source_end": None, "explicitness": "inferred"},
        ],
        "action_items": [],
    })
    monkeypatch.setattr(extract_mod, "get_provider", lambda _: _FakeProvider(response))
    segs = [TranscriptSegment(start=1.0, end=3.0, text="vamos usar Redis")]

    _, _, _, facts = extract(segs, [], _settings())
    assert len(facts) == 4
    kinds = {f.kind for f in facts}
    assert kinds == {"decision", "requirement", "constraint", "open_question"}
    redis = next(f for f in facts if f.kind == "decision")
    assert redis.review_status == "confirmed"
    assert redis.source_start == 1.0
    lat = next(f for f in facts if f.kind == "requirement")
    assert lat.review_status == "needs_review"
