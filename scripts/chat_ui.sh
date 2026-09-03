#!/usr/bin/env bash
#
# The chat UI, on localhost, without the API key ever touching a file.
#
#   ./scripts/chat_ui.sh up      start it   (http://localhost:3000)
#   ./scripts/chat_ui.sh down    stop it, keep the accounts and chats
#   ./scripts/chat_ui.sh reset   stop it and delete the volume
#   ./scripts/chat_ui.sh logs    follow the container
#
# The key is read out of MISTRAL_ENV_FILE at the moment compose runs and passed
# through the environment. It is never echoed and never written to disk.

set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="docker/chat/docker-compose.yml"
MISTRAL_ENV_FILE="${MISTRAL_ENV_FILE:-$HOME/vega-api/.env}"

read_key() {
    if [ -n "${MISTRAL_API_KEY:-}" ]; then
        return  # already in the environment; leave it alone
    fi
    if [ ! -r "$MISTRAL_ENV_FILE" ]; then
        echo "No MISTRAL_API_KEY set and cannot read $MISTRAL_ENV_FILE." >&2
        echo "Export MISTRAL_API_KEY, or point MISTRAL_ENV_FILE at the file." >&2
        exit 1
    fi
    MISTRAL_API_KEY="$(grep -m1 '^MISTRAL_API_KEY' "$MISTRAL_ENV_FILE" \
        | cut -d= -f2- | tr -d "\"' \r")"
    export MISTRAL_API_KEY
    if [ -z "$MISTRAL_API_KEY" ]; then
        echo "MISTRAL_API_KEY is empty in $MISTRAL_ENV_FILE." >&2
        exit 1
    fi
}

case "${1:-up}" in
    up)
        read_key
        docker compose -f "$COMPOSE_FILE" up -d
        printf '\nChat UI:  \033[1mhttp://localhost:3000\033[0m\n'
        printf 'Runlace:  http://host.docker.internal:8000/mcp (from inside the container)\n'
        printf '\nIf Runlace is not running yet:\n'
        printf '  runlace serve --http 8000 --host 0.0.0.0\n'
        ;;
    down)
        docker compose -f "$COMPOSE_FILE" down
        ;;
    reset)
        docker compose -f "$COMPOSE_FILE" down -v
        echo "Volume deleted. The next `up` starts from an empty install."
        ;;
    logs)
        docker compose -f "$COMPOSE_FILE" logs -f
        ;;
    *)
        echo "usage: $0 [up|down|reset|logs]" >&2
        exit 2
        ;;
esac
