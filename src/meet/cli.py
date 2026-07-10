"""Interface de linha de comando para processamento de reuniões."""

from __future__ import annotations

import shutil
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
    no_import: Annotated[
        bool,
        typer.Option(
            "--no-import",
            help="Não copiar o vídeo para ~/.local/share/meet/media/{id}/",
        ),
    ] = False,
) -> None:
    """Processa uma gravação de reunião: áudio → transcript → diarização → action items."""
    from .pipeline import run_pipeline

    if not video.exists():
        err_console.print(f"[red]Erro:[/red] Arquivo não encontrado: {video}")
        raise typer.Exit(1)

    try:
        settings, store = _load_store()
    except Exception as exc:
        err_console.print(f"[red]Erro ao carregar configurações:[/red] {exc}")
        raise typer.Exit(1)

    def on_progress(msg: str) -> None:
        console.print(f"[cyan]{msg}[/cyan]")

    try:
        meeting_id, result, md_path = run_pipeline(
            video,
            settings=settings,
            store=store,
            title=title,
            mic_track=mic_track,
            others_track=others_track,
            no_llm=no_llm,
            keep_wav=keep_wav,
            import_media=not no_import,
            on_progress=on_progress,
        )
    except Exception as exc:
        err_console.print(f"[red]Erro:[/red] {exc}")
        raise typer.Exit(1)

    console.print(f"\n[bold green]✓ Reunião {meeting_id}:[/bold green] {result.title}")
    console.print(f"  Data: {result.date} | Duração: {_fmt_duration(result.duration)}")
    console.print(f"  Arquivo: {md_path}")

    if result.summary:
        console.print(f"\n[bold]Resumo:[/bold]\n{result.summary}")

    if result.action_items:
        tbl = Table(title="Action Items", show_lines=True)
        tbl.add_column("O quê", style="bold")
        tbl.add_column("Onde")
        tbl.add_column("Prioridade")
        for item in result.action_items:
            tbl.add_row(item.what, item.where or "", item.priority)
        console.print(tbl)

    speakers = {s.speaker for s in result.segments if s.speaker}
    unresolved = sorted(s for s in speakers if s and s.startswith("SPEAKER_"))
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
# meet serve (UI web)
# ---------------------------------------------------------------------------


def _port_in_use(host: str, port: int) -> int | None:
    """Retorna PID que escuta em host:port, ou None se livre."""
    import socket

    # Tentativa de bind: se falhar, tenta achar o PID via /proc
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return None
        except OSError:
            pass

    try:
        import subprocess

        out = subprocess.run(
            ["ss", "-ltnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        import re

        m = re.search(r"pid=(\d+)", out)
        return int(m.group(1)) if m else -1
    except Exception:
        return -1


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Porta HTTP")] = 8741,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Abrir navegador ao subir"),
    ] = True,
) -> None:
    """Sobe a interface web local para gerir reuniões."""
    try:
        import uvicorn
    except ImportError as exc:
        err_console.print(
            "[red]Dependências web ausentes.[/red] Rode: uv sync"
        )
        raise typer.Exit(1) from exc

    busy = _port_in_use(host, port)
    if busy is not None:
        err_console.print(
            f"[red]Porta {port} já em uso[/red]"
            + (f" (pid {busy})" if busy > 0 else "")
            + "."
        )
        if busy and busy > 0:
            kill_hint = f"kill {busy}"
        else:
            kill_hint = f"fuser -k {port}/tcp"
        err_console.print(
            "[dim]Encerre o processo antigo e tente de novo:[/dim]\n"
            f"  {kill_hint}\n"
            f"  # ou outra porta: uv run meet serve -p {port + 1}"
        )
        raise typer.Exit(1)

    from .web.app import create_app

    url = f"http://{host}:{port}"
    console.print(f"[bold green]meet UI[/bold green] → {url}")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(create_app(), host=host, port=port, log_level="info")



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
# meet play / meet mix — ouvir gravação multi-track
# ---------------------------------------------------------------------------


