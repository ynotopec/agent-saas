#!/bin/bash
# deploy-agent.sh - Create a new agent instance with all K8s resources
set -euo pipefail

SUBDOMAIN="${1:?Usage: deploy-agent.sh <subdomain>}"
NAMESPACE="${2:-demo1}"
KUBECONFIG="${3:-/etc/kubernetes/admin.conf}"
DOMAIN_SUFFIX=".ailab.infocepo.com"
CLUSTER_ISSUER="letsencrypt-prod"
INGRESS_CLASS="public"

if ! [[ "$SUBDOMAIN" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || [[ "$SUBDOMAIN" == *--* ]]; then
  echo "Invalid subdomain: use 1-63 lowercase letters, digits, or single hyphens" >&2
  exit 2
fi
: "${LLM_API_KEY:?Set LLM_API_KEY in the environment}"
: "${API_SERVER_KEY:?Set API_SERVER_KEY in the environment}"
: "${HERMES_WEBUI_PASSWORD:?Set HERMES_WEBUI_PASSWORD in the environment}"
if [ "${#API_SERVER_KEY}" -lt 16 ]; then
  echo "API_SERVER_KEY must contain at least 16 characters" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Validate the immutable release pair before touching the cluster.
PYTHONPATH="$SCRIPT_DIR" python3 -c 'from hermes_runtime import runtime_settings; runtime_settings()'

# Generate unique prefix
HASH=$(date +%s%N | sha256sum | cut -c1-8)
PREFIX="agent-${HASH}"
NAME="${PREFIX}-${SUBDOMAIN}"
DOMAIN="${SUBDOMAIN}.ailab.infocepo.com"

echo "=== Deploying ${NAME} ==="
echo "Domain: ${DOMAIN}"

# Create the per-instance WebUI secret without writing credentials to the repository.
kubectl --kubeconfig="${KUBECONFIG}" -n "${NAMESPACE}" create secret generic "${NAME}-webui-secret" \
  --from-literal=password="${HERMES_WEBUI_PASSWORD}" \
  --from-literal=API_SERVER_KEY="${API_SERVER_KEY}" \
  --dry-run=client -o yaml | kubectl --kubeconfig="${KUBECONFIG}" apply -f -

# Create PVCs
cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${NAME}-data
  namespace: ${NAMESPACE}
  labels:
    app: agent-instance
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: microk8s-hostpath
  resources:
    requests:
      storage: 20Gi
  volumeMode: Filesystem
EOF

cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${NAME}-workspace
  namespace: ${NAMESPACE}
  labels:
    app: agent-instance
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: microk8s-hostpath
  resources:
    requests:
      storage: 20Gi
  volumeMode: Filesystem
EOF

# Create ConfigMap
cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${NAME}-config
  namespace: ${NAMESPACE}
  labels:
    app: agent-instance
data:
  config.yaml: |
    model:
      default: ai-thinking
      provider: custom
      context_length: ${LLM_CONTEXT_LENGTH:-262144}
      base_url: https://api-nothink.ailab.infocepo.com/v1
      api_key: ${LLM_API_KEY}
    providers:
      ai-nothink:
        name: ai-nothink
        type: openai
        api_url: https://api-nothink.ailab.infocepo.com/v1
        api_key: ${LLM_API_KEY}
    fallback_providers: []
    toolsets:
      - hermes-cli
    web:
      search_backend: ddgs
      extract_backend: trafilatura
EOF

# Render the deployment and runtime ConfigMap using the shared contract.
python3 "$SCRIPT_DIR/scripts/render-hermes-deployment.py" "$NAME" "$NAMESPACE" "$SUBDOMAIN" "$HASH" |
  kubectl --kubeconfig="${KUBECONFIG}" apply -f -

# Create Service
cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: v1
kind: Service
metadata:
  name: ${NAME}-svc
  namespace: ${NAMESPACE}
  labels:
    app: agent-instance
spec:
  type: ClusterIP
  ports:
  - port: 80
    targetPort: 8787
    protocol: TCP
    name: http
  selector:
    app: agent-instance
    agent-instance: '${SUBDOMAIN}'
EOF

# Create Ingress
cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${NAME}-ingress
  namespace: ${NAMESPACE}
  annotations:
    cert-manager.io/cluster-issuer: ${CLUSTER_ISSUER}
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  ingressClassName: ${INGRESS_CLASS}
  tls:
  - hosts:
    - ${DOMAIN}
    secretName: ${NAME}-tls
  rules:
  - host: ${DOMAIN}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: ${NAME}-svc
            port:
              number: 80
EOF

echo "✅ ${NAME} deployed at https://${DOMAIN}"
