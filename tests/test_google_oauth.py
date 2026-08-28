# tests/test_google_oauth.py
# No network calls - just checks the signed OAuth "state" token round-trips
# correctly and rejects tampering/expiry. Doesn't need real Google client
# credentials configured.

import time

import pytest
from itsdangerous import BadSignature, SignatureExpired

from app.api.google_oauth import _state_signer


def test_round_trips_back_to_original_user_id():
    token = _state_signer.dumps("demo-user-1")
    assert _state_signer.loads(token, max_age=600) == "demo-user-1"


def test_tampered_token_is_rejected():
    token = _state_signer.dumps("demo-user-1")
    # Tamper the FIRST character, not the last. The signature is HMAC-SHA1
    # (20 bytes = 160 bits) encoded as 27 url-safe base64 chars (162 bits),
    # so the FINAL character carries 2 "don't care" bits - swapping it for
    # another char in the same equivalence class decodes to identical bytes
    # and still verifies. Every bit of the first character is significant,
    # so altering it always invalidates the signature.
    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(BadSignature):
        _state_signer.loads(tampered, max_age=600)


def test_expired_token_is_rejected():
    token = _state_signer.dumps("demo-user-1")
    time.sleep(2.5)
    with pytest.raises(SignatureExpired):
        _state_signer.loads(token, max_age=1)


def test_different_users_produce_different_tokens():
    a = _state_signer.dumps("user-a")
    b = _state_signer.dumps("user-b")
    assert a != b
