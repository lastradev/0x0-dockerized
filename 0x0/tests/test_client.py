import pytest
import tempfile
import os
import sys
import importlib
import json
import re
from urllib.parse import urlparse
from werkzeug.http import parse_options_header
from zipfile import ZipFile
from flask_migrate import upgrade as db_upgrade
from io import BytesIO
from pathlib import Path

app = None
db = None
url_for = None
File = None
URL = None
TOKEN_RE = re.compile(r"^https://localhost/[A-Za-z0-9_-]{12}(?:\.[A-Za-z0-9._-]+)?\n$")
LINK_RE = re.compile(r"^https://localhost/[0-9a-f]{64}(?:\.[A-Za-z0-9._-]+)?\n$")


def get_uploaded_path(response):
    return response.data.decode().removeprefix("https://localhost/").strip()


def get_download_path(html):
    match = re.search(r'href="([^"]+)"', html)
    assert match is not None
    return urlparse(match.group(1)).path


def get_csv_cfg(html):
    match = re.search(r"const CFG = (\{.*?\});", html, re.DOTALL)
    assert match is not None
    return json.loads(match.group(1))

@pytest.fixture
def client():
    global app, db, url_for, File, URL

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DATABASE_URI"] = f"sqlite:///{tmpdir}/db.sqlite"
        os.environ["FHOST_LINK_SECRET"] = "test-secret-value-32-chars-long"

        if "fhost" in sys.modules:
            fhost = importlib.reload(sys.modules["fhost"])
        else:
            fhost = importlib.import_module("fhost")

        app = fhost.app
        db = fhost.db
        url_for = fhost.url_for
        File = fhost.File
        URL = fhost.URL
        app.config["FHOST_STORAGE_PATH"] = os.path.join(tmpdir, "up")
        os.makedirs(app.config["FHOST_STORAGE_PATH"], exist_ok=True)
        app.config["TESTING"] = True

        with app.test_client() as client:
            with app.app_context():
                db_upgrade()
            yield client


def test_missing_link_secret_raises_outside_testing():
    old_secret = os.environ.get("FHOST_LINK_SECRET")
    old_uri = os.environ.get("DATABASE_URI")
    try:
        if "FHOST_LINK_SECRET" in os.environ:
            del os.environ["FHOST_LINK_SECRET"]
        os.environ["DATABASE_URI"] = "sqlite:///:memory:"
        if "fhost" in sys.modules:
            sys.modules["fhost"].app.config["TESTING"] = False
            with pytest.raises(RuntimeError, match="FHOST_LINK_SECRET is required"):
                importlib.reload(sys.modules["fhost"])
        else:
            with pytest.raises(RuntimeError, match="FHOST_LINK_SECRET is required"):
                importlib.import_module("fhost")
    finally:
        if old_secret is not None:
            os.environ["FHOST_LINK_SECRET"] = old_secret
        if old_uri is not None:
            os.environ["DATABASE_URI"] = old_uri
        if "fhost" in sys.modules:
            sys.modules["fhost"].app.config["TESTING"] = True


def test_models_have_link_and_file_audit(client):
    import fhost
    File = fhost.File
    FileAudit = fhost.FileAudit
    assert hasattr(File, "link")
    assert File.token.property.columns[0].nullable is True
    assert FileAudit.__tablename__ == "file_audit"

    with app.app_context():
        audit = FileAudit(sha256="test-sha", link="test-link", action="TEST_ACTION")
        db.session.add(audit)
        db.session.commit()
        assert audit.id is not None
        assert audit.created_at is not None


def test_client(client):
    payloads = [
        ({ "file" : (BytesIO(b"hello"), "hello.txt") }, 200),
        ({ "file" : (BytesIO(b"hello"), "hello.ignorethis") }, 200),
        ({ "file" : (BytesIO(b"bye"), "bye.truncatethis") }, 200),
        ({ "file" : (BytesIO(b"hi"), "hi.tar.gz") }, 200),
        ({ "file" : (BytesIO(b"lea!"), "lea!") }, 200),
        ({ "file" : (BytesIO(b"why?"), "balls", "application/x-dosexec") }, 200),
        ({}, 400),
    ]

    paths = []
    for p, s in payloads:
        rv = client.post("/", buffered=True,
                        content_type="multipart/form-data",
                        data=p)
        assert rv.status_code == s
        if rv.status_code == 200:
            location = rv.data.decode()
            assert LINK_RE.match(location)
            paths.append(location.removeprefix("https://localhost/").strip())

    assert paths[0] != paths[1]
    assert paths[0].endswith(".txt")
    assert paths[2].endswith(".truncate")
    assert paths[3].endswith(".tar.gz")
    assert paths[4].endswith(".txt")

    f = File.query.get(3)
    f.removed = True
    db.session.add(f)
    db.session.commit()

    rq = [
        (200, [
            "/",
            "robots.txt",
            paths[0],
            f"{paths[0]}/test",
            f"{paths[0]}/test.py",
            paths[4],
            paths[3],
        ]),
        (404, [
            paths[0].split(".", 1)[0],
            f"{paths[0].split('.', 1)[0]}/test",
            f"{paths[0].split('.', 1)[0]}/test.bin",
            "test.bin",
            "test.bin/test",
            "test.bin/test.py",
            "test",
            "test/test",
            "test.bin/test.py",
            f"{paths[0].split('.', 1)[0]}.bin",
        ]),
        (451, [
            paths[2],
        ]),
    ]

    for code, paths in rq:
        for p in paths:
            app.logger.info(f"GET {p}")
            rv = client.get(p)
            assert rv.status_code == code


def test_rejects_remote_url_import(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"url": "https://example.com/file.txt"},
    )
    assert rv.status_code == 400


@pytest.mark.parametrize("x_accel", [False, True])
@pytest.mark.parametrize("filename,payload", [
    ("index.html", b"<!doctype html><html><script>alert(1)</script></html>"),
    ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
    ("data.xml", b'<?xml version="1.0"?><root>hello</root>'),
    ("app.js", b"function x(){return 1;}\n"),
    ("app.mjs", b"function x(){return 1;}\n"),
    ("app.cjs", b"function x(){return 1;}\n"),
    ("page.mhtml", b"MIME-Version: 1.0\nContent-Type: multipart/related; boundary=x\n"),
    ("page.mht", b"MIME-Version: 1.0\nContent-Type: multipart/related; boundary=x\n"),
    ("misnamed.png", b"<!doctype html><html><script>alert(1)</script></html>"),
])
def test_active_web_upload_is_download_only(client, filename, payload, x_accel):
    app.config["FHOST_USE_X_ACCEL_REDIRECT"] = x_accel
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(payload), filename)},
    )
    assert rv.status_code == 200
    path = get_uploaded_path(rv)

    for endpoint in (f"/{path}", f"/download/{path}"):
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.mimetype == "application/octet-stream"
        assert response.headers["Content-Disposition"] == f'attachment; filename="{filename}"'
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Security-Policy"] == (
            "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'"
        )
        assert response.content_length == len(payload)
        if x_accel:
            assert response.headers["X-Accel-Redirect"]
            assert response.get_data() == b""
        else:
            assert response.get_data() == payload

    assert client.get(f"/raw/{path}").status_code == 404


