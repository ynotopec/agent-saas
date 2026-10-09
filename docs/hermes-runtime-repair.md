# Corriger le runtime Hermes des instances existantes et futures

## Pourquoi `pip install python-dotenv` ne suffit pas

La WebUI officielle utilise une image de base Python 3.12. Les sources récentes
de Hermes Agent conditionnent leurs dépendances principales à Python >= 3.14.
L'installation du package peut donc réussir sous 3.12 sans installer `dotenv`,
alors que `from run_agent import AIAgent` échoue. L'acceptation de Python >= 3.11
dans les métadonnées de l'agent sert à la migration de l'ancien updater, pas à
définir un runtime d'exécution supporté.

Le contrat de ce dépôt utilise Python **3.14.7** (configurable par version patch
exacte), un couple d'images figé par digest, et vérifie l'import d'`AIAgent`
avant le démarrage du serveur. Une image WebUI dont le script de démarrage ne
correspond plus au contrat attendu échoue explicitement. Les scripts du runtime
sont versionnés dans un ConfigMap immutable pour conserver la possibilité de
revenir au template précédent.

## 1. Choisir et vérifier le couple d'images

Choisissez des versions Agent/WebUI compatibles. Un digest fixe une image ; il
ne prouve pas à lui seul que deux images sont compatibles.

Avec Docker, récupérez les références après avoir téléchargé les versions choisies :

```bash
docker pull nousresearch/hermes-agent:<version-agent>
docker pull ghcr.io/nesquena/hermes-webui:<version-webui>
docker image inspect --format '{{index .RepoDigests 0}}' nousresearch/hermes-agent:<version-agent>
docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/nesquena/hermes-webui:<version-webui>
```

Remplacez les valeurs ci-dessous par les références complètes retournées :

```bash
export HERMES_AGENT_IMAGE='nousresearch/hermes-agent@sha256:<digest-agent>'
export HERMES_WEBUI_IMAGE='ghcr.io/nesquena/hermes-webui@sha256:<digest-webui>'
export HERMES_WEBUI_PYTHON='3.14.7'
```

Les commandes refusent une référence vide, un tag seul ou `latest` avant toute
création de ressources. La WebUI conserve son installation de dépendances par
l'entrypoint officiel, mais crée son venv avec la version Python explicite.
Le premier démarrage nécessite l'accès aux téléchargements Python de `uv`, aux
registres des dépendances et, selon les extras, à GitHub. Pour un environnement
sans Internet, préparez une image dérivée et ses caches avant le déploiement.

Le workflow GitHub **Hermes runtime regression checks** peut être lancé
manuellement avec les deux digests. Il démarre un vrai conteneur WebUI, vérifie
le Python du venv, l'import d'`AIAgent` et `/health`. Ce smoke test est AMD64 ;
faites également un canari sur le cluster ARM64 avant de migrer toutes les instances.

## 2. Configurer les futures instances créées par le SaaS

```bash
kubectl -n demo1 create configmap agents-saas-runtime \
  --from-literal=HERMES_AGENT_IMAGE="$HERMES_AGENT_IMAGE" \
  --from-literal=HERMES_WEBUI_IMAGE="$HERMES_WEBUI_IMAGE" \
  --from-literal=HERMES_WEBUI_PYTHON="$HERMES_WEBUI_PYTHON" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f manifests/01-rbac.yaml
kubectl -n demo1 create configmap agents-saas-app \
  --from-file=app.py=app.py \
  --from-file=hermes_runtime.py=hermes_runtime.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f manifests/03-deployment.yaml
kubectl -n demo1 rollout restart deployment/agents-saas
kubectl -n demo1 rollout status deployment/agents-saas --timeout=180s
```

Le Secret `agents-saas-secrets` doit déjà contenir `DEPLOY_TOKEN`,
`HERMES_WEBUI_PASSWORD`, `API_SERVER_KEY` (au moins 16 caractères) et `LLM_API_KEY`.
Chaque nouvelle instance reçoit son Secret de runtime et son ConfigMap de scripts.
Le ServiceAccount du SaaS peut créer ces Secrets, mais n'obtient pas de nouveaux
droits pour lire les Secrets du namespace. Les pods Hermes ne montent aucun token
Kubernetes.

