# Hermes : versions fixes et déploiement proche du Compose officiel

## Couple retenu le 9 octobre 2026

La sélection utilise un seuil de sept jours : aucune image publiée après le
**2 octobre 2026 à 14:05 UTC**. Les registres proposent les versions suivantes :

| Composant | Version | Publication de l'image | Digest multiarchitecture |
|---|---|---|---|
| Agent | `v2026.9.24` | 24 septembre | `sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7` |
| WebUI | `0.52.404` | 2 octobre | `sha256:005718b9dc5249f385ab30ccf968b00830791ea59379d26210cde085075f94a5` |

Il n'existe pas d'image Agent versionnée publiée exactement le 2 octobre dans
la liste du registre : le 24 septembre est la dernière disponible avant le seuil.
Le fichier [hermes-images.json](../releases/hermes-images.json) conserve les
révisions des sources, dates et architectures AMD64/ARM64. Les tags servent à
identifier les versions ; les Deployments utilisent exclusivement les digests.
La release WebUI correspond à `exp-v0.52.404` en amont : ce canal est expérimental,
même si son image porte le tag numérique fixe `0.52.404`.

Le `pyproject.toml` de la révision Agent `f97608f178d1ffeca59860195ab7da295f7c8e5f`
déclare `>=3.11,<3.14`, et les dépendances `python-dotenv`/`psutil` ne sont pas
conditionnées à Python 3.14. Le WebUI de la révision
`62e4d7b9e2560f9d49c0c29c2dc1414fbd4e2d12` utilise Python **3.12.15**.
Cette compatibilité déclarée doit être complétée par les tests des images et
un essai réel sur une instance avant généralisation.

Les images `latest` reproduites le 9 octobre échouaient aussi avec le Compose
officiel : `AIAgent` ne pouvait pas importer `dotenv`. Les dépendances du nouvel
Agent étaient conditionnées à Python >=3.14, alors que le WebUI utilisait 3.12.
Un défaut de droits sur `tools/.install.lock` bloquait également la copie Docker.

## Ce qui reste conforme au Compose

- Images officielles, entrypoints officiels et commande `gateway run`.
- Le WebUI conserve son Python et son installation native des dépendances.
  Aucun patch du script upstream, téléchargement forcé de Python ou venv partagé.
- Home, configuration, historique et workspace persistent sur leurs PVC.
- Les sources Agent sont séparées du home et montées en lecture seule côté WebUI.
- `API_SERVER_KEY` est transmis au WebUI comme `HERMES_WEBUI_GATEWAY_API_KEY`.
- Le mot de passe utilise `HERMES_WEBUI_PASSWORD`, selon l'authentification native.

Adaptations Kubernetes : un initContainer copie les sources depuis l'image
Agent dans un `emptyDir` neuf à chaque Pod, au lieu d'initialiser un volume Docker.
La copie inclut les dotfiles, exclut les venvs, caches, métadonnées de build et le
verrou transitoire `.install.lock`; elle attribue les sources à `1000:1000` et
retire les permissions d'écriture groupe/autres. Le WebUI reste démarré par
l'entrypoint officiel, qui copie ces sources dans son répertoire de build.

Les sondes vérifient l'import de `AIAgent` **sans ajout manuel à sys.path**, puis
`/health`. Un serveur HTTP sans Agent fonctionnel ne devient donc pas Ready.
La sonde de démarrage dispose d'environ 20 minutes; le Deployment a un délai
minimal de progression de 30 minutes. Le premier démarrage dépend encore de
l'accès aux registres et dépôts de paquets, comme le Compose officiel.

## Délai configurable et mises à jour

```bash
python3 scripts/hermes-release.py check
HERMES_RELEASE_DELAY_DAYS=14 python3 scripts/hermes-release.py check
```

Le défaut est sept jours depuis la publication de chaque image, pas sept jours
supplémentaires depuis son téléchargement. Les dates du fichier proviennent des
registres; ne les modifier que lors d'une nouvelle sélection vérifiée.
`HERMES_RELEASE_FILE` peut désigner un autre manifeste de versions fixes.
Dans Helm, le paramètre correspondant est `releaseDelayDays`.

Il n'y a **aucune mise à jour automatique** et aucun fallback vers `latest`.
Les Pods redémarrés réutilisent les mêmes digests. Augmenter le délai ne change
pas les Pods existants; cela peut empêcher une création/migration utilisant une
image trop récente pour la nouvelle politique.

Pour une mise à jour : sélectionner les deux images publiées avant le nouveau
seuil, relever leurs digests et révisions, vérifier les déclarations Python,
mettre à jour le manifeste et les valeurs Helm, puis exécuter la CI et un canary.
Si le canary échoue, conserver le dernier couple validé; sept jours ne constituent
pas une garantie de correction upstream.

