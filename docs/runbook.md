# 0x0-dockerized operations runbook

Deploy, maintain, back up, and debug `0x0-dockerized`.

---

## 1. Prerequisites and environment

### System requirements
- Linux host (Ubuntu 22.04+, Debian 12+, RHEL/Rocky 9+, or similar)
- Docker Engine 24.0+ with Docker Compose v2.20+
- Host reverse proxy (Nginx, Caddy, Traefik, or Cloudflare Tunnel) for TLS
- Disk space under `./data/` for the database and uploaded files

### Access requirements
- SSH with `sudo`, or membership in the `docker` group on the host
- Access to DNS records for the public domain

---

## 2. Initial production deployment

### Step 1: Clone the repository
```bash
git clone https://github.com/hattapauzi/0x0-dockerized.git /opt/0x0-dockerized
cd /opt/0x0-dockerized
```

### Step 2: Configure environment variables
Copy `.env.example` to `.env` and set a password:

```bash
cp .env.example .env
```

Edit `.env`:
```ini
POSTGRES_DB=fhost
POSTGRES_USER=fhost
POSTGRES_PASSWORD=generate_a_strong_random_password_here
DATABASE_URI=postgresql://fhost:generate_a_strong_random_password_here@db/fhost
MAX_SIZE_MB=512
MAX_SIZE_NGINX=512M
# Generate a 32-byte (64-character hex) random secret:
# openssl rand -hex 32
FHOST_LINK_SECRET=generate_a_64_hex_secret_with_openssl
```

> [!IMPORTANT]
> - `FHOST_LINK_SECRET` is required outside test mode. The application raises `RuntimeError` on startup if it is unset or empty.
> - Do not commit `.env` or files inside `data/`.

### Step 3: Build and start containers
```bash
docker compose up -d --build
```

Check that services are healthy:
```bash
docker compose ps
```
Both `0x0st` and `postgres` should report `Up` and `healthy`.

---

## 3. Reverse proxy configuration

The application container listens on host port `8081` (`8081:8080`). Put a reverse proxy on port 80/443 with HTTPS.

### Option A: Nginx reverse proxy
Create `/etc/nginx/sites-available/0x0.conf`:

```nginx
server {
    listen 80;
    server_name files.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name files.example.com;

    ssl_certificate /etc/letsencrypt/live/files.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/files.example.com/privkey.pem;

    # Match or exceed MAX_SIZE_MB from .env
    client_max_body_size 512M;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Support streaming and range requests
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
    }
}
```

