"""HTTP-level checks for mountable, role-aware, read-only atlas access."""

import base64
import importlib
from html.parser import HTMLParser
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree

import pytest
import yaml
from werkzeug.security import generate_password_hash
from werkzeug.test import Client
from werkzeug.wrappers import Response
from werkzeug.serving import make_server, WSGIRequestHandler

pytest.importorskip("wsgidav")
from webui.webdav_server import create_app
from webui.webdav_session import TOKEN_HEADER, session_token_path


def basic(email, password="Password!1"):
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def dav(tmp_path):
    public, nist = tmp_path / "atlas", tmp_path / "nist"
    for root in (public, nist):
        for name in ("h_plus_out_mgf", "h_minus_out_mgf"):
            (root / name).mkdir(parents=True)
    content = b"BEGIN IONS\nFORMULA=C2H6O\nEND IONS\n"
    file = public / "h_plus_out_mgf" / "test file ü.mgf"
    file.write_bytes(content)
    (file.parent / "test.mgf.idx").write_bytes(b"private index")
    (nist / "h_plus_out_mgf" / "nist.mgf").write_bytes(b"restricted NIST")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"outside secret")
    (file.parent / "escape.mgf").symlink_to(outside)
    password_hash = generate_password_hash("Password!1", method="pbkdf2:sha256:1000")
    records = {
        f"{role}@example.com": {"password": password_hash, "role": role}
        for role in ("user", "authorized_user", "admin")
    }
    records["override@example.com"] = {"password": password_hash, "role": "user"}
    records["legacy@example.com"] = password_hash
    users = tmp_path / "users.yaml"
    users.write_text(yaml.safe_dump(records))
    app = create_app(atlas_dir=public, nist_atlas_dir=nist, users_file=users,
                     admin_emails={"override@example.com", "absent@example.com"})
    return Client(app, Response), users, public, nist, content


def hrefs(response):
    root = ElementTree.fromstring(response.data)
    return {unquote(node.text) for node in root.findall("{DAV:}response/{DAV:}href")}


def html_links(response, path="/dav/"):
    links = set()

    class Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name == "href" and value:
                    links.add(unquote(urlsplit(urljoin(path, value)).path))

    Parser().feed(response.data.decode())
    return links