`deploy-agent.sh` utilise le même générateur que `app.py` : exportez les variables
de runtime et les credentials habituels, puis lancez le script depuis le dépôt.

## 3. Migrer les Deployments existants

Le script cible les Deployments portant le label `app=agent-instance`. Il ne
supprime aucun PVC et ne modifie pas les sessions, la configuration utilisateur,
le workspace ni les références aux PVC. Il conserve les sidecars supplémentaires,
les ressources et le placement du pod. Les anciennes sources présentes dans le
PVC restent sur disque, mais sont masquées par un `emptyDir` dédié et monté en
lecture seule côté WebUI.

Commencez par le plan, sans modification du cluster :

```bash
python3 scripts/repair-existing-hermes.py demo1
```

Puis choisissez une instance canari :

```bash
python3 scripts/repair-existing-hermes.py demo1 \
  --deployment agent-081d366d-abigot-agent --apply
```

Après validation du chat et des outils sur ce canari :

```bash
python3 scripts/repair-existing-hermes.py demo1 --apply
```

Avant la première modification, le script valide tous les targets et sauvegarde
les Deployments dans `hermes-runtime-backups/<date>/` (dossier 0700, fichiers
0600, exclus de Git). **Ces sauvegardes peuvent contenir des credentials**.
Il applique ensuite les migrations une à une, avec une garde `resourceVersion`
contre une modification concurrente. Un rollout échoué restaure le template
précédent de l'instance concernée puis arrête la migration ; les autres instances
ne sont pas modifiées. Les instances déjà migrées avec succès restent migrées.

Le passage à `Recreate` entraîne une interruption par instance. Le budget du
startup probe est de 20 minutes et celui du rollout de 25 minutes. L'import de
l'agent et `/health` doivent réussir pour accepter du trafic.

La migration conserve les réglages canoniques existants. Elle ne recopie pas le
hash de l'ancien seed `.hermes/webui/settings.json` : ce hash utilisait un format
et un salt différents de l'authentification upstream actuelle. Si une ancienne
instance ne possède aucun mot de passe valide, définissez-en un via l'API
`/api/change-password` protégée par `DEPLOY_TOKEN` après la migration. Le mot de
passe géré par le SaaS est ensuite porté par un Secret, transmis à la WebUI et
hashé par son implémentation native ; il a priorité sur le mot de passe des
Settings. Les autres réglages ne sont pas modifiés. Un échec du patch de rollout
est remonté au client au lieu d'être ignoré.

## 4. Helm : installations et releases existantes

Le chart est un déploiement WebUI autonome, pas une stack gateway à trois
conteneurs. Son runtime corrigé est activé par défaut et nécessite les deux
digests :

```yaml
image:
  repository: ghcr.io/nesquena/hermes-webui
  digest: sha256:<digest-webui>
agentImage:
  repository: nousresearch/hermes-agent
  digest: sha256:<digest-agent>
runtime:
  enabled: true
  pythonVersion: "3.14.7"
```

Pour une release existante, faites `helm upgrade` avec les values et références
aux PVC de cette release, après sauvegarde des values et du manifeste :

```bash
helm get values <release> -n <namespace> -a > values-before.yaml
helm get manifest <release> -n <namespace> > manifest-before.yaml
helm upgrade <release> charts/hermes-webui -n <namespace> \
  -f <values-de-la-release> -f <values-runtime> --atomic --timeout 25m
```

Protégez ces sauvegardes : elles peuvent contenir des secrets. Le script de
migration des Deployments SaaS refuse le chart autonome ; utilisez Helm pour
préserver l'identité et l'historique de la release. `runtime.enabled=false`
conserve le comportement historique pour un rollback explicite, sans protection
contre l'incompatibilité Python.

## Validation locale

```bash
pip install pytest fastapi httpx pyyaml argon2-cffi trafilatura
python3 -m pytest -q
bash -n deploy-agent.sh
```

Installez Helm pour exécuter également les tests de rendu du chart. Les tests
couvrent la préservation des PVC/configs/settings, la migration idempotente,
l'arrêt et la restauration après échec, les digests obligatoires, les mauvaises
versions Python, les imports d'agent manquants, le rendu Helm et la synchronisation
des scripts embarqués.
