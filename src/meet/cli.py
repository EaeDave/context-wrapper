"""Interface de linha de comando para processamento de reuniões."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Pipeline gravação → transcript → action items")
speakers_app = typer.Typer(help="Gerenciar banco de vozes")
app.add_typer(speakers_app, name="speakers")

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Utilitários internos
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    """Formata segundos como h:mm."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    return f"{h}:{m:02d}"


def _load_store() -> tuple:
    """Carrega settings + Store; retorna (settings, store)."""
    from .config import load_settings
    from .store import Store

    settings = load_settings()
    store = Store(settings.db_path)
    return settings, store


# ---------------------------------------------------------------------------
# meet process
# ---------------------------------------------------------------------------


@app.command()
def process(
    video: Annotated[Path, typer.Argument(help="Arquivo de vídeo/áudio da reunião")],
    title: Annotated[
        str | None,
        typer.Option("--title", "-t", help="Título da reunião (sobrescreve sugestão do LLM)"),
    ] = None,
    mic_track: Annotated[
        int,
        typer.Option("--mic-track", help="Índice da faixa do microfone, 1-based"),
    ] = 1,
    others_track: Annotated[
        int,
        typer.Option("--others-track", help="Índice das faixas dos outros participantes, 1-based"),
    ] = 2,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Pular extração de action items via LLM"),
    ] = False,
    keep_wav: Annotated[
        bool,
        typer.Option("--keep-wav", help="Manter arquivos wav temporários após processamento"),
    ] = False,
) -> None:
    """Processa uma gravação de reunião: áudio → transcript → diarização → action items."""
    from datetime import date

    if not video.exists():
        err_console.print(f"[red]Erro:[/red] Arquivo não encontrado: {video}")
        raise typer.Exit(1)

    try:
        settings, store = _load_store()
    except Exception as exc:
        err_console.print(f"[red]Erro ao carregar configurações:[/red] {exc}")
        raise typer.Exit(1)

    workdir = Path(tempfile.mkdtemp(prefix="meet-"))
    try:
        _run_pipeline(
            video=video,
            title=title,
            mic_track=mic_track,
            others_track=others_track,
            no_llm=no_llm,
            settings=settings,
            store=store,
            workdir=workdir,
            today=date.today().isoformat(),
        )
    except typer.Exit:
        raise
    except Exception as exc:
        err_console.print(f"[red]Erro inesperado:[/red] {exc}")
        raise typer.Exit(1)
    finally:
        if not keep_wav:
            shutil.rmtree(workdir, ignore_errors=True)


