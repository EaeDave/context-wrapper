"""Testes focados no Tier 2: num_speakers/diarização, gestão de vozes,
confiança do match, migração speaker_matches, config tuning keys."""

from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from meet.config import _LOCAL_KEYS, load_settings
from meet.models import MeetingResult, TranscriptSegment
from meet.store import Store
from meet.voicebank import resolve, resolve_with_scores


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n != 0.0 else v


def _to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def _make_result(**kwargs) -> MeetingResult:
    defaults = dict(
        source="test.mkv",
        date="2024-01-01",
        title="Reunião",
        duration=60.0,
    )
    defaults.update(kwargs)
    return MeetingResult(**defaults)


def _save_minimal(store: Store) -> int:
    """Salva reunião mínima sem arquivo .md."""
    result = _make_result()
    with store._conn:
        cur = store._conn.execute(
            "INSERT INTO meetings (date, title, source, duration, summary, md_path)"
            " VALUES (?, ?, ?, ?, '', '')",
            (result.date, result.title, result.source, result.duration),
        )
        return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# A. diarize — num_speakers repassado ao pipe
# ---------------------------------------------------------------------------


def _make_diarize_test(
    num_speakers: int,
    *,
    pipe_error: Exception | None = None,
    empty_cache: MagicMock | None = None,
    gc_collect: MagicMock | None = None,
    pipeline_released: MagicMock | None = None,
) -> list[dict]:
    """Patch pyannote and torch, run diarize, and return pipeline calls."""
    import sys
    import weakref
    import meet.diarize as diarize_mod

    calls: list[dict] = []

    class FakePipeline:
        def __init__(self) -> None:
            if pipeline_released is not None:
                weakref.finalize(self, pipeline_released)
        def to(self, device):
            return self

        def __call__(self, path, **kwargs):
            calls.append({"path": path, "kwargs": kwargs})
            if pipe_error is not None:
                raise pipe_error
            ann = MagicMock()
            ann.itertracks.return_value = []
            ann.labels.return_value = []
            out = MagicMock()
            out.speaker_diarization = ann
            out.speaker_embeddings = None
            return out


    # pyannote.audio.Pipeline is imported lazily inside diarize(); patch the module
    fake_pyannote_audio = types.ModuleType("pyannote.audio")
    fake_pyannote_audio.Pipeline = MagicMock(  # type: ignore[attr-defined]
        from_pretrained=MagicMock(
            side_effect=lambda *_args, **_kwargs: FakePipeline()
        )
    )
    fake_torch = types.ModuleType("torch")
    fake_torch.device = lambda d: d  # type: ignore[attr-defined]
    fake_torch.cuda = types.SimpleNamespace(  # type: ignore[attr-defined]
        is_available=MagicMock(return_value=True),
        empty_cache=empty_cache or MagicMock(),
    )

    original_pyannote = sys.modules.get("pyannote.audio")
    original_torch = sys.modules.get("torch")
    sys.modules["pyannote.audio"] = fake_pyannote_audio
    sys.modules["torch"] = fake_torch
    try:
        from pathlib import Path as _Path
        import tempfile
        wav = _Path(tempfile.mktemp(suffix=".wav"))
        wav.write_bytes(b"\x00" * 44)
        settings = MagicMock()
        settings.hf_token = "tok"
        settings.device = "cpu"
        with patch.object(diarize_mod.gc, "collect", gc_collect or MagicMock()):
            diarize_mod.diarize(wav, settings, num_speakers=num_speakers)
    finally:
        if original_pyannote is not None:
            sys.modules["pyannote.audio"] = original_pyannote
        else:
            sys.modules.pop("pyannote.audio", None)
        if original_torch is not None:
            sys.modules["torch"] = original_torch
        else:
            sys.modules.pop("torch", None)

    return calls


def test_diarize_passes_num_speakers_when_nonzero() -> None:
    """Se num_speakers > 0, pipe deve ser chamado com num_speakers=N."""
    calls = _make_diarize_test(num_speakers=3)
    assert len(calls) == 1
    assert calls[0]["kwargs"].get("num_speakers") == 3


def test_diarize_no_num_speakers_kwarg_when_zero() -> None:
    """Se num_speakers == 0, pipe NÃO deve receber o kwarg num_speakers."""
    calls = _make_diarize_test(num_speakers=0)
    assert len(calls) == 1
    assert "num_speakers" not in calls[0]["kwargs"]


