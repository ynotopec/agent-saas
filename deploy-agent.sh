#!/bin/bash
# deploy-agent.sh - Create a new agent instance with all K8s resources
set -e

SUBDOMAIN="${1:?Usage: deploy-agent.sh <subdomain>}"
NAMESPACE="${2:-demo1}"
KUBECONFIG="${3:-/etc/kubernetes/admin.conf}"
DOMAIN_SUFFIX=".ailab.infocepo.com"
CLUSTER_ISSUER="letsencrypt-prod"
INGRESS_CLASS="public"

# Generate unique prefix
HASH=$(date +%s%N | sha256sum | cut -c1-8)
PREFIX="agent-${HASH}"
NAME="${PREFIX}-${SUBDOMAIN}"
DOMAIN="${NAME}.${SUBDOMAIN}${DOMAIN_SUFFIX}"

echo "=== Deploying ${NAME} ==="
echo "Domain: ${DOMAIN}"

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
      context_length: 262144
      base_url: http://10.10.0.2:8571/v1
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
      containers:
      - args:
          - |
            set -e
            echo "=== Hermes WebUI starting ==="
            echo "Agent source:"
            ls -la /home/hermeswebui/.hermes/hermes-agent/ 2>/dev/null | head -20 || echo "NOT FOUND"
            echo "Starting..."
            exec /hermeswebui_init.bash
        command:
        - /bin/bash
        - -c
        env:
        - name: HERMES_WEBUI_STATE_DIR
          value: /home/hermeswebui/.hermes/webui
        - name: HERMES_WEBUI_PORT
          value: "8080"
        - name: HERMES_WEBUI_HOST
          value: "0.0.0.0"
        - name: HERMES_WEBUI_WORKSPACE
          value: /workspace
        - name: HERMES_WEBUI_SKIP_ONBOARDING
          value: "1"
        - name: HERMES_HOME
          value: /home/hermeswebui/.hermes
        - name: HERMES_CONFIG
          value: /etc/hermes-config/config.yaml
        image: ghcr.io/nesquena/hermes-webui:latest
        imagePullPolicy: Always
        livenessProbe:
          httpGet:
            path: /
            port: 8080
            scheme: HTTP
          initialDelaySeconds: 30
          periodSeconds: 30
        name: webui
        ports:
        - containerPort: 8080
          name: http
          protocol: TCP
        readinessProbe:
          httpGet:
            path: /
            port: 8080
            scheme: HTTP
          initialDelaySeconds: 10
          periodSeconds: 10
        resources:
          limits:
            cpu: "4"
            memory: 4Gi
          requests:
            cpu: 500m
            memory: 1Gi
        securityContext:
          runAsGroup: 1024
          runAsNonRoot: true
          runAsUser: 1024
        volumeMounts:
        - mountPath: /home/hermeswebui/.hermes
          name: agent-data
        - mountPath: /workspace
          name: workspace-data
        - mountPath: /.hermes
          name: agent-data
        - mountPath: /etc/hermes-config
          name: hermes-config
          readOnly: true
      initContainers:
      - args:
          - |
            echo "=== Initializing WebUI data ==="
            mkdir -p /data
            cp -a /home/hermeswebui/* /data/ 2>/dev/null || true
            cp -a /home/hermeswebui/.bashrc /home/hermeswebui/.profile /data/ 2>/dev/null || true
            chown -R 1024:1024 /data
            echo "=== WebUI data ready ==="
        command:
        - sh
        - -c
        image: ghcr.io/nesquena/hermes-webui:latest
        imagePullPolicy: Always
        name: init-webui-data
        resources:
          limits:
            cpu: 200m
            memory: 512Mi
        securityContext:
          runAsUser: 0
        volumeMounts:
        - mountPath: /data
          name: agent-data
      - args:
          - |
            echo "=== Copying hermes-agent source ==="
            mkdir -p /data/hermes-agent
            cd /opt/hermes
            if command -v rsync >/dev/null 2>&1; then
              rsync -a . /data/hermes-agent/
            else
              cp -a . /data/hermes-agent/
            fi
            chown -R 1024:1024 /data
            echo "=== Agent source copied ==="
        command:
        - sh
        - -c
        image: nousresearch/hermes-agent:latest
        imagePullPolicy: Always
        name: copy-agent-src
        resources:
          limits:
            cpu: 500m
            memory: 1Gi
        securityContext:
          runAsUser: 0
        volumeMounts:
        - mountPath: /data
          name: agent-data
      - name: copy-hermes-config
        image: alpine:3.19
        command:
        - sh
        - -c
        - |
          cat /etc/hermes-config/config.yaml > /data/config.yaml &&
          echo "Config copied to /data/config.yaml"
        securityContext:
          runAsUser: 0
        volumeMounts:
        - name: hermes-config
          mountPath: /etc/hermes-config
          readOnly: true
        - name: agent-data
          mountPath: /data
      restartPolicy: Always
      securityContext: {}
      terminationGracePeriodSeconds: 30
      volumes:
      - name: hermes-config
        configMap:
          name: ${NAME}-config
      - name: agent-data
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
    targetPort: 8080
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
