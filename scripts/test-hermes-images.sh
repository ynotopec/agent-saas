#!/bin/bash
# Exercise the official entrypoints with the repository's exact image pair.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
mapfile -t images < <(PYTHONPATH="$REPO_DIR" python3 "$SCRIPT_DIR/hermes-release.py" check)
AGENT_IMAGE=${images[0]:?Missing fixed Agent image}
WEBUI_IMAGE=${images[1]:?Missing fixed WebUI image}
TEST_ROOT=$(mktemp -d)
TEST_PREFIX="hermes-check-$$"
cleanup() {
  result=$?
  if [ "$result" -ne 0 ]; then
    docker logs "$TEST_PREFIX-webui" || true
    docker logs "$TEST_PREFIX-agent" || true
  fi
  docker rm -f "$TEST_PREFIX-webui" "$TEST_PREFIX-agent" >/dev/null 2>&1 || true
  docker network rm "$TEST_PREFIX-net" >/dev/null 2>&1 || true
  # Container-owned files may require root cleanup; CI runners are disposable.
  rm -rf "$TEST_ROOT" 2>/dev/null || true
}
trap cleanup EXIT
mkdir -p "$TEST_ROOT/source" "$TEST_ROOT/home" "$TEST_ROOT/workspace"
PYTHONPATH="$REPO_DIR" python3 - "$TEST_ROOT" <<'PY'
from pathlib import Path
import sys
from hermes_runtime import COPY_SOURCE
Path(sys.argv[1], "copy-source.sh").write_text(COPY_SOURCE)
PY
docker pull "$AGENT_IMAGE"
docker pull "$WEBUI_IMAGE"
docker run --rm --entrypoint sh -u 0 \
  -v "$TEST_ROOT/source:/agent-src" \
  -v "$TEST_ROOT/copy-source.sh:/copy-source.sh:ro" \
  "$AGENT_IMAGE" /copy-source.sh
docker network create "$TEST_PREFIX-net" >/dev/null
# No entrypoint override: gateway run is exactly the official Compose command.
docker run -d --name "$TEST_PREFIX-agent" --network "$TEST_PREFIX-net" \
  -e HERMES_HOME=/home/hermes/.hermes -e HERMES_UID=1000 -e HERMES_GID=1000 \
  -e API_SERVER_ENABLED=true -e API_SERVER_HOST=0.0.0.0 \
  -e API_SERVER_KEY=hermes-ci-disposable-key \
  -v "$TEST_ROOT/home:/home/hermes/.hermes" \
  -v "$TEST_ROOT/workspace:/workspace" \
  "$AGENT_IMAGE" gateway run >/dev/null
# No entrypoint, CMD or Python override; the official WebUI performs installation.
docker run -d --name "$TEST_PREFIX-webui" --network "$TEST_PREFIX-net" \
  -e HERMES_WEBUI_HOST=0.0.0.0 -e HERMES_WEBUI_PORT=8787 \
  -e WANTED_UID=1000 -e WANTED_GID=1000 \
  -e HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui \
  -e HERMES_WEBUI_AGENT_DIR=/home/hermeswebui/.hermes/hermes-agent \
  -e HERMES_API_URL="http://$TEST_PREFIX-agent:8642" \
  -e HERMES_WEBUI_GATEWAY_API_KEY=hermes-ci-disposable-key \
  -e HERMES_WEBUI_PASSWORD=hermes-ci-disposable-password \
  -v "$TEST_ROOT/source:/home/hermeswebui/.hermes/hermes-agent:ro" \
  -v "$TEST_ROOT/home:/home/hermeswebui/.hermes" \
  -v "$TEST_ROOT/workspace:/workspace" \
  "$WEBUI_IMAGE" >/dev/null
PROBE=$(PYTHONPATH="$REPO_DIR" python3 -c 'from hermes_runtime import PROBE_SCRIPT; print(PROBE_SCRIPT)')
for attempt in $(seq 1 120); do
  if docker exec --user 1000 "$TEST_PREFIX-webui" /app/venv/bin/python -c "$PROBE" >"$TEST_ROOT/probe.log" 2>&1; then
    docker exec --user 1000 "$TEST_PREFIX-webui" /app/venv/bin/python -c \
      'import subprocess; assert subprocess.check_output(["/bin/sh", "-c", "printf hermes-check"], text=True) == "hermes-check"; print("AIAgent + health + shell OK")'
    docker exec --user 1000 "$TEST_PREFIX-webui" /app/venv/bin/python -c \
      'import socket,sys; s=socket.create_connection((sys.argv[1],8642),timeout=5); s.close(); print("Gateway reachable")' "$TEST_PREFIX-agent"
    exit 0
  fi
  test "$(docker inspect -f '{{.State.Running}}' "$TEST_PREFIX-webui")" = true || break
  if (( attempt % 12 == 0 )); then
    echo "Waiting for official WebUI startup (attempt $attempt/120)"
    tail -n 6 "$TEST_ROOT/probe.log"
  fi
  sleep 10
done
cat "$TEST_ROOT/probe.log"
exit 1