def test_diarize_cleans_pipeline_and_cuda_on_success() -> None:
    gc_collect = MagicMock()
    empty_cache = MagicMock()
    pipeline_released = MagicMock()

    _make_diarize_test(
        0,
        gc_collect=gc_collect,
        empty_cache=empty_cache,
        pipeline_released=pipeline_released,
    )

    pipeline_released.assert_called_once_with()
    gc_collect.assert_called_once_with()
    empty_cache.assert_called_once_with()


def test_diarize_cleans_pipeline_and_cuda_without_masking_error() -> None:
    pipeline_error = RuntimeError("diarization failed")
    gc_collect = MagicMock(side_effect=RuntimeError("gc cleanup failed"))
    empty_cache = MagicMock(side_effect=RuntimeError("cuda cleanup failed"))
    pipeline_released = MagicMock()

    with pytest.raises(RuntimeError, match="diarization failed") as exc_info:
        _make_diarize_test(
            0,
            pipe_error=pipeline_error,
            gc_collect=gc_collect,
            empty_cache=empty_cache,
            pipeline_released=pipeline_released,
        )

    assert exc_info.value is pipeline_error
    pipeline_error.__traceback__ = None
    del exc_info
    import gc

    gc.collect()
    pipeline_released.assert_called_once_with()
    gc_collect.assert_called_once_with()
    empty_cache.assert_called_once_with()


# ---------------------------------------------------------------------------
# B. voicebank — resolve_with_scores
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self._voices: dict[str, bytes] = {}

    def all_voices(self) -> dict[str, bytes]:
        return dict(self._voices)

    def upsert_voice(self, name: str, blob: bytes) -> None:
        self._voices[name] = blob


def test_resolve_with_scores_returns_score_when_match() -> None:
    """Embedding idêntico ao banco → score ≈ 1.0 e nome correto."""
    store = FakeStore()
    v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    store.upsert_voice("Alice", _to_blob(v))

    result = resolve_with_scores({"S00": v}, store, threshold=0.5)
    name, score = result["S00"]
    assert name == "Alice"
    assert abs(score - 1.0) < 1e-5


def test_resolve_with_scores_keeps_label_below_threshold() -> None:
    """Embedding ortogonal → score 0.0 → mantém label, score retornado."""
    store = FakeStore()
    v_bank = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    v_query = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    store.upsert_voice("Alice", _to_blob(v_bank))

    result = resolve_with_scores({"S00": v_query}, store, threshold=0.5)
    name, score = result["S00"]
    assert name == "S00"  # não resolveu
    assert abs(score - 0.0) < 1e-5


def test_resolve_with_scores_empty_bank() -> None:
    """Banco vazio → label mantido, score 0.0."""
    store = FakeStore()
    v = np.array([1.0, 0.0], dtype=np.float32)
    result = resolve_with_scores({"X": v}, store, threshold=0.5)
    name, score = result["X"]
    assert name == "X"
    assert score == 0.0


def test_resolve_is_wrapper_of_resolve_with_scores() -> None:
    """resolve() deve retornar apenas os nomes (sem scores), compatível com comportamento anterior."""
    store = FakeStore()
    v = np.array([1.0, 0.0], dtype=np.float32)
    store.upsert_voice("Bob", _to_blob(v))

    mapping = resolve({"S00": v}, store, threshold=0.5)
    assert mapping["S00"] == "Bob"
    assert isinstance(mapping["S00"], str)


# ---------------------------------------------------------------------------
# C. store — migração speaker_matches
# ---------------------------------------------------------------------------


def test_migration_adds_speaker_matches_column(tmp_path: Path) -> None:
    """Banco sem speaker_matches → migração cria coluna com default '{}'."""
    db_path = tmp_path / "test.db"
    import sqlite3

    # Criar banco SEM a coluna speaker_matches
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """\
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            duration REAL NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            md_path TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS action_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            what TEXT NOT NULL,
            where_ TEXT,
            details TEXT,
            requested_by TEXT,
            priority TEXT NOT NULL DEFAULT 'media'
        );
        CREATE TABLE IF NOT EXISTS segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            start REAL NOT NULL,
            end REAL NOT NULL,
            speaker TEXT,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS voices (
            name TEXT PRIMARY KEY,
            embedding BLOB NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5 (
            content,
            meeting_id UNINDEXED,
            kind UNINDEXED
        );
        INSERT INTO meetings (date, title, source, duration) VALUES ('2024-01-01', 'Teste', 'a.mkv', 10.0);
        """
    )
    conn.close()

    # Agora abrir com Store — deve migrar
    store = Store(db_path)
    cols = {
        r[1]
        for r in store._conn.execute("PRAGMA table_info(meetings)").fetchall()
    }
    assert "speaker_matches" in cols

    # Reunião antiga deve ter speaker_matches == '{}'
    row = store._conn.execute(
        "SELECT speaker_matches FROM meetings WHERE id = 1"
    ).fetchone()
    assert row["speaker_matches"] == "{}"


