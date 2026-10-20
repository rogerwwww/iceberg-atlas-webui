# ICEBERG Atlas WebUI

Standalone Flask application for ICEBERG Atlas spectrum retrieval and visualization, account administration, analytics, and read-only SFTP/WebDAV access. The scientific `ms_pred` library remains an external dependency; model training and atlas generation stay in the ms-pred repository.

This application is built with Flask and should be served in production using **Gunicorn behind NGINX**.

---

## Deployment Overview

Production stack:

```
Internet
   ↓
NGINX (port 80/443)
   ↓
Gunicorn (127.0.0.1:4285)
   ↓
Flask app (webui.wsgi:app)
```

Optional read-only atlas file access:

```
SFTP client
   ↓
AsyncSSH SFTP service (port 2222)
   ↓
Atlas virtual filesystem
```

---

## Installation and development

Run commands from this repository root. No GitHub remote is assumed; this checkout
was extracted from `ms-pred-public`. Python 3.10+ is required; Python 3.12 is used
for CI. No GPU is needed to serve an existing atlas.

The app imports `ms_pred.common` and `ms_pred.magma.fragmentation`. Install the
scientific backend using the **CPU installation instructions in ms-pred's README**
first. Its PyTorch/DGL wheel sources are configured there. Then install this app
into that same Python environment. Do not install an unrelated PyPI package as a
substitute for the coleygroup/ms-pred backend.

For the existing WSL environment:

```bash
conda run -n ms-gen python -m pip install -e '.[test,studies]'
conda run -n ms-gen python -m pytest -q
conda run -n ms-gen bash webui/run_local_debug.sh
```

For a fresh backend checkout, a complete CPU setup using its uv configuration is:

```bash
# MSPRED_SOURCE points to a local checkout of coleygroup/ms-pred.
export MSPRED_SOURCE=/absolute/path/to/ms-pred
uv sync --project "$MSPRED_SOURCE" --extra cpu
uv pip install --python "$MSPRED_SOURCE/.venv/bin/python" -e '.[test,studies]'
"$MSPRED_SOURCE/.venv/bin/python" -m pytest -q
"$MSPRED_SOURCE/.venv/bin/python" -m gunicorn -w 2 -k gthread --threads 8 \
  -b 127.0.0.1:4285 webui.wsgi:app
```

`environment.yml` defines the **web service dependencies** for Conda deployments:

```bash
mamba env create -f environment.yml
mamba activate iceberg-atlas-webui
# Install ms_pred and its CPU dependencies into this environment following its README.
python -m pip install -e '.[test,studies]'
```

The YAML alone does not install the scientific backend. An existing environment
can also be cloned with `conda create -n iceberg-atlas-webui --clone ms-gen`, then
updated with `python -m pip install -e '.[test,studies]'`.

`webui/run_local_debug.sh` starts on `http://127.0.0.1:4285` with an empty atlas
and runtime files under ignored `var/`. Set `PYTHON` to select another interpreter.
To use real data, copy `.env.example` to `.env`, edit the absolute paths and secret,
and load it with `set -a; source .env; set +a` before starting the server. Atlas
files are deployment data and are not bundled. The built-in example spectrum is
packaged under `webui/examples/`.

Existing local users, analytics, MGF files, and GeoLite2 data were preserved under
`webui/` during extraction and are ignored by Git. Set the corresponding variables
to their new absolute paths if reusing them. Keep runtime data outside the source
checkout for production.

## Repository layout

- `webui/`: application, templates, assets, packaged example, and file services.
- `webui/deploy/`: systemd/NGINX templates and the WebDAV installer.
- `webui/tire_aging_unknowns/`: separate static campaign viewer and build tools.
- `tests/`: file permissions, browser access, WebDAV, and installer regression tests.
- `environment.yml`, `pyproject.toml`, `.env.example`: dependencies and configuration.

---

## Required Environment Variables

The application requires the following environment variables:

| Variable            | Description                           |
|--------------------|---------------------------------------|
| `FLASK_SECRET_KEY` | Secret key for session security       |
| `MSPRED_ATLAS_DIR` | Base directory of predicted MGF atlas |
| `MSPRED_JOB_DIR`   | Directory for temporary job storage   |
| `ICEBERG_USERS_FILE` | YAML file with WebUI email/password users |

The optional NIST'23 atlas is configured with:

