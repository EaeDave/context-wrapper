"""Extração e preparação de áudio a partir de vídeos de reunião."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
import json
import subprocess
from pathlib import Path

from .models import AudioTracks

AudioProgress = Callable[[float], None]

_WAV_OPTS = ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"]
# Nivelamento de fala: vozes gravadas baixas (mic com pouco ganho, participante
# remoto quieto) escapam do VAD do whisper; speechnorm levanta até 12.5x.
_SPEECHNORM = "speechnorm=e=12.5:r=0.0001:l=1"

# Timeouts: file corrompido não pode travar a fila FIFO de jobs.
_FFPROBE_TIMEOUT_S = 30.0
_FFMPEG_MIN_TIMEOUT_S = 120.0
_FFMPEG_DEFAULT_TIMEOUT_S = 600.0


def _ffprobe_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ffprobe timeout após {_FFPROBE_TIMEOUT_S:.0f}s — arquivo corrompido ou ilegível?"
        ) from exc


def _ffmpeg_timeout(duration: float | None) -> float:
    if duration is not None and duration > 0:
        # 4× duração + margem, com piso; re-encode pesado de 2h não deve estourar cedo
        return max(_FFMPEG_MIN_TIMEOUT_S, duration * 4.0 + 60.0)
    return _FFMPEG_DEFAULT_TIMEOUT_S


def probe_audio_streams(input_path: Path) -> int:
    """Retorna o número de streams de áudio no arquivo via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "a",
        str(input_path),
    ]
    proc = _ffprobe_run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe falhou ao listar streams: {proc.stderr[:500]}")
    data = json.loads(proc.stdout)
    return len(data.get("streams", []))


def _probe_duration(input_path: Path) -> float:
    """Retorna duração em segundos do container via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(input_path),
    ]
    proc = _ffprobe_run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe falhou ao ler duração: {proc.stderr[:500]}")
    data = json.loads(proc.stdout)
    return float(data["format"]["duration"])


def _run_ffmpeg(
    args: list[str],
    *,
    duration: float | None = None,
    on_progress: AudioProgress | None = None,
    timeout: float | None = None,
) -> None:
    """Executa ffmpeg e reporta avanço temporal quando a duração é conhecida.

    ``timeout`` em segundos; se omitido, deriva da duração do media.
    """
    limit = timeout if timeout is not None else _ffmpeg_timeout(duration)
    deadline = time.monotonic() + limit
    with tempfile.TemporaryFile(mode="w+") as errors:
        proc = subprocess.Popen(
            ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats", *args],
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if time.monotonic() > deadline:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise RuntimeError(
                        f"ffmpeg timeout após {limit:.0f}s — arquivo corrompido ou travado?"
                    )
                if not line.startswith("out_time_us=") or not duration or duration <= 0:
                    continue
                try:
                    elapsed = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                if on_progress is not None:
                    on_progress(min(elapsed / duration, 1.0))
            remaining = max(0.1, deadline - time.monotonic())
            try:
                returncode = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError(
                    f"ffmpeg timeout após {limit:.0f}s — arquivo corrompido ou travado?"
                ) from None
        except Exception:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            raise
        if returncode != 0:
            errors.seek(0)
            raise RuntimeError(f"ffmpeg falhou: {errors.read()[-500:]}")
    if on_progress is not None:
        on_progress(1.0)


def prepare(
    input_path: Path,
    workdir: Path,
    mic_track: int = 1,
    others_track: int = 2,
    on_progress: AudioProgress | None = None,
) -> AudioTracks:
    """Extrai streams de áudio para wav 16 kHz mono pcm_s16le.

    1 stream → mic=None, others==mixed (mesmo arquivo wav).
    ≥2 streams → mic e others separados (1-based) em uma execução ffmpeg.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    n_streams = probe_audio_streams(input_path)
    duration = _probe_duration(input_path)

    command_count = 1
    completed_commands = 0

    def command_progress(fraction: float) -> None:
        if on_progress is not None:
            on_progress((completed_commands + fraction) / command_count)

    def run(args: list[str]) -> None:
        nonlocal completed_commands
        _run_ffmpeg(
            args,
            duration=duration,
            on_progress=command_progress,
        )
        completed_commands += 1

    if n_streams < 2:
        mixed = workdir / "mixed.wav"
        run([
            "-i", str(input_path),
            "-map", "0:a:0",
            "-af", _SPEECHNORM,
            *_WAV_OPTS,
            str(mixed),
        ])
        return AudioTracks(mic=None, others=mixed, mixed=mixed, duration=duration)

    mic_idx = mic_track - 1
    others_idx = others_track - 1

    mic_path = workdir / "mic.wav"
    others_path = workdir / "others.wav"

    run([
        "-i", str(input_path),
        "-map", f"0:a:{mic_idx}",
        "-af", _SPEECHNORM,
        *_WAV_OPTS,
        str(mic_path),
        "-map", f"0:a:{others_idx}",
        "-af", _SPEECHNORM,
        *_WAV_OPTS,
        str(others_path),
    ])

    return AudioTracks(
        mic=mic_path,
        others=others_path,
        mixed=others_path,
        duration=duration,
    )