## Vérifier les images

La CI exécute `scripts/test-hermes-images.sh` sur runners AMD64 et ARM64. Ce test
utilise les images officielles et leurs entrypoints intacts, monte des répertoires
isolés, ne publie aucun port, vérifie `AIAgent`, `/health`, l'exécution d'un shell
et la connexion au gateway. Il ne réalise pas d'appel payant à un LLM.

```bash
bash scripts/test-hermes-images.sh
```

La présence des images et leur compatibilité déclarée ont été vérifiées pendant
la préparation de cette PR. Le conteneur de travail ne possède ni Docker ni accès
au cluster : les tests des images doivent passer en CI, puis un chat réel doit
être vérifié sur le canary ARM64 avant de migrer toutes les instances.

## Mettre à jour le SaaS

Depuis la racine du dépôt, avec le bon contexte kubectl :

```bash
kubectl -n demo1 create configmap agents-saas-release \
  --from-file=hermes-images.json=releases/hermes-images.json \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n demo1 create configmap agents-saas-app \
  --from-file=app.py=app.py \
  --from-file=hermes_runtime.py=hermes_runtime.py \
  --from-file=release_policy.py=release_policy.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f manifests/01-rbac.yaml
kubectl apply -f manifests/03-deployment.yaml
kubectl -n demo1 rollout restart deployment/agents-saas
kubectl -n demo1 rollout status deployment/agents-saas --timeout=180s
```

Le SaaS monte le manifeste à `/etc/hermes-release/hermes-images.json`; changer
le ConfigMap ne migre pas les instances déjà créées. Les prochains déploiements
utilisent le même contrat que le script autonome et Helm.

## Réparer une instance existante, puis les autres

```bash
# Lecture seule : vérifier la sélection et les ressources cibles.
python3 scripts/repair-existing-hermes.py demo1 \
  --deployment agent-081d366d-abigot-agent

# Correction ciblée. Le rollout reste visible, sans attente silencieuse.
python3 scripts/repair-existing-hermes.py demo1 \
  --deployment agent-081d366d-abigot-agent --apply

python3 scripts/hermes-release.py validate --namespace demo1 \
  --deployment agent-081d366d-abigot-agent
```

Vérifier ensuite une conversation, un outil et l'accès au workspace dans le
navigateur. Quand le canary est validé :

```bash
python3 scripts/repair-existing-hermes.py demo1 --apply
```

La migration conserve les PVC, ressources, placement, sidecars et secrets.
Elle retire les anciens overrides Python et le `password-seed`; si ce dernier
contenait le seul mot de passe explicite, sa valeur/source est transmise au
WebUI natif. Elle ne supprime aucun ancien fichier du PVC.

Chaque Deployment reçoit une sauvegarde privée (`0700` pour le dossier, `0600`
pour les fichiers), un patch gardé par `resourceVersion` et une annotation de
réparation unique pour déclencher un nouveau rollout. Le script applique les
changements séquentiellement et restaure le template et le délai précédents
sur la première erreur, puis s'arrête. Les sauvegardes peuvent contenir des
secrets; elles sont exclues de Git. La stratégie `Recreate` interrompt brièvement
l'instance; la sauvegarde du Deployment ne remplace pas une sauvegarde du PVC.

## Helm

Le chart inclut les mêmes pins et scripts de copie/sonde. Il refuse une image
mutable, un digest différent du manifeste ou une image trop récente.
Pour synchroniser les valeurs avec un manifeste personnalisé :

```bash
python3 scripts/hermes-release.py helm-values > /tmp/hermes-pins.json
helm upgrade --install hermes charts/hermes-webui -n demo1 \
  -f /tmp/hermes-pins.json -f /path/to/instance-values.yaml \
  --wait --timeout 35m
```

Migrer les releases Helm avec Helm, pas avec le script réservé aux Deployments
`app=agent-instance`. Conserver les `existingClaim` actuels. Pour revenir à la
révision précédente : `helm rollback hermes <revision> -n demo1 --wait --timeout 35m`.

## Références officielles

- [Compose à deux conteneurs](https://github.com/nesquena/hermes-webui/blob/62e4d7b9e2560f9d49c0c29c2dc1414fbd4e2d12/docker-compose.two-container.yml)
- [Entrypoint WebUI sélectionné](https://github.com/nesquena/hermes-webui/blob/62e4d7b9e2560f9d49c0c29c2dc1414fbd4e2d12/docker_init.bash)
- [Dépendances Agent sélectionné](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/pyproject.toml)
- [Politique de compatibilité](https://github.com/nesquena/hermes-webui/blob/master/docs/docker.md#compatibility-policy-and-version-pinning)
- [Signalement Docker similaire #8036](https://github.com/nesquena/hermes-webui/issues/8036)
