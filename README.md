# 0x0 Dockerized

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![License: EUPL 1.2](https://img.shields.io/badge/License-EUPL%201.2-blue.svg)](https://joinup.ec.europa.eu/software/page/eupl)

A self-hosted file host based on [0x0 / The Null Pointer](https://0x0.st). It runs under Docker Compose with PostgreSQL and automatic retention cleanup.

---

## Features

- Upload files with `multipart/form-data` from `curl`, a browser, or a script.
- Canonical public URLs are 64-character hexadecimal HMAC-SHA256 digests of a server secret, file hash, extension, and original filename. Without the secret, a guessed URL does not tell you whether a file exists. The same bytes, extension, and filename always produce the same link.
- Legacy 12-character random tokens return `301 Moved Permanently` to the canonical 64-hex URL.
- Browser previews:
  - Markdown (`.md`): server-rendered Markdown with Bleach HTML sanitization, tables, and client-side Mermaid diagrams.
  - CSV (`.csv`): virtualized table grid with on-disk block seeking (`.csv-index`), row and column jump boxes, adaptive column widths, and a paginated JSON tile/cell API (`GET /csv/`).
  - Video (`video/*`): HTML5 `<video>` player with HTTP byte-range seeking (`206 Partial Content`).
  - Raster images (`image/*` except SVG): `<img>` preview. Bytes are served inline from `/raw/`.
  - PDF (`application/pdf`): served inline with `Content-Disposition: inline`, so the browser's built-in PDF viewer handles display and download.
  - Plain text (`text/*`): `<pre>` preview.
- Downloads through `/download/` keep the original filename. HTML, SVG, XML, and JavaScript are download-only attachments.
- Identical uploads (same bytes, extension, and original filename) reuse the canonical link and `touch` storage to refresh retention. Distinct filenames for identical bytes get distinct links that point at the same `data/up/<sha256>` payload.
- Retention scales from 30 days at maximum file size to 365 days for small files. Cleanup deletes expired disk files and unmoderated database rows, sets `addr = NULL` on moderated records, and writes events to a `file_audit` table that does not store IP addresses.
- Security:
  - `FHOST_LINK_SECRET` is required outside test mode.
  - Re-upload of moderated content returns `451 Unavailable For Legal Reasons`.
  - Detected Windows executables and Java class/archive files, plus `.exe`/`.jar`/`.class` filenames, return `415 Unsupported Media Type`. A JAR detected as ZIP can be accepted if renamed; archive entries are not inspected.
  - HTML (including MHTML), SVG, XML, and JavaScript (including `.mjs`/`.cjs`) download as opaque attachments (`application/octet-stream`); they are not rendered as live pages. CSV cells and embedded HTML in Markdown are shown only through their safe previews.
  - Remote URL import and generic URL shortening are off.
  - Responses include `X-Content-Type-Options: nosniff`.
  - The app runs as user `app` (UID/GID 1000).

---

## Quick start (< 5 minutes)

### 1. Clone and configure
```bash
git clone https://github.com/hattapauzi/0x0-dockerized.git
cd 0x0-dockerized

cp .env.example .env
```

Edit `.env` and set a database password and HMAC secret:
```ini
POSTGRES_DB=fhost
POSTGRES_USER=fhost
POSTGRES_PASSWORD=your_secure_password_here
DATABASE_URI=postgresql://fhost:your_secure_password_here@db/fhost
MAX_SIZE_MB=512
MAX_SIZE_NGINX=512M
# Generate a 32-byte (64-character hex) random secret:
# openssl rand -hex 32
FHOST_LINK_SECRET=your_secure_random_64_hex_secret_here
```

### 2. Start the stack
```bash
docker compose up -d --build
```

The application listens on host port `8081` (`8081:8080`). Database migrations run on startup.

### 3. Smoke test
```bash
curl -F'file=@0x0/README.rst' http://localhost:8081/
```

Output:
```text
https://localhost/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.rst
```

---

## Usage

### Uploading files
```bash
# Upload a file
curl -F'file=@image.png' https://your-domain.example/

# Upload with a specific filename
curl -F'file=@document.md;filename=architecture-spec.md' https://your-domain.example/
```

### Retrieving and downloading files
- View / preview: `GET /<link><ext>` (for example `https://your-domain.example/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.md`)
  - `.md` files render a sanitized Markdown preview with Mermaid diagrams and a Download link. HTML documents named `.md` stay download-only.
  - `.csv` files render a virtualized CSV table preview with jump boxes and a Download link, even when libmagic detects HTML in a cell.
  - `video/*` files render an HTML5 video preview.
  - Raster images render an `<img>` preview (SVG stays download-only).
  - PDFs open directly in the browser's built-in PDF viewer (inline bytes, viewer supplies its own download button).
  - `text/*` files render a plain text preview.
  - Other binaries download as attachments.
- Inline media: `GET /raw/<link><ext>` returns image bytes with `Content-Disposition: inline` for the `<img>` preview tag. PDFs are served inline at their own URL instead. HTML/SVG/XML/JavaScript are not served here.
  - Legacy URLs (`GET /<token><ext>`) return `301 Moved Permanently` to the canonical link.
- Forced download: `GET /download/<link><ext>` (for example `https://your-domain.example/download/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.md`)
  - Skips preview pages and sends the file with the original filename in `Content-Disposition`.
- CSV data tiles: `GET /csv/<link><ext>?rb=0&cb=0` (or `?r=0&c=0` for one cell)
  - Returns paginated 200×50 JSON cell matrices for the CSV table preview.

See [API documentation](docs/api.md).

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `FHOST_LINK_SECRET` | None (required) | 32-byte hex secret (`openssl rand -hex 32`) used for HMAC-SHA256 link derivation. Startup fails if this is unset. |
| `DATABASE_URI` | `postgresql://fhost:...@db/fhost` | SQLAlchemy connection string (`postgresql://` falls back to `psycopg2`). |
| `POSTGRES_DB` | `fhost` | PostgreSQL database name |
| `POSTGRES_USER` | `fhost` | PostgreSQL username |
| `POSTGRES_PASSWORD` | `change-me` | PostgreSQL user password |
| `MAX_SIZE_MB` | `512` | Max upload size in megabytes |
| `PREFERRED_URL_SCHEME`| `https` | URL scheme used for generated public links |
| `FHOST_TOKEN_LENGTH` | `12` | Length of generated legacy random tokens |

---

## Maintenance and retention cleanup

Run the cleanup job by hand or from cron to delete files past the retention window and prune database rows:

```bash
docker compose --profile maintenance run --rm cleanup
```

The maintenance worker:

1. Disk retention (`cleanup_expired`). Deletes expired payloads from disk and leftover `.csv-index` files.
2. Database minimization (`prune_rows`). Deletes rows for unmoderated expired files, sets `addr = NULL` on moderated records, and writes events without IP addresses to `file_audit`.

See the [operations runbook](docs/runbook.md) for cron / systemd timers, backups, audit queries, secret rotation, and Nginx / Caddy templates.

---

## Local development and testing

A local virtualenv can run tests without Docker:

```bash
cd 0x0
~/.venvs/0x0/bin/python -m pytest tests/ -q
```

See the [development guide](docs/development.md) and [contributing guide](CONTRIBUTING.md).

---

## Documentation

- [API](docs/api.md)
- [Architecture](docs/architecture.md)
- [Operations runbook](docs/runbook.md)
- [Development](docs/development.md)

---

## License

Licensed under the EUPL, Version 1.2 (or later approved versions). See [LICENSE](0x0/LICENSE).