Enable and reload Nginx:
```bash
sudo ln -s /etc/nginx/sites-available/0x0.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### Option B: Caddy reverse proxy
Add to `/etc/caddy/Caddyfile`:

```caddy
files.example.com {
    request_body {
        max_size 512MB
    }
    reverse_proxy 127.0.0.1:8081 {
        header_up Host {host}
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

Reload Caddy:
```bash
sudo systemctl reload caddy
```

---

## 4. Verification and smoke testing

### Upload
```bash
# Upload a test plain text file
curl -F'file=@0x0/README.rst' https://files.example.com/
```
The output is a 64-character hexadecimal HMAC-SHA256 URL, for example:  
`https://files.example.com/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.rst`

### Retrieval
```bash
# 1. Preview retrieval
curl -i https://files.example.com/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.rst

# 2. Forced download with original filename
curl -i https://files.example.com/download/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.rst
```

### CSV preview
```bash
# 1. Upload a CSV file
curl -F'file=@test.csv' https://files.example.com/

# 2. CSV table preview HTML page
curl -i https://files.example.com/<link>.csv

# 3. CSV tile JSON API
curl -i "https://files.example.com/csv/<link>.csv?rb=0&cb=0"
```

---

## 5. Maintenance and retention cleanup

Old files expire on the retention curve. Run cleanup on a schedule.

### Manual cleanup
```bash
docker compose --profile maintenance run --rm cleanup
```

### What cleanup does
`cleanup.py` runs two operations:

1. Disk retention (`cleanup_expired`). Computes retention from file size using the cubic curve, deletes expired payloads from `data/up/`, and removes leftover `.csv-index` files whose SHA-256 targets have expired.
2. Database minimization (`prune_rows`).
   - Calls `is_file()` on disk storage before changing rows, so a concurrent upload is not raced (TOCTOU).
   - Deletes database rows for unmoderated expired files (`removed=False`) so `addr` and original filenames are not kept after expiry. Writes `PRUNED_EXPIRED` to `file_audit`.
   - Sets `addr = NULL` on expired moderated files (`removed=True`). Keeps `sha256`, `ext`, `original_name`, and `link` so a re-upload still gets HTTP 451. Writes `MODERATED_ADDR_SCRUBBED` to `file_audit`.

### Inspecting `file_audit`
The append-only `file_audit` table records cleanup and moderation. It does not store user IP addresses or payload bytes:

```bash
docker compose exec db psql -U fhost -d fhost -c "SELECT id, sha256, link, action, created_at FROM file_audit ORDER BY id DESC LIMIT 25;"
```

Action types:

- `MODERATED`: file marked as removed by an administrator.
- `PRUNED_EXPIRED`: expired active database row deleted during retention cleanup.
- `MODERATED_ADDR_SCRUBBED`: uploader IP set to NULL on an expired moderated row.

### Automated cleanup via systemd or cron

#### Option A: Host cron job
Add to `/etc/cron.d/0x0-cleanup` (daily at 03:00):
```cron
0 3 * * * root cd /opt/0x0-dockerized && /usr/bin/docker compose --profile maintenance run --rm cleanup > /var/log/0x0-cleanup.log 2>&1
```

#### Option B: Systemd timer
Create `/etc/systemd/system/0x0-cleanup.service`:
```ini
[Unit]
Description=0x0 File Retention Cleanup
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/0x0-dockerized
ExecStart=/usr/bin/docker compose --profile maintenance run --rm cleanup
```

Create `/etc/systemd/system/0x0-cleanup.timer`:
```ini
[Unit]
Description=Run 0x0 File Retention Cleanup Daily

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable the timer:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now 0x0-cleanup.timer
```

---

## 6. Backup and disaster recovery

### What to back up
1. `.env` (database credentials and configuration).
2. `./data/up/` (uploaded file payloads).
3. PostgreSQL dump of the `fhost` database.

### Backup procedure
Create `backup-0x0.sh`:

```bash
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/var/backups/0x0/$(date +%Y-%m-%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

cd /opt/0x0-dockerized

# 1. Backup .env
cp .env "$BACKUP_DIR/.env"

# 2. Dump PostgreSQL database
docker compose exec -T db pg_dump -U fhost fhost > "$BACKUP_DIR/fhost.sql"

# 3. Sync upload storage
rsync -av ./data/up/ "$BACKUP_DIR/up/"

# 4. Create compressed archive
tar -czf "$BACKUP_DIR.tar.gz" -C "/var/backups/0x0" "$(basename "$BACKUP_DIR")"
rm -rf "$BACKUP_DIR"

echo "Backup completed: $BACKUP_DIR.tar.gz"
```

### Restore procedure
```bash
# 1. Stop app container
docker compose stop app

# 2. Extract backup data
tar -xzf /var/backups/0x0/2026-08-27_000000.tar.gz -C /tmp/

# 3. Restore files
rsync -av /tmp/2026-08-27_000000/up/ /opt/0x0-dockerized/data/up/
chown -R 1000:1000 /opt/0x0-dockerized/data/up/

# 4. Restore database
cat /tmp/2026-08-27_000000/fhost.sql | docker compose exec -T db psql -U fhost -d fhost

# 5. Start app container
docker compose start app
```

---

## 7. Updates

To move to a newer release:
```bash
cd /opt/0x0-dockerized
git pull origin master

# Rebuild images and restart containers
docker compose up -d --build

# Verify migrations ran
docker compose logs app | grep "Running upgrade"
```

---

## 8. Troubleshooting

### Issue 1: `413 Payload Too Large` during upload
**Cause:** Upload size exceeds Flask's `MAX_SIZE_MB` or the reverse proxy `client_max_body_size`.  
**Resolution:**
1. Check `.env`: set `MAX_SIZE_MB` as needed.
2. Check Nginx/Caddy config: `client_max_body_size` and `proxy_request_buffering off`.
3. Restart the stack: `docker compose up -d`.

### Issue 2: `502 Bad Gateway` on reverse proxy
**Cause:** The `0x0st` container is not running or has crashed.  
**Resolution:**
1. Inspect container logs: `docker compose logs app --tail=100`.
2. Check the database: `docker compose exec db pg_isready -U fhost`.
3. Restart the container: `docker compose restart app`.

### Issue 3: Permission denied on `data/up/`
**Cause:** Storage directory owned by the wrong UID.  
**Resolution:**
```bash
sudo chown -R 1000:1000 ./data/up
docker compose restart app
```

### Issue 4: Database migration failure
**Cause:** Schema state mismatch or a locked table.  
**Resolution:**
```bash
# Inspect current revision
docker compose exec app flask db current

# Manually trigger upgrade
docker compose exec app flask db upgrade
```

### Issue 5: Container fails on startup with `FHOST_LINK_SECRET is required outside testing mode`
**Cause:** `FHOST_LINK_SECRET` is missing, blank, or not passed into the container.  
**Resolution:**
1. Check `.env`: set `FHOST_LINK_SECRET` to a 64-hex string (`openssl rand -hex 32`).
2. Confirm `docker-compose.yml` passes `FHOST_LINK_SECRET: "${FHOST_LINK_SECRET}"` to both `app` and `cleanup`.
3. Restart: `docker compose up -d`.

### Issue 6: PostgreSQL driver URL compatibility
**Cause:** SQLAlchemy 2.x treats `postgresql://` as `psycopg` (v3) by default. Production uses `psycopg2`.  
**Resolution:**
`0x0/instance/config.py` detects a missing `psycopg` package and rewrites `postgresql://` URIs to `postgresql+psycopg2://`. Outside Docker, install `psycopg2` or `psycopg2-binary`.

---

## 9. Rollback

If a deployment or upgrade fails:

```bash
# 1. Rollback code to previous git commit/tag
git checkout <previous-stable-tag-or-commit>

# 2. If database migration was applied, downgrade revision
# NOTE: If new uploads occurred since upgrading to revision b2c3d4e5f6a7 (which have token IS NULL),
# the downgrade script will refuse to run so those rows are not dropped by accident.
# To proceed, restore the pre-upgrade PostgreSQL backup or delete new rows:
# docker compose exec db psql -U fhost -d fhost -c "DELETE FROM file WHERE token IS NULL;"
docker compose exec app flask db downgrade

# 3. Rebuild and restart
docker compose up -d --build
```

---

## 10. Secret key incident response and rotation

`FHOST_LINK_SECRET` is the HMAC key:

`link = HMAC-SHA256(secret, sha256\0ext\0original_name)`

### Routine secret rotation
Canonical links are stored in `file.link`.

- Changing `FHOST_LINK_SECRET` in `.env` makes later uploads use the new secret.
- Existing links keep working. Lookups read `file.link`, so URLs created under the previous secret still resolve.

### Compromise or leak
If an attacker has `FHOST_LINK_SECRET`:

1. They can check offline whether a known file hash would produce a hosted URL.
2. Response:
   1. Generate a new secret with `openssl rand -hex 32` and set `FHOST_LINK_SECRET` in `.env`.
   2. Restart: `docker compose up -d`.
   3. Recompute canonical links for existing rows inside the container:
      ```bash
      docker compose exec app python -c '
      import hmac, hashlib
      from fhost import app, db, File

      secret = app.config["FHOST_LINK_SECRET"].encode("utf-8")
      with app.app_context():
          for f in File.query.all():
              msg = f"{f.sha256}\0{f.ext or \"\"}\0{f.original_name or \"\"}".encode("utf-8")
              f.link = hmac.new(secret, msg, hashlib.sha256).hexdigest()
          db.session.commit()
      print("Re-derived canonical links.")
      '
      ```
   4. Re-deriving links changes the canonical URL of every hosted file. Previously shared URLs stop resolving until they are shared again.
