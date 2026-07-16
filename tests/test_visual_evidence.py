"""Persistência, vínculo temporal e API de evidências visuais."""

from pathlib import Path

from fastapi.testclient import TestClient

from meet.models import ActionItem, MeetingFact, MeetingResult, VisualEvidence
from meet.store import Store


def _meeting() -> MeetingResult:
    return MeetingResult(
        source="/tmp/demo.mkv",
        date="2026-07-16",
        title="Demo visual",
        duration=120,
        action_items=[ActionItem(what="Corrigir campo", source_start=38, source_end=42)],
        facts=[MeetingFact(kind="requirement", text="Campo obrigatório", source_start=70, source_end=72)],
    )


def test_visual_evidence_round_trip_e_vinculo_temporal(tmp_path: Path) -> None:
    store = Store(tmp_path / "meet.db")
    meeting_id = store.save_meeting(_meeting(), tmp_path / "meeting.md")
    source_a = tmp_path / "a.jpg"
    source_b = tmp_path / "b.jpg"
    source_a.write_bytes(b"jpeg-a")
    source_b.write_bytes(b"jpeg-b")

    stored = store.replace_visual_evidence(
        meeting_id,
        [
            VisualEvidence(40, str(source_a), "Tela cadastro", ["CNPJ"], "high"),
            VisualEvidence(71, str(source_b), "Modal de validação", ["Obrigatório"], "medium"),
        ],
        tmp_path / "data",
    )
    loaded = store.get_meeting(meeting_id)

    assert loaded is not None
    assert len(stored) == 2
    assert [item.description for item in loaded.visual_evidence] == [
        "Tela cadastro", "Modal de validação"
    ]
    assert [item.description for item in loaded.action_items[0].visual_evidence] == [
        "Tela cadastro"
    ]
    assert [item.description for item in loaded.facts[0].visual_evidence] == [
        "Modal de validação"
    ]
    assert all(Path(item.image_path).is_file() for item in loaded.visual_evidence)


def test_visual_evidence_api_lista_e_serve_thumbnail(
    tmp_path: Path, monkeypatch
) -> None:
    import meet.web.app as app_module
    from meet.config import Settings

    settings = Settings(data_dir=tmp_path / "data", output_dir=tmp_path / "output")
    store = Store(settings.db_path)
    meeting_id = store.save_meeting(_meeting(), tmp_path / "meeting.md")
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpeg-content")
    evidence = store.replace_visual_evidence(
        meeting_id,
        [VisualEvidence(40, str(image), "Tela cadastro", ["CNPJ"], "high")],
        settings.data_dir,
    )[0]
    monkeypatch.setattr(app_module, "_settings_store", lambda: (settings, Store(settings.db_path)))
    client = TestClient(app_module.create_app())

    detail = client.get(f"/api/meetings/{meeting_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["visual_evidence"][0]["description"] == "Tela cadastro"
    assert payload["action_items"][0]["visual_evidence"][0]["id"] == evidence.id

    thumbnail = client.get(payload["visual_evidence"][0]["thumbnail_url"])
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"].startswith("image/jpeg")
    assert thumbnail.content == b"jpeg-content"
