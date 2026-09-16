# AGENTS.md

GitOps-репо одного k3s-кластера `fin-cluster` (mortypython.ru, VIP
`158.160.183.114`, диск 50GB SSD, single-node). Только манифесты, без
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

Публичные A-записи `→ 158.160.183.114` (нужны ДО push: LE HTTP-01 и
`wait: true` у KS `infrastructure`):

- `registry.mortypython.ru` (Harbor, дан)
- `grafana.mortypython.ru`, `headlamp.mortypython.ru`, `weave.mortypython.ru`
- `s3.mortypython.ru`, `console-s3.mortypython.ru`
- `jaeger.mortypython.ru`

Внутренний (на клиентах через /etc/hosts → `158.160.183.114`):
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
vault write auth/kubernetes/config \
  kubernetes_host=https://kubernetes.default.svc \
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