def _resolve_player() -> str | None:
    """Retorna o player disponível (mpv preferido, depois ffplay)."""
    for name in ("mpv", "ffplay"):
        if shutil.which(name):
            return name
    return None


@app.command()
def play(
    video: Annotated[Path, typer.Argument(help="Gravação multi-track (mkv/mp4)")],
    mic_track: Annotated[
        int,
        typer.Option("--mic-track", help="Índice da faixa do microfone, 1-based"),
    ] = 1,
    others_track: Annotated[
        int,
        typer.Option("--others-track", help="Índice da faixa desktop/Discord, 1-based"),
    ] = 2,
) -> None:
    """Toca a gravação com mic + desktop misturados (pra ouvir a reunião completa)."""
    import subprocess

    from . import audio as audio_mod

    if not video.exists():
        err_console.print(f"[red]Erro:[/red] Arquivo não encontrado: {video}")
        raise typer.Exit(1)

    player = _resolve_player()
    if player is None:
        err_console.print(
            "[red]Erro:[/red] Nenhum player encontrado (instale mpv ou ffplay)."
        )
        err_console.print(
            "[dim]Alternativa: uv run meet mix VIDEO  → gera um .listen.m4a[/dim]"
        )
        raise typer.Exit(1)

    try:
        n = audio_mod.probe_audio_streams(video)
    except Exception as exc:
        err_console.print(f"[red]Erro ao ler áudio:[/red] {exc}")
        raise typer.Exit(1)

    if n < 2:
        console.print("[dim]1 track só — tocando direto.[/dim]")
        cmd = [player, str(video)] if player == "mpv" else [player, "-autoexit", str(video)]
        raise typer.Exit(subprocess.call(cmd))

    mic_idx = mic_track - 1
    others_idx = others_track - 1
    console.print(
        f"[dim]Misturando tracks {mic_track}+{others_track} → {player}…[/dim]"
    )

    if player == "mpv":
        # aid1/aid2 = 1-based stream indices no mpv
        lavfi = (
            f"[aid{mic_track}][aid{others_track}]"
            f"amix=inputs=2:duration=longest:normalize=0[ao]"
        )
        cmd = ["mpv", f"--lavfi-complex={lavfi}", str(video)]
        raise typer.Exit(subprocess.call(cmd))

    # ffplay: amix via filter_complex
    fc = (
        f"[0:a:{mic_idx}][0:a:{others_idx}]"
        f"amix=inputs=2:duration=longest:normalize=0[a]"
    )
    cmd = [
        "ffplay", "-autoexit",
        "-i", str(video),
        "-filter_complex", fc,
        "-map", "[a]",
    ]
    raise typer.Exit(subprocess.call(cmd))


@app.command()
def mix(
    video: Annotated[Path, typer.Argument(help="Gravação multi-track (mkv/mp4)")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Arquivo de saída (default: VIDEO.listen.m4a)"),
    ] = None,
    mic_track: Annotated[
        int,
        typer.Option("--mic-track", help="Índice da faixa do microfone, 1-based"),
    ] = 1,
    others_track: Annotated[
        int,
        typer.Option("--others-track", help="Índice da faixa desktop/Discord, 1-based"),
    ] = 2,
) -> None:
    """Exporta um .m4a com mic + desktop misturados pra ouvir depois (duplo-clique)."""
    from . import audio as audio_mod

    if not video.exists():
        err_console.print(f"[red]Erro:[/red] Arquivo não encontrado: {video}")
        raise typer.Exit(1)

    try:
        out = audio_mod.export_listen_mix(
            video, output, mic_track=mic_track, others_track=others_track
        )
    except Exception as exc:
        err_console.print(f"[red]Erro ao misturar:[/red] {exc}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Mix de ouvir: [bold]{out}[/bold]")
    console.print("[dim]Abre no player normal — as duas vozes juntas.[/dim]")


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
