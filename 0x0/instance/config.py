import os

_db_uri = os.getenv("DATABASE_URI")
if _db_uri and _db_uri.startswith("postgresql://"):
    try:
        import psycopg
    except ImportError:
        try:
            import psycopg2
            _db_uri = _db_uri.replace("postgresql://", "postgresql+psycopg2://", 1)
        except ImportError:
            pass

SQLALCHEMY_DATABASE_URI = _db_uri

if os.getenv("MAX_SIZE"):
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_SIZE")) * 1024 * 1024

FHOST_LINK_SECRET = os.getenv("FHOST_LINK_SECRET")

# Force HTTPS for generated URLs
PREFERRED_URL_SCHEME = "https"

# WSGI middleware to override request scheme for url_for
class ForceSchemeMiddleware:
    def __init__(self, app, scheme='https'):
        self.app = app
        self.scheme = scheme

    def __call__(self, environ, start_response):
        environ['wsgi.url_scheme'] = self.scheme
        return self.app(environ, start_response)