def listen_mix_path(input_path: Path) -> Path:
    """Path padrão do mix de áudio ao lado da gravação."""
    return input_path.with_name(f"{input_path.stem}.listen.m4a")


# Qualidades de preview pro player (Plyr quality menu).
# web  = leve (≤1280px) | full = resolução original (ainda re-encoded p/ browser)
PREVIEW_WEB = "web"
PREVIEW_FULL = "full"
_PREVIEW_MAX_WIDTH = {PREVIEW_WEB: 1280, PREVIEW_FULL: 0}  # 0 = sem downscale


def listen_preview_path(input_path: Path, quality: str = PREVIEW_WEB) -> Path:
    """Path do preview: ``.listen.mp4`` (web) ou ``.listen.full.mp4`` (original)."""
    q = _normalize_quality(quality)
    if q == PREVIEW_FULL:
        return input_path.with_name(f"{input_path.stem}.listen.full.mp4")
    return input_path.with_name(f"{input_path.stem}.listen.mp4")


def _normalize_quality(quality: str) -> str:
    q = (quality or PREVIEW_WEB).strip().lower()
    if q in ("full", "orig", "original", "source", "0"):
        return PREVIEW_FULL
    return PREVIEW_WEB


def probe_video_streams(input_path: Path) -> int:
    """Retorna o número de streams de vídeo no arquivo via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "v",
        str(input_path),
    ]
    proc = _ffprobe_run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe falhou ao listar vídeo: {proc.stderr[:500]}")
    data = json.loads(proc.stdout)
    return len(data.get("streams", []))


def probe_video_size(input_path: Path) -> tuple[int, int]:
    """(width, height) do primeiro stream de vídeo; (0, 0) se não houver."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "v:0",
        str(input_path),
    ]
    try:
        proc = _ffprobe_run(cmd)
    except RuntimeError:
        return 0, 0
    if proc.returncode != 0:
        return 0, 0
    streams = json.loads(proc.stdout).get("streams") or []
    if not streams:
        return 0, 0
    return int(streams[0].get("width") or 0), int(streams[0].get("height") or 0)


def _cache_is_fresh(out: Path, source: Path) -> bool:
    return (
        out.is_file()
        and out.stat().st_size > 0
        and out.stat().st_mtime >= source.stat().st_mtime
    )


def ensure_listen_mix(
    input_path: Path,
    *,
    force: bool = False,
    mic_track: int = 1,
    others_track: int = 2,
    output_path: Path | None = None,
) -> Path:
    """Retorna o .listen.m4a, gerando se ainda não existir (ou se force=True).

    Reusa o cache se for mais novo que a gravação-fonte.
    """
    input_path = Path(input_path)
    out = Path(output_path) if output_path else listen_mix_path(input_path)
    if not force and _cache_is_fresh(out, input_path):
        return out
    return export_listen_mix(
        input_path,
        out,
        mic_track=mic_track,
        others_track=others_track,
    )


