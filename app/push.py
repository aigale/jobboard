"""Browser push notifications (Web Push with VAPID).

Every in-app Notification also becomes a PushMessage automatically (see the flush listener below).
The worker and request background tasks send waiting messages to each browser the person turned
push on in. Without VAPID keys in .env, push is simply off and messages are discarded.

Generate keys once with:  python -m app.cli vapid-keys

The Android and iPhone app registers its device with POST /api/me/devices; those phones get the same
messages through Firebase Cloud Messaging (FCM HTTP v1) when FCM_PROJECT_ID and
FCM_SERVICE_ACCOUNT_FILE are set. FCM delivers to iPhones too, once an APNs key is added in Firebase.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import timedelta

from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import DeviceToken, Notification, PushMessage, PushSubscription, utcnow

log = logging.getLogger("sinag.push")


def enabled() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


@event.listens_for(Session, "before_flush")
def _notifications_become_pushes(session: Session, _ctx, _instances) -> None:
    for obj in list(session.new):
        if isinstance(obj, Notification) and not getattr(obj, "_pushed", False):
            obj._pushed = True
            session.add(PushMessage(user_id=obj.user_id, title="Sinag Jobs", body=obj.text[:500], link=obj.link))


def generate_keys() -> tuple[str, str]:
    """Returns (public, private) as base64url strings for .env."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return b64(pub), b64(priv)


def _send(sub: PushSubscription, payload: dict) -> int:
    """Returns the push service's HTTP status (201 = delivered to the service)."""
    from pywebpush import WebPushException, webpush
    try:
        resp = webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=json.dumps(payload), vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject}, ttl=86400, timeout=10)
        return resp.status_code
    except WebPushException as exc:
        return exc.response.status_code if exc.response is not None else 0


# ---------------------------------------------------------------------------
# Firebase Cloud Messaging (phone app)
# ---------------------------------------------------------------------------

_fcm_token: dict = {"value": None, "expires": 0.0}
_fcm_transport = None          # tests swap in a fake Google


def fcm_enabled() -> bool:
    return bool(settings.fcm_project_id and settings.fcm_service_account_file)


def _fcm_access_token(client) -> str:
    """OAuth token for FCM from the service account key (a signed JWT swapped at Google's token endpoint)."""
    import time
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    if _fcm_token["value"] and _fcm_token["expires"] > time.time() + 60:
        return _fcm_token["value"]
    with open(settings.fcm_service_account_file) as f:
        sa = json.load(f)
    now = int(time.time())
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64(json.dumps({"iss": sa["client_email"], "scope": "https://www.googleapis.com/auth/firebase.messaging",
                             "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"), "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = b64(key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    r = client.post(sa.get("token_uri", "https://oauth2.googleapis.com/token"),
                    data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{header}.{claims}.{sig}"})
    r.raise_for_status()
    _fcm_token["value"], _fcm_token["expires"] = r.json()["access_token"], now + int(r.json().get("expires_in", 3600))
    return _fcm_token["value"]


def _fcm_send(client, token: str, title: str, body: str, url: str) -> str:
    """Returns 'ok', 'gone' (forget this device) or 'error'."""
    msg = {"message": {"token": token, "notification": {"title": title, "body": body}, "data": {"url": url},
                       "android": {"priority": "high"},
                       "apns": {"payload": {"aps": {"sound": "default"}}}}}
    r = client.post(f"https://fcm.googleapis.com/v1/projects/{settings.fcm_project_id}/messages:send", json=msg,
                    headers={"Authorization": f"Bearer {_fcm_access_token(client)}"})
    if r.status_code == 200:
        return "ok"
    detail = r.text
    if r.status_code == 404 or "UNREGISTERED" in detail or (r.status_code == 400 and "registration token" in detail.lower()):
        return "gone"
    log.warning("FCM send failed: %s %s", r.status_code, detail[:200])
    return "error"


def flush_push(session_factory: sessionmaker | None = None, limit: int = 200) -> int:
    from .db import SessionLocal
    from .mailer import default_factory
    factory = session_factory or default_factory or SessionLocal
    delivered = 0
    with factory() as db:
        msgs = db.scalars(select(PushMessage).where(PushMessage.sent_at.is_(None))
                          .order_by(PushMessage.id).limit(limit)).all()
        fcm_client = None
        if fcm_enabled() and msgs:
            import httpx
            fcm_client = httpx.Client(timeout=10, transport=_fcm_transport)
        for m in msgs:
            m.sent_at = utcnow()
            if fcm_client is not None:
                for d in db.scalars(select(DeviceToken).where(DeviceToken.user_id == m.user_id)).all():
                    try:
                        res = _fcm_send(fcm_client, d.token, m.title, m.body, m.link or "/")
                    except Exception:            # network trouble or a bad key file: keep going
                        log.exception("FCM send failed")
                        res = "error"
                    if res == "gone":
                        db.delete(d)
                    elif res == "ok":
                        delivered += 1
            if not enabled():
                continue
            for sub in db.scalars(select(PushSubscription).where(PushSubscription.user_id == m.user_id)).all():
                status = _send(sub, {"title": m.title, "body": m.body, "url": m.link or "/"})
                if status in (404, 410):          # the browser unsubscribed; forget it
                    db.delete(sub)
                elif 200 <= status < 300:
                    delivered += 1
                else:
                    log.warning("push to %s… failed with status %s", sub.endpoint[:40], status)
        if fcm_client is not None:
            fcm_client.close()
        db.execute(delete(PushMessage).where(PushMessage.sent_at < utcnow() - timedelta(days=7)))
        db.commit()
    return delivered
