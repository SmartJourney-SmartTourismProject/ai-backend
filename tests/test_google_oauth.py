# tests/test_google_oauth.py
# Exercises the real HTTP layer (app/api/google_oauth.py) end to end:
# GET /login -> extract state from the redirect -> GET /callback with it.
# Only flow.fetch_token (the actual network call to Google) is mocked - the
# state signing, PKCE code_verifier generation, and the login->callback
# handoff all run for real.
#
# This exists because a prior version of this file only unit-tested
# _state_signer against a bare string in isolation, which is exactly why a
# real bug shipped unnoticed: /login generated a PKCE code_verifier on its
# own throwaway Flow instance, /callback built an unrelated Flow with no
# verifier, and the token exchange failed with "Missing code verifier" the
# first time anyone actually clicked through the flow. A signer test alone
# can't catch that class of bug - only a real round trip can.

import time
from unittest.mock import patch, PropertyMock
from urllib.parse import urlparse, parse_qs

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import BadSignature, SignatureExpired

import app.api.google_oauth as google_oauth_module
from app.api.google_oauth import _state_signer, _generate_code_verifier, _PKCE_CHARS

app = FastAPI()
app.include_router(google_oauth_module.router)
client = TestClient(app, follow_redirects=False)


def _state_from_login(user_id: str = "demo-user-1") -> str:
    """Hits the real /login endpoint and pulls the signed state out of the
    redirect Google would receive - the same value /callback will get back."""
    resp = client.get(f"/auth/google/login?user_id={user_id}")
    assert resp.status_code in (302, 307)
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    return qs["state"][0]


# ---- the round trip itself -------------------------------------------------

def test_login_to_callback_round_trip_uses_matching_code_verifier():
    """The bug this file exists to catch: the code_verifier /callback hands
    to fetch_token must be the exact one /login generated and challenged
    Google with - not a fresh one from a new Flow instance."""
    state = _state_from_login()

    seen = {}
    def fake_fetch_token(self, code):
        # The only thing this test needs to prove: whatever verifier /login
        # generated and challenged Google with is exactly what /callback's
        # fetch_token call carries. Real fetch_token doesn't return anything
        # useful to assert on directly, so intercept it here instead.
        seen["code_verifier"] = self.code_verifier

    fake_creds = type("Creds", (), {
        "token": "at", "refresh_token": "rt", "expiry": None, "scopes": None,
    })()

    with patch.object(google_oauth_module.Flow, "fetch_token", fake_fetch_token), \
         patch.object(google_oauth_module.Flow, "credentials",
                      new_callable=PropertyMock, return_value=fake_creds), \
         patch.object(google_oauth_module, "save_credentials", return_value=None):
        resp = client.get(f"/auth/google/callback?code=fake-code&state={state}")

    assert resp.status_code == 200
    assert resp.json() == {"status": "connected", "user_id": "demo-user-1"}
    # the exact assertion the old tests couldn't make: verifier round-tripped
    payload = _state_signer.loads(state, max_age=600)
    assert seen["code_verifier"] == payload["code_verifier"]
    assert seen["code_verifier"] is not None


def test_login_redirect_carries_matching_code_challenge():
    """The /login redirect's code_challenge must be derived from the same
    verifier packed into state - otherwise Google rejects the eventual
    fetch_token even though our own state check would pass."""
    import hashlib
    from base64 import urlsafe_b64encode

    resp = client.get("/auth/google/login?user_id=demo-user-1")
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    state_payload = _state_signer.loads(qs["state"][0], max_age=600)

    expected_challenge = urlsafe_b64encode(
        hashlib.sha256(state_payload["code_verifier"].encode()).digest()
    ).decode().rstrip("=")
    assert qs["code_challenge"][0] == expected_challenge
    assert qs["code_challenge_method"][0] == "S256"


def test_code_verifier_meets_pkce_requirements():
    """RFC 7636 §4.1: 43-128 chars from [A-Za-z0-9\\-._~]."""
    v = _generate_code_verifier()
    assert 43 <= len(v) <= 128
    assert all(c in _PKCE_CHARS for c in v)


def test_callback_rejects_state_missing_code_verifier():
    """A state token in the old bare-string shape (or otherwise missing the
    verifier) must be rejected cleanly, not crash with a raw KeyError."""
    legacy_shaped_state = _state_signer.dumps("demo-user-1")  # old format
    resp = client.get(f"/auth/google/callback?code=fake-code&state={legacy_shaped_state}")
    assert resp.status_code == 400


# ---- state signing/verification (unchanged behavior, updated payload shape) --

def test_round_trips_back_to_original_user_id():
    token = _state_signer.dumps({"user_id": "demo-user-1", "code_verifier": "x" * 64})
    payload = _state_signer.loads(token, max_age=600)
    assert payload["user_id"] == "demo-user-1"
    assert payload["code_verifier"] == "x" * 64


def test_tampered_token_is_rejected():
    token = _state_signer.dumps({"user_id": "demo-user-1", "code_verifier": "x" * 64})
    # Tamper the FIRST character, not the last. The signature is HMAC-SHA1
    # (20 bytes = 160 bits) encoded as base64 url-safe chars, so the final
    # character can carry "don't care" bits - swapping it for another char
    # in the same equivalence class can decode to identical bytes and still
    # verify. Every bit of the first character is significant, so altering
    # it always invalidates the signature.
    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(BadSignature):
        _state_signer.loads(tampered, max_age=600)


def test_expired_token_is_rejected():
    token = _state_signer.dumps({"user_id": "demo-user-1", "code_verifier": "x" * 64})
    time.sleep(2.5)
    with pytest.raises(SignatureExpired):
        _state_signer.loads(token, max_age=1)


def test_different_users_produce_different_tokens():
    a = _state_signer.dumps({"user_id": "user-a", "code_verifier": "x" * 64})
    b = _state_signer.dumps({"user_id": "user-b", "code_verifier": "x" * 64})
    assert a != b
