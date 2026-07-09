"""Extração e preparação de áudio a partir de vídeos de reunião."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .models import AudioTracks

_WAV_OPTS = ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"]
# Nivelamento de fala: vozes gravadas baixas (mic com pouco ganho, participante
# remoto quieto) escapam do VAD do whisper; speechnorm levanta até 12.5x.
_SPEECHNORM = "speechnorm=e=12.5:r=0.0001:l=1"


def probe_audio_streams(input_path: Path) -> int:
    """Retorna o número de streams de áudio no arquivo via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "a",
        str(input_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
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
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe falhou ao ler duração: {proc.stderr[:500]}")
    data = json.loads(proc.stdout)
    return float(data["format"]["duration"])


def _run_ffmpeg(args: list[str]) -> None:
    """Executa ffmpeg com -y; RuntimeError com stderr resumido se falhar."""
    proc = subprocess.run(["ffmpeg", "-y"] + args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {proc.stderr[-500:]}")


def prepare(
    input_path: Path,
    workdir: Path,
    mic_track: int = 1,
    others_track: int = 2,
) -> AudioTracks:
    """Extrai streams de áudio para wav 16 kHz mono pcm_s16le.

    1 stream → mic=None, others==mixed (mesmo arquivo wav).
    ≥2 streams → mic e others separados (1-based) + mixdown completo via amix.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    n_streams = probe_audio_streams(input_path)
    duration = _probe_duration(input_path)

    if n_streams < 2:
        mixed = workdir / "mixed.wav"
        _run_ffmpeg([
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
    mixed_path = workdir / "mixed.wav"

    _run_ffmpeg([
        "-i", str(input_path),
        "-map", f"0:a:{mic_idx}",
        "-af", _SPEECHNORM,
        *_WAV_OPTS,
        str(mic_path),
    ])

    _run_ffmpeg([
        "-i", str(input_path),
        "-map", f"0:a:{others_idx}",
        "-af", _SPEECHNORM,
        *_WAV_OPTS,
        str(others_path),
    ])

    # Mixdown de todos os K streams via amix; nomeia saída para mapeamento explícito
    stream_refs = "".join(f"[0:a:{i}]" for i in range(n_streams))
    filter_str = (
        f"{stream_refs}amix=inputs={n_streams}:duration=longest[mx];"
        f"[mx]{_SPEECHNORM}[amixed]"
    )
    _run_ffmpeg([
        "-i", str(input_path),
        "-filter_complex", filter_str,
        "-map", "[amixed]",
        *_WAV_OPTS,
        str(mixed_path),
    ])

    return AudioTracks(
        mic=mic_path,
        others=others_path,
        mixed=mixed_path,
        duration=duration,
    )


def listen_mix_path(input_path: Path) -> Path:
    """Path padrão do mix de ouvir ao lado da gravação."""
    return input_path.with_name(f"{input_path.stem}.listen.m4a")


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
    if (
        not force
        and out.is_file()
        and out.stat().st_size > 0
        and out.stat().st_mtime >= input_path.stat().st_mtime
    ):
        return out
    return export_listen_mix(
        input_path,
        out,
        mic_track=mic_track,
        others_track=others_track,
    )


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
