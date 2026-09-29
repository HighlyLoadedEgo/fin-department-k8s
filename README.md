# fin-department-k8s

GitOps (Flux) одного single-node k3s-кластера `fin-cluster`:
домен `mortypython.ru`, VIP `77.91.112.72`, диск 50GB SSD.
Только манифесты, без исходников приложений. Паттерны унаследованы
от `tmst-k8s`.

Runbook (bootstrap сервера, Vault unseal/auth, DNS, сиды секретов) —
[`AGENTS.md`](./AGENTS.md).

## Layout

```
clusters/fin-cluster/            # Flux entrypoint (flux bootstrap --path ...)
  flux-system/                   #   gotk-манифесты (генерирует bootstrap)
  cluster-settings.yaml          #   ${VAR} для postBuild.substitute
  infrastructure-crds.yaml       #   KS: CRD-пакеты
  infrastructure.yaml            #   KS: контроллеры (dependsOn: flux-helm, crds)
  apps.yaml                      #   KS: приложения (dependsOn: infrastructure)
helm/                            # HelmRepository sources
infrastructure/base/crds/        # сырые CRD (cert-manager, cnpg, istio, ESO,
                                 #   prometheus-operator, barman-cloud)
infrastructure/base/controllers/ # один KS на весь кластер, ns = компонент
apps/                            # приложения + image-automation
```

Цепочка синка: `flux-system → {flux-helm, infrastructure-crds} → infrastructure → apps`.
У `infrastructure` и `apps` включены `wait: true` — DNS A-записи публичных
хостов должны существовать **до** push (LE HTTP-01 и helm-пробы).

## Компоненты

| Namespace | Что | Chart / версия |
|---|---|---|
| `metallb-system` | MetalLB, пул `77.91.112.72`. VIP = IP ноды (L2 ARP в облаке режется) → ingressgateway ловит трафик через `spec.externalIPs` | 0.16.1 |
| `longhorn` | storage, нужен `open-iscsi` на ноде. `storageOverProvisioningPercentage: 200` — scheduled может превышать физику | 1.12.0 |
| `istio-system` | istiod + ingressgateway (`fin-gw`, LoadBalancer на VIP). TLS: LE для публичных хостов, self-signed CA для `vault.mortypython.local` | istiod base |
| `cert-manager` | ClusterIssuer `letsencrypt` (HTTP-01) + `ca-issuer` | v1.21.0 |
| `external-secrets` | ESO → Vault (`ClusterSecretStore/vault-backend`), биндинг `system:auth-delegator` для TokenReview | 2.8.0 |
| `vault-system` | HashiCorp Vault (raft на longhorn, UI на `vault.mortypython.local`) | локальный chart |
| `cnpg-system` | CloudNativePG operator + barman-cloud plugin | 0.29.0 |
| `postgres` | кластер CNPG `infra-db` + Database CR (`harbor-db`, `hackaton-fin-db`), бэкапы → Yandex S3, PodMonitor метрик | — |
| `harbor` | registry.mortypython.ru (PG на infra-db, redis internal, S3 → Yandex Object Storage) | 1.17.4 |
| `monitoring` | kube-prometheus-stack + Grafana + 2 дашборда (configmap → sidecar: «Grafana Alerting», «Игровая аналитика») | 87.21.0 |
| `jaeger` | Jaeger v2, storage badger на PVC 5Gi, TTL 7d, UI на jaeger.mortypython.ru | 4.13.1 |
| `opentelemetry-system` | OTel Operator (CR `Instrumentation` / `OpenTelemetryCollector` в ns приложений) | 0.123.0 |
| `headlamp` | Kubernetes UI (вход SA-token) | 0.43.0 |
| `weave` | Weave GitOps UI (admin-пароль из секрета) | weave-gitops (GitRepository, v0.38.0) |

Auth без Keycloak: Grafana — admin-пароль из Vault, Headlamp —
`kubectl -n headlamp create token headlamp`, Weave — admin-пароль
(секрет `weave-gitops-admin-auth` в ns `weave`).

## Трафик

```
интернет → 77.91.112.72 (externalIPs) → istio ingressgateway (fin-gw)
  → VirtualService по хосту → Service приложения
```

- публичные хосты (`grafana`, `jaeger`, `headlamp`, `weave`, `registry`,
  `otel`, `fin-api.mortypython.ru`) — серт LE, DNS A → VIP; `otel` —
  публичный приём трейсов OTLP/HTTP (`POST /v1/traces` → jaeger `:4318`);
