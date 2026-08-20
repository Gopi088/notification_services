#!/usr/bin/env bash
# CLI for the Notification Service.
#
# Run it with no arguments and it will ask you everything step by step.
# The server is started automatically if it isn't already running.
#
#   ./cli.sh                     -> interactive mode (asks for parameters)
#   ./cli.sh -v                  -> interactive + show full API details
#   ./cli.sh send whatsapp 919812345678 "Hello"
#   ./cli.sh status <message_id>
#   ./cli.sh -v status <message_id>
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
PORT=8000
VERBOSE=false

usage() {
    cat <<'EOF'
Usage:
  ./cli.sh                     Ask for parameters step by step
  ./cli.sh -v                  Interactive mode with full details
  ./cli.sh send <whatsapp|sms|email> <number-or-email> "<message>"
  ./cli.sh status <message_id>
  ./cli.sh -v status <message_id>
EOF
}

server_up() {
    curl -s -m 2 "$BASE_URL/health" >/dev/null 2>&1
}

ensure_server() {
    if server_up; then
        return 0
    fi

    # Port occupied by a stuck/suspended process (e.g. a Ctrl+Z'd server)?
    # A suspended process still holds the port but never responds.
    if ! python3 -c "
import socket
s = socket.socket()
try:
    s.bind(('127.0.0.1', $PORT))
    free = True
except OSError:
    free = False
s.close()
print(free)
" | grep -q True; then
        echo "Port $PORT is occupied by a stuck process - killing it..."
        python3 -c "
import os

port = $PORT
hex_port = f'{port:04X}'
pids = set()
for pid in os.listdir('/proc'):
    if not pid.isdigit():
        continue
    try:
        with open(f'/proc/{pid}/net/tcp') as fh:
            for line in fh.readlines()[1:]:
                parts = line.split()
                if len(parts) > 3 and parts[3] == '0A':
                    local_port = parts[1].split(':')[1]
                    if local_port == hex_port:
                        pids.add(int(pid))
    except (OSError, ValueError):
        pass
for pid in pids:
    try:
        os.kill(pid, 9)
        print(f'killed pid {pid}')
    except (OSError, ProcessLookupError):
        pass
"
        sleep 2
    fi

    echo "Starting server at $BASE_URL..."
    if [ -x ./run.sh ]; then
        (./run.sh > /tmp/notification-service.log 2>&1 &)
    else
        (venv/bin/uvicorn app.main:app --reload --reload-dir app --host 127.0.0.1 --port "$PORT" > /tmp/notification-service.log 2>&1 &)
    fi
    for _ in $(seq 1 30); do
        sleep 1
        if server_up; then
            echo "Server ready at $BASE_URL"
            return 0
        fi
    done
    echo "Server failed to start. Check /tmp/notification-service.log" >&2
    return 1
}

json_build() {
    python3 -c 'import json, sys; print(json.dumps({"channel": sys.argv[1], "contact": sys.argv[2], "message": sys.argv[3]}))' "$1" "$2" "$3"
}

do_send() {
    local channel="$1" contact="$2" message="$3"
    local response
    response=$(curl -s -X POST "$BASE_URL/send" -H "Content-Type: application/json" \
        -d "$(json_build "$channel" "$contact" "$message")") || true
    if [ -z "$response" ]; then
        echo "Error: no response from $BASE_URL" >&2
        return 1
    fi
    local message_id
    message_id=$(echo "$response" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("message_id", ""))')
    if [ -z "$message_id" ]; then
        echo "Error: $response" >&2
        return 1
    fi
    echo "Queued for $channel -> $contact"
    echo "Message id  : $message_id"
    echo "Check status: ./cli.sh status $message_id"
    if [ "$VERBOSE" = true ]; then
        echo "--- full response ---"
        echo "$response" | python3 -m json.tool
    fi
}

do_status() {
    local message_id="$1"
    local response http_code body
    response=$(curl -s -w $'\n%{http_code}' "$BASE_URL/status/$message_id") || true
    body=$(echo "$response" | sed '$d')
    http_code=$(echo "$response" | tail -n1)
    if [ -z "$http_code" ] || [ "$http_code" = "000" ]; then
        echo "Error: server not reachable at $BASE_URL" >&2
        return 1
    fi
    if [ "$http_code" = "404" ]; then
        echo "Message $message_id not found." >&2
        return 1
    fi
    local status
    status=$(echo "$body" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("status", ""))')
    case "$status" in
    delivered) mark="DELIVERED" ;;
    failed)    mark="FAILED" ;;
    sent)      mark="SENT" ;;
    queued)    mark="QUEUED" ;;
    *)         mark="$status" ;;
    esac
    echo "Status: $mark"
    echo "$body" | python3 -c 'import json, sys; d = json.load(sys.stdin); print("channel      :", d.get("channel")); print("contact      :", d.get("contact")); print("provider     :", d.get("provider")); print("error        :", d.get("error")); print("updated at   :", d.get("updated_at"))'
    if [ "$VERBOSE" = true ]; then
        echo "--- full response ---"
        echo "$body" | python3 -m json.tool
    fi
}

interactive() {
    ensure_server || exit 1
    echo "Notification Service CLI"
    echo "------------------------"
    PS3="What do you want to do? (1=send 2=check status 3=quit) "
    options=("Send message" "Check message status" "Quit")
    select action in "${options[@]}"; do
        case "$REPLY" in
        1)
            read -rp "Channel (whatsapp/sms/email): " channel
            read -rp "Contact (phone or email): " contact
            read -rp "Message: " message
            do_send "$channel" "$contact" "$message"
            ;;
        2)
            read -rp "Message id: " mid
            do_status "$mid"
            ;;
        3)
            echo "Bye!"
            exit 0
            ;;
        *)
            # A bare message id pasted in also works
            if [[ "$REPLY" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
                do_status "$REPLY"
            else
                echo "Pick 1, 2 or 3 (or paste a message id)."
            fi
            ;;
        esac
        echo
    done
}

# parse args
args=()
while [ $# -gt 0 ]; do
    case "$1" in
    -v|--verbose) VERBOSE=true ;;
    -h|--help) usage; exit 0 ;;
    *) args+=("$1") ;;
    esac
    shift
done
set -- "${args[@]}"

cmd="${1:-}"
case "$cmd" in
send)
    channel="${2:-}"
    contact="${3:-}"
    message="${4:-}"
    if [ -z "$channel" ] || [ -z "$contact" ] || [ -z "$message" ]; then
        echo "Error: send needs <channel> <number-or-email> \"<message>\"" >&2
        usage
        exit 1
    fi
    ensure_server || exit 1
    do_send "$channel" "$contact" "$message"
    ;;
status)
    message_id="${2:-}"
    if [ -z "$message_id" ]; then
        echo "Error: status needs a <message_id>" >&2
        usage
        exit 1
    fi
    ensure_server || exit 1
    do_status "$message_id"
    ;;
"")
    interactive
    ;;
*)
    usage
    exit 1
    ;;
esac