@pytest.mark.parametrize("payload", [
    b"<html><body><h1>not markdown</h1></body></html>",
    b"\xef\xbb\xbf<!doctype html><html><script>alert(1)</script></html>",
    b"<!--x--><html><script>alert(1)</script></html>",
    b"<!--x-->\n<!--y-->\n<html><script>alert(1)</script></html>",
    b"<script>alert(1)</script>",
    b"\xc2\xa0<html><script>alert(1)</script></html>",
    b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><h1>x</h1></html>',
])
def test_html_named_as_markdown_is_not_previewed(client, payload):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(payload), "readme.md", "text/html")},
    )
    assert rv.status_code == 200
    path = get_uploaded_path(rv)

    for endpoint in (f"/{path}", f"/download/{path}"):
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.get_data() == payload
        assert response.mimetype == "application/octet-stream"
        assert response.headers["Content-Disposition"] == 'attachment; filename="readme.md"'
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Security-Policy"] == (
            "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'"
        )
    assert client.get(f"/raw/{path}").status_code == 404


@pytest.mark.parametrize("filename", ["setup.exe", "renamed.txt", "extensionless", "SETUP.EXE"])
def test_windows_executable_upload_is_rejected_with_explanation(client, filename):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"MZ" + b"\x00" * 64), filename)},
    )
    body = rv.get_data(as_text=True)
    assert rv.status_code == 415
    assert "executable" in body.lower()
    assert "Segmentation fault" not in body
    assert "application/x-dosexec" in body


def test_plain_text_with_executable_extension_is_rejected(client):
    rv = client.post("/", data={"file": (BytesIO(b"hello"), "setup.exe")})
    assert rv.status_code == 415
    assert "executable" in rv.get_data(as_text=True).lower()


def test_java_archive_mime_is_rejected_without_jar_extension(client, monkeypatch):
    import fhost

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        archive.writestr("Main.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\x00" * 32)
    payload.seek(0)
    # libmagic versions disagree between application/zip and application/java-archive.
    monkeypatch.setattr(fhost.mimedetect, "from_buffer", lambda data: "application/java-archive")
    rv = client.post("/", data={"file": (payload, "renamed.zip")})
    assert rv.status_code == 415
    assert "application/java-archive" in rv.get_data(as_text=True)


def test_java_class_upload_is_rejected(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(bytes([0xCA, 0xFE, 0xBA, 0xBE, 0, 0, 0, 0x34]) + b"\x00" * 32), "Main.class")},
    )
    body = rv.get_data(as_text=True)
    assert rv.status_code == 415
    assert "executable" in body.lower()
    assert "Segmentation fault" not in body


def test_jar_upload_is_rejected(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"PK\x03\x04" + b"\x00" * 26), "app.jar")},
    )
    assert rv.status_code == 415
    assert "executable" in rv.get_data(as_text=True).lower()


def test_rejects_generic_url_shortening(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"shorten": "https://example.com"},
    )
    assert rv.status_code == 400


def test_randomized_links_render_text_preview_with_download_link(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"line one\nline two\n"), "preview.txt")},
    )

    preview_path = get_uploaded_path(rv)
    preview = client.get(preview_path)
    preview_html = preview.get_data(as_text=True)

    assert preview.status_code == 200
    assert preview.mimetype == "text/html"
    assert '<a href="/download/' in preview_html
    assert ">Download</a>" in preview_html
    assert "line one" in preview_html
    assert "line two" in preview_html
    assert client.get(f"/raw/{preview_path}").status_code == 404

    download = client.get(get_download_path(preview_html))

    assert download.status_code == 200
    assert download.headers["Content-Disposition"] == 'attachment; filename="preview.txt"'


def test_text_preview_escapes_html_like_content(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"<b>bold</b>\n"), "escaped.txt")},
    )

    preview = client.get(get_uploaded_path(rv))
    preview_html = preview.get_data(as_text=True)

    assert preview.status_code == 200
    assert "&lt;b&gt;bold&lt;/b&gt;" in preview_html
    assert "<b>bold</b>" not in preview_html


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc``\x00\x00\x00\x02"
    b"\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_png_upload_renders_image_preview_with_download_link(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(PNG_1X1), "image.png", "image/png")},
    )

    file_path = get_uploaded_path(rv)
    preview = client.get(file_path)

    assert preview.status_code == 200
    assert preview.mimetype == "text/html"
    preview_html = preview.get_data(as_text=True)
    assert preview.headers["X-Content-Type-Options"] == "nosniff"
    assert '<a href="/download/' in preview_html
    assert ">Download</a>" in preview_html
    assert "<img" in preview_html
    assert f'src="/raw/{file_path}"' in preview_html

    download = client.get(get_download_path(preview_html))
    assert download.status_code == 200
    assert download.mimetype == "image/png"
    assert download.headers["Content-Disposition"] == 'attachment; filename="image.png"'
    assert download.get_data() == PNG_1X1

    media = client.get(f"/raw/{file_path}")
    assert media.status_code == 200
    assert media.mimetype == "image/png"
    assert media.headers["Content-Disposition"] == 'inline; filename="image.png"'
    assert media.get_data() == PNG_1X1


@pytest.mark.parametrize("x_accel", [False, True])
@pytest.mark.parametrize("filename", ['quote".png', 'foo"; filename=evil.png', 'back\\slash.png', 'café.png', '画像.png'])
def test_download_filename_is_safely_encoded(client, filename, x_accel):
    app.config["FHOST_USE_X_ACCEL_REDIRECT"] = x_accel
    # The test client's tuple shorthand does not escape multipart filenames.
    upload_name = filename.replace("\\", "\\\\").replace('"', '\\"')
    multipart = (
        f'--boundary\r\nContent-Disposition: form-data; name="file"; filename="{upload_name}"\r\n'
        'Content-Type: image/png\r\n\r\n'
    ).encode("utf-8") + PNG_1X1 + b"\r\n--boundary--\r\n"
    rv = client.post("/", data=multipart, content_type="multipart/form-data; boundary=boundary")
    assert rv.status_code == 200
    path = get_uploaded_path(rv)

    for endpoint, disposition in ((f"/download/{path}", "attachment"), (f"/raw/{path}", "inline")):
        response = client.get(endpoint)
        assert response.status_code == 200
        header = response.headers["Content-Disposition"]
        assert header.isascii()
        kind, options = parse_options_header(header)
        assert kind == disposition
        assert options == {"filename": filename}
        assert response.mimetype == "image/png"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        if not x_accel:
            assert response.get_data() == PNG_1X1


