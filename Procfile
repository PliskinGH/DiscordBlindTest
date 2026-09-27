release: python manage.py migrate --no-input
web: gunicorn discordblindtest.wsgi:application --bind 0.0.0.0:${PORT:-8000}
worker: python manage.py runbot
