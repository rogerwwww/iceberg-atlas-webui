"""Ensure installing WebDAV cannot replace an unrelated NGINX route."""

import pytest
import os
import pwd

from webui.deploy.install_webdav import INCLUDE, add_https_include
from webui.deploy import install_webdav


SITE = '''server {
    listen 80;
    server_name iceberg-ms.mit.edu;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl;
    server_name iceberg-ms.mit.edu;
    ssl_certificate /etc/letsencrypt/live/iceberg-ms/fullchain.pem;
    # A comment with } { must not change parsing.
    location / { proxy_pass http://127.0.0.1:4285; }
    location /demo/ { proxy_pass http://127.0.0.1:4286; }
    add_header Example "value with { braces }";
}
'''


def test_only_https_server_changes_and_existing_routes_survive():
    result = add_https_include(SITE, "iceberg-ms.mit.edu")
    assert result.count(INCLUDE) == 1
    assert result.split("server {", 2)[1] == SITE.split("server {", 2)[1]
    assert result.replace(f"\n    {INCLUDE}\n", "") == SITE
    assert add_https_include(result, "iceberg-ms.mit.edu") == result


@pytest.mark.parametrize("site", [SITE.split("server {", 2)[0], SITE + SITE, SITE.replace("iceberg-ms.mit.edu", "other.mit.edu")])
def test_ambiguous_or_missing_site_is_rejected(site):
    with pytest.raises(RuntimeError, match="Expected one HTTPS server"):
        add_https_include(site, "iceberg-ms.mit.edu")


def test_existing_dav_route_is_not_replaced():
    site = SITE.replace("location /demo/", "location /dav/")
    with pytest.raises(RuntimeError, match="manual review"):
        add_https_include(site, "iceberg-ms.mit.edu")


def test_health_waits_for_route_after_reload(monkeypatch):
    failures = iter([RuntimeError("HTTPS PROPFIND /dav/: 404, expected 401"),
                     ConnectionRefusedError("backend starting"), None])
    calls = []

    def check(hostname, frontend=False):
        calls.append((hostname, frontend))
        failure = next(failures)
        if failure:
            raise failure

    monkeypatch.setattr(install_webdav, "health", check)
    install_webdav.wait_for_health("iceberg-ms.mit.edu", frontend=True, interval=0)
    assert calls == [("iceberg-ms.mit.edu", True)] * 3


def test_health_wait_still_fails_when_route_never_appears(monkeypatch):
    def check(hostname, frontend=False):
        raise RuntimeError("HTTPS PROPFIND /dav/: 404, expected 401")

    monkeypatch.setattr(install_webdav, "health", check)
    with pytest.raises(RuntimeError, match="last failure: HTTPS PROPFIND /dav/: 404"):
        install_webdav.wait_for_health("iceberg-ms.mit.edu", frontend=True, timeout=0)


def test_session_credential_is_private_and_survives_redeployment(tmp_path):
    user = pwd.getpwuid(os.getuid()).pw_name
    path = install_webdav.ensure_session_token(tmp_path / "users.yaml", user)
    credential = path.read_bytes()
    assert path.stat().st_mode & 0o077 == 0
    assert path.stat().st_uid == os.getuid()
    assert install_webdav.ensure_session_token(tmp_path / "users.yaml", user).read_bytes() == credential
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="must be private"):
        install_webdav.ensure_session_token(tmp_path / "users.yaml", user)
