set -eu
test -f /opt/hermes/run_agent.py
test -f /opt/hermes/pyproject.toml
mkdir -p /agent-src
# emptyDir is new on every Pod creation. Include dotfiles, omit caches and
# image-local venvs; never merge image sources into the persistent home.
tar -C /opt/hermes --exclude=.venv --exclude=venv --exclude=.git \
    --exclude=__pycache__ --exclude=.playwright --exclude='*.egg-info' \
    --exclude=build --exclude=dist --exclude=.install.lock -cf - . > /tmp/hermes-source.tar
tar -C /agent-src -xf /tmp/hermes-source.tar
rm /tmp/hermes-source.tar
chown -R 1000:1000 /agent-src
chmod -R u+rwX,go+rX,go-w /agent-src
test -f /agent-src/run_agent.py
