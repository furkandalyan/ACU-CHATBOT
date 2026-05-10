#!/bin/sh
set -eu

if [ -n "${DB_HOST:-}" ]; then
  echo "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT:-5432}..."
  python - <<'PY'
import os
import socket
import time

host = os.environ.get("DB_HOST", "postgres")
port = int(os.environ.get("DB_PORT", "5432"))
deadline = time.time() + int(os.environ.get("DB_WAIT_TIMEOUT", "60"))

while True:
    try:
        with socket.create_connection((host, port), timeout=2):
            break
    except OSError:
        if time.time() > deadline:
            raise SystemExit(f"PostgreSQL is not reachable at {host}:{port}")
        time.sleep(1)
PY
fi

if [ "${DJANGO_MIGRATE:-1}" = "1" ]; then
  python manage.py migrate --noinput
fi

if [ "${DJANGO_COLLECTSTATIC:-1}" = "1" ]; then
  python manage.py collectstatic --noinput
fi

if [ "${DJANGO_SEED_MOCK_USERS:-1}" = "1" ]; then
  python manage.py seed_mock_users
fi

if [ "${DJANGO_CREATE_SUPERUSER:-1}" = "1" ]; then
  python manage.py shell <<'PY'
import os
from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "admin")
email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "admin@example.com")
password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "admin123")

if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username=username, email=email, password=password)
    print(f"Created demo superuser: {username}")
else:
    print(f"Demo superuser already exists: {username}")
PY
fi

exec "$@"
