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
- `hermesConfig.seed.*` pour précharger un `config.yaml` Hermes incluant un endpoint/modèle initial.
- `extraEnvFrom` pour injecter des clés provider depuis des Secrets Kubernetes.
- `env.HERMES_WEBUI_SKIP_ONBOARDING=1` pour éviter que le wizard de première connexion écrive un provider différent de celui préchargé par Helm.
- `ingress.enabled`, `ingress.className`, `ingress.hosts` et `ingress.tls` si le chart doit créer l'Ingress.


## Configuration Hermes initiale

Hermes lit sa configuration depuis `config.yaml` dans `HERMES_HOME`. Le chart définit explicitement `HERMES_CONFIG_PATH=/home/hermeswebui/.hermes/config.yaml` et persiste ce répertoire dans le PVC `hermesHome`.

Vous pouvez fournir un `config.yaml` complet via `hermesConfig.seed.config` ou un Secret existant via `hermesConfig.seed.existingSecret`. Le seed est copié dans le PVC avant le démarrage :

- `overwrite=false` conserve un `config.yaml` déjà présent, ce qui évite d'écraser les modifications faites depuis Hermes WebUI ou `hermes model`.
- `overwrite=true` force la recopie de `config.yaml` au prochain `helm upgrade`; utilisez-le temporairement si un PVC contient déjà une configuration incomplète.
- `envConfig` permet aussi de précharger `.env`; avec `envOverwrite=true`, vous pouvez remplacer une ancienne `.env`.

## Exploitation

```bash
kubectl -n tenant-acme get pods,pvc,svc
kubectl -n tenant-acme logs deploy/hermes-acme-hermes-webui
kubectl -n tenant-acme rollout status deploy/hermes-acme-hermes-webui
```

Pour supprimer l'application en gardant les données, supprimez la release Helm mais conservez les PVC ou configurez la politique de rétention de votre StorageClass selon vos besoins.
