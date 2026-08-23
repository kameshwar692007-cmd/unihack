from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_login_works_with_default_dev_credentials() -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert payload["access_token"]


def test_signup_and_me_profile_flow() -> None:
    from app.services.auth_store import USERS_FILE
    if USERS_FILE.is_file():
        try:
            USERS_FILE.unlink()
        except Exception:
            pass
            
    response = client.post(
        "/api/auth/signup",
        json={"username": "newanalyst", "email": "analyst@unilog.com", "password": "securepassword123"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    token = payload["access_token"]
    assert token

    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200, me_resp.text
    user = me_resp.json()
    assert user["username"] == "newanalyst"
    assert user["email"] == "analyst@unilog.com"
