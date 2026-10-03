#!/bin/sh
set -e
cd /app

# config.py is expected to be mounted into the container (see docker-compose.yml).
if [ -d config.py ]; then
	echo "ERROR: /app/config.py is a directory, not a file." >&2
	echo "The host path for the config.py mount did not exist when the container first started," >&2
	echo "so Docker created a directory there. Delete that directory, create the real config.py" >&2
	echo "file (copy config.py.example), and start the container again." >&2
	exit 1
fi

# If it isn't mounted, fall back to the example config (no extensions enabled).
if [ ! -f config.py ]; then
	echo "config.py not found, using config.py.example (no extensions enabled)."
	cp config.py.example config.py
fi

# Fail with a readable message instead of a bare traceback if the config is broken
if ! python3 -c "import ast,sys; ast.parse(open('config.py').read())" 2>/tmp/cfgerr; then
	echo "ERROR: config.py has a syntax error:" >&2
	tail -n 4 /tmp/cfgerr >&2
	exit 1
fi

exec python3 proxy.py --host 0.0.0.0 --port "${PORT:-5001}" "$@"