def test_legacy_filename_cannot_inject_header_lines(client):
    rv = client.post("/", data={"file": (BytesIO(PNG_1X1), "image.png")})
    assert rv.status_code == 200
    path = get_uploaded_path(rv)
    with app.app_context():
        f = File.query.filter_by(link=path.split(".", 1)[0]).one()
        f.original_name = "line\r\nbreak.png"
        db.session.commit()

    for endpoint in (f"/download/{path}", f"/raw/{path}"):
        response = client.get(endpoint)
        assert response.status_code == 200
        _, options = parse_options_header(response.headers["Content-Disposition"])
        assert options == {"filename": "linebreak.png"}
        assert response.get_data() == PNG_1X1


MINIMAL_PDF = (
    b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def test_pdf_upload_served_inline_for_native_viewer(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(MINIMAL_PDF), "doc.pdf")},
    )

    file_path = get_uploaded_path(rv)
    preview = client.get(file_path)

    assert preview.status_code == 200
    assert preview.mimetype == "application/pdf"
    assert preview.headers["Content-Disposition"] == 'inline; filename="doc.pdf"'
    assert preview.headers["X-Content-Type-Options"] == "nosniff"
    assert preview.get_data() == MINIMAL_PDF

    download = client.get(f"/download/{file_path}")
    assert download.status_code == 200
    assert download.mimetype == "application/pdf"
    assert download.headers["Content-Disposition"] == 'attachment; filename="doc.pdf"'
    assert download.get_data() == MINIMAL_PDF

    assert client.get(f"/raw/{file_path}").status_code == 404


def test_randomized_links_preserve_uploaded_filename_on_download(client):
    first = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"same-bytes"), "first-name.txt")},
    )
    second = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"same-bytes"), "second name.txt")},
    )

    first_path = get_uploaded_path(first)
    second_path = get_uploaded_path(second)

    assert first_path != second_path

    first_preview = client.get(first_path)
    second_preview = client.get(second_path)

    assert first_preview.status_code == 200
    assert second_preview.status_code == 200

    first_download = client.get(get_download_path(first_preview.get_data(as_text=True)))
    second_download = client.get(get_download_path(second_preview.get_data(as_text=True)))

    assert first_download.headers["Content-Disposition"] == 'attachment; filename="first-name.txt"'
    assert second_download.headers["Content-Disposition"] == 'attachment; filename="second name.txt"'


def test_uploaded_file_urls_use_64_hex_hmac(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"hmac-content"), "sample.txt")},
    )
    assert rv.status_code == 200
    assert LINK_RE.match(rv.data.decode())


def test_upload_deduplication_same_bytes_and_name(client):
    rv1 = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"identical-bytes"), "same.txt")},
    )
    rv2 = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"identical-bytes"), "same.txt")},
    )
    assert rv1.data == rv2.data
    link_val = rv1.data.decode().removeprefix("https://localhost/").split(".")[0]
    with app.app_context():
        assert File.query.filter_by(link=link_val).count() == 1


def test_upload_different_filename_different_link(client):
    rv1 = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"same-bytes-diff-name"), "first.txt")},
    )
    rv2 = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"same-bytes-diff-name"), "second.txt")},
    )
    assert rv1.data != rv2.data


def test_reupload_of_removed_file_returns_451(client):
    rv1 = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"moderated-bytes"), "bad.txt")},
    )
    link_val = rv1.data.decode().removeprefix("https://localhost/").split(".")[0]
    with app.app_context():
        f = File.query.filter_by(link=link_val).first()
        f.removed = True
        db.session.commit()

    rv2 = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"moderated-bytes"), "bad.txt")},
    )
    assert rv2.status_code == 451


def test_canonical_link_returns_200_without_redirect(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"test-content"), "canon.txt")},
    )
    path = get_uploaded_path(rv)
    resp = client.get(path)
    assert resp.status_code == 200


def test_legacy_token_301_redirects_to_canonical_link(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"legacy-content"), "leg.txt")},
    )
    link_val = rv.data.decode().removeprefix("https://localhost/").split(".")[0]
    with app.app_context():
        f = File.query.filter_by(link=link_val).first()
        f.token = "1234567890ab"
        db.session.commit()
        token = f.token
        link = f.link

    resp = client.get(f"/{token}.txt?foo=bar")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith(f"/{link}.txt?foo=bar")

    dl_resp = client.get(f"/download/{token}.txt")
    assert dl_resp.status_code == 301
    assert dl_resp.headers["Location"].endswith(f"/download/{link}.txt")


def test_legacy_token_removed_returns_451_before_redirect(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"legacy-removed"), "bad.txt")},
    )
    link_val = rv.data.decode().removeprefix("https://localhost/").split(".")[0]
    with app.app_context():
        f = File.query.filter_by(link=link_val).first()
        f.token = "legacyrem001"
        f.removed = True
        db.session.commit()

    resp = client.get("/legacyrem001.txt")
    assert resp.status_code == 451
    assert "Location" not in resp.headers


def test_legacy_token_missing_storage_returns_404_before_redirect(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"legacy-missing"), "gone.txt")},
    )
    link_val = rv.data.decode().removeprefix("https://localhost/").split(".")[0]
    with app.app_context():
        f = File.query.filter_by(link=link_val).first()
        f.token = "legacymiss01"
        db.session.commit()
        storage_file = Path(app.config["FHOST_STORAGE_PATH"]) / f.sha256
        if storage_file.exists():
            storage_file.unlink()

    resp = client.get("/legacymiss01.txt")
    assert resp.status_code == 404
    assert "Location" not in resp.headers


def test_multiple_legacy_duplicates_451_if_any_removed(client):
    from fhost import make_link
    with app.app_context():
        link_shared = make_link("dup-sha", ".txt", "dup.txt")
        f1 = File("dup-sha", "token1111111", ".txt", "text/plain", "127.0.0.1", "dup.txt", link=link_shared)
        f2 = File("dup-sha", "token2222222", ".txt", "text/plain", "127.0.0.1", "dup.txt", link=link_shared)
        f2.removed = True
        storage_file = Path(app.config["FHOST_STORAGE_PATH"]) / "dup-sha"
        storage_file.write_bytes(b"content")
        db.session.add_all([f1, f2])
        db.session.commit()

    resp = client.get(f"/{link_shared}.txt")
    assert resp.status_code == 451


