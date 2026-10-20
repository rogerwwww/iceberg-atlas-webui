"""Read-only WebDAV for the same atlas paths and accounts as the WebUI/SFTP."""

from __future__ import annotations

import os
from pathlib import Path

from werkzeug.security import check_password_hash
from werkzeug.wsgi import get_path_info
from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider
from wsgidav.dc.base_dc import BaseDomainController
from wsgidav.default_conf import DEFAULT_CONFIG
from wsgidav.dir_browser import WsgiDavDirBrowser
from wsgidav.wsgidav_app import WsgiDAVApp

try:
    from .sftp_server import _get_record, _load_users
    from .webdav_session import browser_session_identity, record_webdav_event
    from .vfs import AtlasVirtualFS, VfsEntry, VfsNotFoundError, VfsPermissionError, role_can_access_nist
except ImportError:  # Direct Gunicorn import from webui/.
    from sftp_server import _get_record, _load_users
    from webdav_session import browser_session_identity, record_webdav_event
    from vfs import AtlasVirtualFS, VfsEntry, VfsNotFoundError, VfsPermissionError, role_can_access_nist


class AtlasDomainController(BaseDomainController):
    """Check the existing Werkzeug password hash without storing plain passwords."""

    def get_domain_realm(self, path_info, environ):
        return "ICEBERG atlas"

    def require_authentication(self, realm, environ):
        if environ is not None:
            identity = browser_session_identity(environ, users_file=self.config["iceberg"]["users_file"])
            if identity is not None:
                email = identity["email"].strip().lower()
                record = _get_record(_load_users(self.config["iceberg"]["users_file"]), email)
                if record is not None:
                    self._set_identity(environ, email, record)
                    environ["iceberg.include_nist"] &= identity["can_access_nist"]
                    environ["wsgidav.auth.user_name"] = email
                    return False
        return True

    def supports_http_digest_auth(self):
        # Digest cannot be derived from the existing salted password hashes.
        return False

    def basic_auth_user(self, realm, user_name, password, environ):
        opts = self.config["iceberg"]
        email = user_name.strip().lower()
        record = _get_record(_load_users(opts["users_file"]), email)
        if record is None:
            return False
        try:
            valid = check_password_hash(record["password"], password)
        except (KeyError, TypeError, ValueError):
            return False
        if not valid:
            return False
        # Take permissions from the same record used for password verification.
        # No cross-request credential or role cache: resets/revocations apply now.
        self._set_identity(environ, email, record)
        return True

    def _set_identity(self, environ, email, record):
        opts = self.config["iceberg"]
        environ["iceberg.email"] = email
        environ["iceberg.include_nist"] = role_can_access_nist(
            str(record.get("role", "user")), email, opts["admin_emails"]
        )
        environ["wsgidav.auth.roles"] = ("reader",)
        environ["wsgidav.auth.permissions"] = ("browse_dir",)


class AtlasDAVProvider(DAVProvider):
    def __init__(self, vfs: AtlasVirtualFS):
        super().__init__()
        self.vfs = vfs

    def is_readonly(self):
        return True

    def get_resource_inst(self, path, environ):
        # Never grant file access if called without our authentication middleware.
        if not environ.get("iceberg.email"):
            raise DAVError(HTTP_FORBIDDEN)
        try:
            entry = self.vfs.stat(path or "/", bool(environ.get("iceberg.include_nist")))
        except VfsPermissionError as exc:
            raise DAVError(HTTP_FORBIDDEN) from exc
        except (VfsNotFoundError, FileNotFoundError, NotADirectoryError):
            return None
        resource_cls = AtlasCollection if entry.is_dir else AtlasFile
        # The first resolved resource is the request target; PROPFIND may then
        # resolve children, whose types must not change the recorded action.
        environ.setdefault("iceberg.target_is_dir", entry.is_dir)
        return resource_cls(path or "/", environ, entry)


class AtlasCollection(DAVCollection):
    def __init__(self, path, environ, entry: VfsEntry):
        super().__init__(path, environ)
        self.entry = entry

    def get_member_names(self):
        entries = self.provider.vfs.listdir(
            self.path, bool(self.environ.get("iceberg.include_nist"))
        )
        return [entry.name for entry in entries]

    def get_last_modified(self):
        return self.entry.mtime


class AtlasFile(DAVNonCollection):
    def __init__(self, path, environ, entry: VfsEntry):
        super().__init__(path, environ)
        self.entry = entry

    def get_content_length(self):
        return self.entry.size

    def get_content_type(self):
        return "application/octet-stream"

    def get_last_modified(self):
        return self.entry.mtime

    def get_etag(self):
        return f"{self.entry.size:x}-{int((self.entry.mtime or 0) * 1_000_000_000):x}"

    def support_etag(self):
        return True

    def support_ranges(self):
        return True

    def get_content(self):
        # Reuse traversal/symlink checks at open time as well as lookup time.
        return self.provider.vfs.open_read(
            self.path, bool(self.environ.get("iceberg.include_nist"))
        )