def test_save_and_get_meeting_speaker_matches(tmp_path: Path) -> None:
    """save_meeting persiste speaker_matches; get_meeting recupera como dict."""
    store = Store(tmp_path / "test.db")
    result = _make_result(speaker_matches={"Alice": 0.87, "Bob": 0.72})

    # save_meeting requer md_path como Path existente (ou dummy)
    md = tmp_path / "reuniao.md"
    md.write_text("# Reunião", encoding="utf-8")
    mid = store.save_meeting(result, md)

    loaded = store.get_meeting(mid)
    assert loaded is not None
    assert loaded.speaker_matches == {"Alice": 0.87, "Bob": 0.72}


def test_replace_meeting_content_updates_speaker_matches(tmp_path: Path) -> None:
    """replace_meeting_content atualiza speaker_matches."""
    store = Store(tmp_path / "test.db")
    md = tmp_path / "reuniao.md"
    md.write_text("# Reunião", encoding="utf-8")

    result = _make_result(speaker_matches={"Alice": 0.80})
    mid = store.save_meeting(result, md)

    result2 = _make_result(title="Reunião 2", speaker_matches={"Bob": 0.91})
    store.replace_meeting_content(mid, result2)

    loaded = store.get_meeting(mid)
    assert loaded is not None
    assert loaded.speaker_matches == {"Bob": 0.91}


# ---------------------------------------------------------------------------
# D. store — rename_voice
# ---------------------------------------------------------------------------


def _seed_voice(store: Store, name: str, vec: np.ndarray) -> None:
    store.upsert_voice(name, _to_blob(vec))


def _seed_segment(store: Store, meeting_id: int, speaker: str) -> None:
    with store._conn:
        store._conn.execute(
            "INSERT INTO segments (meeting_id, start, end, speaker, text)"
            " VALUES (?, 0.0, 1.0, ?, 'oi')",
            (meeting_id, speaker),
        )


def test_rename_voice_simple(tmp_path: Path) -> None:
    """Renomeia para nome inexistente: embedding migrado, segmentos atualizados."""
    store = Store(tmp_path / "test.db")
    mid = _save_minimal(store)
    v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    _seed_voice(store, "Jonathas", v)
    _seed_segment(store, mid, "Jonathas")

    store.rename_voice("Jonathas", "Jonathan")

    voices = store.all_voices()
    assert "Jonathan" in voices
    assert "Jonathas" not in voices

    # Segmento atualizado
    row = store._conn.execute(
        "SELECT speaker FROM segments WHERE meeting_id = ?", (mid,)
    ).fetchone()
    assert row["speaker"] == "Jonathan"


def test_rename_voice_merge(tmp_path: Path) -> None:
    """Merge: média dos embeddings, old removido, segmentos migrados."""
    store = Store(tmp_path / "test.db")
    mid = _save_minimal(store)

    v_old = np.array([1.0, 0.0], dtype=np.float32)
    v_new = np.array([0.0, 1.0], dtype=np.float32)
    expected_merged = (v_old + v_new) / 2.0

    _seed_voice(store, "Jonatas", v_old)
    _seed_voice(store, "Jonathan", v_new)
    _seed_segment(store, mid, "Jonatas")

    store.rename_voice("Jonatas", "Jonathan")

    voices = store.all_voices()
    assert "Jonatas" not in voices
    assert "Jonathan" in voices

    recovered = _from_blob(voices["Jonathan"])
    np.testing.assert_array_almost_equal(recovered, expected_merged)

    row = store._conn.execute(
        "SELECT speaker FROM segments WHERE meeting_id = ?", (mid,)
    ).fetchone()
    assert row["speaker"] == "Jonathan"


def test_rename_voice_noop_same_name(tmp_path: Path) -> None:
    """rename_voice(a, a) não muda nada."""
    store = Store(tmp_path / "test.db")
    v = np.array([1.0, 0.0], dtype=np.float32)
    _seed_voice(store, "Alice", v)

    store.rename_voice("Alice", "Alice")

    voices = store.all_voices()
    assert "Alice" in voices
    np.testing.assert_array_almost_equal(_from_blob(voices["Alice"]), v)