def test_uploaded_file_urls_use_long_random_tokens(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"randomized"), "sample.txt")},
    )

    assert rv.status_code == 200
    assert LINK_RE.match(rv.data.decode())


def test_dotted_filenames_keep_real_file_extension_in_public_url(client):
    pdf = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={
            "file": (
                BytesIO(b"%PDF-1.4 dummy receipt"),
                "cepat.smartselangor.com.my_fpx-receipt.pdf",
            )
        },
    )
    video = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={
            "file": (
                BytesIO(b"\x00\x00\x00\x18ftypmp42"),
                "Sophia_Xing_-_The_AI_labs_finally_made_peace._Jensen_brought_the_chips._created_in_b5xhFt.mp4",
            )
        },
    )

    pdf_path = get_uploaded_path(pdf)
    video_path = get_uploaded_path(video)

    assert pdf.status_code == 200
    assert video.status_code == 200
    assert pdf_path.endswith(".pdf")
    assert not pdf_path.endswith(".my_fpx-r")
    assert video_path.endswith(".mp4")
    assert not video_path.endswith("._created")
    assert LINK_RE.match(pdf.data.decode())
    assert LINK_RE.match(video.data.decode())

    pdf_download = client.get(f"/download/{pdf_path}")
    video_download = client.get(f"/download/{video_path}")

    assert pdf_download.status_code == 200
    assert video_download.status_code == 200
    assert pdf_download.headers["Content-Disposition"] == (
        'attachment; filename="cepat.smartselangor.com.my_fpx-receipt.pdf"'
    )
    assert video_download.headers["Content-Disposition"] == (
        'attachment; filename="Sophia_Xing_-_The_AI_labs_finally_made_peace._Jensen_brought_the_chips._created_in_b5xhFt.mp4"'
    )


def test_uploaded_file_response_sets_nosniff(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"hello"), "hello.txt")},
    )

    preview = client.get(get_uploaded_path(rv))
    download = client.get(get_download_path(preview.get_data(as_text=True)))

    assert download.status_code == 200
    assert download.headers["X-Content-Type-Options"] == "nosniff"


def test_markdown_upload_returns_md_url(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"# Title\n"), "README.md")},
    )

    assert rv.status_code == 200
    assert get_uploaded_path(rv).endswith(".md")
    assert LINK_RE.match(rv.data.decode())


def test_markdown_preview_renders_common_markdown(client):
    content = b"""# Heading

Paragraph with [link](https://example.com).

- one
- two

> quoted

```python
print('<safe>')
```
"""
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(content), "doc.md")},
    )

    preview = client.get(get_uploaded_path(rv))
    html = preview.get_data(as_text=True)

    assert preview.status_code == 200
    assert preview.mimetype == "text/html"
    assert "<h1>Heading</h1>" in html
    assert '<p>Paragraph with <a href="https://example.com">link</a>.</p>' in html
    assert "<ul>" in html
    assert "<li>one</li>" in html
    assert "<blockquote>" in html
    assert "<pre><code" in html
    assert "&lt;safe&gt;" in html


def test_markdown_preview_renders_tables(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"| A | B |\n|---|---|\n| 1 | 2 |\n"), "table.md")},
    )

    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)

    assert "<table>" in html
    assert "<thead>" in html
    assert "<tbody>" in html
    assert "<tr>" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html


def test_markdown_preview_download_link_preserves_filename(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"# Download me\n"), "original name.md")},
    )

    preview = client.get(get_uploaded_path(rv))
    html = preview.get_data(as_text=True)
    download = client.get(get_download_path(html))

    assert '<a href="/download/' in html
    assert ">Download</a>" in html
    assert download.status_code == 200
    assert download.headers["Content-Disposition"] == 'attachment; filename="original name.md"'


def test_markdown_preview_renders_mermaid_container(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"```mermaid\ngraph TD\n  A-->B\n```\n"), "diagram.md")},
    )

    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)

    assert '<div class="mermaid">' in html
    assert "graph TD" in html
    assert "A--&gt;B" in html
    assert "mermaid@10.9.1" in html
    assert "mermaid.initialize" in html


def test_markdown_preview_keeps_non_mermaid_code_as_code(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"```python\nprint('hello')\n```\n"), "code.md")},
    )

    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)

    assert '<div class="mermaid">' not in html
    assert "<pre><code" in html
    assert "print('hello')" in html


def test_plain_text_preview_does_not_include_mermaid(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"```mermaid\ngraph TD\n```\n"), "plain.txt")},
    )

    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)

    assert "mermaid@10.9.1" not in html
    assert "mermaid.initialize" not in html
    assert "```mermaid" in html


@pytest.mark.parametrize("prefix", [b"", b"<!-- Markdown comment -->\n\n"])
def test_markdown_preview_sanitizes_script_tags(client, prefix):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(prefix + b"# Safe\n\nRegular Markdown paragraph before raw HTML.\n\n<script>alert('x')</script>\n"), "unsafe.md")},
    )
    assert rv.status_code == 200
    path = get_uploaded_path(rv)
    preview = client.get(path)
    html = preview.get_data(as_text=True)

    assert preview.status_code == 200
    assert preview.mimetype == "text/html"
    assert "<h1>Safe</h1>" in html
    assert "<script>alert('x')</script>" not in html
    with app.app_context():
        assert File.query.filter_by(link=path.split(".", 1)[0]).one().mime == "text/plain; charset=utf-8"


def test_markdown_preview_sanitizes_inline_event_attributes(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"<img src='https://example.com/x.png' onerror='alert(1)' onclick='alert(2)'>"), "events.md")},
    )

    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)

    assert "onerror" not in html
    assert "onclick" not in html
    assert "onload" not in html


def test_markdown_preview_sanitizes_javascript_urls(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"[bad](javascript:alert('x'))"), "link.md")},
    )

    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)

    assert "javascript:" not in html


def test_text_with_markdown_content_remains_plain_text(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"# Not rendered\n\n| A | B |\n|---|---|\n"), "markdown-looking.txt")},
    )

    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)

    assert "<pre>" in html
    assert "# Not rendered" in html
    assert "<h1>Not rendered</h1>" not in html
    assert "<table>" not in html


