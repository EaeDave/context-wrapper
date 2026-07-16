"""Contratos da seleção e análise visual de reuniões."""

from pathlib import Path
import subprocess

import pytest

import meet.visual as visual_mod
from meet.extract import LLMProvider, analyze_visual_frames, extract
from meet.config import Settings
from meet.models import TranscriptSegment
from meet.visual import VisualFrame, candidate_timestamps, extract_relevant_frames


def test_candidate_timestamps_prioriza_fala_sobre_tela() -> None:
    segments = [
        TranscriptSegment(10, 15, "Conversa comum"),
        TranscriptSegment(40, 45, "Olha aqui nessa tela quando eu clico"),
    ]

    assert candidate_timestamps(segments, 300, max_frames=3) == [37.0, 40.0, 43.0]


def test_extract_relevant_frames_ignora_timestamp_com_erro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def signature(_video: Path, timestamp: float) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.CalledProcessError(1, ["ffmpeg"])
        return bytes([int(timestamp) % 255] * 1024)

    def write_frame(_video: Path, _timestamp: float, output: Path) -> None:
        output.write_bytes(b"jpeg")

    monkeypatch.setattr(visual_mod, "candidate_timestamps", lambda *_args, **_kwargs: [1.0, 2.0])
    monkeypatch.setattr(visual_mod, "_visual_signature", signature)
    monkeypatch.setattr(visual_mod, "_write_frame", write_frame)

    frames = extract_relevant_frames(
        tmp_path / "video.mp4", [], 3.0, tmp_path / "frames"
    )

    assert [frame.timestamp for frame in frames] == [2.0]


class _VisualProvider(LLMProvider):
    def __init__(self) -> None:
        self.text_calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.text_calls.append(user)
        return '{"title":"Demo","summary":"Cadastro falhou","facts":[],"action_items":[]}'

    def complete_with_images(self, system: str, user: str, images: list) -> str:
        return '{"observations":[{"timestamp":"00:00:05","description":"Campo CNPJ vazio","visible_text":["Cadastro","CNPJ"],"relevance":"high"}]}'


def test_observacao_visual_entra_no_contexto_da_extracao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpeg")
    provider = _VisualProvider()
    segments = [TranscriptSegment(4, 6, "Olha aqui nessa tela", speaker="me")]
    observations = analyze_visual_frames(
        provider, [VisualFrame(5.0, image)], segments
    )
    monkeypatch.setattr("meet.extract.get_provider", lambda _settings: provider)

    extract(
        segments,
        ["me"],
        Settings(llm_provider="anthropic", anthropic_api_key="fake"),
        visual_observations=observations,
    )

    assert observations[0]["description"] == "Campo CNPJ vazio"
    assert "[00:00:05] TELA: Campo CNPJ vazio" in provider.text_calls[0]
    assert "texto visível: Cadastro, CNPJ" in provider.text_calls[0]


def test_provider_sem_visao_degrada_para_lista_vazia(tmp_path: Path) -> None:
    class TextOnlyProvider(LLMProvider):
        def complete(self, system: str, user: str) -> str:
            return "{}"

    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpeg")

    assert analyze_visual_frames(
        TextOnlyProvider(), [VisualFrame(5.0, image)], []
    ) == []
