# agent-saas

Ce dépôt fournit un socle Kubernetes/Helm pour exposer [nesquena/hermes-webui](https://github.com/nesquena/hermes-webui) en mode SaaS, avec stockage persistant et déploiement par namespace tenant.

## Architecture proposée

- **Une release Helm par tenant** dans son namespace Kubernetes.
- **Un `Deployment` mono-réplica** en stratégie `Recreate` pour éviter deux writers simultanés sur les volumes `ReadWriteOnce`.
- **Deux volumes persistants** :
  - `/home/hermeswebui/.hermes` pour la configuration, les sessions et l'état Hermes/WebUI.
  - `/workspace` pour les fichiers de travail du tenant.
- **Un `Service` `ClusterIP`** nommé comme la release Helm.
- **Ingress optionnel** : désactivé par défaut pour s'intégrer à un Ingress existant. Activez-le uniquement si vous souhaitez que le chart crée l'objet `Ingress`.
- **Authentification WebUI** via `HERMES_WEBUI_PASSWORD`, idéalement injecté depuis un `Secret` existant.

## Pré-requis

- Kubernetes avec un provisioner de `PersistentVolume` disponible.
- Helm 3.
- Un contrôleur Ingress déjà installé si vous exposez l'application en HTTP(S).
- Un `Secret` contenant le mot de passe WebUI si l'application est exposée.

## Déploiement dans un namespace tenant

```bash
kubectl create namespace tenant-acme
kubectl -n tenant-acme create secret generic hermes-webui-auth \
  --from-literal=HERMES_WEBUI_PASSWORD='change-me-strong-password'
helm upgrade --install hermes-acme ./charts/hermes-webui \
  --namespace tenant-acme \
  -f examples/values-tenant.yaml
```

Le fichier `examples/values-tenant.yaml` garde `ingress.enabled=false` pour le cas demandé où l'Ingress existe déjà. Pointez cet Ingress vers le service créé par Helm :

```yaml
backend:
  service:
    name: hermes-acme-hermes-webui
    port:
      name: http
```

## Créer un Ingress depuis le chart si nécessaire

Si vous n'avez finalement pas d'objet Ingress applicatif et souhaitez que Helm le crée, utilisez `examples/values-create-ingress.yaml` comme base :

```bash
helm upgrade --install hermes-acme ./charts/hermes-webui \
  --namespace tenant-acme \
  -f examples/values-create-ingress.yaml
```

## Personnalisation rapide

Les valeurs principales sont dans [`charts/hermes-webui/values.yaml`](charts/hermes-webui/values.yaml) :

- `image.repository` / `image.tag` pour choisir l'image Hermes WebUI.
- `persistentStorage.*.size` et `persistentStorage.*.storageClass` pour dimensionner les PVC.
- `auth.existingSecret` pour utiliser un secret géré hors Helm.
- `hermesConfig.openAICompatibleEndpoint.*` pour déclarer directement une URL OpenAI-compatible sans utiliser de provider Hermes `custom`.
- `hermesConfig.seed.*` pour précharger un `config.yaml` Hermes incluant un endpoint/modèle initial.
- `extraEnvFrom` pour injecter des clés provider depuis des Secrets Kubernetes.
- `env.HERMES_WEBUI_SKIP_ONBOARDING=1` pour éviter que le wizard de première connexion écrive un provider différent de celui préchargé par Helm.
- `ingress.enabled`, `ingress.className`, `ingress.hosts` et `ingress.tls` si le chart doit créer l'Ingress.


## Endpoint OpenAI-compatible

Hermes lit le modèle et l'URL API depuis `config.yaml` dans `HERMES_HOME`. Le chart définit explicitement `HERMES_CONFIG_PATH=/home/hermeswebui/.hermes/config.yaml` et persiste ce répertoire dans le PVC `hermesHome`.

Pour déclarer un endpoint OpenAI-compatible dès le déploiement, utilisez `examples/values-custom-model.yaml`. Cette configuration ne dépend plus d'un provider Hermes nommé `custom` : elle garde un provider connu (`openai-api` par défaut) et renseigne `model.base_url`, car Hermes route l'appel directement vers `base_url` quand ce champ est présent.

```yaml
hermesConfig:
  openAICompatibleEndpoint:
    enabled: true
    provider: openai-api
    model: qwen2.5-coder:32b
    baseUrl: http://ollama.ollama.svc.cluster.local:11434/v1
    apiKey: local
    contextLength: 64000
  seed:
    enabled: true
```

Le `config.yaml` généré contient seulement le bloc `model` attendu par Hermes, par exemple `provider: openai-api` + `base_url: http://.../v1`. Il n'écrit plus `custom_providers` et n'utilise plus `provider: custom:<name>`, car cette version d'Hermes/WebUI peut ne pas connaître ce provider.

Vous pouvez aussi fournir vous-même un `config.yaml` complet via `hermesConfig.seed.config` ou un Secret existant via `hermesConfig.seed.existingSecret`. Le seed est copié dans le PVC avant le démarrage :

- `overwrite=false` conserve un `config.yaml` déjà présent, ce qui évite d'écraser les modifications faites depuis Hermes WebUI ou `hermes model`.
- `overwrite=true` force la recopie de `config.yaml` au prochain `helm upgrade`; utilisez-le temporairement si un PVC contient déjà une configuration incomplète ou une ancienne tentative avec `provider: custom`.
- `envConfig` permet aussi de précharger `.env`; avec `envOverwrite=true`, vous pouvez remplacer une ancienne `.env` qui contient par exemple `OPENAI_API_KEY` alors que le tenant doit utiliser uniquement l'endpoint défini par `base_url`.

```bash
helm upgrade --install hermes-acme ./charts/hermes-webui \
  --namespace tenant-acme \
  -f examples/values-custom-model.yaml
```

Si vous utilisez encore une image ancienne, redéployez avec le tag charté `0.51.185` ou plus récent : les tags GHCR de Hermes WebUI n'utilisent pas le préfixe `v`, et les versions récentes corrigent l'affichage des modèles/providers définis dans `config.yaml`.

> Note: si le wizard WebUI ne liste pas de provider `Custom`, c'est précisément pour cela que le chart n'utilise plus `custom_providers`. Le point important est que le modèle actif possède une `base_url` OpenAI-compatible; le provider reste une valeur connue par Hermes.

Pour corriger un PVC déjà initialisé avec OpenAI ou `custom` par erreur, faites un upgrade ponctuel avec `hermesConfig.seed.overwrite=true` et, si une ancienne `.env` contient `OPENAI_API_KEY`, `hermesConfig.seed.envOverwrite=true` avec un `envConfig` sans clé OpenAI. Remettez ensuite ces deux options à `false`.

## Exploitation

```bash
kubectl -n tenant-acme get pods,pvc,svc
kubectl -n tenant-acme logs deploy/hermes-acme-hermes-webui
kubectl -n tenant-acme rollout status deploy/hermes-acme-hermes-webui
```

Pour supprimer l'application en gardant les données, supprimez la release Helm mais conservez les PVC ou configurez la politique de rétention de votre StorageClass selon vos besoins.