def test_video_upload_renders_player_preview_with_download_link(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"\x00\x00\x00\x18ftypmp42"), "clip.mp4")},
    )

    preview_path = get_uploaded_path(rv)
    preview = client.get(preview_path)
    preview_html = preview.get_data(as_text=True)

    assert preview.status_code == 200
    assert preview.mimetype == "text/html"
    assert preview.headers["X-Content-Type-Options"] == "nosniff"
    assert '<a href="/download/' in preview_html
    assert ">Download</a>" in preview_html
    assert "<video controls" in preview_html
    assert '<source src="/download/' in preview_html
    assert 'type="video/mp4"' in preview_html

    download = client.get(get_download_path(preview_html))

    assert download.status_code == 200
    assert download.mimetype == "video/mp4"
    assert download.headers["Content-Disposition"] == 'attachment; filename="clip.mp4"'


def test_webm_upload_renders_player_preview(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"\x1a\x45\xdf\xa3\x93\x42\x82\x84webm"), "clip.webm")},
    )

    preview = client.get(get_uploaded_path(rv))
    preview_html = preview.get_data(as_text=True)

    assert preview.status_code == 200
    assert preview.mimetype == "text/html"
    assert "<video controls" in preview_html
    assert 'type="video/webm"' in preview_html


def test_csv_upload_renders_table_preview_with_download_link(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a,b\n1,2\n"), "sheet.csv")},
    )
    preview_path = get_uploaded_path(rv)
    preview = client.get(preview_path)
    html = preview.get_data(as_text=True)

    assert preview.status_code == 200
    assert preview.mimetype == "text/html"
    assert preview.headers["X-Content-Type-Options"] == "nosniff"
    assert '<a href="/download/' in html
    assert ">Download</a>" in html
    assert "<pre>a,b" not in html

    download = client.get(get_download_path(html))
    assert download.status_code == 200
    assert download.headers["Content-Disposition"] == 'attachment; filename="sheet.csv"'
    assert download.get_data() == b"a,b\n1,2\n"


def test_csv_with_html_mime_keeps_table_preview(client):
    payload = b"col\n<!doctype html><html>x</html>\n"
    rv = client.post("/", data={"file": (BytesIO(payload), "pages.csv")})
    assert rv.status_code == 200
    path = get_uploaded_path(rv)
    with app.app_context():
        assert File.query.filter_by(link=path.split(".", 1)[0]).one().mime.startswith("text/html")

    preview = client.get(path)
    assert preview.status_code == 200
    assert preview.mimetype == "text/html"
    cfg = get_csv_cfg(preview.get_data(as_text=True))
    assert cfg["rows"] == 2
    assert cfg["cols"] == 1
    tile = client.get(f"/csv/{path}?rb=0&cb=0")
    assert tile.status_code == 200
    assert tile.get_json() == {"rows": [["col"], ["<!doctype html><html>x</html>"]]}
    download = client.get(f"/download/{path}")
    assert download.mimetype == "application/octet-stream"
    assert download.get_data() == payload
    assert client.get(f"/raw/{path}").status_code == 404


def test_compound_csv_extension_gets_table_preview(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a,b\n"), "report.2024.csv")},
    )
    preview = client.get(get_uploaded_path(rv))
    html = preview.get_data(as_text=True)
    assert preview.status_code == 200
    assert preview.mimetype == "text/html"
    assert ">Download</a>" in html
    assert "<pre>a,b" not in html


def test_txt_with_commas_stays_plain_text_preview(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a,b\n1,2\n"), "notes.txt")},
    )
    preview = client.get(get_uploaded_path(rv))
    html = preview.get_data(as_text=True)
    assert "<pre>" in html
    assert "a,b" in html
    assert "const CFG" not in html


def test_csv_preview_empty_file_message(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b""), "empty.csv")},
    )
    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)
    assert "Empty file." in html
    assert "Couldn't read past row" not in html
    assert 'class="scrollbox"' not in html
    assert 'type="number"' not in html
    cfg = get_csv_cfg(html)
    assert cfg["rows"] == 0
    assert cfg["cols"] == 0


def test_csv_preview_bom_only_is_empty(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"\xef\xbb\xbf"), "bom.csv")},
    )
    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)
    assert "Empty file." in html
    assert get_csv_cfg(html)["rows"] == 0


def test_csv_preview_crlf_only_is_one_empty_row(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"\r\n"), "crlf.csv")},
    )
    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)
    assert "Empty file." not in html
    assert get_csv_cfg(html)["rows"] == 1


def test_csv_preview_bom_plus_newline_is_one_row(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"\xef\xbb\xbf\n"), "bomnl.csv")},
    )
    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)
    assert "Empty file." not in html
    assert get_csv_cfg(html)["rows"] == 1


def test_csv_preview_cr_only_stops_at_row_zero(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"\r"), "mac.csv")},
    )
    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)
    assert "Couldn't read past row 0." in html
    assert "Empty file." not in html
    assert 'class="scrollbox"' not in html
    assert get_csv_cfg(html)["rows"] == 0


def test_csv_preview_huge_field_keeps_prior_rows(client):
    payload = b"a\nb\n" + b"x" * 131073 + b"\n"
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(payload), "widefield.csv")},
    )
    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)
    assert "Couldn't read past row 2." in html
    assert get_csv_cfg(html)["rows"] == 2


def test_csv_preview_overlong_line_stops_at_row_zero(client):
    payload = b"x" * (4 * 1024 * 1024 + 1)
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(payload), "longline.csv")},
    )
    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)
    assert "Couldn't read past row 0." in html
    assert get_csv_cfg(html)["rows"] == 0


def test_csv_preview_grid_chrome_when_rows_exist(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a,b\n1,2\n"), "grid.csv")},
    )
    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)
    cfg = get_csv_cfg(html)
    assert cfg["rows"] == 2
    assert cfg["cols"] == 2
    assert cfg["dataUrl"].startswith("/csv/")
    assert 'class="scrollbox"' in html
    assert 'class="sizer"' in html
    assert "<dialog" in html
    assert 'id="goto"' in html
    assert 'id="gotocol"' in html
    assert "Couldn't read past row" not in html


def test_csv_preview_no_grid_when_empty(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b""), "nogrid.csv")},
    )
    html = client.get(get_uploaded_path(rv)).get_data(as_text=True)
    assert 'class="scrollbox"' not in html
    assert "<dialog" not in html


