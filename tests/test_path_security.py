"""Confinamento de path e Host/Origin nos endpoints de arquivo local."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meet.config import Settings
from meet.store import Store
import meet.web.app as app_module
from meet.web.app import create_app, _assert_user_media_path, _get_store


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


@pytest.fixture()
def web_client(fake_home: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = fake_home / ".local" / "share" / "meet"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "test.db"
    settings = Settings(data_dir=data_dir, output_dir=fake_home / "reunioes")

    monkeypatch.setattr(app_module, "_settings_store", lambda: (settings, Store(db)))
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    # CONFIG_PATH.parent sob fake home
    cfg = fake_home / ".config" / "meet" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_module, "CONFIG_PATH", cfg)

    app = create_app()
    return TestClient(app, raise_server_exceptions=True), settings, fake_home


def test_files_blocks_auth_json(web_client) -> None:
    client, settings, _ = web_client
    auth = settings.data_dir / "auth.json"
    auth.write_text('{"anthropic":{"access":"SECRET"}}')

    r = client.get("/files", params={"path": str(auth)})
    assert r.status_code == 403
    assert "SECRET" not in r.text


def test_files_blocks_outside_home(web_client) -> None:
    client, _, _ = web_client
    r = client.get("/files", params={"path": "/etc/passwd"})
    assert r.status_code == 403


def test_files_allows_media_under_home(web_client) -> None:
    client, _, home = web_client
    sample = home / "Videos" / "clip.wav"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_bytes(b"RIFF....WAVE")

    r = client.get("/files", params={"path": str(sample)})
    assert r.status_code == 200
    assert r.content == b"RIFF....WAVE"


def test_files_rejects_non_media_under_home(web_client) -> None:
    """LFI surface: /files não pode servir .ssh, .env, texto arbitrário."""
    client, _, home = web_client
    secret = home / ".ssh" / "id_rsa"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("PRIVATE KEY MATERIAL")
    note = home / "notes.txt"
    note.write_text("not media")

    r_key = client.get("/files", params={"path": str(secret)})
    assert r_key.status_code == 403
    assert "PRIVATE KEY" not in r_key.text

    r_txt = client.get("/files", params={"path": str(note)})
    assert r_txt.status_code == 403


def test_browse_confined_to_home(web_client) -> None:
    client, _, home = web_client
    r = client.get("/api/browse", params={"path": "/etc"})
    assert r.status_code == 200
    data = r.json()
    assert Path(data["path"]).resolve() == home.resolve()


def test_browse_hides_data_dir(web_client) -> None:
    client, settings, home = web_client
    parent = settings.data_dir.parent  # .../share
    parent.mkdir(parents=True, exist_ok=True)
    r = client.get("/api/browse", params={"path": str(parent)})
    assert r.status_code == 200
    names = {e["name"] for e in r.json()["entries"]}
    assert settings.data_dir.name not in names


def test_probe_blocks_data_dir_file(web_client) -> None:
    client, settings, _ = web_client
    secret = settings.data_dir / "auth.json"
    secret.write_text("{}")
    r = client.get("/api/probe", params={"path": str(secret)})
    assert r.status_code == 403


def test_bad_host_rejected(web_client) -> None:
    client, _, _ = web_client
    r = client.get("/api/settings", headers={"Host": "evil.example"})
    assert r.status_code == 403


def test_spoofed_loopback_host_with_remote_peer_rejected(
    web_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host: 127.0.0.1 sozinho não basta — peer não-loopback deve ser 403."""
    client, _, _ = web_client
    monkeypatch.setattr(app_module, "_peer_is_loopback", lambda _req: False)
    r = client.get("/api/settings", headers={"Host": "127.0.0.1:8741"})
    assert r.status_code == 403
    assert "loopback" in r.json()["detail"].lower() or "MEET_ALLOW" in r.json()["detail"]


def test_preview_force_get_rejected(web_client) -> None:
    client, _, _ = web_client
    r = client.get("/meetings/1/preview", params={"force": "true"})
    assert r.status_code == 405


def test_audio_force_get_rejected(web_client) -> None:
    client, _, _ = web_client
    r = client.get("/meetings/1/audio", params={"force": "true"})
    assert r.status_code == 405


def test_speaker_rename_rejects_slash(web_client) -> None:
    client, _, _ = web_client
    r = client.patch(
        "/api/speakers",
        json={"name": "Alice", "new_name": "Alice/Bob"},
    )
    assert r.status_code == 400