def _preview_is_browser_safe(path: Path, quality: str = PREVIEW_WEB) -> bool:
    """True se o mp4 é re-encode browser-safe (não o High@L5.1 copy do OBS)."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "v:0",
            str(path),
        ]
        proc = _ffprobe_run(cmd)
        if proc.returncode != 0:
            return False
        streams = json.loads(proc.stdout).get("streams") or []
        if not streams:
            return False
        v = streams[0]
        if v.get("codec_name") != "h264":
            return False
        profile = (v.get("profile") or "").lower()
        width = int(v.get("width") or 0)
        # Só aceitamos Main/Baseline — o copy quebrado do OBS era High@L5.1
        if "main" not in profile and "baseline" not in profile:
            return False
        q = _normalize_quality(quality)
        if q == PREVIEW_WEB and width > 1280:
            return False
        return True
    except Exception:
        return False


def ensure_listen_preview(
    input_path: Path,
    *,
    force: bool = False,
    mic_track: int = 1,
    others_track: int = 2,
    output_path: Path | None = None,
    quality: str = PREVIEW_WEB,
) -> Path:
    """Retorna preview mp4 (web ou full), gerando se faltar.

    Sem stream de vídeo no arquivo-fonte, cai no mix só de áudio (.listen.m4a).
    Invalida cache antigo (copy High@L5.1) que o browser não toca.
    """
    input_path = Path(input_path)
    q = _normalize_quality(quality)
    if probe_video_streams(input_path) < 1:
        return ensure_listen_mix(
            input_path,
            force=force,
            mic_track=mic_track,
            others_track=others_track,
        )
    out = Path(output_path) if output_path else listen_preview_path(input_path, q)
    if (
        not force
        and _cache_is_fresh(out, input_path)
        and _preview_is_browser_safe(out, q)
    ):
        return out
    max_w = _PREVIEW_MAX_WIDTH[q]
    return export_listen_preview(
        input_path,
        out,
        mic_track=mic_track,
        others_track=others_track,
        max_width=max_w,
        quality=q,
    )


def export_listen_preview(
    input_path: Path,
    output_path: Path | None = None,
    *,
    mic_track: int = 1,
    others_track: int = 2,
    max_width: int = 1280,
    quality: str = PREVIEW_WEB,
) -> Path:
    """Gera mp4 browser-safe: H.264 Main + áudio mic/desktop misturados.

    Não usa ``-c:v copy``: o bitstream OBS High@L5.1 2560×1080@60 falha em
    vários browsers. Re-encode + ``faststart``.

    - ``max_width > 0``: limita largura (qualidade web / 720p-ish)
    - ``max_width == 0``: mantém resolução original (qualidade full)
    """
    n_audio = probe_audio_streams(input_path)
    if probe_video_streams(input_path) < 1:
        raise RuntimeError("Arquivo sem stream de vídeo — use export_listen_mix")

    q = _normalize_quality(quality)
    if output_path is None:
        output_path = listen_preview_path(input_path, q)
    output_path = Path(output_path)

    # full = sem downscale, só garante yuv420p; web = scale ≤ max_width
    if max_width and max_width > 0:
        vf = f"scale='min({max_width},iw)':-2,format=yuv420p"
        crf, level, abitrate = "22", "4.0", "160k"
    else:
        vf = "format=yuv420p"
        # original res @60fps precisa L5.1; Main profile ainda é browser-ok
        crf, level, abitrate = "18", "5.1", "192k"

    video_audio_out = [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", crf,
        "-profile:v", "main",
        "-level", level,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", abitrate,
        "-movflags", "+faststart",
        str(output_path),
    ]

    if n_audio < 2:
        _run_ffmpeg([
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-vf", vf,
            *video_audio_out,
        ])
        return output_path

    mic_idx = mic_track - 1
    others_idx = others_track - 1
    filter_str = (
        f"[0:v:0]{vf}[v];"
        f"[0:a:{mic_idx}][0:a:{others_idx}]"
        f"amix=inputs=2:duration=longest:normalize=0,"
        f"alimiter=limit=0.95[a]"
    )
    _run_ffmpeg([
        "-i", str(input_path),
        "-filter_complex", filter_str,
        "-map", "[v]",
        "-map", "[a]",
        *video_audio_out,
    ])
    return output_path


def export_listen_mix(
    input_path: Path,
    output_path: Path | None = None,
    *,
    mic_track: int = 1,
    others_track: int = 2,
) -> Path:
    """Gera um arquivo de ouvir com as tracks misturadas (mic + desktop).

    Saída padrão: mesmo diretório/nome do vídeo com sufixo ``.listen.m4a``.
    1 stream de áudio → remux/reencode da track única (sem amix).
    """
    n_streams = probe_audio_streams(input_path)
    if output_path is None:
        output_path = listen_mix_path(input_path)
    output_path = Path(output_path)

    if n_streams < 2:
        _run_ffmpeg([
            "-i", str(input_path),
            "-map", "0:a:0",
            "-c:a", "aac",
            "-b:a", "192k",
            str(output_path),
        ])
        return output_path

    mic_idx = mic_track - 1
    others_idx = others_track - 1
    # normalize=0: amix default divide por N e abaixa tudo; sem isso o mix
    # fica artificialmente quieto. duration=longest cobre tracks de tamanhos
    # levemente diferentes.
    filter_str = (
        f"[0:a:{mic_idx}][0:a:{others_idx}]"
        f"amix=inputs=2:duration=longest:normalize=0,"
        f"alimiter=limit=0.95"
    )
    _run_ffmpeg([
        "-i", str(input_path),
        "-filter_complex", filter_str,
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_path),
    ])
    return output_path
