"""Daily OAuth for both Fyers apps.

Flow (official v3): user clicks "Reconnect" for a role -> browser goes to
``/generate-authcode`` -> Fyers redirects to ``/api/auth/callback?auth_code=..&state=<role>:<nonce>``
-> we POST ``/validate-authcode`` with ``appIdHash = sha256(app_id:secret)`` *from that
role's source IP* -> token stored -> sockets restarted -> broadcast to every tab.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time

from viper.config import FyersApp
from viper.fyers.rest import API, FyersError, auth_url
from viper.net.binding import bound_to, make_session
from viper.store.db import DB

_pending_states: dict[str, tuple[str, float]] = {}


def begin(app: FyersApp) -> str:
    if not app.configured:
        raise FyersError(f"{app.role} app is not configured (set FYERS_{app.role.upper()}_APP_ID / _SECRET)")
    nonce = secrets.token_urlsafe(12)
    state = f"{app.role}:{nonce}"
    _pending_states[state] = (app.role, time.time())
    for k in [k for k, (_, t) in _pending_states.items() if time.time() - t > 900]:
        del _pending_states[k]
    return auth_url(app.app_id, app.redirect_uri, state)


def role_for_state(state: str) -> str | None:
    hit = _pending_states.pop(state, None)
    return hit[0] if hit else None


def jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(data.get("exp", 0))
    except Exception:
        return 0.0


def exchange(app: FyersApp, auth_code: str, source_ip: str | None) -> str:
    app_hash = hashlib.sha256(f"{app.app_id}:{app.secret}".encode()).hexdigest()
    s = make_session(source_ip)
    with bound_to(source_ip):
        r = s.post(
            f"{API}/validate-authcode",
            json={"grant_type": "authorization_code", "appIdHash": app_hash, "code": auth_code},
            timeout=15,
        )
    try:
        data = r.json()
    except ValueError as e:
        raise FyersError(f"validate-authcode returned non-JSON (HTTP {r.status_code})") from e
    if data.get("s") != "ok" or not data.get("access_token"):
        raise FyersError(f"token exchange failed: {data.get('message', data)}", code=data.get("code"))
    return data["access_token"]


def store(db: DB, role: str, app_id: str, token: str, profile: dict | None = None) -> None:
    exp = jwt_exp(token) or (time.time() + 18 * 3600)
    db.x(
        "INSERT INTO tokens(role,app_id,access_token,issued_at,expires_at,profile) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(role) DO UPDATE SET app_id=excluded.app_id, access_token=excluded.access_token, "
        "issued_at=excluded.issued_at, expires_at=excluded.expires_at, profile=excluded.profile",
        (role, app_id, token, time.time(), exp, json.dumps(profile or {})),
    )


def load(db: DB, role: str) -> dict | None:
    row = db.one("SELECT * FROM tokens WHERE role=?", (role,))
    if not row or row["expires_at"] < time.time():
        return None
    return row