def test_speaker_management_supports_legacy_slash_names(web_client) -> None:
    client, _, _ = web_client
    store = app_module._settings_store()[1]
    for name in ("Alice/Bob", "Carol/Dave", "Eve/Mallory"):
        store.upsert_voice(name, b"\0\0\0\0")

    usage = client.get("/api/speakers/usage", params={"name": "Alice/Bob"})
    assert usage.status_code == 200
    assert usage.json() == []

    renamed = client.patch(
        "/api/speakers",
        json={"name": "Carol/Dave", "new_name": "Carol Dave"},
    )
    assert renamed.status_code == 200

    deleted = client.delete("/api/speakers", params={"name": "Eve/Mallory"})
    assert deleted.status_code == 204

    names = {speaker["name"] for speaker in client.get("/api/speakers").json()}
    assert names == {"Alice/Bob", "Carol Dave"}


def test_cross_origin_mutation_rejected(web_client) -> None:
    client, _, _ = web_client
    r = client.post(
        "/api/auth/openai/authorize",
        headers={"Origin": "https://evil.example", "Host": "127.0.0.1:8741"},
    )
    assert r.status_code == 403


def test_store_singleton_same_path(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "d", output_dir=tmp_path / "o")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # limpa cache entre keys
    app_module._store_cache.clear()
    a = _get_store(settings)
    b = _get_store(settings)
    assert a is b


def test_assert_blocks_config_dir(web_client) -> None:
    _, settings, home = web_client
    cfg = home / ".config" / "meet" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("x = 1\n")
    with pytest.raises(Exception) as ei:
        _assert_user_media_path(str(cfg))
    assert getattr(ei.value, "status_code", None) == 403


def test_process_rejects_outside_home(web_client) -> None:
    client, _, _ = web_client
    r = client.post(
        "/api/process",
        json={"video": "/etc/passwd", "title": "x", "import_media": False},
    )
    assert r.status_code == 403


def test_preview_without_cache_returns_409_not_blocking_encode(
    web_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /preview sem force não deve chamar ensure_listen_* (sem ffmpeg no request)."""
    from meet.models import MeetingResult

    client, settings, home = web_client
    src = home / "Videos" / "clip.mkv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"fake-mkv")
    store = Store(settings.db_path)
    mid = store.save_meeting(
        MeetingResult(
            source=str(src),
            date="2024-01-01",
            title="m",
            duration=1.0,
            summary="",
        ),
        settings.output_dir / "m.md",
    )
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    app_module._store_cache.clear()
    monkeypatch.setattr(
        app_module, "_settings_store", lambda: (settings, Store(settings.db_path))
    )

    calls: list[str] = []

    def boom(*_a, **_k):
        calls.append("encode")
        raise AssertionError("encode no request path")

    monkeypatch.setattr("meet.audio.ensure_listen_preview", boom)
    monkeypatch.setattr("meet.audio.ensure_listen_mix", boom)
    monkeypatch.setattr("meet.audio.probe_video_streams", lambda _p: 1)

    r = client.get(f"/meetings/{mid}/preview")
    assert r.status_code == 409, r.text
    assert "mix" in r.json()["detail"].lower() or "force" in r.json()["detail"].lower()
    assert calls == []

    r2 = client.get(f"/meetings/{mid}/audio")
    assert r2.status_code == 409
    assert calls == []


def test_assign_missing_meeting_404(web_client) -> None:
    client, _, _ = web_client
    r = client.post(
        "/api/meetings/99999/assign",
        json={"label": "SPEAKER_00", "name": "Alice"},
    )
    assert r.status_code == 404


def test_search_snippet_escapes_html(web_client, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from meet.models import MeetingResult, TranscriptSegment
    from meet.store import Store

    client, settings, _ = web_client
    store = Store(settings.db_path)
    md = settings.output_dir / "m.md"
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    md.write_text("# x\n")
    result = MeetingResult(
        source=str(fake_home / "a.wav"),
        date="2024-01-01",
        title="XSS",
        duration=1.0,
        summary="",
        segments=[
            TranscriptSegment(
                start=0,
                end=1,
                text='hello <script>alert(1)</script> world',
                speaker="me",
            )
        ],
        action_items=[],
    )
    store.save_meeting(result, md)
    # force store used by API to see the row — clear cache and re-point fixture
    app_module._store_cache.clear()
    monkeypatch.setattr(
        app_module,
        "_settings_store",
        lambda: (settings, Store(settings.db_path)),
    )
    r = client.get("/api/search", params={"q": "hello"})
    assert r.status_code == 200
    body = r.text
    assert "<script>" not in body
    data = r.json()
    assert data
    assert all("<script>" not in row["snippet"] for row in data)
    assert any("&lt;script&gt;" in row["snippet"] for row in data)
