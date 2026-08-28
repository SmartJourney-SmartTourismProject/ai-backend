# test_google_oauth.py
# Run from your project root: python3 test_google_oauth.py
# No network calls - just checks the signed OAuth "state" token round-trips
# correctly and rejects tampering/expiry. Doesn't need real Google client
# credentials configured.

import time

from itsdangerous import BadSignature, SignatureExpired
from app.api.google_oauth import _state_signer


def main():
    checks = []

    token = _state_signer.dumps("demo-user-1")
    print("signed token:", token)

    decoded = _state_signer.loads(token, max_age=600)
    checks.append(("Round-trips back to the original user_id", decoded == "demo-user-1"))

    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    try:
        _state_signer.loads(tampered, max_age=600)
        checks.append(("Tampered token is rejected", False))
    except BadSignature:
        checks.append(("Tampered token is rejected", True))

    # Sign a fresh token, wait past its allowed window, then check it's
    # rejected as expired - max_age=0 immediately after signing is flaky
    # since itsdangerous compares whole elapsed seconds.
    short_lived_token = _state_signer.dumps("demo-user-1")
    time.sleep(2.5)
    try:
        _state_signer.loads(short_lived_token, max_age=1)
        checks.append(("Expired token (max_age=1, waited 2.5s) is rejected", False))
    except SignatureExpired:
        checks.append(("Expired token (max_age=1, waited 2.5s) is rejected", True))

    other_user_token = _state_signer.dumps("some-other-user")
    checks.append((
        "Different user_ids produce different tokens",
        other_user_token != token
    ))

    passed = 0
    for label, ok in checks:
        if ok:
            passed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")

    print(f"\n{passed}/{len(checks)} checks passed")


if __name__ == "__main__":
    main()
