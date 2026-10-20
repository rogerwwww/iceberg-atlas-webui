#!/usr/bin/env python3
"""Install the staged ICEBERG WebDAV service and HTTPS route, with rollback."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import difflib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
import time


SNIPPET = Path("/etc/nginx/snippets/iceberg-webdav.locations.conf")
UNIT = Path("/etc/systemd/system/iceberg-webdav.service")
SERVICE = "iceberg-webdav"
INCLUDE = f"include {SNIPPET};"


def ensure_session_token(users_file, user):
    """Create the private loopback credential without changing an existing one."""
    path = Path(users_file).parent / ".iceberg-webdav-session-token"
    account = pwd.getpwnam(user)
    if path.exists():
        metadata = path.stat()
        if metadata.st_uid != account.pw_uid or metadata.st_mode & 0o077 or len(path.read_text().strip()) < 32:
            raise RuntimeError("Existing WebDAV session credential must be private and owned by the service user")
        return path
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        os.fchown(file.fileno(), account.pw_uid, account.pw_gid)
        file.write(secrets.token_hex(32) + "\n")
    return path


def run(*args, check=True, env=None, cwd=None):
    result = subprocess.run(args, capture_output=True, text=True, env=env, cwd=cwd)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def add_https_include(text, hostname):
    """Locate exactly one matching TLS server, ignoring braces inside comments/quotes."""
    tokens = list(re.finditer(r'''\#[^\n]*|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|[{};]|[^\s{};\#]+''', text))
    tokens = [token for token in tokens if not token.group().startswith("#")]
    candidates = []
    for index, token in enumerate(tokens[:-1]):
        if token.group() != "server" or tokens[index + 1].group() != "{":
            continue
        depth = 0
        for end in range(index + 1, len(tokens)):
            value = tokens[end].group()
            depth += (value == "{") - (value == "}")
            if depth == 0:
                body = " ".join(t.group() for t in tokens[index + 2:end])
                names = re.findall(r"\bserver_name\s+([^;]+);", body)
                listens = re.findall(r"\blisten\s+([^;]+);", body)
                if any(hostname in name.split() for name in names) and any("ssl" in listen.split() for listen in listens):
                    candidates.append((tokens[end].start(), body))
                break
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one HTTPS server for {hostname}, found {len(candidates)}")
    position, body = candidates[0]
    if re.search(r"\binclude\s+" + re.escape(str(SNIPPET)) + r"\s*;", body):
        return text
    if re.search(r"\blocation\s+[^;{}]*\/dav(?:\/|\s|$)", body):
        raise RuntimeError("An existing /dav location needs manual review; refusing to replace it")
    return text[:position] + f"\n    {INCLUDE}\n" + text[position:]


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".iceberg-webdav-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            os.fchmod(file.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def health(hostname, frontend=False):
    """Check backend auth/write refusal; HTTPS route and existing homepage after reload."""
    checks = [("PROPFIND", "/dav/", 401), ("PUT", "/dav/public/no-write-test", 405)]
    if frontend:
        checks.append(("GET", "/", 200))
    for method, path, expected in checks:
        conn = (http.client.HTTPSConnection(hostname, timeout=10) if frontend
                else http.client.HTTPConnection("127.0.0.1", 4287, timeout=5))
        try:
            conn.request(method, path, headers={"Depth": "0", "Content-Length": "0"})
            response = conn.getresponse()
            response.read(1024)
            if response.status != expected:
                raise RuntimeError(f"{'HTTPS' if frontend else 'Backend'} {method} {path}: {response.status}, expected {expected}")
            if expected == 401 and not (response.getheader("WWW-Authenticate") or "").startswith("Basic "):
                raise RuntimeError("WebDAV authentication challenge missing")
        finally:
            conn.close()


def wait_for_health(hostname, frontend=False, *, timeout=20, interval=0.25):
    """Wait for new workers after startup or an asynchronous NGINX reload."""
    deadline = time.monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        try:
            health(hostname, frontend=frontend)
            if attempts > 1:
                print(f"{'HTTPS route' if frontend else 'Backend'} became ready after {attempts} checks.")
            return
        except (RuntimeError, OSError, http.client.HTTPException) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Health checks did not pass within {timeout}s; last failure: {exc}") from exc
            time.sleep(min(interval, remaining))


def restore(backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    run("systemctl", "stop", SERVICE, check=False)
    if not manifest["enabled"]:
        run("systemctl", "disable", SERVICE, check=False)
    for entry in manifest["files"]:
        target = Path(entry["path"])
        if entry["existed"]:
            original = backup / entry["name"]
            atomic_write(target, original.read_bytes(), entry["mode"])
            os.chown(target, entry["uid"], entry["gid"])
        elif target.exists():
            target.unlink()
    run("systemctl", "daemon-reload")
    if manifest["active"]:
        run("systemctl", "start", SERVICE)
    run("nginx", "-t")
    run("systemctl", "reload", "nginx")
    print(f"Restored the previous configuration from {backup}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--apply", action="store_true")
    actions.add_argument("--rollback", type=Path)
    parser.add_argument("--hostname", default="iceberg-ms.mit.edu")
    parser.add_argument("--site", type=Path, default=Path("/etc/nginx/sites-available/iceberg-webui"))
    parser.add_argument("--python", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo: reading and changing the active NGINX configuration requires root")
    if args.rollback:
        restore(args.rollback.resolve())
        return
    project = Path(__file__).resolve().parents[2]
    settings = dict(line.split("=", 1) for line in run(
        "systemctl", "show", "iceberg-sftp", "--property=User,Group,WorkingDirectory,Environment"
    ).stdout.splitlines() if "=" in line)
    file_env = dict(item.split("=", 1) for item in shlex.split(settings["Environment"]) if "=" in item)
    file_env = {key: value for key, value in file_env.items() if key in (
        "MSPRED_ATLAS_DIR", "MSPRED_ATLAS_DIR_NIST", "ICEBERG_USERS_FILE", "ICEBERG_ADMIN_EMAILS"
    )}
    if not all(file_env.get(key) for key in ("MSPRED_ATLAS_DIR", "ICEBERG_USERS_FILE")):
        raise RuntimeError("The SFTP service must configure atlas and user-file paths")
    user = settings["User"]
    group = settings.get("Group") or pwd.getpwnam(user).pw_gid
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", user):
        raise RuntimeError("Unexpected service user")
    python = args.python or Path(pwd.getpwnam(user).pw_dir) / ".local/share/iceberg-webdav-venv/bin/python"
    if not python.is_file():
        raise RuntimeError(f"Staged WebDAV Python is missing: {python}")
    env = os.environ.copy()
    env.update(file_env)
    run("runuser", "-u", user, "--", str(python), "-m", "gunicorn", "--check-config",
        "webui.webdav_server:create_app()", env=env, cwd=project)
    service = (project / "webui/deploy/systemd-iceberg-webdav.service").read_text()
    service = service.replace("User=USER", f"User={user}").replace("Group=USER", f"Group={group}")
    service = service.replace("WorkingDirectory=/path/to/iceberg-atlas-webui", f"WorkingDirectory={project}")
    service = re.sub(r"(?m)^#? ?Environment=.*\n", "", service)
    environment = "".join(f"Environment={json.dumps(key + '=' + value)}\n" for key, value in file_env.items())
    service = service.replace("ExecStart=", environment + "ExecStart=", 1)
    service = service.replace("/path/to/miniforge3/envs/iceberg-atlas-webui/bin/gunicorn", f"{python} -m gunicorn")
    # Gunicorn 25.1+ creates a control socket by default. This service needs
    # no control interface and its filesystem is intentionally read-only.
    # Detect the option so the template remains compatible with older Gunicorn.
    if "--no-control-socket" in run(str(python), "-m", "gunicorn", "--help").stdout:
        service = service.replace(" --bind ", " --no-control-socket --bind ", 1)
    if any(character in str(project) + str(python) + "".join(file_env.values()) for character in "\r\n\x00%"):
        raise RuntimeError("Unsupported characters in deployment paths or settings")
    if " " in str(project) + str(python):
        raise RuntimeError("Deployment helper requires paths without spaces")
    site = args.site.resolve()
    previous = site.read_text()
    updated = add_https_include(previous, args.hostname)
    snippet = (project / "webui/deploy/nginx-iceberg-webdav.locations.conf").read_bytes()
    print("".join(difflib.unified_diff(previous.splitlines(True), updated.splitlines(True),
                                     fromfile=str(site), tofile=str(site))), end="")
    print(f"Service: {user}:{group}, backend 127.0.0.1:4287; source settings inherited from iceberg-sftp")
    print(f"HTTPS snippet: {SNIPPET}")
    run("nginx", "-t")
    if args.check:
        print("Preflight passed; no production configuration changed.")
        return
    ensure_session_token(file_env["ICEBERG_USERS_FILE"], user)
    active = run("systemctl", "is-active", SERVICE, check=False).stdout.strip() == "active"
    enabled = run("systemctl", "is-enabled", SERVICE, check=False).stdout.strip() == "enabled"
    if not active:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 4287))
    backup = Path("/var/backups/iceberg-webdav") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(parents=True, mode=0o700)
    manifest = {"active": active, "enabled": enabled, "files": []}
    writes = [(site, updated.encode()), (SNIPPET, snippet), (UNIT, service.encode())]
    for index, (path, _) in enumerate(writes):
        entry = {"path": str(path), "name": str(index), "existed": path.exists()}
        if path.exists():
            st = path.stat()
            entry.update(mode=st.st_mode & 0o777, uid=st.st_uid, gid=st.st_gid)
            shutil.copy2(path, backup / str(index))
        manifest["files"].append(entry)
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Backup and rollback directory: {backup}")
    try:
        for (path, data), entry in zip(writes, manifest["files"]):
            atomic_write(path, data, entry.get("mode", 0o644))
            if entry["existed"]:
                os.chown(path, entry["uid"], entry["gid"])
        run("nginx", "-t")
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", SERVICE)
        run("systemctl", "restart", SERVICE)
        wait_for_health(args.hostname)
        run("systemctl", "reload", "nginx")
        # `nginx -s reload` returns after signalling the master. Old workers
        # may still answer new connections before the replacement workers start.
        wait_for_health(args.hostname, frontend=True)
        print(f"WebDAV is active at https://{args.hostname}/dav/")
        print(f"Rollback: sudo python3 {Path(__file__).resolve()} --rollback {backup}")
    except Exception:
        restore(backup)
        raise


if __name__ == "__main__":
    main()
