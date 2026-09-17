#!/usr/bin/env bash
# Remove Kubernetes ServiceAccount credentials from already deployed Hermes pods.
set -euo pipefail

NAMESPACE="${1:-demo1}"
SELECTOR="app=agent-instance"

command -v kubectl >/dev/null 2>&1 || {
  echo "kubectl is required" >&2
  exit 1
}

mapfile -t deployments < <(
  kubectl -n "${NAMESPACE}" get deployments -l "${SELECTOR}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'
)

if ((${#deployments[@]} == 0)); then
  echo "No Hermes deployments found in namespace ${NAMESPACE}."
  exit 0
fi

echo "Disabling ServiceAccount token mounts on ${#deployments[@]} Hermes deployment(s)..."
for deployment in "${deployments[@]}"; do
  kubectl -n "${NAMESPACE}" patch deployment "${deployment}" --type=merge \
    -p '{"spec":{"template":{"spec":{"automountServiceAccountToken":false}}}}'
done

# Changing the pod template starts a rollout. Waiting here ensures that pods
# which still contain an already-issued token have actually disappeared.
for deployment in "${deployments[@]}"; do
  kubectl -n "${NAMESPACE}" rollout status "deployment/${deployment}" --timeout=5m
done

remaining="$({
  kubectl -n "${NAMESPACE}" get pods -l "${SELECTOR}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.volumes[*]}{.name}{" "}{end}{"\n"}{end}'
} | awk '$0 ~ /(^|[[:space:]])kube-api-access-/ { print $1 }')"

if [[ -n "${remaining}" ]]; then
  echo "The following pods still expose a Kubernetes API token:" >&2
  printf '  %s\n' ${remaining} >&2
  exit 1
fi

echo "All Hermes pods in ${NAMESPACE} are isolated from ServiceAccount credentials."
