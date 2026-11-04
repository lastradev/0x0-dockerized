FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
	build-essential \
	libmagic1 \
	libmagic-dev \
	libpq-dev \
	&& rm -rf /var/lib/apt/lists/* \
	&& (getent group app >/dev/null || groupadd --gid 1000 app) \
	&& (id -u app >/dev/null 2>&1 || useradd --uid 1000 --gid app --shell /bin/false --create-home app)

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

RUN chmod +x /app/docker-entrypoint.sh \
	&& chown -R app:app /app

EXPOSE 8080

# Fix bind-mounted upload directory ownership, run migrations as root, then switch to app user for uWSGI
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "flask db upgrade && uwsgi --http-socket 0.0.0.0:8080 --wsgi-file fhost.py --callable app --processes 4 --threads 2 --uid 1000 --gid 1000"]

