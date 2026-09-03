# app/api/google_oauth.py
from string import ascii_letters, digits
from random import SystemRandom

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config.settings import settings
from app.tools.calendar_tool import save_credentials, SCOPES

router = APIRouter(prefix="/auth/google", tags=["google-oauth"])

_state_signer = URLSafeTimedSerializer(settings.secret_key, salt="oauth-state")

# RFC 7636 §4.1: 43-128 chars from [A-Za-z0-9\-._~]. Matches
# google_auth_oauthlib.flow.Flow.authorization_url()'s own generator exactly
# (same charset, same length, same SystemRandom) - not oauthlib's generic
# new_state(), which targets the (unrelated) `state` param and isn't
# guaranteed to satisfy PKCE's length/charset requirements.
_PKCE_CHARS = ascii_letters + digits + "-._~"


def _generate_code_verifier() -> str:
    rnd = SystemRandom()
    return "".join(rnd.choice(_PKCE_CHARS) for _ in range(128))


def _build_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": settings.google_calendar_client_id,
            "client_secret": settings.google_calendar_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_calendar_redirect_uri],
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=settings.google_calendar_redirect_uri,
    )


@router.get("/login")
async def google_login(user_id: str = Query(...)):
    flow = _build_flow()
    # google-auth-oauthlib auto-generates PKCE's code_verifier the first time
    # authorization_url() runs (default since 1.2+) and keeps it only on this
    # Flow instance, which is discarded once this request returns. /callback
    # builds a brand-new Flow with no verifier, so the token exchange fails
    # with "Missing code verifier" unless the same verifier is carried across
    # the redirect - same problem `state` already solves, so it travels the
    # same way: packed into the signed state payload (not a second query
    # param) so it can't be tampered with independently of the CSRF check.
    #
    # Generate the verifier ourselves *before* calling authorization_url(),
    # so it's available to sign into state up front rather than needing a
    # second pass over the returned URL.
    flow.code_verifier = _generate_code_verifier()
    signed_state = _state_signer.dumps({
        "user_id": user_id,
        "code_verifier": flow.code_verifier,
    })
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=signed_state,
    )
    return RedirectResponse(auth_url)


@router.get("/callback")
async def google_callback(code: str = Query(...), state: str = Query(...)):
    try:
        payload = _state_signer.loads(state, max_age=600)  # 10-minute window
        user_id, code_verifier = payload["user_id"], payload["code_verifier"]
    except (BadSignature, SignatureExpired, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    flow = _build_flow()
    flow.code_verifier = code_verifier  # must match the challenge sent in /login

    try:
        flow.fetch_token(code=code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth exchange failed: {e}")

    creds = flow.credentials
    await save_credentials(user_id, {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
        "scope": " ".join(creds.scopes or SCOPES),
    })

    return {"status": "connected", "user_id": user_id}