def test_mount_discovery_auth_and_virtual_paths(dav):
    client, _, _, _, _ = dav
    response = client.open("/dav/", method="PROPFIND", headers={"Depth": "1"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic ")
    assert client.get("/dav/", headers=basic("user@example.com", "wrong")).status_code == 401
    for email in ("user@example.com", "legacy@example.com", " USER@EXAMPLE.COM "):
        response = client.open("/dav/", method="PROPFIND", headers={**basic(email), "Depth": "1"})
        assert response.status_code == 207
        assert hrefs(response) == {"/dav/", "/dav/public/"}
    response = client.open("/dav/public/", method="PROPFIND",
                           headers={**basic("user@example.com"), "Depth": "1"})
    assert hrefs(response) == {"/dav/public/", "/dav/public/h_plus/", "/dav/public/h_minus/"}
    assert client.get("/dav/", headers=basic("absent@example.com")).status_code == 401


@pytest.mark.parametrize("email", ["authorized_user@example.com", "admin@example.com", "override@example.com"])
def test_nist_authorized_roles(dav, email):
    client, _, _, _, _ = dav
    response = client.open("/dav/", method="PROPFIND", headers={**basic(email), "Depth": "1"})
    assert "/dav/nist23/" in hrefs(response)
    assert client.get("/dav/nist23/h_plus/nist.mgf", headers=basic(email)).data == b"restricted NIST"


def test_browser_directory_listing_and_assets(dav):
    client = dav[0]
    auth = basic("user@example.com")
    assert client.get("/dav/").status_code == 401
    root = client.get("/dav/", headers=auth)
    assert root.status_code == 200 and root.mimetype == "text/html"
    assert '/dav/public/' in html_links(root) and '/dav/nist23/' not in html_links(root)
    assert b'/dav/:dir_browser/style.css' in root.data
    assert b'/dav/:dir_browser/script.js' in root.data
    folder = client.get("/dav/public/h_plus/", headers=auth)
    assert folder.status_code == 200
    assert b'test file' in folder.data and b'escape.mgf' not in folder.data
    assert b'test.mgf.idx' not in folder.data
    for asset in ("style.css", "script.js"):
        assert client.get(f"/dav/:dir_browser/{asset}", headers=auth).status_code == 200
        assert client.get(f"/dav/:dir_browser/{asset}").status_code == 401
    authorized = client.get("/dav/", headers=basic("authorized_user@example.com"))
    assert authorized.status_code == 200 and '/dav/nist23/' in html_links(authorized)
    assert client.get("/dav/nist23/", headers=auth).status_code == 403


@pytest.mark.parametrize("method", ["GET", "HEAD", "PROPFIND"])
def test_nist_denied_for_regular_user(dav, method):
    response = dav[0].open("/dav/nist23/h_plus/nist.mgf", method=method,
                          headers={**basic("user@example.com"), "Depth": "0"})
    assert response.status_code == 403
    assert b"restricted NIST" not in response.data


def test_read_range_head_and_conditional_get(dav):
    client, _, _, _, content = dav
    path = "/dav/public/h_plus/test%20file%20%C3%BC.mgf"
    auth = basic("user@example.com")
    response = client.get(path, headers=auth)
    assert response.status_code == 200
    assert response.data == content
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Cache-Control"] == "private, no-store"
    head = client.head(path, headers=auth)
    assert head.status_code == 200 and head.data == b""
    assert int(head.headers["Content-Length"]) == len(content)
    ranged = client.get(path, headers={**auth, "Range": "bytes=3-9"})
    assert ranged.status_code == 206 and ranged.data == content[3:10]
    assert ranged.headers["Content-Range"] == f"bytes 3-9/{len(content)}"
    assert client.get(path, headers={**auth, "Range": "bytes=999-"}).status_code == 416
    assert client.get(path, headers={**auth, "If-None-Match": response.headers["ETag"]}).status_code == 304
    listing = client.open("/dav/public/h_plus/", method="PROPFIND",
                          headers={**auth, "Depth": "1"})
    assert path.replace("%20", " ").replace("%C3%BC", "ü") in hrefs(listing)
    assert not any("escape" in href or ".idx" in href for href in hrefs(listing))


@pytest.mark.parametrize("path", [
    "/dav/public/h_plus/escape.mgf", "/dav/public/h_plus/test.mgf.idx",
    "/dav/%2E%2E/secret.txt", "/dav/public/h_plus/%2E%2E/%2E%2E/secret.txt",
    "/dav/public/h_plus/%2E%2E/%2E%2E/%2E%2E/nist23/h_plus/nist.mgf",
])
def test_path_escape_and_hidden_files_denied(dav, path):
    response = dav[0].get(path, headers=basic("user@example.com"))
    assert response.status_code in (403, 404)
    assert b"outside secret" not in response.data
    assert b"private index" not in response.data
    assert b"restricted NIST" not in response.data


@pytest.mark.parametrize("role", ["user", "authorized_user", "admin"])
@pytest.mark.parametrize("method", ["PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK", "POST"])
def test_all_roles_cannot_modify_files(dav, role, method):
    client, _, public, nist, _ = dav
    before = {str(p): p.read_bytes() for root in (public, nist) for p in root.rglob("*") if p.is_file()}
    response = client.open("/dav/public/h_plus/test%20file%20%C3%BC.mgf", method=method,
                          headers={**basic(f"{role}@example.com"), "Destination": "/dav/public/h_plus/new.mgf"},
                          data=b"mutation attempt")
    assert response.status_code == 405
    assert "PUT" not in response.headers["Allow"]
    after = {str(p): p.read_bytes() for root in (public, nist) for p in root.rglob("*") if p.is_file()}
    assert before == after
    assert not (public / "h_plus_out_mgf" / "new.mgf").exists()


def test_credential_and_role_changes_apply_without_restart(dav):
    client, users, _, _, _ = dav
    path = "/dav/nist23/h_plus/nist.mgf"
    auth = basic("authorized_user@example.com")
    assert client.get(path, headers=auth).status_code == 200
    records = yaml.safe_load(users.read_text())
    records["authorized_user@example.com"]["role"] = "user"
    users.write_text(yaml.safe_dump(records))
    assert client.get(path, headers=auth).status_code == 403
    records["authorized_user@example.com"]["password"] = generate_password_hash("new", method="pbkdf2:sha256:1000")
    users.write_text(yaml.safe_dump(records))
    assert client.get("/dav/public/", headers=auth).status_code == 401
    del records["authorized_user@example.com"]
    users.write_text(yaml.safe_dump(records))
    assert client.get("/dav/public/", headers=basic("authorized_user@example.com", "new")).status_code == 401


def test_mount_options_and_finite_depth(dav):
    client = dav[0]
    auth = basic("user@example.com")
    options = client.open("/dav/", method="OPTIONS", headers=auth)
    assert options.status_code == 200 and options.headers["DAV"] == "1"
    assert "PROPFIND" in options.headers["Allow"] and "PUT" not in options.headers["Allow"]
    for headers in (auth, {**auth, "Depth": "infinity"}):
        response = client.open("/dav/", method="PROPFIND", headers=headers)
        assert response.status_code == 403 and b"propfind-finite-depth" in response.data
    assert client.open("/dav/", method="PROPFIND", headers={**auth, "Depth": "0"}).status_code == 207


def test_large_file_range_is_streamed(dav):
    client, _, public, _, _ = dav
    path = public / "h_plus_out_mgf" / "large.mgf"
    with path.open("wb") as f:
        f.truncate(64 * 1024 * 1024)
        f.seek(60 * 1024 * 1024)
        f.write(b"range marker")
    offset = 60 * 1024 * 1024
    response = client.get("/dav/public/h_plus/large.mgf",
                          headers={**basic("user@example.com"), "Range": f"bytes={offset}-{offset+11}"})
    assert response.status_code == 206 and response.data == b"range marker"


@pytest.fixture
def browser_login(dav, monkeypatch, tmp_path):
    """Exercise the real WebUI login and a real authenticated loopback RPC."""
    _, users, public, nist, _ = dav
    monkeypatch.setenv("FLASK_SECRET_KEY", "webdav-session-test-key")
    monkeypatch.setenv("MSPRED_ATLAS_DIR", str(public))
    monkeypatch.setenv("MSPRED_ATLAS_DIR_NIST", str(nist))
    monkeypatch.setenv("ICEBERG_USERS_FILE", str(users))
    monkeypatch.setenv("ICEBERG_ADMIN_EMAILS", "override@example.com")
    monkeypatch.setenv("ICEBERG_ANALYTICS_DB", str(tmp_path / "analytics.db"))
    token_path = session_token_path(users)
    token_path.write_text(secrets.token_hex(32))
    token_path.chmod(0o600)
    sys.modules.pop("webui.app", None)
    module = importlib.import_module("webui.app")
    app = module.app
    app.config.update(TESTING=True, SESSION_PROTECTION="strong")

    class QuietHandler(WSGIRequestHandler):
        def log(self, *args, **kwargs):
            pass

    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
    monkeypatch.setenv("ICEBERG_WEBUI_AUTH_URL", f"http://127.0.0.1:{server.server_port}/_internal/webdav-session")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app.test_client(), Client(create_app(atlas_dir=public, nist_atlas_dir=nist,
          users_file=users, admin_emails={"override@example.com"}), Response, use_cookies=False), users, token_path
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def session_headers(client, email="user@example.com"):
    response = client.post("/login", data={"email": email, "password": "Password!1"},
                           headers={"User-Agent": "WebDAV browser test"},
                           environ_overrides={"REMOTE_ADDR": "203.0.113.8"})
    assert response.status_code == 302
    cookies = [f"{name}={client.get_cookie(name).value}" for name in ("session", "remember_token")
               if client.get_cookie(name)]
    return {"Cookie": "; ".join(cookies), "User-Agent": "WebDAV browser test",
            "X-Real-IP": "203.0.113.8"}


@pytest.mark.parametrize("email,has_nist", [("user@example.com", False),
    ("authorized_user@example.com", True), ("admin@example.com", True), ("override@example.com", True)])
def test_existing_webui_session_opens_dav_without_password_prompt(browser_login, email, has_nist):
    web, client, _, _ = browser_login
    headers = session_headers(web, email)
    response = client.get("/dav/", headers=headers)
    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers
    assert ("/dav/nist23/" in html_links(response)) == has_nist
    assert client.get("/dav/:dir_browser/style.css", headers=headers).status_code == 200
    listing = client.open("/dav/", method="PROPFIND", headers={**headers, "Depth": "1"})
    assert listing.status_code == 207
    assert ("/dav/nist23/" in hrefs(listing)) == has_nist
    assert client.open("/dav/public/new.mgf", method="PUT", headers=headers, data=b"no write").status_code == 405


def test_remember_cookie_only_is_accepted(browser_login):
    web, client, _, _ = browser_login
    headers = session_headers(web)
    headers["Cookie"] = "remember_token=" + web.get_cookie("remember_token").value
    assert client.get("/dav/", headers=headers).status_code == 200


def test_invalid_session_and_forged_headers_do_not_grant_access(browser_login):
    web, client, _, token_path = browser_login
    forged = {"Cookie": "session=forged", "X-Remote-User": "admin@example.com", "REMOTE_USER": "admin@example.com"}
    assert client.get("/dav/", headers=forged).status_code == 401
    # The internal endpoint rejects requests even with a valid session unless
    # the caller supplies the private credential and originates on loopback.
    headers = session_headers(web)
    assert web.get("/_internal/webdav-session", headers=headers).status_code == 403
    assert web.get("/_internal/webdav-session", headers={**headers, TOKEN_HEADER: "incorrect"},
                   environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 403
    assert web.get("/_internal/webdav-session", headers={TOKEN_HEADER: token_path.read_text()},
                   environ_overrides={"REMOTE_ADDR": "203.0.113.8"}).status_code == 403


def test_logout_and_role_revocation_apply_to_dav(browser_login):
    web, client, users, _ = browser_login
    headers = session_headers(web, "authorized_user@example.com")
    assert client.get("/dav/nist23/h_plus/nist.mgf", headers=headers).status_code == 200
    records = yaml.safe_load(users.read_text())
    records["authorized_user@example.com"]["role"] = "user"
    users.write_text(yaml.safe_dump(records))
    assert client.get("/dav/nist23/h_plus/nist.mgf", headers=headers).status_code == 403
    web.post("/logout", headers={"User-Agent": headers["User-Agent"]},
             environ_overrides={"REMOTE_ADDR": "203.0.113.8"})
    cookies = [f"{name}={web.get_cookie(name).value}" for name in ("session", "remember_token") if web.get_cookie(name)]
    headers["Cookie"] = "; ".join(cookies)
    assert client.get("/dav/", headers=headers).status_code == 401


def test_session_identity_takes_precedence_over_cached_basic_credentials(browser_login):
    web, client, _, _ = browser_login
    headers = {**session_headers(web), **basic("admin@example.com")}
    assert client.get("/dav/nist23/h_plus/nist.mgf", headers=headers).status_code == 403


def test_auth_service_failure_falls_back_to_password_login(browser_login, monkeypatch):
    web, client, _, _ = browser_login
    headers = session_headers(web)
    monkeypatch.setenv("ICEBERG_WEBUI_AUTH_URL", "http://127.0.0.1:1/_internal/webdav-session")
    assert client.get("/dav/", headers=headers).status_code == 401
    assert client.get("/dav/", headers={**headers, **basic("user@example.com")}).status_code == 200


@pytest.mark.parametrize("login_kind", ["session", "basic"])
def test_webdav_activity_reaches_admin_analytics(browser_login, monkeypatch, login_kind):
    web, client, users, _ = browser_login
    module = sys.modules["webui.app"]
    # Refresh deterministically to check the portal's aggregate API as well as
    # the raw log; production uses the same background refresh path.
    monkeypatch.setattr(module, "_maybe_start_analytics_refresh", lambda **kwargs: None)
    headers = (session_headers(web) if login_kind == "session"
               else {**basic("user@example.com"), "X-Real-IP": "203.0.113.8"})
    assert client.get("/dav/public/h_plus/", headers=headers).status_code == 200
    assert client.open("/dav/public/h_plus/", method="PROPFIND",
                       headers={**headers, "Depth": "1"}).status_code == 207
    path = "/dav/public/h_plus/test%20file%20%C3%BC.mgf"
    assert client.get(path, headers={**headers, "Range": "bytes=0-4"}).status_code == 206
    assert client.head(path, headers=headers).status_code == 200
    assert client.get("/dav/nist23/h_plus/nist.mgf", headers=headers).status_code == 403
    assert client.get("/dav/:dir_browser/style.css", headers=headers).status_code == 200
    assert client.open("/dav/", method="OPTIONS", headers=headers).status_code == 200
    assert client.get("/dav/").status_code == 401  # normal auth challenge
    assert client.get("/dav/", headers=basic("user@example.com", "wrong")).status_code == 401
    with sqlite3.connect(users.parent / "analytics.db") as conn:
        rows = conn.execute("SELECT ip,path,method,user_id FROM requests WHERE source='webdav' ORDER BY id").fetchall()
        assert len(rows) == 6
        assert all(row[0] == "203.0.113.8" and row[3] == "user@example.com" for row in rows[:5])
        assert rows[2][1] == "/webdav/download/dav/public/h_plus/test%20file%20%C3%BC.mgf"
        assert rows[2][2] == "GET" and rows[3][2] == "HEAD"
        assert rows[4][1].startswith("/webdav/denied/")
        assert rows[5][1].startswith("/webdav/auth_failed/") and rows[5][3] == ""
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE path LIKE '/_internal/%'").fetchone()[0] == 0
        assert "Password!1" not in repr(rows) and "Cookie" not in repr(rows)
    module._refresh_analytics_aggregates(max_batches=5)
    admin = web.application.test_client()
    session_headers(admin, "admin@example.com")
    response = admin.get("/admin/api/stats", headers={"User-Agent": "WebDAV browser test"},
                         environ_overrides={"REMOTE_ADDR": "203.0.113.8"})
    assert response.status_code == 200
    actions = {row["action"]: row["count"] for row in response.json["top_actions"]}
    assert actions["WebDAV browse directory"] == 2
    assert actions["WebDAV download request"] == 1
    assert actions["WebDAV metadata check"] == 1
    assert actions["WebDAV denied access"] == 1
    assert actions["WebDAV failed login"] == 1


def test_private_analytics_writer_rejects_forgery_and_malformed_events(browser_login):
    web, _, _, token_path = browser_login
    path = "/_internal/webdav-event"
    event = dict(action="browse", path="/dav/", method="GET", ip="203.0.113.8", user_id="user@example.com")
    assert web.post(path, json=event).status_code == 403
    token_header = {TOKEN_HEADER: token_path.read_text()}
    assert web.post(path, json=event, headers=token_header,
                    environ_overrides={"REMOTE_ADDR": "203.0.113.8"}).status_code == 403
    for changes in ({"path": "/admin/"}, {"action": "bogus"}, {"ip": "forged"}, {"user_id": []}):
        assert web.post(path, json={**event, **changes}, headers=token_header,
                        environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 400


def test_analytics_outage_does_not_interrupt_streamed_download(browser_login, monkeypatch):
    _, client, _, _ = browser_login
    monkeypatch.setenv("ICEBERG_WEBUI_AUTH_URL", "http://127.0.0.1:1/_internal/webdav-session")
    response = client.get("/dav/public/h_plus/test%20file%20%C3%BC.mgf",
                          headers={**basic("user@example.com"), "Range": "bytes=0-4"})
    assert response.status_code == 206 and response.data == b"BEGIN"