def _run_pipeline(  # noqa: PLR0912, PLR0915
    *,
    video: Path,
    title: str | None,
    mic_track: int,
    others_track: int,
    no_llm: bool,
    settings: object,
    store: object,
    workdir: Path,
    today: str,
) -> None:
    """Núcleo do pipeline; todos os imports pesados ficam aqui."""
    # Imports pesados sempre dentro da função
    from . import audio as audio_mod
    from . import diarize as diarize_mod
    from . import merge as merge_mod
    from . import transcribe as transcribe_mod
    from . import voicebank as voicebank_mod
    from .models import ActionItem, MeetingResult
    from .store import Store

    assert isinstance(store, Store)

    # --- Preparar áudio ---
    console.print("[cyan]Preparando áudio...[/cyan]")
    try:
        tracks = audio_mod.prepare(video, workdir, mic_track, others_track)
    except Exception as exc:
        err_console.print(f"[red]Erro ao preparar áudio:[/red] {exc}")
        raise typer.Exit(1)

    # --- Transcrição + Diarização ---
    embeddings: dict[str, object] = {}

    if tracks.mic is not None:
        # Dual-track: microfone separado dos demais participantes
        console.print("[cyan]Transcrevendo microfone...[/cyan]")
        try:
            mic_segs = transcribe_mod.transcribe(tracks.mic, settings)
        except Exception as exc:
            err_console.print(f"[red]Erro ao transcrever microfone:[/red] {exc}")
            raise typer.Exit(1)

        console.print("[cyan]Transcrevendo outros participantes...[/cyan]")
        try:
            others_segs = transcribe_mod.transcribe(tracks.others, settings)
        except Exception as exc:
            err_console.print(f"[red]Erro ao transcrever outros participantes:[/red] {exc}")
            raise typer.Exit(1)

        console.print("[cyan]Diarizando...[/cyan]")
        try:
            turns, embeddings = diarize_mod.diarize(tracks.others, settings)
        except RuntimeError as exc:
            err_console.print(f"[red]Erro na diarização:[/red] {exc}")
            raise typer.Exit(1)
        except Exception as exc:
            err_console.print(f"[red]Erro inesperado na diarização:[/red] {exc}")
            raise typer.Exit(1)

        others_segs = merge_mod.assign_speakers(others_segs, turns)
        segments = merge_mod.combine(mic_segs, others_segs)
    else:
        # Single-track: mixdown
        console.print(
            "[yellow]Aviso: gravação com 1 track de áudio só — sem separação"
            " automática da sua voz ('me'). Se gravou no OBS, use formato mkv"
            " com Audio Track 1 e 2 (mp4 mantém apenas a track 1).[/yellow]"
        )
        console.print("[cyan]Transcrevendo...[/cyan]")
        try:
            segments = transcribe_mod.transcribe(tracks.mixed, settings)
        except Exception as exc:
            err_console.print(f"[red]Erro ao transcrever:[/red] {exc}")
            raise typer.Exit(1)

        console.print("[cyan]Diarizando...[/cyan]")
        try:
            turns, embeddings = diarize_mod.diarize(tracks.mixed, settings)
        except RuntimeError as exc:
            err_console.print(f"[red]Erro na diarização:[/red] {exc}")
            raise typer.Exit(1)
        except Exception as exc:
            err_console.print(f"[red]Erro inesperado na diarização:[/red] {exc}")
            raise typer.Exit(1)

        segments = merge_mod.assign_speakers(segments, turns)

    # --- Banco de vozes ---
    console.print("[cyan]Resolvendo falantes...[/cyan]")
    mapping = voicebank_mod.resolve(embeddings, store, settings.similarity_threshold)
    unresolved = [label for label, name in mapping.items() if label == name]
    segments = merge_mod.rename_speakers(segments, mapping)
    participants = sorted({s.speaker for s in segments if s.speaker})

    # --- Extração LLM ---
    summary = ""
    action_items: list[ActionItem] = []
    suggested_title = ""

    if not no_llm:
        console.print("[cyan]Extraindo action items com LLM...[/cyan]")
        from . import extract as extract_mod

        try:
            summary, action_items, suggested_title = extract_mod.extract(
                segments, participants, settings
            )
        except ValueError as exc:
            err_console.print(f"[red]Erro ao parsear resposta do LLM:[/red] {exc}")
            raise typer.Exit(1)
        except Exception as exc:
            err_console.print(f"[red]Erro na extração LLM:[/red] {exc}")
            raise typer.Exit(1)

    # --- Montar resultado ---
    meeting_title = title or suggested_title or video.stem

    result = MeetingResult(
        source=str(video),
        date=today,
        title=meeting_title,
        duration=tracks.duration,
        participants=participants,
        summary=summary,
        action_items=action_items,
        segments=segments,
    )

    # --- Render e salvar markdown ---
    from . import render as render_mod

    md_content = render_mod.to_markdown(result)
    filename = render_mod.meeting_filename(result)
    md_path = settings.output_dir / filename
    md_path.write_text(md_content, encoding="utf-8")

    meeting_id = store.save_meeting(result, md_path)

    # --- Embeddings pendentes (falantes não reconhecidos) ---
    if unresolved:
        import numpy as np

        pending_dir = settings.data_dir / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_data = {lbl: embeddings[lbl] for lbl in unresolved if lbl in embeddings}
        if pending_data:
            np.savez(str(pending_dir / f"{meeting_id}.npz"), **pending_data)

    # --- Saída ---
    console.print(f"\n[bold green]✓ Reunião {meeting_id}:[/bold green] {meeting_title}")
    console.print(f"  Data: {today} | Duração: {_fmt_duration(tracks.duration)}")
    console.print(f"  Arquivo: {md_path}")

    if summary:
        console.print(f"\n[bold]Resumo:[/bold]\n{summary}")

    if action_items:
        tbl = Table(title="Action Items", show_lines=True)
        tbl.add_column("O quê", style="bold")
        tbl.add_column("Onde")
        tbl.add_column("Prioridade")
        for item in action_items:
            tbl.add_row(item.what, item.where or "", item.priority)
        console.print(tbl)

    if unresolved:
        console.print(
            "\n[yellow]Falantes não reconhecidos:[/yellow] " + ", ".join(unresolved)
        )
        console.print("[dim]Para identificar cada falante:[/dim]")
        for lbl in unresolved:
            console.print(
                f"  [cyan]meet speakers assign {meeting_id} {lbl} NOME[/cyan]"
            )


# ---------------------------------------------------------------------------
# meet speakers *
# ---------------------------------------------------------------------------


@speakers_app.command("list")
def speakers_list() -> None:
    """Lista nomes no banco de vozes."""
    _, store = _load_store()
    voices = store.all_voices()
    if not voices:
        console.print("[dim]Banco de vozes vazio.[/dim]")
        return
    tbl = Table(title="Banco de vozes")
    tbl.add_column("Nome")
    tbl.add_column("Embedding", style="dim")
    for name, blob in sorted(voices.items()):
        tbl.add_row(name, f"{len(blob) // 4} floats")
    console.print(tbl)