class AtlasDirBrowser(WsgiDavDirBrowser):
    """Keep browser assets within /dav/, which is the NGINX-proxied prefix."""

    def __init__(self, wsgidav_app, next_app, config):
        super().__init__(wsgidav_app, next_app, config)
        wsgidav_app.add_provider("/dav/:dir_browser", self.htdocs_path, readonly=True)

    def _get_context(self, environ, dav_res):
        context = super()._get_context(environ, dav_res)
        context["htdocs"] = "/dav/:dir_browser"
        return context


class DAVAnalytics:
    """Record accepted requests, not completed transfers, without buffering files."""

    def __init__(self, app, users_file):
        self.app = app
        self.users_file = users_file

    def __call__(self, environ, start_response):
        path = get_path_info(environ)
        method = environ.get("REQUEST_METHOD", "")
        recorded = False

        def respond(status, headers, exc_info=None):
            nonlocal recorded
            code = int(status.split()[0])
            action = None
            if path.startswith("/dav/") and not path.startswith("/dav/:dir_browser/"):
                if environ.get("iceberg.email") and code in (200, 206, 207, 304):
                    if method == "GET":
                        action = "browse" if environ.get("iceberg.target_is_dir") else "download"
                    elif method == "PROPFIND":
                        action = "browse" if environ.get("iceberg.target_is_dir") else "metadata"
                    elif method == "HEAD":
                        action = "metadata"
                elif code in (403, 405):
                    action = "denied"
                elif code == 401 and environ.get("HTTP_AUTHORIZATION"):
                    action = "auth_failed"
            if action and not recorded:
                recorded = True
                record_webdav_event({"action": action, "path": path, "method": method,
                                     "ip": environ.get("HTTP_X_REAL_IP") or environ.get("REMOTE_ADDR", "127.0.0.1"),
                                     "user_id": environ.get("iceberg.email", "")}, users_file=self.users_file)
            return start_response(status, headers, exc_info)

        return self.app(environ, respond)


class ReadOnlyDAV:
    """Reject mutations and unbounded recursive listings before they do work."""

    allowed_methods = ("OPTIONS", "PROPFIND", "GET", "HEAD")

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        def respond(status, headers, exc_info=None):
            headers = list(headers) + [("Cache-Control", "private, no-store")]
            return start_response(status, headers, exc_info)

        method = environ.get("REQUEST_METHOD", "")
        if method not in self.allowed_methods:
            body = b"The ICEBERG atlas is read-only.\n"
            respond("405 Method Not Allowed", [
                ("Allow", ", ".join(self.allowed_methods)),
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ])
            return [body]
        if method == "PROPFIND" and environ.get("HTTP_DEPTH", "infinity").lower() == "infinity":
            # Clients mount using Depth: 0/1. Avoid recursively listing millions
            # of atlas files in one request (RFC 4918 finite-depth error).
            body = b'<D:error xmlns:D="DAV:"><D:propfind-finite-depth/></D:error>'
            respond("403 Forbidden", [
                ("Content-Type", "application/xml; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ])
            return [body]
        return self.app(environ, respond)


def create_app(*, atlas_dir=None, nist_atlas_dir=None, users_file=None, admin_emails=None):
    """Build the WSGI app; HTTPS is terminated by NGINX, backend is loopback-only."""
    atlas_dir = atlas_dir or os.environ.get("MSPRED_ATLAS_DIR")
    users_file = users_file or os.environ.get("ICEBERG_USERS_FILE")
    nist_atlas_dir = nist_atlas_dir or os.environ.get("MSPRED_ATLAS_DIR_NIST")
    if not atlas_dir or not users_file:
        raise RuntimeError("MSPRED_ATLAS_DIR and ICEBERG_USERS_FILE are required")
    if admin_emails is None:
        admin_emails = os.environ.get("ICEBERG_ADMIN_EMAILS", "").split(",")
    config = {
        "provider_mapping": {"/dav": AtlasDAVProvider(AtlasVirtualFS(atlas_dir, nist_atlas_dir))},
        "iceberg": {
            "users_file": Path(users_file),
            "admin_emails": {email.strip().lower() for email in admin_emails if email.strip()},
        },
        "http_authenticator": {
            "domain_controller": AtlasDomainController,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
            "trusted_auth_header": None,
        },
        "lock_storage": False,
        "property_manager": False,
        "dir_browser": {
            "enable": True,
            "response_trailer": False,
            "ms_sharepoint_support": False,
            "libre_office_support": False,
        },
        "middleware_stack": [
            AtlasDirBrowser if middleware is WsgiDavDirBrowser else middleware
            for middleware in DEFAULT_CONFIG["middleware_stack"]
        ],
        "block_size": 64 * 1024,
        "suppress_version_info": True,
        "verbose": 1,
        "logging": {"enable": False},
    }
    return DAVAnalytics(ReadOnlyDAV(WsgiDAVApp(config)), Path(users_file))
