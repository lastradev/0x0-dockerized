The Null Pointer (0x0 application package)
==========================================

This directory holds the Flask application for 0x0-dockerized.

For Docker deployment, architecture, and operations, see the repository root ``README.md`` and the ``docs/`` directory.

Features
--------

* Runs as an HTTP file host under uWSGI / Flask.
* Canonical public URLs are 64-character hexadecimal HMAC-SHA256 digests of a server secret, file hash, extension, and original filename. Legacy 12-character tokens return HTTP 301 to the canonical link.
* Previews:

  - Markdown (``.md``): Bleach sanitization and Mermaid diagrams.
  - CSV (``.csv``): virtualized table, on-disk block seeking (``.csv-index``), row and column jump boxes, and a paginated JSON API at ``GET /csv/``.
  - Video (``video/*``): HTML5 player and HTTP byte-range seeking (``206 Partial Content``).
  - Plain text (``text/*``).

* ``/download/<link><ext>`` keeps the original filename.
* Payloads are stored by SHA-256 digest in ``up/``. The same file and filename reuse the canonical link and ``touch`` storage to refresh retention.
* Expired database rows are deleted. Moderated rows have ``addr`` set to NULL. Events go to ``file_audit`` without IP addresses.

Configuration
-------------

Change settings in ``instance/config.py`` or with environment variables:

* ``FHOST_LINK_SECRET``: 32-byte hex secret (for example ``openssl rand -hex 32``). Required outside testing.
* ``DATABASE_URI``: SQLAlchemy database URI (``postgresql://`` falls back to ``psycopg2``).
* ``MAX_SIZE``: Maximum upload size in megabytes.

To change the home and error pages, create a ``templates`` directory in your instance directory and copy the templates you want to edit.

Retention and cleanup
---------------------

To expire files on the retention curve and prune database rows, run ``python cleanup.py`` on a schedule (or use the Docker Compose ``cleanup`` profile):

1. Disk cleanup (``cleanup_expired``): deletes expired storage payloads and leftover ``.csv-index`` files.
2. Database minimization (``prune_rows``): deletes expired unmoderated rows and sets uploader IP addresses to NULL on moderated records. Events without IP addresses go to ``file_audit``.

Development and testing
-----------------------

Local virtualenv:

::

    python3 -m venv ~/.venvs/0x0
    cat requirements.txt <(grep -v -e '^-r' -e '^uwsgi' -e '^psycopg2' requirements-dev.txt) \
      | grep -v -e '^uwsgi' -e '^psycopg2' > /tmp/reqs-0x0.txt
    ~/.venvs/0x0/bin/pip install -r /tmp/reqs-0x0.txt
    ~/.venvs/0x0/bin/python -m playwright install chromium

Run tests from this directory:

::

    ~/.venvs/0x0/bin/python -m pytest tests/ -q

See ``docs/development.md`` and ``docs/api.md`` in the repository root.