| Variable                 | Description                                  |
|--------------------------|----------------------------------------------|
| `MSPRED_ATLAS_DIR_NIST`  | Base directory of the gated NIST'23 atlas    |
| `ICEBERG_ADMIN_EMAILS`   | Comma-separated admin safety-net email list  |

Email notifications are optional. If email is not configured, the admin
dashboard displays generated temporary passwords on-screen.

SMTP credentials must be stored only in the deployment environment or an
`EnvironmentFile` excluded from source control. The available settings are:

| Variable           | Description                                      |
|--------------------|--------------------------------------------------|
| `SMTP_HOST`        | SMTP relay hostname                              |
| `SMTP_PORT`        | SMTP relay port, defaults to `587`               |
| `SMTP_USER`        | SMTP login username                              |
| `SMTP_PASSWORD`    | SMTP login password                              |
| `SMTP_FROM`        | Sender address, defaults to `SMTP_USER`          |
| `SMTP_USE_TLS`     | Whether to use STARTTLS, defaults to `true`      |

Example:

```bash
export FLASK_SECRET_KEY="replace-with-long-random-string"
export MSPRED_ATLAS_DIR="/data/atlas"
export MSPRED_ATLAS_DIR_NIST="/data/atlas_nist"
export MSPRED_JOB_DIR="/var/lib/iceberg_jobs"
export ICEBERG_USERS_FILE="/var/lib/iceberg_users.yaml"
export ICEBERG_ADMIN_EMAILS="admin@example.com"
export SMTP_HOST="smtp.example.com"
export SMTP_PORT="587"
export SMTP_USER="iceberg@example.com"
export SMTP_PASSWORD="replace-with-an-app-password"
export SMTP_FROM="iceberg@example.com"
export SMTP_USE_TLS="true"
```

Ensure the job directory exists and has proper permissions:

```bash
sudo mkdir -p /var/lib/iceberg_jobs
sudo chown -R coley-group:coley-group /var/lib/iceberg_jobs
```

---

## Test Gunicorn Manually

From the repository root after loading the environment:

```bash
mamba activate iceberg-atlas-webui
gunicorn -w 2 -k gthread --threads 8 -b 127.0.0.1:4285 webui.wsgi:app
```

Then open:

```
http://127.0.0.1:4285
```

If the application loads correctly, proceed to the systemd setup.

---

## Install NGINX

On Ubuntu:

```bash
sudo apt update
sudo apt install nginx -y
sudo systemctl enable nginx
sudo systemctl start nginx
```

---

## Install systemd Service

Copy the service file:

```bash
sudo cp webui/deploy/systemd-iceberg-webui.service \
  /etc/systemd/system/iceberg-webui.service
```
Please remember to replace the ``FLASK_SECRET_KEY`` and update other parameters accordingly.

Reload and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable iceberg-webui
sudo systemctl start iceberg-webui
sudo systemctl status iceberg-webui
```

---

## Install Read-Only SFTP Service

The SFTP service uses the same `ICEBERG_USERS_FILE` credentials as the WebUI.
Usernames are full email addresses. Users with role `user` see only `/public`;
roles `authorized_user` and `admin` also see `/nist23` when
`MSPRED_ATLAS_DIR_NIST` is configured.

Install or update the environment so `asyncssh` is available:

```bash
python -m pip install -e .
```

Generate a dedicated host key outside the repository:

```bash
ssh-keygen -t ed25519 -f /path/to/iceberg_sftp_ed25519 -N ""
```

Copy the service file:

```bash
sudo cp webui/deploy/systemd-iceberg-sftp.service \
  /etc/systemd/system/iceberg-sftp.service
```

Edit the placeholders in `/etc/systemd/system/iceberg-sftp.service`, then reload
and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now iceberg-sftp
sudo systemctl status iceberg-sftp
```

Open TCP port `2222` in any host firewall or security group if it is blocked.

Client examples:

```bash
sftp -P 2222 -o User=user@domain.edu iceberg-ms.mit.edu
sshfs -p 2222 -o User=user@domain.edu iceberg-ms.mit.edu:/ ~/iceberg-atlas
```

The exposed virtual paths are:

