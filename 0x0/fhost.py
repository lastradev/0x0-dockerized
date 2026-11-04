#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    Copyright © 2020 Mia Herkt
    Licensed under the EUPL, Version 1.2 or - as soon as approved
    by the European Commission - subsequent versions of the EUPL
    (the "License");
    You may not use this work except in compliance with the License.
    You may obtain a copy of the license at:

        https://joinup.ec.europa.eu/software/page/eupl

    Unless required by applicable law or agreed to in writing,
    software distributed under the License is distributed on an
    "AS IS" basis, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
    either express or implied.
    See the License for the specific language governing permissions
    and limitations under the License.
"""

from flask import Flask, abort, has_request_context, make_response, redirect, request, send_from_directory, url_for, Response, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy.exc import IntegrityError
from jinja2.exceptions import *
from jinja2 import ChoiceLoader, FileSystemLoader
from markupsafe import Markup
from urllib.parse import quote
from werkzeug.http import quote_header_value
from hashlib import sha256
from magic import Magic
from mimetypes import guess_extension
import bleach
import csv
import fcntl
import hmac
import json
import markdown
import os
import re
import struct
import sys
import threading
from pathlib import Path

app = Flask(__name__, instance_relative_config=True)
app.config.update(
    SQLALCHEMY_TRACK_MODIFICATIONS = False,
    PREFERRED_URL_SCHEME = "https", # nginx users: make sure to have 'uwsgi_param UWSGI_SCHEME $scheme;' in your config
    MAX_CONTENT_LENGTH = 256 * 1024 * 1024,
    MAX_URL_LENGTH = 4096,
    USE_X_SENDFILE = False,
    FHOST_USE_X_ACCEL_REDIRECT = False,  # disabled - no nginx
    FHOST_STORAGE_PATH = "up",
    FHOST_MAX_EXT_LENGTH = 9,
    FHOST_EXT_OVERRIDE = {
        "audio/flac" : ".flac",
        "image/gif" : ".gif",
        "image/jpeg" : ".jpg",
        "image/png" : ".png",
        "image/svg+xml" : ".svg",
        "video/webm" : ".webm",
        "video/x-matroska" : ".mkv",
        "application/octet-stream" : ".bin",
        "text/plain" : ".log",
        "text/plain" : ".txt",
        "text/x-diff" : ".diff",
    },
    FHOST_MIME_BLACKLIST = [
        "application/x-dosexec",
        "application/java-archive",
        "application/java-vm",
        "application/x-java-applet",
    ],
    # shortcut: JARs detected as ZIP rely on .jar names; inspect entries if stricter blocking is needed.
    FHOST_EXECUTABLE_EXTS = (".exe", ".jar", ".class"),
    FHOST_DOWNLOAD_ONLY_MIMES = [
        "application/javascript",
        "application/ecmascript",
        "application/xhtml+xml",
        "application/xml",
        "application/x-javascript",
        "image/svg+xml",
        "text/html",
        "text/javascript",
        "text/xml",
    ],
    FHOST_DOWNLOAD_ONLY_EXTS = (
        ".html", ".htm", ".svg", ".xml", ".js", ".xhtml", ".mhtml", ".mht", ".mjs", ".cjs",
    ),
    FHOST_UPLOAD_BLACKLIST = None,
    NSFW_DETECT = False,
    NSFW_THRESHOLD = 0.608,
    URL_ALPHABET = "DEQhd2uFteibPwq0SWBInTpA_jcZL5GKz3YCR14Ulk87Jors9vNHgfaOmMXy6Vx-",
    FHOST_LINK_SECRET = None,
)

if not app.config["TESTING"]:
    app.config.from_pyfile("config.py")
    if not app.config.get("FHOST_LINK_SECRET"):
        raise RuntimeError("FHOST_LINK_SECRET is required outside testing mode")
    app.jinja_loader = ChoiceLoader([
        FileSystemLoader(str(Path(app.instance_path) / "templates")),
        app.jinja_loader
    ])

    if app.config["DEBUG"]:
        app.config["FHOST_USE_X_ACCEL_REDIRECT"] = False

if app.config["NSFW_DETECT"]:
    from nsfw_detect import NSFWDetector
    nsfw = NSFWDetector()

try:
    mimedetect = Magic(mime=True, mime_encoding=False)
except:
    print("""Error: You have installed the wrong version of the 'magic' module.
