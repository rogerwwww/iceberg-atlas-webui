"""Validate WebUI login cookies for WebDAV over an authenticated loopback RPC."""

from __future__ import annotations

import hmac
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

from flask import abort, jsonify, request
from flask_login import current_user


TOKEN_HEADER = "X-Iceberg-WebDAV-Auth"
DEFAULT_AUTH_URL = "http://127.0.0.1:4285/_internal/webdav-session"


def session_token_path(users_file):
    configured = os.environ.get("ICEBERG_WEBDAV_SESSION_TOKEN_FILE")
    if configured:
        return Path(configured)
    return Path(users_file).parent / ".iceberg-webdav-session-token" if users_file else None


def read_session_token(path):
    if path is None:
        return ""
    try:
        # This deployment credential stays outside source control and must not
        # be readable by another account. Neither logs nor responses include it.
        if path.stat().st_mode & 0o077:
            return ""
        token = path.read_text().strip()
        return token if len(token) >= 32 else ""
    except (OSError, UnicodeError):
        return ""


def register_session_auth(app, users_file, record_event=None):
    """Use Flask-Login itself, including remember cookies and current user roles."""
    def require_service():
        peer = request.environ.get("werkzeug.proxy_fix.orig", {}).get("REMOTE_ADDR", request.remote_addr)
        expected = read_session_token(session_token_path(users_file))
        supplied = request.headers.get(TOKEN_HEADER, "")
        if peer not in ("127.0.0.1", "::1") or not expected or not hmac.compare_digest(
            expected.encode(), supplied.encode()
        ):
            abort(403)

    def webdav_session_auth():
        require_service()
        if not current_user.is_authenticated:
            response = jsonify(authenticated=False)
            response.status_code = 401
        else:
            response = jsonify(authenticated=True, email=current_user.get_id(),
                               can_access_nist=bool(current_user.can_access_nist))
        response.headers["Cache-Control"] = "private, no-store"
        return response

    app.add_url_rule("/_internal/webdav-session", "webdav_session_auth", webdav_session_auth, methods=["GET"])

    def webdav_event():
        require_service()
        if request.content_length is None or request.content_length > 16384:
            abort(400)
        event = request.get_json(silent=True)
        if not isinstance(event, dict):
            abort(400)
        fields = ("action", "path", "method", "ip", "user_id")
        if any(not isinstance(event.get(key), str) for key in fields):
            abort(400)
        if (event["action"] not in ("browse", "download", "metadata", "denied", "auth_failed")
                or not event["path"].startswith("/dav/") or len(event["path"]) > 8192
                or len(event["user_id"]) > 320 or len(event["method"]) > 32
                or not event["method"].isascii() or not event["method"].isalpha()):
            abort(400)
        try:
            ipaddress.ip_address(event["ip"])
        except ValueError:
            abort(400)
        path = "/webdav/" + event["action"] + quote(event["path"], safe="/._-")
        if record_event is None or not record_event(event["ip"], path, event["method"], event["user_id"]):
            abort(503)
        return "", 204

    app.add_url_rule("/_internal/webdav-event", "webdav_event", webdav_event, methods=["POST"])


def record_webdav_event(event, *, users_file):
    """Send activity to the WebUI writer; the DAV service keeps a read-only home."""
    token = read_session_token(session_token_path(users_file))
    connection = None
    try:
        if not token:
            raise ValueError("Missing private service credential")
        target = urlsplit(os.environ.get("ICEBERG_WEBUI_AUTH_URL", DEFAULT_AUTH_URL))
        if target.scheme != "http" or target.hostname not in ("127.0.0.1", "::1", "localhost") or target.username:
            raise ValueError("WebUI endpoint must be HTTP loopback")
        connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=1)
        connection.request("POST", "/_internal/webdav-event", body=json.dumps(event).encode(),
                           headers={TOKEN_HEADER: token, "Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 204:
            raise ValueError("Analytics writer rejected event")
        return True
    except (OSError, ValueError, http.client.HTTPException):
        logging.getLogger(__name__).warning("WebDAV analytics event could not be recorded")
        return False
    finally:
        if connection is not None:
            connection.close()


def browser_session_identity(environ, *, users_file, auth_url=None):
    """Return a validated browser identity, or None to continue password auth."""
    cookie = environ.get("HTTP_COOKIE", "")
    if not cookie:
        return None
    token = read_session_token(session_token_path(users_file))
    if not token:
        return None
    target = urlsplit(auth_url or os.environ.get("ICEBERG_WEBUI_AUTH_URL", DEFAULT_AUTH_URL))
    if target.scheme != "http" or target.hostname not in ("127.0.0.1", "::1", "localhost") or target.username:
        raise ValueError("ICEBERG_WEBUI_AUTH_URL must be an HTTP loopback endpoint")
    headers = {
        "Cookie": cookie,
        TOKEN_HEADER: token,
        "User-Agent": environ.get("HTTP_USER_AGENT", ""),
        # Preserve Flask-Login's client fingerprint, even with strong session
        # protection. NGINX overwrites X-Real-IP at the loopback-only DAV backend.
        "X-Forwarded-For": environ.get("HTTP_X_REAL_IP", environ.get("REMOTE_ADDR", "127.0.0.1")),
        "X-Forwarded-Proto": "https",
    }
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=2)
    try:
        connection.request("GET", target.path or "/", headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = response.read(8193)
        if len(body) > 8192:
            return None
        identity = json.loads(body)
        if (not isinstance(identity, dict) or identity.get("authenticated") is not True or not isinstance(identity.get("email"), str)
                or not isinstance(identity.get("can_access_nist"), bool)):
            return None
        return identity
    except (OSError, ValueError, http.client.HTTPException):
        # An invalid/expired session or unavailable WebUI must never grant access.
        return None
    finally:
        connection.close()
