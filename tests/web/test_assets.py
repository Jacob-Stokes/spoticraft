import io

import yaml
from fastapi.testclient import TestClient

from spotifreak.web.api import app


def _write_minimal_config(base_dir):
    config_path = base_dir / "config.yml"
    state_dir = base_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "spotify": {
            "client_id": "client",
            "client_secret": "secret",
            "redirect_uri": "http://localhost/callback",
            "scopes": [],
        },
        "runtime": {
            "timezone": "UTC",
            "storage_dir": str(state_dir),
        },
    }

    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle)


def _setup_app_context(tmp_path):
    base_dir = tmp_path / "spotifreak"
    assets_dir = base_dir / "assets"
    syncs_dir = base_dir / "syncs"

    assets_dir.mkdir(parents=True)
    syncs_dir.mkdir(parents=True)

    _write_minimal_config(base_dir)
    return base_dir


def test_create_asset_folder(tmp_path):
    base_dir = _setup_app_context(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/config/assets/folders",
        json={"path": "covers/night"},
        params={"config_dir": str(base_dir)},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["is_dir"] is True
    assert payload["path"] == "covers/night"
    assert (base_dir / "assets" / "covers" / "night").is_dir()


def test_create_asset_folder_conflict(tmp_path):
    base_dir = _setup_app_context(tmp_path)
    target = base_dir / "assets" / "covers"
    target.mkdir(parents=True)

    client = TestClient(app)

    response = client.post(
        "/config/assets/folders",
        json={"path": "covers"},
        params={"config_dir": str(base_dir)},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Folder already exists"


def test_create_asset_folder_rejects_traversal(tmp_path):
    base_dir = _setup_app_context(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/config/assets/folders",
        json={"path": "../outside"},
        params={"config_dir": str(base_dir)},
    )

    assert response.status_code == 400
    assert "cannot contain" in response.json()["detail"]


def test_upload_asset_to_specific_folder(tmp_path):
    base_dir = _setup_app_context(tmp_path)
    (base_dir / "assets" / "covers" / "night").mkdir(parents=True)
    client = TestClient(app)

    files = {"file": ("cover.png", io.BytesIO(b"fake"), "image/png")}
    response = client.post(
        "/config/assets",
        params={"config_dir": str(base_dir), "target_dir": "covers/night"},
        files=files,
    )

    assert response.status_code == 201
    stored = base_dir / "assets" / "covers" / "night"
    assert any(stored.iterdir())


def test_upload_asset_missing_folder(tmp_path):
    base_dir = _setup_app_context(tmp_path)
    client = TestClient(app)

    files = {"file": ("cover.png", io.BytesIO(b"fake"), "image/png")}
    response = client.post(
        "/config/assets",
        params={"config_dir": str(base_dir), "target_dir": "covers/night"},
        files=files,
    )

    assert response.status_code == 404


def test_move_asset(tmp_path):
    base_dir = _setup_app_context(tmp_path)
    source_dir = base_dir / "assets" / "covers"
    source_dir.mkdir(parents=True)
    destination_dir = base_dir / "assets" / "hero"
    destination_dir.mkdir(parents=True)
    source_file = source_dir / "night.png"
    source_file.write_bytes(b"fake")

    client = TestClient(app)
    response = client.post(
        "/config/assets/move",
        json={"source": "covers/night.png", "destination": "hero/night.png"},
        params={"config_dir": str(base_dir)},
    )

    assert response.status_code == 200
    assert not source_file.exists()
    assert (destination_dir / "night.png").exists()


def test_delete_folder_recursive(tmp_path):
    base_dir = _setup_app_context(tmp_path)
    target_dir = base_dir / "assets" / "covers"
    nested_file = target_dir / "night" / "cover.png"
    nested_file.parent.mkdir(parents=True)
    nested_file.write_bytes(b"fake")

    client = TestClient(app)

    response = client.delete(
        "/config/assets/covers",
        params={"config_dir": str(base_dir)},
    )
    assert response.status_code == 409

    response = client.delete(
        "/config/assets/covers",
        params={"config_dir": str(base_dir), "recursive": True},
    )
    assert response.status_code == 204
    assert not target_dir.exists()