@pytest.mark.parametrize("width", [240, 900])
def test_csv_preview_adapts_columns_and_preserves_horizontal_scroll(client, width):
    from playwright.sync_api import sync_playwright

    long_cell = "BEGIN_" + "W" * 200 + "_employees"
    rv = client.post(
        "/",
        content_type="multipart/form-data",
        data={"file": (BytesIO((
            "Id,Number of employees,Details\n1,1234," + long_cell + "\n"
        ).encode()), "wide.csv")},
    )
    path = get_uploaded_path(rv)
    responses = {
        "/" + path: client.get(path),
        "/csv/" + path + "?rb=0&cb=0": client.get(
            "/csv/" + path, query_string={"rb": "0", "cb": "0"},
        ),
        "/csv/" + path + "?r=1&c=2": client.get(
            "/csv/" + path, query_string={"r": "1", "c": "2"},
        ),
    }

    def serve(route):
        url = urlparse(route.request.url)
        response = responses[url.path + ("?" + url.query if url.query else "")]
        route.fulfill(
            status=response.status_code,
            headers=dict(response.headers),
            body=response.data,
        )

    with sync_playwright() as playwright:
        with playwright.chromium.launch(
            channel="chromium",
            executable_path=os.environ.get("CHROMIUM_EXECUTABLE_PATH"),
        ) as browser:
            page = browser.new_page(viewport={"width": width, "height": 300})
            page.set_default_timeout(5000)
            page.route("http://localhost/**", serve)
            page.goto("http://localhost/" + path)
            page.wait_for_function("document.querySelector('tbody td').textContent.length > 0")
            dimensions = page.evaluate("""() => {
                const box = document.querySelector('.scrollbox');
                const cells = document.querySelector('tbody tr').cells;
                return [...cells].map(cell => cell.offsetWidth);
            }""")
            assert dimensions[0] == 80
            assert dimensions[1] < dimensions[2] < dimensions[3]
            label = page.locator('tbody tr').first.locator('td').nth(1)
            assert label.text_content() == "Number of employees"
            assert label.evaluate("cell => cell.scrollWidth === cell.clientWidth")
            detail = page.locator('tbody tr').nth(1).locator('td').nth(2)
            assert detail.text_content().startswith("BEGIN_")
            assert detail.text_content().endswith("_employees")
            assert "…" in detail.text_content()

            page.evaluate("document.querySelector('.scrollbox').scrollLeft = 50")
            page.wait_for_function("document.querySelector('tbody td').getBoundingClientRect().x === 30")
            assert page.locator('tbody th').first.bounding_box()["x"] == 0
            assert page.locator('thead .cell').first.bounding_box()["x"] == 30
            page.evaluate("document.querySelector('.scrollbox').scrollLeft = 1000000")
            page.wait_for_function("""() => {
                const cell = [...document.querySelectorAll('thead .cell')].find(cell => cell.textContent === 'C');
                return cell && cell.getBoundingClientRect().right <= innerWidth;
            }""")
            page.locator('tbody tr').nth(1).locator('td:visible').last.click()
            page.wait_for_function("document.querySelector('dialog pre').textContent.endsWith('_employees')")
            assert page.locator('dialog pre').text_content() == long_cell


def test_csv_preview_scaled_wheel_advances_variable_width_columns(client):
    from playwright.sync_api import sync_playwright

    rv = client.post(
        "/",
        content_type="multipart/form-data",
        data={"file": (BytesIO((",".join(["x"] * 100000) + "\n").encode()), "scaled.csv")},
    )
    path = get_uploaded_path(rv)
    html = client.get(path)
    tile = client.get("/csv/" + path, query_string={"rb": "0", "cb": "0"})

    def serve(route):
        response = tile if urlparse(route.request.url).path.startswith("/csv/") else html
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

    with sync_playwright() as playwright:
        with playwright.chromium.launch(
            channel="chromium",
            executable_path=os.environ.get("CHROMIUM_EXECUTABLE_PATH"),
        ) as browser:
            page = browser.new_page(viewport={"width": 900, "height": 300})
            page.set_default_timeout(5000)
            page.route("http://localhost/**", serve)
            page.goto("http://localhost/" + path)
            page.wait_for_function("document.querySelector('tbody td').textContent === 'x'")
            page.mouse.move(450, 150)
            for label in ["B", "C", "D"]:
                page.mouse.wheel(1, 0)
                page.wait_for_function(
                    "label => document.querySelector('thead .cell').textContent === label", arg=label,
                )
            page.mouse.wheel(-1, 0)
            page.wait_for_function("document.querySelector('thead .cell').textContent === 'C'")


def test_csv_preview_column_finder_jumps_to_column(client):
    from playwright.sync_api import sync_playwright

    header = ",".join([f"col_{i+1}" for i in range(54)]) + "\n"
    row = ",".join([f"val_{i+1}" for i in range(54)]) + "\n"
    rv = client.post(
        "/",
        content_type="multipart/form-data",
        data={"file": (BytesIO((header + row).encode()), "finder.csv")},
    )
    path = get_uploaded_path(rv)
    html = client.get(path)
    assert 'id="gotocol"' in html.get_data(as_text=True)

    responses = {
        "/" + path: html,
        "/csv/" + path + "?rb=0&cb=0": client.get("/csv/" + path, query_string={"rb": "0", "cb": "0"}),
        "/csv/" + path + "?rb=0&cb=1": client.get("/csv/" + path, query_string={"rb": "0", "cb": "1"}),
    }

    def serve(route):
        url = urlparse(route.request.url)
        response = responses.get(url.path + ("?" + url.query if url.query else ""), html)
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

    with sync_playwright() as playwright:
        with playwright.chromium.launch(
            channel="chromium",
            executable_path=os.environ.get("CHROMIUM_EXECUTABLE_PATH"),
        ) as browser:
            page = browser.new_page(viewport={"width": 600, "height": 400})
            page.set_default_timeout(5000)
            page.route("http://localhost/**", serve)
            page.goto("http://localhost/" + path)
            page.wait_for_function("document.querySelector('tbody td').textContent.length > 0")

            assert page.locator("thead .cell").first.text_content() == "A"

            col_input = page.locator("#gotocol")
            col_input.fill("AA")
            col_input.press("Enter")
            page.wait_for_function("document.querySelector('thead .cell').textContent === 'AA'")
            assert col_input.input_value() == "AA"

            col_input.fill("28")
            col_input.press("Enter")
            page.wait_for_function("document.querySelector('thead .cell').textContent === 'AB'")
            assert col_input.input_value() == "AB"

            col_input.fill("bb")
            col_input.press("Enter")
            page.wait_for_function("""() => {
                const cell = [...document.querySelectorAll('thead .cell')].find(c => c.textContent === 'BB');
                return cell && cell.style.visibility !== 'hidden';
            }""")
            assert col_input.input_value() == "BB"


def test_csv_index_is_reused_on_second_get(client, monkeypatch):
    fhost = sys.modules["fhost"]
    calls = {"n": 0}
    real = fhost._scan_csv_file

    def wrapped(fpath):
        calls["n"] += 1
        return real(fpath)

    monkeypatch.setattr(fhost, "_scan_csv_file", wrapped)
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a,b\n1,2\n"), "reuse.csv")},
    )
    path = get_uploaded_path(rv)
    client.get(path)
    client.get(path)
    assert calls["n"] == 1


