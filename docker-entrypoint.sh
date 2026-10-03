#!/bin/sh
set -e
cd /app

# config.py is expected to be mounted into the container (see docker-compose.yml).
# If it isn't, fall back to the example config (no extensions enabled).
if [ ! -f config.py ]; then
	echo "config.py not found, using config.py.example (no extensions enabled)."
	cp config.py.example config.py
fi

exec python3 proxy.py --host 0.0.0.0 --port "${PORT:-5001}" "$@"
