"""Normalização contextual conservadora do transcript."""

import json

from meet.config import Settings
from meet.extract import LLMProvider, normalize_transcript
from meet.models import TranscriptSegment


class _CrossBatchProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if "Identifique termos canônicos" in system:
            terms = ["DUN14"] if "DUN14" in user else []
            return json.dumps({"terms": terms})
        if "Doom 14" in user:
            assert "DUN14" in user
            return json.dumps({
                "corrections": [{
                    "index": 0,
                    "original": "Doom 14",
                    "corrected": "DUN14",
                    "confidence": 0.98,
                    "reason": "DUN14 aparece literalmente em outro trecho",
                }]
            })
        return '{"corrections":[]}'


def test_termo_correto_de_outro_bloco_corrige_erro_fonetico() -> None:
    provider = _CrossBatchProvider()
    segments = [
        TranscriptSegment(0, 2, "Bipou o Doom 14 e o lote", speaker="me"),
        TranscriptSegment(100, 200, "contexto geral " * 900, speaker="Igor"),
        TranscriptSegment(300, 302, "O identificador correto é DUN14", speaker="Igor"),
    ]

    normalized = normalize_transcript(
        segments,
        Settings(),
        provider=provider,
        learned_terms=[],
    )

    assert normalized[0].text == "Bipou o DUN14 e o lote"
    assert normalized[0].original_text == "Bipou o Doom 14 e o lote"
    assert normalized[0].corrections[0].corrected == "DUN14"
    assert segments[0].text == "Bipou o Doom 14 e o lote"
    correction_calls = [user for system, user in provider.calls if "corrige somente" in system]
    assert len(correction_calls) >= 2


def test_correcao_insegura_ou_que_muda_numero_e_rejeitada() -> None:
    class UnsafeProvider(LLMProvider):
        def complete(self, system: str, _user: str) -> str:
            if "Identifique termos canônicos" in system:
                return '{"terms":[]}'
            return json.dumps({"corrections": [
                {"index": 0, "original": "Doom 14", "corrected": "DUN13", "confidence": 0.99},
                {"index": 0, "original": "Doom", "corrected": "DUN", "confidence": 0.7},
            ]})

    segment = TranscriptSegment(0, 1, "Doom 14")
    normalized = normalize_transcript([segment], Settings(), provider=UnsafeProvider())

    assert normalized[0].text == "Doom 14"
    assert normalized[0].original_text is None
    assert normalized[0].corrections == []


def test_falha_da_llm_preserva_transcript_bruto() -> None:
    class FailingProvider(LLMProvider):
        def complete(self, _system: str, _user: str) -> str:
            raise RuntimeError("provider offline")

    segment = TranscriptSegment(0, 1, "fala original")
    normalized = normalize_transcript([segment], Settings(), provider=FailingProvider())

    assert normalized[0].text == "fala original"
    assert normalized[0].original_text is None