| Virtual path       | Source directory                              |
|--------------------|-----------------------------------------------|
| `/public/h_plus/`  | `MSPRED_ATLAS_DIR/h_plus_out_mgf/`            |
| `/public/h_minus/` | `MSPRED_ATLAS_DIR/h_minus_out_mgf/`           |
| `/nist23/h_plus/`  | `MSPRED_ATLAS_DIR_NIST/h_plus_out_mgf/`       |
| `/nist23/h_minus/` | `MSPRED_ATLAS_DIR_NIST/h_minus_out_mgf/`      |

---

## Read-Only WebDAV over HTTPS

WebDAV mounts expose the same `/public/h_plus`, `/public/h_minus`,
`/nist23/h_plus`, and `/nist23/h_minus` virtual paths as SFTP. The endpoint is
`https://iceberg-ms.mit.edu/dav/`. Authenticate with the existing WebUI email and
password. Public atlas access requires a registered account; NIST access requires
`authorized_user` or `admin` (including configured admin-email overrides).
Changes to passwords, accounts, or roles apply on the next request.
Browser users already signed into the WebUI open `/dav/` without a second
password prompt. A private localhost endpoint validates the WebUI session or
remember cookie without sharing Flask's signing key. The installer creates a
mode-600 `.iceberg-webdav-session-token` in the users-file directory to
authenticate this internal request. Mounted drives use the existing email and
password. Deploy both applications and reload their workers after updates.

WebDAV activity appears in the Admin portal's User actions, daily counts, and
visitor map. The private analytics endpoint writes `source=webdav`, authenticated
email (when available), client IP, method, and the escaped virtual path to the
WebUI's `ICEBERG_ANALYTICS_DB`. Actions distinguish browsing, download requests,
metadata checks, denied access, and failed password authentication. Download
requests include range requests and do not establish a completed transfer.
Browser assets, OPTIONS handshakes, anonymous authentication challenges, and
internal RPC calls are excluded. Credentials and cookies are not recorded.
Analytics outages leave file access working and emit a service warning; failed
events are not replayed. Refresh the portal after the next aggregation cycle.

The separate WsgiDAV/Gunicorn backend listens only on `127.0.0.1:4287`; NGINX
terminates HTTPS on the existing port 443. HTTP Basic authentication verifies the
existing Werkzeug password hashes, so no duplicate password database is needed.
Only `OPTIONS`, `PROPFIND`, `GET`, and `HEAD` are accepted. Locks and property
writes are disabled even for administrators. Clients must use `Depth: 0` or `1`
for directory discovery; recursive `Depth: infinity` listings are rejected to
avoid traversing the entire atlas. File reads support `Range`, `HEAD`, and ETags.

---

## Configure NGINX

Copy the NGINX configuration:

```bash
sudo cp webui/deploy/nginx-iceberg-webui.conf \
  /etc/nginx/sites-available/iceberg-webui
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/iceberg-webui \
  /etc/nginx/sites-enabled/
```

Test and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## Enable HTTPS (Recommended)

Install Certbot:

```bash
sudo apt install certbot python3-certbot-nginx -y
```

Request a certificate:

```bash
sudo certbot --nginx -d your.domain.com
```

---

## Logs and Debugging

### systemd logs

```bash
journalctl -u iceberg-webui -f
```

### NGINX logs

```bash
sudo tail -f /var/log/nginx/error.log
sudo tail -f /var/log/nginx/access.log
```

## Installing WebDAV

After configuring and starting the WebUI and SFTP units, run the helper from the
repository root. It reads atlas/user settings from the installed SFTP unit.

```bash
sudo /path/to/environment/bin/python webui/deploy/install_webdav.py \
  --check --python /path/to/environment/bin/python
sudo /path/to/environment/bin/python webui/deploy/install_webdav.py \
  --apply --python /path/to/environment/bin/python
```

`--check` validates configuration without installing it. `--apply` records a
backup under `/var/backups/iceberg-webdav/`; use `--rollback BACKUP_DIRECTORY` to
restore it. Keep the same accounts file and role settings across all services.

## Migrating an existing deployment

This extraction changes local source files only. To migrate a server, install
this checkout and its dependencies, update all three systemd `WorkingDirectory`
and `ExecStart` paths, and preserve the existing environment files, systemd
overrides, signing secret, users YAML, analytics database, SFTP host key, and
WebDAV session token. Copying a service template does not copy active overrides.
Use absolute atlas/runtime paths; the atlas and research results stay in place.
After an intentional service restart, verify the homepage, login, file browsing,
role-gated NIST access, and any enabled SFTP/WebDAV endpoints.