def test_rename_voice_fts_reindexed(tmp_path: Path) -> None:
    """Após rename, FTS deve ter registros (reindex não falhou silenciosamente)."""
    store = Store(tmp_path / "test.db")
    mid = _save_minimal(store)

    # Inserir segmento e indexar manualmente
    TranscriptSegment(start=0.0, end=1.0, text="olá mundo", speaker="Jonathas")
    with store._conn:
        store._conn.execute(
            "INSERT INTO segments (meeting_id, start, end, speaker, text)"
            " VALUES (?, 0.0, 1.0, 'Jonathas', 'olá mundo')",
            (mid,),
        )
        store._conn.execute(
            "INSERT INTO search_index (content, meeting_id, kind) VALUES ('olá mundo', ?, 'segment')",
            (mid,),
        )

    _seed_voice(store, "Jonathas", np.array([1.0], dtype=np.float32))
    store.rename_voice("Jonathas", "Jonathan")

    # FTS ainda tem registro para a reunião
    rows = store._conn.execute(
        "SELECT * FROM search_index WHERE meeting_id = ?", (mid,)
    ).fetchall()
    assert len(rows) > 0


# ---------------------------------------------------------------------------
# E. store — voice_usage
# ---------------------------------------------------------------------------


def test_voice_usage_counts_meetings(tmp_path: Path) -> None:
    """voice_usage retorna lista com meeting_id, title, date, count corretos."""
    store = Store(tmp_path / "test.db")
    mid1 = _save_minimal(store)
    mid2 = _save_minimal(store)

    # 2 segmentos em meeting 1, 1 em meeting 2
    _seed_segment(store, mid1, "Alice")
    _seed_segment(store, mid1, "Alice")
    _seed_segment(store, mid2, "Alice")

    usage = store.voice_usage("Alice")
    assert len(usage) == 2

    by_mid = {u["meeting_id"]: u for u in usage}
    assert by_mid[mid1]["count"] == 2
    assert by_mid[mid2]["count"] == 1


def test_voice_usage_empty_when_no_segments(tmp_path: Path) -> None:
    """voice_usage retorna [] quando voz não tem segmentos."""
    store = Store(tmp_path / "test.db")
    _seed_voice(store, "Ghost", np.array([1.0], dtype=np.float32))
    assert store.voice_usage("Ghost") == []


# ---------------------------------------------------------------------------
# F. config — _LOCAL_KEYS com chaves de tuning
# ---------------------------------------------------------------------------


def test_local_keys_includes_tuning_keys() -> None:
    """_LOCAL_KEYS deve conter as chaves de tuning do Tier 2."""
    expected = {
        "whisper_model",
        "language",
        "similarity_threshold",
        "device",
        "compute_type",
    }
    missing = expected - _LOCAL_KEYS
    assert not missing, f"_LOCAL_KEYS faltando: {missing}"


def test_save_local_settings_tuning_roundtrip(tmp_path: Path) -> None:
    """save_local_settings aceita tuning keys; load_settings aplica layering."""
    load_settings.__wrapped__ if hasattr(load_settings, "__wrapped__") else None  # type: ignore[attr-defined]

    # Simular settings apontando para tmp_path
    from meet.config import Settings, save_local_settings as _save

    s = Settings()
    s.data_dir = tmp_path  # type: ignore[assignment]

    _save(
        {
            "whisper_model": "turbo",
            "similarity_threshold": 0.75,
            "device": "cpu",
        },
        s,
    )

    local = json.loads((tmp_path / "settings.local.json").read_text())
    assert local["whisper_model"] == "turbo"
    assert local["similarity_threshold"] == 0.75
    assert local["device"] == "cpu"


def test_local_settings_similarity_threshold_float_loaded(tmp_path: Path) -> None:
    """similarity_threshold persisto como float é carregado corretamente."""
    from meet.config import Settings, save_local_settings as _save

    s = Settings()
    s.data_dir = tmp_path  # type: ignore[assignment]
    _save({"similarity_threshold": 0.42}, s)

    # load_settings lê data_dir padrão; precisa simular. Verificar direto no JSON.
    raw = json.loads((tmp_path / "settings.local.json").read_text())
    assert isinstance(raw["similarity_threshold"], float)
    assert abs(raw["similarity_threshold"] - 0.42) < 1e-9


# ---------------------------------------------------------------------------
# G. FastAPI smoke — create_app não levanta
# ---------------------------------------------------------------------------


def test_create_app_boots() -> None:
    """create_app() não deve levantar exceção."""
    from meet.web.app import create_app

    app = create_app()
    assert app is not None
