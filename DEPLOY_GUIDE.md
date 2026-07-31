# Installation complète d'Agents SaaS

Ce guide déploie le tableau de bord dans le namespace `demo1`. Le service crée ensuite les ressources de chaque instance dans ce même namespace.

## Prérequis

- Un cluster Kubernetes avec une StorageClass `microk8s-hostpath`.
- Un contrôleur Ingress dont la classe est `public`.
- cert-manager et un `ClusterIssuer` nommé `letsencrypt-prod` (le manifeste optionnel `06-clusterissuer.yaml` peut le créer).
- Un DNS wildcard `*.ailab.infocepo.com` pointant vers l'Ingress.
- `kubectl` configuré pour le cluster.

> Les noms de namespace, StorageClass, domaine, classe Ingress et ClusterIssuer sont actuellement définis dans `app.py` et les manifests. Adaptez-les ensemble avant le déploiement.

## 1. Créer le namespace et les secrets

Ne stockez jamais les vraies valeurs dans Git. Créez le Secret directement dans le cluster :

```bash
kubectl create namespace demo1 --dry-run=client -o yaml | kubectl apply -f -
kubectl -n demo1 create secret generic agents-saas-secrets \
  --from-literal=LLM_API_KEY='<llm-api-key>' \
  --from-literal=API_SERVER_KEY='<random-api-server-key>' \
  --from-literal=DEPLOY_TOKEN='<random-dashboard-token>' \
  --from-literal=HERMES_WEBUI_PASSWORD='<initial-instance-password>'
```

Générez des valeurs aléatoires longues pour `API_SERVER_KEY`, `DEPLOY_TOKEN` et `HERMES_WEBUI_PASSWORD`. Si une valeur précédemment publiée a été utilisée, révoquez-la et faites-en la rotation.

## 2. Installer les ressources

```bash
kubectl apply -f manifests/01-rbac.yaml
kubectl create configmap agents-saas-app \
  --from-file=app.py=app.py \
  --dry-run=client -o yaml | kubectl apply -f - -n demo1
kubectl apply -f manifests/03-deployment.yaml
kubectl apply -f manifests/04-service.yaml
kubectl apply -f manifests/05-ingress.yaml
```

Le `ClusterIssuer` est une ressource de portée cluster. Ne l'installez que si un objet du même nom n'existe pas déjà, et remplacez d'abord l'adresse e-mail :

```bash
kubectl get clusterissuer letsencrypt-prod || kubectl apply -f manifests/06-clusterissuer.yaml
```

## 3. Vérifier

```bash
kubectl -n demo1 rollout status deployment/agents-saas --timeout=180s
kubectl -n demo1 get pods,service,ingress
kubectl -n demo1 logs deployment/agents-saas -c install-pip-packages
```

Le premier démarrage télécharge les dépendances Python dans un `emptyDir`; il nécessite donc un accès au registre Python et peut être plus long.

## 4. Utiliser l'API

Les opérations qui modifient le cluster exigent le jeton `DEPLOY_TOKEN` dans l'en-tête `X-Deploy-Token` :

```bash
curl -X POST https://agents-saas.ailab.infocepo.com/api/deploy \
  -H 'Content-Type: application/json' \
  -H 'X-Deploy-Token: <random-dashboard-token>' \
  -d '{"subdomain":"my-agent"}'
```

Le formulaire Web demande le même jeton. L'endpoint `GET /health` reste public pour les probes Kubernetes.

## Mise à jour

Après une modification de `app.py`, recréez le ConfigMap puis redémarrez le Deployment :

```bash
kubectl create configmap agents-saas-app \
  --from-file=app.py=app.py \
  --dry-run=client -o yaml | kubectl apply -f - -n demo1
kubectl -n demo1 rollout restart deployment/agents-saas
kubectl -n demo1 rollout status deployment/agents-saas --timeout=180s
```