Please install python-magic.""")
    sys.exit(1)

db = SQLAlchemy(app)
migrate = Migrate(app, db)

class URL(db.Model):
    id = db.Column(db.Integer, primary_key = True)
    url = db.Column(db.UnicodeText, unique = True)

    def __init__(self, url):
        self.url = url

    def getname(self):
        return su.enbase(self.id)

    def geturl(self):
        return url_for("get", path=self.getname(), _external=True, _scheme="https") + "\n"

    def get(url):
        u = URL.query.filter_by(url=url).first()

        if not u:
            u = URL(url)
            db.session.add(u)
            db.session.commit()

        return u

def make_link(sha256_val, ext_val, original_name_val, secret=None):
    if secret is None:
        secret = app.config.get("FHOST_LINK_SECRET") or b"test-secret"
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    msg = "\0".join([sha256_val or "", ext_val or "", original_name_val or ""])
    return hmac.new(secret, msg.encode("utf-8"), "sha256").hexdigest()

class FileAudit(db.Model):
    __tablename__ = "file_audit"
    id = db.Column(db.Integer, primary_key=True)
    sha256 = db.Column(db.String, nullable=False, index=True)
    link = db.Column(db.String(64), nullable=True, index=True)
    action = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

class File(db.Model):
    id = db.Column(db.Integer, primary_key = True)
    sha256 = db.Column(db.String, index = True)
    token = db.Column(db.String(32), unique = True, nullable = True)
    link = db.Column(db.String(64), nullable = False)
    ext = db.Column(db.UnicodeText)
    mime = db.Column(db.UnicodeText)
    addr = db.Column(db.UnicodeText)
    removed = db.Column(db.Boolean, default=False)
    nsfw_score = db.Column(db.Float)
    original_name = db.Column(db.UnicodeText)

    def __init__(self, sha256, token, ext, mime, addr, original_name=None, link=None):
        self.sha256 = sha256
        self.token = token
        self.ext = ext
        self.mime = mime
        self.addr = addr
        self.original_name = original_name
        self.link = link or make_link(sha256, ext, original_name)

    def getname(self):
        return u"{0}{1}".format(self.link, self.ext)

    def getdownloadname(self):
        return self.original_name or self.getname()

    def geturl(self):
        n = self.getname()
        scheme = app.config.get("PREFERRED_URL_SCHEME") or (request.scheme if has_request_context() else "https")

        if self.nsfw_score and self.nsfw_score > app.config["NSFW_THRESHOLD"]:
            return url_for("get", path=n, _external=True, _scheme=scheme, _anchor="nsfw") + "\n"
        else:
            return url_for("get", path=n, _external=True, _scheme=scheme) + "\n"

    def store(file_, addr):
        data = file_.stream.read()
        digest = sha256(data).hexdigest()

        def get_mime():
            guess = mimedetect.from_buffer(data)
            app.logger.debug(f"MIME - specified: '{file_.content_type}' - detected: '{guess}'")

            if guess and "/" in guess:
                mime = guess
            elif file_.content_type and "/" in file_.content_type:
                mime = file_.content_type
            else:
                mime = "application/octet-stream"

            if mime in app.config["FHOST_MIME_BLACKLIST"] or guess in app.config["FHOST_MIME_BLACKLIST"]:
                abort(415, description=(guess or mime).split(";", 1)[0].strip())

            suffix = Path(file_.filename or "").suffix.lower()
            if suffix in app.config["FHOST_EXECUTABLE_EXTS"]:
                abort(415, description=(guess or mime or suffix).split(";", 1)[0].strip())

            if mime.startswith("text/") and not "charset" in mime:
                mime += "; charset=utf-8"

            return mime

        def get_ext(mime):
            suffixes = Path(file_.filename or "").suffixes[-2:]
            ext = "".join(suffixes)
            gmime = mime.split(";")[0]
            guess = guess_extension(gmime)
            max_len = app.config["FHOST_MAX_EXT_LENGTH"]

            app.logger.debug(f"extension - specified: '{ext}' - detected: '{guess}'")

            if not ext:
                if gmime in app.config["FHOST_EXT_OVERRIDE"]:
                    ext = app.config["FHOST_EXT_OVERRIDE"][gmime]
                else:
                    ext = guess_extension(gmime)

            # Extra dots in a basename are not a compound extension. Keep
            # short pairs such as .tar.gz, otherwise use only the last suffix.
            if ext and len(ext) > max_len and len(suffixes) > 1:
                ext = suffixes[-1]

            return ext[:max_len] or ".bin"

        def get_original_name(ext):
            original_name = Path(file_.filename or "").name

            if not original_name:
                return f"file{ext}"

            if not original_name.endswith(ext):
                return f"{original_name}{ext}"

            return original_name

        mime = get_mime()
        ext = get_ext(mime)
        # libmagic also labels Markdown containing embedded scripts as HTML.
        if ext.lower() == ".md" and _bare_mime(mime) in ("text/html", "application/xhtml+xml"):
            document = data.decode("utf-8-sig", errors="replace")
            start = re.match(r"\s*(?:<!--.*?-->\s*)*", document, re.DOTALL).end()
            if not document[start:start + 16].lower().startswith(("<!doctype", "<html", "<?xml", "<script")):
                mime = "text/plain; charset=utf-8"
        original_name = get_original_name(ext)
        link = make_link(digest, ext, original_name)

        storage = Path(app.config["FHOST_STORAGE_PATH"])
        storage.mkdir(parents=True, exist_ok=True)
        p = storage / digest

        if File.query.filter_by(link=link, removed=True).first():
            abort(451)

        existing = File.query.filter_by(link=link, removed=False).first()
        if existing:
            if not p.is_file():
                file_.stream.seek(0)
                file_.save(p)
            else:
                p.touch()
            return existing

        f = File(digest, None, ext, mime, addr, original_name=original_name, link=link)

        if not p.is_file():
            file_.stream.seek(0)
            file_.save(p)
        else:
            p.touch()

        if not f.nsfw_score and app.config["NSFW_DETECT"]:
            f.nsfw_score = nsfw.detect(p)

        try:
            db.session.add(f)
            db.session.commit()
            return f
        except IntegrityError:
            db.session.rollback()
            if File.query.filter_by(link=link, removed=True).first():
                abort(451)
            existing = File.query.filter_by(link=link, removed=False).first()
            if existing:
                return existing
            raise



class UrlEncoder(object):
    def __init__(self,alphabet, min_length):
        self.alphabet = alphabet
        self.min_length = min_length

    def enbase(self, x):
        n = len(self.alphabet)
        str = ""
        while x > 0:
            str = (self.alphabet[int(x % n)]) + str
            x = int(x // n)
        padding = self.alphabet[0] * (self.min_length - len(str))
        return '%s%s' % (padding, str)

    def debase(self, x):
        n = len(self.alphabet)
        result = 0
        for i, c in enumerate(reversed(x)):
            result += self.alphabet.index(c) * (n ** i)
        return result

su = UrlEncoder(alphabet=app.config["URL_ALPHABET"], min_length=1)

def in_upload_bl(addr):
    if app.config["FHOST_UPLOAD_BLACKLIST"]:
        with app.open_instance_resource(app.config["FHOST_UPLOAD_BLACKLIST"]) as bl:
            check = addr.lstrip("::ffff:")
            for l in bl.readlines():
                if not l.startswith("#"):
                    if check == l.rstrip():
                        return True

    return False

def store_file(f, addr):
    if in_upload_bl(addr):
        return "Your host is blocked from uploading files.\n", 451

    sf = File.store(f, addr)

    return sf.geturl()

def _bare_mime(mime):
    return (mime or "").split(";", 1)[0].strip().lower()


def is_download_only(f):
    if _bare_mime(f.mime) in app.config["FHOST_DOWNLOAD_ONLY_MIMES"]:
        return True
    return Path("x" + (f.ext or "")).suffix.lower() in app.config["FHOST_DOWNLOAD_ONLY_EXTS"]


def build_file_response(f, fpath, inline=False):
    mime = "application/octet-stream" if is_download_only(f) else f.mime
    if app.config["FHOST_USE_X_ACCEL_REDIRECT"]:
        response = make_response()
        response.headers["Content-Length"] = fpath.stat().st_size
        response.headers["X-Accel-Redirect"] = "/" + str(fpath)
    else:
        response = send_from_directory(app.config["FHOST_STORAGE_PATH"], f.sha256, mimetype = mime)

    response.headers["Content-Type"] = mime
    disposition = "inline" if inline and not is_download_only(f) else "attachment"
    filename = f.getdownloadname().replace("\r", "").replace("\n", "")
    ascii_filename = filename.encode("ascii", "ignore").decode("ascii")
    response.headers["Content-Disposition"] = (
        f"{disposition}; filename={quote_header_value(ascii_filename, allow_token=False)}"
    )
    if ascii_filename != filename:
        response.headers["Content-Disposition"] += f"; filename*=UTF-8''{quote(filename, safe='')}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if is_download_only(f):
        response.headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'"
        )
    return response


def should_preview_file(f):
    return bool(f.mime and f.mime.startswith("text/")) and not is_download_only(f)


def should_render_markdown(f):
    return bool(f.ext and f.ext.lower() == ".md")


def should_preview_video(f):
    return bool(f.mime and f.mime.startswith("video/"))


def should_preview_image(f):
    return _bare_mime(f.mime).startswith("image/") and not is_download_only(f)


def should_preview_pdf(f):
    return _bare_mime(f.mime) == "application/pdf"


def should_render_csv(f):
    return bool(f.ext and f.ext.lower().endswith(".csv"))


MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(?P<diagram>[\s\S]*?)\n```", re.IGNORECASE)
MERMAID_PLACEHOLDER_RE = re.compile(r"<p>MERMAID_BLOCK_(?P<index>\d+)</p>")


def extract_mermaid_blocks(content):
    diagrams = []

    def replace_block(match):
        diagrams.append(match.group("diagram"))
        return f"\n\nMERMAID_BLOCK_{len(diagrams) - 1}\n\n"

    return MERMAID_BLOCK_RE.sub(replace_block, content), diagrams


def render_markdown_with_mermaid(sanitized_html, diagrams):
    def replace_placeholder(match):
        diagram = diagrams[int(match.group("index"))]
        return '<div class="mermaid">{}</div>'.format(bleach.clean(diagram, tags=[], strip=True))

    return MERMAID_PLACEHOLDER_RE.sub(replace_placeholder, sanitized_html)


def render_markdown(content):
    markdown_source, mermaid_diagrams = extract_mermaid_blocks(content)
    rendered_html = markdown.markdown(
        markdown_source,
        extensions=["fenced_code", "tables"],
        output_format="html5",
    )
    sanitized_html = bleach.clean(
        rendered_html,
        tags=[
            "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
            "blockquote", "pre", "code", "a", "strong", "em", "table", "thead",
            "tbody", "tr", "th", "td", "br", "hr", "img",
        ],
        attributes={
            "a": ["href", "title"],
            "img": ["src", "alt", "title"],
            "th": ["align"],
            "td": ["align"],
        },
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    return Markup(render_markdown_with_mermaid(sanitized_html, mermaid_diagrams))


def build_markdown_preview_response(f, fpath):
    response = make_response(render_template(
        "markdown_preview.html",
        content=render_markdown(fpath.read_text(encoding="utf-8", errors="replace")),
        download_url=url_for("download", path=f.getname()),
    ))
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def build_preview_response(f, fpath):
    response = make_response(render_template(
        "preview.html",
        content=fpath.read_text(encoding="utf-8", errors="replace"),
        download_url=url_for("download", path=f.getname()),
    ))
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def build_video_preview_response(f, fpath):
    response = make_response(render_template(
        "video_preview.html",
        download_url=url_for("download", path=f.getname()),
        mime=f.mime,
    ))
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def build_image_preview_response(f, fpath):
    response = make_response(render_template(
        "image_preview.html",
        download_url=url_for("download", path=f.getname()),
        media_url=url_for("raw", path=f.getname()),
        filename=f.getdownloadname(),
    ))
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


CSV_BLOCK_ROWS = 200
CSV_TILE_COLS = 50
CSV_CELL_PREVIEW_CHARS = 50
CSV_MAX_LINE_BYTES = 4 * 1024 * 1024
CSV_INDEX_HEADER = struct.Struct("<QQQ")
CSV_INDEX_OFFSET = struct.Struct("<Q")
CSV_BOM = b"\xef\xbb\xbf"


def _csv_index_dir():
    return Path(app.config["FHOST_STORAGE_PATH"]) / ".csv-index"


def _write_csv_index(path, rows, cols, stopped, offsets):
    payload = CSV_INDEX_HEADER.pack(rows, cols, 1 if stopped else 0)
    payload += b"".join(CSV_INDEX_OFFSET.pack(off) for off in offsets)
    path.write_bytes(payload)


def _read_csv_header(path):
    with path.open("rb") as f:
        data = f.read(CSV_INDEX_HEADER.size)
    if len(data) < CSV_INDEX_HEADER.size:
        raise struct.error("short CSV index header")
    rows, cols, stopped = CSV_INDEX_HEADER.unpack(data)
    return rows, cols, bool(stopped)


def _read_csv_offset(path, block):
    with path.open("rb") as f:
        f.seek(CSV_INDEX_HEADER.size + block * CSV_INDEX_OFFSET.size)
        data = f.read(CSV_INDEX_OFFSET.size)
    if len(data) < CSV_INDEX_OFFSET.size:
        raise struct.error("short CSV index offset")
    return CSV_INDEX_OFFSET.unpack(data)[0]


def _drop_csv_index(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _scan_csv_file(fpath):
    size = fpath.stat().st_size
    if size == 0:
        return 0, 0, False, []
    if size == 3 and fpath.read_bytes() == CSV_BOM:
        return 0, 0, False, []

    rows = 0
    cols = 0
    stopped = False
    offsets = []
    pos = 0
    first_line = True
    oversize = False

    def lines(raw):
        nonlocal pos, first_line, oversize
        while True:
            line = raw.readline()
            if not line:
                return
            if len(line) > CSV_MAX_LINE_BYTES:
                oversize = True
                return
            pos += len(line)
            text = line.decode("utf-8", "replace")
            if first_line:
                if text.startswith("\ufeff"):
                    text = text[1:]
                first_line = False
            # Python 3.14 treats a lone \r as an empty row; older csv raised Error.
            if "\r" in text and not text.endswith("\n"):
                raise csv.Error("CR line ending")
            yield text

    try:
        with open(fpath, "rb") as raw:
            reader = csv.reader(lines(raw))
            while True:
                start = pos
                try:
                    row = next(reader)
                except StopIteration:
                    break
                if oversize:
                    stopped = True
                    break
                if rows % CSV_BLOCK_ROWS == 0:
                    offsets.append(start)
                rows += 1
                if len(row) > cols:
                    cols = len(row)
            if oversize:
                stopped = True
    except csv.Error:
        stopped = True

    return rows, cols, stopped, offsets


def csv_index(fpath):
    index_path = _csv_index_dir() / fpath.name
    try:
        if index_path.is_file():
            rows, cols, stopped = _read_csv_header(index_path)
            return rows, cols, stopped, index_path
    except (struct.error, OSError):
        _drop_csv_index(index_path)

    _csv_index_dir().mkdir(parents=True, exist_ok=True)

    try:
        raw = open(fpath, "rb")
    except FileNotFoundError:
        abort(404)

    try:
        fcntl.flock(raw.fileno(), fcntl.LOCK_EX)
        try:
            if index_path.is_file():
                rows, cols, stopped = _read_csv_header(index_path)
                return rows, cols, stopped, index_path
        except (struct.error, OSError):
            _drop_csv_index(index_path)

        rows, cols, stopped, offsets = _scan_csv_file(fpath)
        # Name must be <sha256>.tmp-<pid>-<thread> so cleanup's split(".")[0] is the sha.
        tmp = _csv_index_dir() / "{}.tmp-{}-{}".format(fpath.name, os.getpid(), threading.get_ident())
        try:
            _write_csv_index(tmp, rows, cols, stopped, offsets)
            os.replace(str(tmp), str(index_path))
        except FileNotFoundError:
            abort(404)
        return rows, cols, stopped, index_path
    finally:
        raw.close()


def _parse_csv_from(fpath, offset, count, strip_bom):
    rows = []
    first_line = True

    def lines(raw):
        nonlocal first_line
        while True:
            line = raw.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace")
            if first_line and strip_bom and text.startswith("\ufeff"):
                text = text[1:]
            first_line = False
            yield text

    with open(fpath, "rb") as raw:
        raw.seek(offset)
        reader = csv.reader(lines(raw))
        for _ in range(count):
            try:
                rows.append(next(reader))
            except (StopIteration, csv.Error):
                break
    return rows


def csv_rows(fpath, index_path, start_row, count):
    try:
        rows, _, _ = _read_csv_header(index_path)
        if start_row >= rows or count <= 0:
            return []
        offset = _read_csv_offset(index_path, start_row // CSV_BLOCK_ROWS)
    except (struct.error, OSError):
        _drop_csv_index(index_path)
        rows, _, _, index_path = csv_index(fpath)
        if start_row >= rows or count <= 0:
            return []
        offset = _read_csv_offset(index_path, start_row // CSV_BLOCK_ROWS)
    skip = start_row % CSV_BLOCK_ROWS
    parsed = _parse_csv_from(
        fpath, offset, skip + min(count, rows - start_row), strip_bom=(offset == 0)
    )
    return parsed[skip:]


def _csv_json(payload):
    response = make_response(json.dumps(payload, ensure_ascii=False))
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _csv_int_arg(name):
    if name not in request.args:
        return None
    raw = request.args[name]
    if not re.fullmatch(r"[0-9]+", raw):
        abort(400)
    return int(raw)


def build_csv_preview_response(f, fpath):
    try:
        rows, cols, stopped, _ = csv_index(fpath)
    except FileNotFoundError:
        abort(404)
    response = make_response(render_template(
        "csv_preview.html",
        rows=rows,
        cols=cols,
        stopped=stopped,
        download_url=url_for("download", path=f.getname()),
        data_url=url_for("csv_data", path=f.getname()),
    ))
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

def lookup_file(path):
    parts = path.split("/", 1)
    file_part = Path(parts[0])
    extra = parts[1] if len(parts) > 1 else None
    sufs = "".join(file_part.suffixes[-2:])
    name = file_part.name[:-len(sufs) or None]

    if not sufs or not name:
        abort(404)

    is_legacy = False
    if HEX64_RE.match(name):
        rows = File.query.filter_by(link=name, ext=sufs).all()
        if not rows:
            abort(404)
        if any(r.removed for r in rows):
            abort(451)
        f = rows[0]
    else:
        # ponytail: legacy token links. Remove when
        #   SELECT count(*) FROM file WHERE token IS NOT NULL;  -> 0
        # then: migration dropping file.token and recreating uq_file_link without WHERE.
        f = File.query.filter_by(token=name, ext=sufs).first()
        if not f:
            abort(404)
        if f.removed:
            abort(451)
        if f.link and File.query.filter_by(link=f.link, removed=True).first():
            abort(451)
        is_legacy = True

    fpath = Path(app.config["FHOST_STORAGE_PATH"]) / f.sha256
    if not fpath.is_file():
        abort(404)

    return f, fpath, is_legacy, extra


def _make_legacy_redirect(endpoint, f, extra=None):
    qs = f"?{request.query_string.decode('utf-8')}" if request.query_string else ""
    target_path = f.getname() if not extra else f"{f.getname()}/{extra}"
    scheme = app.config.get("PREFERRED_URL_SCHEME") or request.scheme or "https"
    target = url_for(endpoint, path=target_path, _external=True, _scheme=scheme) + qs
    return redirect(target, code=301)


@app.route("/download/<path:path>")
def download(path):
    f, fpath, is_legacy, extra = lookup_file(path)
    if is_legacy:
        return _make_legacy_redirect("download", f, extra)
    return build_file_response(f, fpath)


@app.route("/raw/<path:path>")
def raw(path):
    f, fpath, is_legacy, extra = lookup_file(path)
    if is_legacy:
        return _make_legacy_redirect("raw", f, extra)
    if is_download_only(f) or not should_preview_image(f):
        abort(404)
    return build_file_response(f, fpath, inline=True)


@app.route("/csv/<path:path>")
def csv_data(path):
    f, fpath, is_legacy, extra = lookup_file(path)
    if is_legacy:
        return _make_legacy_redirect("csv_data", f, extra)
    if not should_render_csv(f):
        abort(404)
    try:
        nrows, ncols, stopped, index_path = csv_index(fpath)
    except FileNotFoundError:
        abort(404)

    try:
        if "r" in request.args or "c" in request.args:
            r = _csv_int_arg("r")
            c = _csv_int_arg("c")
            if r is None or c is None:
                abort(400)
            if r >= nrows or c >= ncols:
                return _csv_json({"text": ""})
            row = csv_rows(fpath, index_path, r, 1)
            if not row or c >= len(row[0]):
                return _csv_json({"text": ""})
            return _csv_json({"text": row[0][c]})

        rb = _csv_int_arg("rb")
        cb = _csv_int_arg("cb")
        if rb is None or cb is None:
            abort(400)
        start = rb * CSV_BLOCK_ROWS
        if start >= nrows:
            return _csv_json({"rows": []})
        n = min(CSV_BLOCK_ROWS, nrows - start)
        col0 = cb * CSV_TILE_COLS
        if col0 >= ncols:
            return _csv_json({"rows": [[] for _ in range(n)]})
        parsed = csv_rows(fpath, index_path, start, n)
        out = []
        left = (CSV_CELL_PREVIEW_CHARS - 1) // 2
        right = CSV_CELL_PREVIEW_CHARS - 1 - left
        for row in parsed:
            slice_ = row[col0:col0 + CSV_TILE_COLS]
            out.append([
                cell if len(cell) <= CSV_CELL_PREVIEW_CHARS else cell[:left] + "…" + cell[-right:]
                for cell in slice_
            ])
        return _csv_json({"rows": out})
    except FileNotFoundError:
        abort(404)


@app.route("/<path:path>")
def get(path):
    f, fpath, is_legacy, extra = lookup_file(path)
    if is_legacy:
        return _make_legacy_redirect("get", f, extra)

    if should_render_csv(f):
        return build_csv_preview_response(f, fpath)

    if is_download_only(f):
        return build_file_response(f, fpath)

    if should_render_markdown(f):
        return build_markdown_preview_response(f, fpath)

    if should_preview_video(f):
        return build_video_preview_response(f, fpath)

    if should_preview_image(f):
        return build_image_preview_response(f, fpath)

    if should_preview_pdf(f):
        return build_file_response(f, fpath, inline=True)

    if should_preview_file(f):
        return build_preview_response(f, fpath)

    return build_file_response(f, fpath)

@app.route("/", methods=["GET", "POST"])
def fhost():
    if request.method == "POST":
        if "file" in request.files:
            return store_file(request.files["file"], request.remote_addr)

        abort(400)
    else:
        return render_template("index.html")

@app.route("/robots.txt")
def robots():
    return """User-agent: *
Disallow: /
"""

@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(411)
@app.errorhandler(413)
@app.errorhandler(414)
@app.errorhandler(415)
@app.errorhandler(451)
def ehandler(e):
    try:
        return render_template(
            f"{e.code}.html",
            id=id,
            description=getattr(e, "description", ""),
        ), e.code
    except TemplateNotFound:
        return "Segmentation fault\n", e.code
