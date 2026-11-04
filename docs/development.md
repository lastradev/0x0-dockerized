# 0x0-dockerized development

Use this guide for local setup, the test suite, migrations, and pull requests.

---

## 1. Prerequisites

- Python 3.10+ (Python 3.14 tested)
- `libmagic` (`libmagic1` and `libmagic-dev` on Debian/Ubuntu, or the `file` package on Fedora/Alpine)
- SQLite3 (for the local test runner)
- Docker and Docker Compose (for full-stack tests)

---

## 2. Local environment

### Test virtualenv

Put a persistent virtualenv at `~/.venvs/0x0`:

```bash
# Create the virtual environment
python3 -m venv ~/.venvs/0x0

# Prepare dependency list excluding deploy-only packages (uwsgi, psycopg2)
cat 0x0/requirements.txt <(grep -v -e '^-r' -e '^uwsgi' -e '^psycopg2' 0x0/requirements-dev.txt) \
  | grep -v -e '^uwsgi' -e '^psycopg2' > /tmp/reqs-0x0.txt

# Install dependencies
~/.venvs/0x0/bin/pip install -r /tmp/reqs-0x0.txt

# Install Chromium for CSV preview browser regression tests
~/.venvs/0x0/bin/python -m playwright install chromium
```

---

## 3. Running tests

Tests use an in-memory or temporary SQLite database set in `0x0/tests/test_client.py`. The fixture sets `FHOST_LINK_SECRET = "test-secret"`. Local unit tests do not need PostgreSQL.

### Run all tests
Always run pytest from `0x0/`:

```bash
cd 0x0
~/.venvs/0x0/bin/python -m pytest tests/ -q
```

All tests should pass. They cover uploads, HMAC derivation, CSV indexing and virtualization (including HTML-like cells), sanitized Markdown and disguised HTML, video, image/PDF preview, download-only HTML/SVG/XML/JS and MHTML, filename header escaping, and retention pruning. Download-only headers and filename encoding are checked with and without X-Accel-Redirect; these tests do not exercise a live Nginx deployment.

### Run specific tests
```bash
# HMAC link derivation and deduplication
~/.venvs/0x0/bin/python -m pytest tests/test_client.py -k "hmac or legacy" -v

# CSV table preview and tile API
~/.venvs/0x0/bin/python -m pytest tests/test_client.py -k "csv" -v

# Video player
~/.venvs/0x0/bin/python -m pytest tests/test_client.py -k "video" -v

# Retention and audit cleanup
~/.venvs/0x0/bin/python -m pytest tests/test_client.py -k "prune or cleanup" -v
```

---

## 4. Project layout

```text
0x0-dockerized/
├── 0x0/                          # Flask application package
│   ├── instance/
│   │   └── config.py             # Instance configuration overrides and driver fallback
│   ├── migrations/               # Alembic database migration scripts
│   │   └── versions/             # Migration revision files (0d5b8c4b9f0d, a1b2c3d4e5f6, b2c3d4e5f6a7)
│   ├── templates/                # Server-rendered Jinja2 HTML templates
│   │   ├── index.html            # Landing / retention ASCII info page
│   │   ├── preview.html          # Plain text preview template
│   │   ├── markdown_preview.html # Markdown + Mermaid.js preview template
│   │   ├── video_preview.html    # Native HTML5 video player template
│   │   ├── image_preview.html    # Raster image preview template
│   │   ├── csv_preview.html      # Virtualized CSV table preview template
│   │   └── 404.html, etc.        # HTTP error pages (includes 415.html)
│   ├── tests/                    # Pytest test suite
│   │   └── test_client.py        # 75 unit and integration tests
│   ├── cleanup.py                # Retention cleanup (storage) and prune_rows (DB minimization)
│   ├── Dockerfile                # App image build recipe
│   ├── fhost.py                  # Flask application entrypoint and routing
│   ├── requirements.txt          # Production runtime dependencies
│   └── requirements-dev.txt      # Development and testing dependencies (pytest, playwright)
├── data/                         # Persistent runtime data (untracked)
│   ├── db/                       # PostgreSQL data files
│   └── up/                       # Uploaded file payloads (keyed by SHA-256)
│       └── .csv-index/           # Binary block offset indexes for CSV seeking
├── docs/                         # Technical documentation
│   ├── api.md                    # HTTP API
│   ├── architecture.md           # Architecture and Mermaid diagrams
│   ├── runbook.md                # Deployment and operations
│   └── development.md            # This guide
├── docker-compose.yml            # Docker Compose service definition
├── .env.example                  # Environment template
└── README.md                     # Repository overview and quick start
```

---

## 5. Database migration workflow

Flask-Migrate (Alembic) owns schema changes.

### Creating a new migration
When you change models (`File`, `FileAudit`, or `URL`) in `0x0/fhost.py`:

```bash
cd 0x0
export FLASK_APP=fhost
export DATABASE_URI="sqlite:///test.db"
export FHOST_LINK_SECRET="test-secret"

~/.venvs/0x0/bin/flask db migrate -m "describe your changes"
```

Inspect the generated script under `0x0/migrations/versions/`. Check that:

- Both `upgrade()` and `downgrade()` work on SQLite and PostgreSQL (`op.batch_alter_table`).
- Backfills read `current_app.config.get("FHOST_LINK_SECRET")`.
- Partial indexes use `postgresql_where` and `sqlite_where`.

### Applying migrations locally
```bash
~/.venvs/0x0/bin/flask db upgrade
```

---

## 6. Code guidelines

1. Keep changes narrow. Leave `0x0/fhost.py` as one file unless a split is required.
2. Detect MIME type from bytes with `python-magic`, not from client headers alone.
3. Link derivation must go through `make_link(...)` and `FHOST_LINK_SECRET`. Outside testing, a missing secret must raise.
4. Do not write user IP addresses (`addr`), query strings, or personal data to `file_audit` or other lasting audit trails.
5. Every new endpoint or preview builder must set `X-Content-Type-Options: nosniff`.
6. User content rendered as HTML must go through `bleach` or be escaped.
7. Do not commit files in `data/` or any `.env` secrets.
8. Follow PEP 8 (4-space indent, snake_case functions and variables, UPPER_CASE constants). Match surrounding style.

---

## 7. Submitting changes

Before opening a pull request:

1. Run the test suite: `cd 0x0 && ~/.venvs/0x0/bin/python -m pytest tests/ -q` (all 75 tests must pass).
2. Exercise Docker Compose: `docker compose up -d --build` and check upload/download with `curl`.
3. If you added features or routes, add tests in `0x0/tests/test_client.py`.
4. Keep working documentation, specs, and plans on a `docs/...` branch. Do not merge feature docs branches into `master`. Merge only the implementation branch (`feat/...` or `fix/...`), leave the docs branch, and name it in the merge commit message.
