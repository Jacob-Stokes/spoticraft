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
