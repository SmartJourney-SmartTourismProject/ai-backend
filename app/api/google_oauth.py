# app/api/google_oauth.py
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config.settings import settings
from app.tools.calendar_tool import save_credentials, SCOPES

router = APIRouter(prefix="/auth/google", tags=["google-oauth"])

_state_signer = URLSafeTimedSerializer(settings.secret_key, salt="oauth-state")


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
    signed_state = _state_signer.dumps(user_id)
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
        user_id = _state_signer.loads(state, max_age=600)  # 10-minute window
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    flow = _build_flow()

    try:
        flow.fetch_token(code=code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth exchange failed: {e}")

    creds = flow.credentials
    await save_credentials(user_id, {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
    })

    return {"status": "connected", "user_id": user_id}