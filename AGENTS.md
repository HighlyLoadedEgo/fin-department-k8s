# AGENTS.md

GitOps-репо одного k3s-кластера `fin-cluster` (mortypython.ru, VIP
`77.91.112.72`, диск 50GB SSD, single-node). Только манифесты, без
исходников приложений. Паттерны унаследованы от `tmst-k8s`.

## Порядок запуска (bootstrap)

### 0. Подготовка сервера (k3s)

```bash
# Longhorn требует iscsi:
apt install -y open-iscsi && systemctl enable --now iscsid

# k3s без traefik и servicelb (у нас Istio + MetalLB):
curl -sfL https://get.k3s.io | sh -s - server \
  --disable traefik --disable servicelb --write-kubeconfig-mode 644
```

50GB диск: следи за `df -h` и `kubectl top node`. Longhorn
`storageOverProvisioningPercentage: 200` — scheduled может превышать физику.

### 1. DNS

Публичные A-записи `→ 77.91.112.72` (нужны ДО push: LE HTTP-01 и
`wait: true` у KS `infrastructure`):

- `registry.mortypython.ru` (Harbor, дан)
- `grafana.mortypython.ru`, `headlamp.mortypython.ru`, `weave.mortypython.ru`
- `s3.mortypython.ru`, `console-s3.mortypython.ru`
- `jaeger.mortypython.ru`

Внутренний (на клиентах через /etc/hosts → `77.91.112.72`):
`vault.mortypython.local` — серт self-signed CA `fin-department Root CA`
(`kubectl -n cert-manager get secret ca-key-pair -o jsonpath='{.data.tls\.crt}' | base64 -d` → импортировать в браузер/ОС).

### 2. Flux

```bash
flux bootstrap git --url=ssh://git@<repo-url>/fin-department-k8s.git \
  --branch main --path clusters/fin-cluster
```

Bootstrap создаёт `clusters/fin-cluster/flux-system/gotk-components.yaml` и
`gotk-sync.yaml` (не править руками; `gotk-helm.yaml` — рукописный).

### 3. Vault (до того, как ESO начнёт синк — иначе KS `infrastructure` встанет на wait)

Vault поднимется sealed и без kubernetes-auth — синк ExternalSecret будет
ошибочным, это ожидаемо на первом круге. Далее:

```bash
kubectl -n vault-system exec -it vault-0 -- sh
vault operator init -key-shares=1 -key-threshold=1   # ключи сохранить!
vault operator unseal <key>
export VAULT_TOKEN=<initial root token>

vault auth enable kubernetes

# ⚠️ В свежем Vault kv-v2 на secret/ НЕ создан по умолчанию:
vault secrets enable -path=secret -version=2 kv

# ⚠️ disable_local_ca_jwt=true отключает авто-CA → надо отдать k3s CA явно,
# иначе TokenReview падает с "x509: certificate signed by unknown authority":
kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > /tmp/k3s-ca.crt
kubectl -n vault-system cp /tmp/k3s-ca.crt vault-0:/tmp/k3s-ca.crt
vault write auth/kubernetes/config \
  kubernetes_host=https://kubernetes.default.svc \
  kubernetes_ca_cert=@/tmp/k3s-ca.crt \
  disable_local_ca_jwt=true
vault policy write eso - <<'EOF'
path "secret/data/*"     { capabilities = ["read"] }
path "secret/metadata/*" { capabilities = ["read"] }
EOF
vault write auth/kubernetes/role/eso-role \
  bound_service_account_names=external-secrets \
  bound_service_account_namespaces=external-secrets \
  policies=eso \
  ttl=1h
```

⚠️ Роль `eso-role` требует, чтобы SA ESO мог делать TokenReview — биндинг
`system:auth-delegator` для SA external-secrets уже в манифестах
(`external-secrets/auth-delegator.yaml`). Без него login падает голым
"permission denied". Диагностика: `vault write sys/loggers level=debug` →
спровоцировать логин → `kubectl -n vault-system logs vault-0 | grep "login
unauthorized"` → `vault write sys/loggers level=info`.

