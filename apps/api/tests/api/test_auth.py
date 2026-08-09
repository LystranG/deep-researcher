from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client


def test_registered_user_can_read_current_identity(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )

        assert registered.status_code == 201
        access_token = registered.json()["access_token"]

        current = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert current.status_code == 200
    assert current.json()["email"] == "researcher@example.com"
