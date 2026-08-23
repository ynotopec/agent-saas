#!/bin/bash
# deploy-agent.sh - Create a new agent instance with all K8s resources
set -eu

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

# Create Deployment
cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${NAME}
  namespace: ${NAMESPACE}
  labels:
    app: agent-instance
    agent-instance: '${SUBDOMAIN}'
    agent-hash: '${HASH}'
spec:
  replicas: 1
  selector:
    matchLabels:
      app: agent-instance
      agent-instance: '${SUBDOMAIN}'
  strategy:
    type: Recreate
  template:
    metadata:
      labels:
        app: agent-instance
        agent-instance: '${SUBDOMAIN}'
        agent-hash: '${HASH}'
    spec:
      securityContext:
        fsGroup: 1000
        runAsGroup: 1000
        runAsUser: 1000
      initContainers:
      - name: init-hermes-home
        image: alpine:3.19
        command: ["sh", "-c"]
        args: ["mkdir -p /hermes-home/.hermes/webui /workspace && echo '# Hermes workspace' > /workspace/.gitkeep && if [ -f /configmap/config.yaml ]; then cp /configmap/config.yaml /hermes-home/config.yaml && echo 'config.yaml copied to PVC root'; fi && echo '=== Init done ==='"]
        resources: {limits: {cpu: "50m", memory: 64Mi}}
        securityContext: {runAsUser: 0}
        volumeMounts:
        - mountPath: /hermes-home
          name: hermes-home
        - mountPath: /workspace
          name: workspace-data
        - mountPath: /configmap
          name: hermes-config
          readOnly: true
      - name: init-agent-src
        image: nousresearch/hermes-agent:latest
        imagePullPolicy: Always
        command: ["sh", "-c"]
        args: ["mkdir -p /hermes-home/hermes-agent && if [ -d /opt/hermes ]; then cp -r /opt/hermes/* /hermes-home/hermes-agent/ 2>/dev/null || true; echo 'hermes-agent source copied to PVC'; ls -la /hermes-home/hermes-agent/ | head -10; fi && chown -R 1000:1000 /hermes-home 2>/dev/null || true && echo '=== Init done ==='"]
        resources: {limits: {cpu: "200m", memory: 1Gi}}
        securityContext: {runAsUser: 0}
        volumeMounts:
        - mountPath: /hermes-home
          name: hermes-home
      containers:
      - name: hermes-agent
        image: nousresearch/hermes-agent:latest
        imagePullPolicy: Always
        command: ["sh", "-c"]
        args: ["hermes gateway run --no-supervise --force"]
        ports:
        - containerPort: 8642
          name: gateway
          protocol: TCP
        env:
        - name: HERMES_HOME
          value: /home/hermes/.hermes
        - name: HERMES_UID
          value: "1000"
        - name: HERMES_GID
          value: "1000"
        - name: LLM_BASE_URL
          value: https://api-nothink.ailab.infocepo.com/v1
        - name: LLM_API_KEY
          value: "${LLM_API_KEY}"
        - name: LLM_PROVIDER
          value: infocepo-alias
        - name: LLM_MODEL
          value: ai-thinking
        - name: HERMES_ALLOW_ROOT_GATEWAY
          value: "1"
        - name: HERMES_ACCEPT_HOOKS
          value: "1"
        - name: HERMES_DONT_CHECK_TTY
          value: "1"
        - name: HERMES_GATEWAY_NO_SUPERVISE
          value: "1"
        - name: MCP_DISABLE
          value: "1"
        - name: HERMES_CONFIG
          value: /etc/hermes-config/config.yaml
        - name: API_SERVER_KEY
          value: "${API_SERVER_KEY}"
        readinessProbe:
          exec:
            command: ["sh", "-c", "python3 -c 'import socket; s=socket.socket(); s.settimeout(2); r=s.connect_ex((\"127.0.0.1\",8642)); s.close(); exit(0 if r==0 else 1)'"]
          initialDelaySeconds: 30
          periodSeconds: 10
          failureThreshold: 30
          successThreshold: 1
          timeoutSeconds: 5
        resources:
          limits: {cpu: "2", memory: 4Gi}
          requests: {cpu: 25m, memory: 128Mi}
        securityContext: {runAsUser: 1000, runAsGroup: 1000}
        volumeMounts:
        - mountPath: /home/hermes/.hermes
          name: hermes-home
        - mountPath: /workspace
          name: workspace-data
        - mountPath: /etc/hermes-config
          name: hermes-config
          readOnly: true
      - name: hermes-dashboard
        image: nousresearch/hermes-agent:latest
        imagePullPolicy: Always
        command: ["hermes", "dashboard", "--host", "127.0.0.1"]
        ports:
        - containerPort: 9119
          name: dashboard
          protocol: TCP
        env:
        - name: HERMES_HOME
          value: /home/hermes/.hermes
        - name: HERMES_UID
          value: "1000"
        - name: HERMES_GID
          value: "1000"
        - name: GATEWAY_HEALTH_URL
          value: http://127.0.0.1:8642
        - name: HERMES_CONFIG
          value: /etc/hermes-config/config.yaml
        - name: PATH
          value: /opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
        readinessProbe:
          exec:
            command: ["sh", "-c", "python3 -c 'import socket; s=socket.socket(); s.settimeout(2); r=s.connect_ex((\"127.0.0.1\",9119)); s.close(); exit(0 if r==0 else 1)'"]
          initialDelaySeconds: 45
          periodSeconds: 15
          failureThreshold: 3
          successThreshold: 1
          timeoutSeconds: 10
        livenessProbe:
          exec:
            command: ["sh", "-c", "python3 -c 'import socket; s=socket.socket(); s.settimeout(2); r=s.connect_ex((\"127.0.0.1\",9119)); s.close(); exit(0 if r==0 else 1)'"]
          initialDelaySeconds: 60
          periodSeconds: 30
          failureThreshold: 3
          successThreshold: 1
          timeoutSeconds: 10
        resources:
          limits: {cpu: "500m", memory: 512Mi}
          requests: {cpu: 25m, memory: 64Mi}
        securityContext: {runAsUser: 1000, runAsGroup: 1000}
        volumeMounts:
        - mountPath: /home/hermes/.hermes
          name: hermes-home
        - mountPath: /etc/hermes-config
          name: hermes-config
          readOnly: true
      - name: hermes-webui
        image: ghcr.io/nesquena/hermes-webui:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 8787
          name: webui
          protocol: TCP
        env:
        - name: HERMES_WEBUI_HOST
          value: 0.0.0.0
        - name: HERMES_WEBUI_PORT
          value: "8787"
        - name: HERMES_WEBUI_STATE_DIR
          value: /home/hermeswebui/.hermes/webui
        - name: HERMES_API_URL
          value: http://127.0.0.1:8642
        - name: HERMES_HOME
          value: /home/hermeswebui/.hermes
        - name: HERMES_CONFIG
          value: /etc/hermes-config/config.yaml
        - name: PYTHONPATH
          value: /home/hermeswebui/.hermes/hermes-agent
        - name: HERMES_NIX_BUILD
          value: "1"
        - name: WANTED_UID
          value: "1000"
        - name: WANTED_GID
          value: "1000"
        - name: HERMES_WEBUI_ONBOARDING_OPEN
          value: "1"
        - name: HERMES_WEBUI_PASSWORD
          valueFrom:
            secretKeyRef:
              name: "${NAME}-webui-secret"
              key: password
        readinessProbe:
          httpGet:
            path: /
            port: 8787
            scheme: HTTP
          initialDelaySeconds: 30
          periodSeconds: 15
          failureThreshold: 3
          successThreshold: 1
          timeoutSeconds: 10
        livenessProbe:
          httpGet:
            path: /
            port: 8787
            scheme: HTTP
          initialDelaySeconds: 60
          periodSeconds: 30
          failureThreshold: 3
          successThreshold: 1
          timeoutSeconds: 10
        resources:
          limits: {cpu: "500m", memory: 2Gi}
          requests: {cpu: 25m, memory: 128Mi}
        securityContext: {runAsUser: 0, runAsGroup: 0}
        volumeMounts:
        - mountPath: /home/hermeswebui/.hermes
          name: hermes-home
        - mountPath: /workspace
          name: workspace-data
        - mountPath: /etc/hermes-config
          name: hermes-config
          readOnly: true
      restartPolicy: Always
      terminationGracePeriodSeconds: 30
      volumes:
      - name: hermes-config
        configMap:
          name: ${NAME}-config
      - name: hermes-home
        persistentVolumeClaim:
          claimName: ${NAME}-data
      - name: workspace-data
        persistentVolumeClaim:
          claimName: ${NAME}-workspace
EOF

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