После фиксов: `kubectl annotate clustersecretstore vault-backend
force-sync=$(date +%s) --overwrite` (ESO кэширует InvalidProviderConfig).

### 4. Сиды секретов в Vault

```bash
# Yandex Cloud S3 (статический ключ из консоли Yandex Cloud:
# Object Storage → {bucket} → Service accounts / или IAM → Service account → ключи)
vault kv put secret/s3/yandex \
  accessKey='<YCAJE...>' secretKey='<YCP...>'

# Harbor
vault kv put secret/database/harbor password='<strong>'
vault kv put secret/harbor/admin admin-password='<strong>'
vault kv put secret/harbor/secret-key secretKey='<ровно 16 символов>'

# Grafana (логин admin)
vault kv put secret/grafana/admin admin-password='<strong>'
```

Бакеты создаются руками в Yandex Console: `harbor-mortypython`, `mortypython-loki-logs`,
`mortypython-cnpg-backups`. После синка CNPG сделает immediate-бэкап, Loki начнёт писать чанки.

## Tracing / OTel

- Istio шлёт трейсы (sampling 5%) в Jaeger через zipkin-провайдера
  (`jaeger-collector.jaeger.svc:9411`) — см. `istio-system/istiod.yaml`.
- Приложения: OTLP → `jaeger-collector.jaeger.svc.cluster.local:4317` (gRPC)
  / `:4318` (HTTP).
- OTel Operator: `Instrumentation` / `OpenTelemetryCollector` CR в
  ns приложений (оператор в `opentelemetry-system`).
- Jaeger storage — badger на PVC `jaeger-badger` (5Gi longhorn), TTL 7d.

## Приоритеты (single-node)

`fin-critical` (Vault, CNPG) > `fin-platform` (gateway, MinIO, Loki)
> `fin-apps` > `fin-batch`.

## Conventions

- `${VAR}` — из `clusters/fin-cluster/cluster-settings.yaml`
  (Flux postBuild.substituteFrom). Не хардкодить.
- Version-pinned HelmRelease; chart должен быть в одном из `helm/*.yaml`.
- Проверка перед коммитом: `kubectl kustomize infrastructure/base/controllers`.
- Секреты в git не класть; всё через Vault → ExternalSecret (`creationPolicy: Owner`).
- metallb: CRD ставит сам chart (`crds.enabled: true`) — conversion webhook;
  сырые CRD в `crds/` не класть.

## apps: hackaton-fin-department (бэкенд)

- CI (репо `hackaton-fin-department`, ветка main): Quality → build & push
  `registry.mortypython.ru/fin/hackaton-fin-department:main-<UTC ts14>-<sha12>`
  (GitHub vars/secrets: `HARBOR_USERNAME`, `HARBOR_TOKEN` — robot account
  Harbor с push в project `fin`).
- CD: `ImageRepository`+`ImagePolicy` (numerical по ts) → `ImageUpdateAutomation`
  коммитит тег в `apps/hackaton-fin-department/deployment.yaml` → KS `apps`.
- БД: CNPG `infra-db`, роль `${HACKATON_FIN_DB_USER}`, DB `${HACKATON_FIN_DB}`,
  пароль Vault `database/hackaton-fin`. Миграции — initContainer `make migrate`.
- UI/API: `api.mortypython.ru` (SAN в `fin-tls`), DNS → 77.91.112.72.
- Vault: `vault kv put secret/database/hackaton-fin password='...'`,
  `vault kv put secret/harbor/robot-ci username='robot$fin+ci' password='...'`.

### Включить image-контроллеры (один раз)

Deploy key от bootstrap read-only, а IAU нужен push — ребутстрапни с токеном:

```bash
flux bootstrap github --owner=HighlyLoadedEgo --repository=fin-department-k8s \
  --branch=main --path=clusters/fin-cluster --personal \
  --components-extra=image-reflector,image-automation --token-auth
# GITHUB_TOKEN=<PAT со scope repo> перед командой
```