- `vault.mortypython.local` — только через `/etc/hosts` → VIP, серт
  self-signed CA `fin-department Root CA` (секрет `cert-manager/ca-key-pair`,
  импортировать в браузер/ОС).

## Секреты

Единственный источник — Vault (`secret/kv-v2`). Цепочка:

```
Vault → ClusterSecretStore vault-backend → ExternalSecret (creationPolicy: Owner) → Secret
```

- auth: kubernetes-auth, роль `eso-role` (SA `external-secrets`, read
  `secret/data/*`), k3s CA отдан явно (`disable_local_ca_jwt=true`);
- в git секретов нет: ключи Yandex S3, пароли Harbor/Grafana/БД,
  robot-аккаунты — всё через ExternalSecret.

## PostgreSQL (CNPG)

Operator `cnpg-system`, кластер `infra-db` в ns `postgres`. Базы создаются
CR `Database` (`harbor-db`, `hackaton-fin-db`, `databaseReclaimPolicy: retain`),
пользователи/пароли — Vault → ExternalSecret. Бэкапы: barman-cloud → Yandex S3
(`mortypython-cnpg-backups`), после синка — immediate-бэкап. Метрики —
PodMonitor (порт `metrics`, 9187).

## Приложение hackaton-fin-department (CI/CD)

```
CI (репо hackaton-fin-department): push в main
  → Quality (ruff / ty / pytest / behave)
  → build & push в Harbor
    registry.mortypython.ru/fin/hackaton-fin-department:main-<UTC ts14>-<sha12>

CD (это репо):
  ImageRepository (secretRef harbor-registry, robot pull)
  → ImagePolicy (filterTags main-<ts14>-*, numerical asc)
  → ImageUpdateAutomation: Setters по ./apps, коммит тега в
    apps/hackaton-fin-department/deployment.yaml (marker {"$imagepolicy": ...})
  → Flux перекатывает Deployment, миграции — initContainer `make migrate`
```

Ручных шагов деплоя нет: смержил PR в `main` → новая версия в кластере.
Robot-аккаунты Harbor: `fin+ci` (push, секреты CI), pull-секрет
`harbor-registry` для подов.

## Наблюдаемость

Метрики (Prometheus):

- kube-prometheus-stack подхватывает PodMonitor'ы без лейблов release
  (`podMonitorSelectorNilUsesHelmValues: false`); ServiceMonitor'ы — только
  с лейблом release;
- приложения отдают `/metrics` (OTel SDK + Prometheus reader), сбор —
  PodMonitor (пример: `apps/hackaton-fin-department/pod-monitor.yaml`);
- алерты — PrometheusRule → Grafana Alerting; retention 3d, 8Gi longhorn.

Трейсы (Jaeger):

- Istio: sampling 5%, zipkin → `jaeger-collector.jaeger.svc:9411`
  (`istio-system/istiod.yaml`);
- приложения: OTel SDK, OTLP gRPC `jaeger-collector.jaeger.svc:4317`
  (HTTP `:4318`), env `OTEL_ENDPOINT` в deployment;
- внешний приём — `otel.mortypython.ru` (SAN в `fin-tls`, VS `otel-ingest`
  → jaeger `:4318`) — для мобильного приложения;
- недоступный коллектор приложению не мешает — экспортер только логирует;
- OTel Operator (`opentelemetry-system`) — для `Instrumentation` CR
  в ns приложений, если нужна auto-instrumentation.

## Приоритеты (single-node)

`fin-critical` (Vault, CNPG) > `fin-platform` (ingressgateway,
платформенные сервисы) > `fin-apps` > `fin-batch` (CI/Jobs) —
описания в `infrastructure/base/controllers/priority-classes/`.

## Conventions

- `${VAR}` — из `clusters/fin-cluster/cluster-settings.yaml`
  (Flux postBuild.substituteFrom). Не хардкодить.
- Version-pinned HelmRelease; chart-репо должен быть в одном из `helm/*.yaml`.
- MetalLB: CRD ставит сам chart (`crds.enabled: true`, conversion webhook) —
  сырых CRD в `crds/` не класть.
- Секреты в git не класть — только Vault → ExternalSecret.
- Проверка перед коммитом: `kubectl kustomize infrastructure/base/controllers`.

## Validate

```bash
kubectl kustomize infrastructure/base/crds
kubectl kustomize infrastructure/base/controllers
kubectl kustomize helm
kubectl kustomize apps
```