@speakers_app.command("assign")
def speakers_assign(
    meeting_id: Annotated[int, typer.Argument(help="ID da reunião")],
    label: Annotated[str, typer.Argument(help="Label original (ex: SPEAKER_00)")],
    name: Annotated[str, typer.Argument(help="Nome real do falante")],
) -> None:
    """Atribui nome a um falante e o cadastra no banco de vozes."""
    import numpy as np

    from . import voicebank as voicebank_mod

    settings, store = _load_store()

    pending_path = settings.data_dir / "pending" / f"{meeting_id}.npz"
    if not pending_path.exists():
        err_console.print(
            f"[red]Erro:[/red] Sem embeddings pendentes para reunião {meeting_id}."
            f" (esperado em {pending_path})"
        )
        raise typer.Exit(1)

    data = np.load(str(pending_path))
    if label not in data:
        available = ", ".join(data.files)
        err_console.print(
            f"[red]Erro:[/red] Label '{label}' não encontrado em {pending_path}."
            f" Disponíveis: {available}"
        )
        raise typer.Exit(1)

    embedding = data[label]
    voicebank_mod.enroll(name, embedding, store)
    store.update_speaker(meeting_id, label, name)

    # Regenera markdown com falante renomeado
    result = store.get_meeting(meeting_id)
    if result is None:
        err_console.print(f"[red]Erro:[/red] Reunião {meeting_id} não encontrada no banco.")
        raise typer.Exit(1)

    from . import render as render_mod

    md_content = render_mod.to_markdown(result)
    result.md_path.write_text(md_content, encoding="utf-8")  # type: ignore[attr-defined]

    console.print(
        f"[green]✓[/green] Falante '{label}' identificado como '{name}'"
        f" na reunião {meeting_id}."
    )
    console.print(f"  Markdown atualizado: {result.md_path}")  # type: ignore[attr-defined]


@speakers_app.command("rename")
def speakers_rename(
    old: Annotated[str, typer.Argument(help="Nome atual no banco de vozes")],
    new: Annotated[str, typer.Argument(help="Novo nome")],
) -> None:
    """Renomeia uma voz no banco (aplica a reuniões futuras)."""
    settings, store = _load_store()

    blob = store.get_voice(old)
    if blob is None:
        err_console.print(f"[red]Erro:[/red] Voz '{old}' não encontrada no banco.")
        raise typer.Exit(1)

    store.upsert_voice(new, blob)
    store.delete_voice(old)

    console.print(f"[green]✓[/green] Voz '{old}' renomeada para '{new}'.")


@speakers_app.command("rm")
def speakers_rm(
    name: Annotated[str, typer.Argument(help="Nome no banco de vozes")],
) -> None:
    """Remove uma voz do banco (ex.: cadastro feito com áudio ruim)."""
    _, store = _load_store()

    if store.get_voice(name) is None:
        err_console.print(f"[red]Erro:[/red] Voz '{name}' não encontrada no banco.")
        raise typer.Exit(1)

    store.delete_voice(name)
    console.print(f"[green]✓[/green] Voz '{name}' removida do banco.")


# ---------------------------------------------------------------------------
# meet search
# ---------------------------------------------------------------------------


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Termos de busca (sintaxe FTS5)")],
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Máximo de resultados"),
    ] = 20,
) -> None:
    """Busca no histórico de reuniões via FTS5."""
    _, store = _load_store()

    try:
        results = store.search(query, limit)
    except Exception as exc:
        err_console.print(f"[red]Erro na busca:[/red] {exc}")
        raise typer.Exit(1)

    if not results:
        console.print("[dim]Nenhum resultado encontrado.[/dim]")
        return

    tbl = Table(title=f"Resultados: {query}", show_lines=True)
    tbl.add_column("ID", style="dim", width=5)
    tbl.add_column("Data", width=12)
    tbl.add_column("Título")
    tbl.add_column("Tipo", width=12)
    tbl.add_column("Trecho")

    for row in results:
        tbl.add_row(
            str(row["meeting_id"]),
            row["date"],
            row["title"],
            row["kind"],
            row["snippet"],
        )

    console.print(tbl)


# ---------------------------------------------------------------------------
# meet list
# ---------------------------------------------------------------------------


@app.command("list")
def list_meetings() -> None:
    """Lista reuniões processadas."""
    _, store = _load_store()
    meetings = store.list_meetings()

    if not meetings:
        console.print("[dim]Nenhuma reunião encontrada.[/dim]")
        return

    tbl = Table(title="Reuniões", show_lines=False)
    tbl.add_column("ID", style="dim", width=5)
    tbl.add_column("Data", width=12)
    tbl.add_column("Título")

    for meeting_id, date, title in meetings:
        tbl.add_row(str(meeting_id), date, title)

    console.print(tbl)