def test_csv_truncated_index_rescans_instead_of_500(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a,b\n1,2\n"), "truncidx.csv")},
    )
    path = get_uploaded_path(rv)
    preview = client.get(path)
    assert preview.status_code == 200
    assert get_csv_cfg(preview.get_data(as_text=True))["rows"] == 2
    storage = Path(app.config["FHOST_STORAGE_PATH"])
    index = next(p for p in (storage / ".csv-index").iterdir() if p.is_file())
    index.write_bytes(index.read_bytes()[:10])
    preview = client.get(path)
    assert preview.status_code == 200
    assert get_csv_cfg(preview.get_data(as_text=True))["rows"] == 2
    tile = client.get("/csv/" + path, query_string={"rb": "0", "cb": "0"})
    assert tile.status_code == 200
    assert tile.get_json()["rows"][0] == ["a", "b"]


def test_csv_tile_does_not_load_whole_index(client, monkeypatch):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a,b\n1,2\n"), "seek.csv")},
    )
    path = get_uploaded_path(rv)
    client.get(path)

    def boom(self):
        raise AssertionError("whole index loaded")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert client.get(path).status_code == 200
    assert client.get("/csv/" + path, query_string={"rb": "0", "cb": "0"}).status_code == 200
    assert client.get("/csv/" + path, query_string={"r": "0", "c": "0"}).status_code == 200


def test_csv_tile_parses_quoted_fields_and_escapes(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(
            b'\xef\xbb\xbfname,note\n'
            b'Ada,"hello, world"\n'
            b'Bea,"line1\nline2"\n'
            b'Cal,"a""b"\n'
            b'\n'
            b'1\n'
            b'<b>,x\n'
            b'bad,\xff\n'
        ), "edges.csv")},
    )
    path = get_uploaded_path(rv)
    tile = client.get("/csv/" + path, query_string={"rb": "0", "cb": "0"})
    assert tile.status_code == 200
    assert tile.mimetype == "application/json"
    assert tile.headers["X-Content-Type-Options"] == "nosniff"
    rows = tile.get_json()["rows"]
    assert rows[0] == ["name", "note"]
    assert rows[1] == ["Ada", "hello, world"]
    assert rows[2] == ["Bea", "line1\nline2"]
    assert rows[3] == ["Cal", 'a"b']
    assert rows[4] == []
    assert rows[5] == ["1"]
    assert rows[6] == ["<b>", "x"]
    assert rows[7] == ["bad", "\ufffd"]


def test_csv_tile_straddling_quoted_newline_at_block_boundary(client):
    lines = [b"h\n"]
    # rows 1..199 ordinary; row 200 (0-based) starts with a quoted newline so block 1 must resume mid-record
    lines.extend(b"%d\n" % i for i in range(1, 200))
    lines.append(b'"a\nb"\n')
    payload = b"".join(lines)
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(payload), "span.csv")},
    )
    path = get_uploaded_path(rv)
    assert get_csv_cfg(client.get(path).get_data(as_text=True))["rows"] == 201
    tile = client.get("/csv/" + path, query_string={"rb": "1", "cb": "0"})
    assert tile.get_json()["rows"] == [["a\nb"]]


def test_csv_cell_returns_full_text_tile_truncates_in_middle(client):
    cell = "begin_" + "x" * 60 + "_employees"
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(("a\n" + cell + "\n").encode()), "long.csv")},
    )
    path = get_uploaded_path(rv)
    tile = client.get("/csv/" + path, query_string={"rb": "0", "cb": "0"}).get_json()
    assert tile["rows"][1] == ["begin_" + "x" * 18 + "…" + "x" * 15 + "_employees"]
    full = client.get("/csv/" + path, query_string={"r": "1", "c": "0"}).get_json()
    assert full["text"] == cell


def test_csv_tile_bounds_and_bad_params(client):
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a,b\n1,2\n"), "bounds.csv")},
    )
    path = get_uploaded_path(rv)
    client.get(path)
    base = "/csv/" + path
    assert client.get(base, query_string={"rb": "1", "cb": "0"}).get_json() == {"rows": []}
    assert client.get(base, query_string={"rb": "0", "cb": "1"}).get_json() == {"rows": [[], []]}
    assert client.get(base, query_string={"r": "9", "c": "0"}).get_json() == {"text": ""}
    assert client.get(base, query_string={"r": "0", "c": "9"}).get_json() == {"text": ""}
    assert client.get(base, query_string={"rb": "0"}).status_code == 400
    assert client.get(base, query_string={"rb": "-1", "cb": "0"}).status_code == 400
    assert client.get(base).status_code == 400


def test_csv_data_rejects_non_csv_and_removed(client):
    txt = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"hello\n"), "plain.txt")},
    )
    assert client.get("/csv/" + get_uploaded_path(txt), query_string={"rb": "0", "cb": "0"}).status_code == 404

    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a\n"), "gone.csv")},
    )
    path = get_uploaded_path(rv)
    link = path[:-4]
    f = File.query.filter_by(link=link).first()
    f.removed = True
    db.session.commit()
    assert client.get("/csv/" + path, query_string={"rb": "0", "cb": "0"}).status_code == 451


def test_cleanup_skips_csv_index_dir_and_missing_dir(client):
    import cleanup as cleanup_mod
    storage = app.config["FHOST_STORAGE_PATH"]
    Path(storage, ".csv-index").mkdir()
    cleanup_mod.cleanup_expired(storage, app.config["MAX_CONTENT_LENGTH"])
    assert Path(storage, ".csv-index").is_dir()
    import shutil
    shutil.rmtree(Path(storage, ".csv-index"))
    cleanup_mod.cleanup_expired(storage, app.config["MAX_CONTENT_LENGTH"])


def test_cleanup_deletes_orphan_csv_index(client):
    import cleanup as cleanup_mod
    storage = Path(app.config["FHOST_STORAGE_PATH"])
    index_dir = storage / ".csv-index"
    index_dir.mkdir()
    orphan = index_dir / "deadbeef"
    tmp = index_dir / "deadbeef.tmp-1-2"
    orphan.write_bytes(b"x")
    tmp.write_bytes(b"y")
    cleanup_mod.cleanup_expired(str(storage), app.config["MAX_CONTENT_LENGTH"])
    assert not orphan.exists()
    assert not tmp.exists()


