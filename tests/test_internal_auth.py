# Shared-secret guard on /trip-plan and the admin sync routes.
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config.settings import settings
from app.utils.internal_auth import require_internal_token

app = FastAPI()


@app.post("/protected", dependencies=[Depends(require_internal_token)])
def protected():
    return {"ok": True}


client = TestClient(app)


def test_open_when_no_token_configured(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "")
    assert client.post("/protected").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}])
def test_rejects_missing_or_wrong_token(monkeypatch, headers):
    monkeypatch.setattr(settings, "internal_api_token", "s3cret")
    assert client.post("/protected", headers=headers).status_code == 401


def test_accepts_correct_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "s3cret")
    assert client.post("/protected", headers={"X-Internal-Token": "s3cret"}).status_code == 200
