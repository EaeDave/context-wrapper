"""Testes do contrato e cálculo de progresso estruturado."""

from __future__ import annotations

from meet.progress import ProgressTracker, ProgressUpdate, StepSpec


def test_tracker_calcula_progresso_ponderado_e_estados() -> None:
    updates: list[ProgressUpdate] = []
    tracker = ProgressTracker(
        (
            StepSpec("audio", "Preparar áudio", 1.0),
            StepSpec("transcribe", "Transcrever", 3.0),
        ),
        updates.append,
    )

    tracker.start("audio", "Preparando")
    tracker.update(0.5, "Metade do áudio")
    tracker.start("transcribe", "Transcrevendo")
    tracker.update(0.5, "05:00 de 10:00")

    update = updates[-1]
    assert update.percent == 62.5
    assert update.step == "transcribe"
    assert update.step_percent == 50.0
    assert [step.state for step in update.steps] == ["done", "running"]


def test_tracker_cronometra_total_e_etapas() -> None:
    now = [100.0]
    updates: list[ProgressUpdate] = []
    tracker = ProgressTracker(
        (
            StepSpec("audio", "Preparar áudio", 1.0),
            StepSpec("transcribe", "Transcrever", 1.0),
        ),
        updates.append,
        clock=lambda: now[0],
    )

    tracker.start("audio")
    now[0] = 103.0
    tracker.start("transcribe")
    now[0] = 108.0
    tracker.finish()

    assert updates[-1].elapsed_seconds == 8.0
    assert [step.elapsed_seconds for step in updates[-1].steps] == [3.0, 5.0]


def test_json_legado_sem_tempos_continua_compativel() -> None:
    restored = ProgressUpdate.from_dict(
        {
            "percent": 50,
            "step": "audio",
            "step_label": "Áudio",
            "step_percent": 50,
            "detail": "Extraindo",
            "steps": [{"key": "audio", "label": "Áudio", "state": "running"}],
        }
    )

    assert restored.elapsed_seconds == 0.0
    assert restored.steps[0].elapsed_seconds is None


def test_tracker_representa_etapa_indeterminada_sem_porcentagem_falsa() -> None:
    updates: list[ProgressUpdate] = []
    tracker = ProgressTracker(
        (StepSpec("llm", "Gerar resumo", 1.0),),
        updates.append,
    )

    tracker.start("llm", "Aguardando LLM", determinate=False)

    assert updates[-1].percent == 0.0
    assert updates[-1].step_percent is None
    assert updates[-1].steps[0].state == "running"


def test_indeterminado_preserva_ultimo_avanco_sem_recuar() -> None:
    updates: list[ProgressUpdate] = []
    tracker = ProgressTracker(
        (StepSpec("diarize", "Identificar falantes", 1.0),),
        updates.append,
    )
    tracker.start("diarize")
    tracker.update(0.6, "Segmentando")
    tracker.update(None, "Carregando embeddings")

    assert updates[-1].percent == 60.0
    assert updates[-1].step_percent is None
    assert [update.percent for update in updates] == sorted(
        update.percent for update in updates
    )


def test_tracker_terminal_e_round_trip_json() -> None:
    updates: list[ProgressUpdate] = []
    tracker = ProgressTracker(
        (StepSpec("save", "Salvar", 1.0),),
        updates.append,
    )
    tracker.start("save", "Salvando")
    tracker.finish("Concluído")

    restored = ProgressUpdate.from_dict(updates[-1].to_dict())
    assert restored == updates[-1]
    assert restored.percent == 100.0
    assert restored.step_percent == 100.0
    assert restored.steps[0].state == "done"


def test_finish_preserva_erro_nao_fatal_e_detalhe() -> None:
    updates: list[ProgressUpdate] = []
    tracker = ProgressTracker(
        (StepSpec("save", "Salvar", 1.0), StepSpec("import", "Importar", 1.0)),
        updates.append,
    )
    tracker.start("save")
    tracker.start("import")
    tracker.mark_current_error("Mídia não importada: disco cheio")
    tracker.finish("Concluído")

    assert updates[-1].percent == 100.0
    assert [step.state for step in updates[-1].steps] == ["done", "error"]
    assert updates[-1].detail == "Mídia não importada: disco cheio"


def test_failed_marca_apenas_etapa_atual() -> None:
    updates: list[ProgressUpdate] = []
    tracker = ProgressTracker(
        (
            StepSpec("audio", "Áudio", 1.0),
            StepSpec("llm", "LLM", 1.0),
            StepSpec("save", "Salvar", 1.0),
        ),
        updates.append,
    )
    tracker.start("audio")
    tracker.start("llm", determinate=False)

    failed = updates[-1].failed("Credencial expirada")

    assert [step.state for step in failed.steps] == ["done", "error", "pending"]
    assert failed.detail == "Credencial expirada"