def test_cleanup_keeps_index_for_live_csv_and_drops_it_after_expiry(client):
    import cleanup as cleanup_mod
    import time
    rv = client.post(
        "/",
        buffered=True,
        content_type="multipart/form-data",
        data={"file": (BytesIO(b"a,b\n"), "keep.csv")},
    )
    client.get(get_uploaded_path(rv))
    fhost = sys.modules["fhost"]
    storage = Path(app.config["FHOST_STORAGE_PATH"])
    sha = next(p.name for p in storage.iterdir() if p.is_file())
    index = storage / ".csv-index" / sha
    assert index.is_file()
    cleanup_mod.cleanup_expired(str(storage), app.config["MAX_CONTENT_LENGTH"])
    assert index.is_file()
    old = time.time() - 400 * 86400
    os.utime(storage / sha, (old, old))
    cleanup_mod.cleanup_expired(str(storage), app.config["MAX_CONTENT_LENGTH"])
    assert not (storage / sha).exists()
    assert not index.exists()


def test_prune_rows_deletes_expired_active_records_and_logs_audit(client):
    import cleanup as cleanup_mod
    from fhost import FileAudit

    # 1. Active file that has expired (file deleted on disk)
    rv1 = client.post("/", buffered=True, content_type="multipart/form-data", data={"file": (BytesIO(b"active-expired"), "f1.txt")})
    link1 = rv1.data.decode().removeprefix("https://localhost/").split(".")[0]
    with app.app_context():
        f1 = File.query.filter_by(link=link1).first()
        sha1 = f1.sha256
    storage_file1 = Path(app.config["FHOST_STORAGE_PATH"]) / sha1
    storage_file1.unlink()

    # 2. Active file that has NOT expired (file exists on disk)
    rv2 = client.post("/", buffered=True, content_type="multipart/form-data", data={"file": (BytesIO(b"active-not-expired"), "f2.txt")})
    link2 = rv2.data.decode().removeprefix("https://localhost/").split(".")[0]

    # 3. Moderated file that has expired (file deleted on disk) with IP set
    rv3 = client.post("/", buffered=True, content_type="multipart/form-data", data={"file": (BytesIO(b"moderated-expired"), "f3.txt")})
    link3 = rv3.data.decode().removeprefix("https://localhost/").split(".")[0]
    with app.app_context():
        f3 = File.query.filter_by(link=link3).first()
        f3.removed = True
        f3.addr = "192.168.1.100"
        sha3 = f3.sha256
        db.session.commit()
    storage_file3 = Path(app.config["FHOST_STORAGE_PATH"]) / sha3
    storage_file3.unlink()

    cleanup_mod.prune_rows(app.config["FHOST_STORAGE_PATH"])

    with app.app_context():
        # Verify f1 (active expired) was deleted
        assert File.query.filter_by(link=link1).first() is None
        audit1 = FileAudit.query.filter_by(sha256=sha1, action="PRUNED_EXPIRED").first()
        assert audit1 is not None
        assert audit1.link == link1

        # Verify f2 (active not expired) was kept
        assert File.query.filter_by(link=link2).first() is not None

        # Verify f3 (moderated expired) was kept but addr scrubbed to None
        f3_post = File.query.filter_by(link=link3).first()
        assert f3_post is not None
        assert f3_post.addr is None
        audit3 = FileAudit.query.filter_by(sha256=sha3, action="MODERATED_ADDR_SCRUBBED").first()
        assert audit3 is not None

        # Verify zero PII in audit table
        assert not hasattr(FileAudit, "addr")


def test_store_mixed_legacy_duplicates_451_not_bypassed(client):
    content = b"mixed-legacy-duplicate-content"
    # Seed active row (id=1) and removed row (id=2) with the exact same link and name
    rv1 = client.post("/", buffered=True, content_type="multipart/form-data", data={"file": (BytesIO(content), "mixed.txt")})
    assert rv1.status_code == 200
    link = rv1.data.decode().removeprefix("https://localhost/").split(".")[0]

    with app.app_context():
        # First row is active
        f1 = File.query.filter_by(link=link).first()
        assert f1.removed is False

        # Add second legacy row with different token, same link, but removed=True
        f2 = File(
            sha256=f1.sha256,
            token="leg_tok_mixed",
            ext=".txt",
            mime="text/plain",
            addr="10.0.0.99",
            original_name="mixed.txt",
            link=link
        )
        f2.removed = True
        db.session.add(f2)
        db.session.commit()

    # Attempt re-upload of the file. It must abort with 451, not return 200!
    rv_reupload = client.post("/", buffered=True, content_type="multipart/form-data", data={"file": (BytesIO(content), "mixed.txt")})
    assert rv_reupload.status_code == 451


def test_token_model_unique_flag():
    # Verify model token column has unique=True to match migrated database uq_file_token
    assert File.token.property.columns[0].unique is True


def test_legacy_redirect_respects_preferred_url_scheme(client):
    orig = app.config.get("PREFERRED_URL_SCHEME")
    try:
        app.config["PREFERRED_URL_SCHEME"] = "http"
        # Seed file
        rv = client.post("/", buffered=True, content_type="multipart/form-data", data={"file": (BytesIO(b"scheme-test"), "scheme.txt")})
        canonical_link = rv.data.decode().removeprefix("http://localhost/").split(".")[0]

        with app.app_context():
            f = File.query.filter_by(link=canonical_link).first()
            f.token = "tok_scheme01"
            db.session.commit()

        res = client.get("/tok_scheme01.txt?foo=bar")
        assert res.status_code == 301
        assert res.headers["Location"].startswith("http://localhost/")
        assert res.headers["Location"].endswith(f"/{canonical_link}.txt?foo=bar")
    finally:
        app.config["PREFERRED_URL_SCHEME"] = orig


def test_migration_fail_closed_without_secret():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migration_b2c",
        Path(__file__).parent.parent / "migrations/versions/b2c3d4e5f6a7_hmac_links_and_audit.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # In non-testing context with no secret, upgrade() must raise RuntimeError
    orig_secret = app.config.get("FHOST_LINK_SECRET")
    orig_env = os.environ.get("FHOST_LINK_SECRET")
    orig_testing = app.config.get("TESTING")
    try:
        app.config["FHOST_LINK_SECRET"] = None
        app.config["TESTING"] = False
        if "FHOST_LINK_SECRET" in os.environ:
            del os.environ["FHOST_LINK_SECRET"]

        with app.app_context():
            with pytest.raises(RuntimeError, match="FHOST_LINK_SECRET is required to backfill links"):
                mod.upgrade()
    finally:
        app.config["FHOST_LINK_SECRET"] = orig_secret
        if orig_env is not None:
            os.environ["FHOST_LINK_SECRET"] = orig_env
        app.config["TESTING"] = orig_testing



