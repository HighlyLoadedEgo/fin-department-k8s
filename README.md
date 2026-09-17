# fin-department-k8s

GitOps (Flux) для одного k3s-кластера (`fin-cluster`, домен `mortypython.ru`,
MetalLB VIP `77.91.112.72`).

За основу взят `tmst-k8s` (структура, контроллеры, паттерн Vault → ESO → Secret).

## Layout

- `clusters/fin-cluster/` — Flux entrypoint (`flux bootstrap ... --path clusters/fin-cluster`)
- `helm/` — HelmRepository sources
- `infrastructure/base/crds/` — CRD-источники (KS `infrastructure-crds`)
- `infrastructure/base/controllers/` — все контроллеры (KS `infrastructure`, один на кластер)

Цепочка: `flux-system → {flux-helm, infrastructure-crds} → infrastructure`.

## Компоненты

| Namespace | Что |
|---|---|
| `metallb-system` | MetalLB, пул `77.91.112.72` |
| `longhorn` | storage (нужен open-iscsi на ноде!) |
| `istio-system` | istiod + ingressgateway (LoadBalancer на VIP) |
| `cert-manager` | LE (публичные хосты) + CA-issuer (vault.mortypython.local) |
| `external-secrets` | ESO → Vault (`ClusterSecretStore/vault-backend`) |
| `vault-system` | HashiCorp Vault (raft на longhorn, UI на vault.mortypython.local) |
| `cnpg-system` | CloudNativePG operator + barman-cloud plugin |
| `postgres` | кластер `infra-db` (harbor DB), бэкапы → Yandex S3 |
| `harbor` | registry.mortypython.ru (PG на infra-db, redis internal, S3 на Yandex Object Storage) |
| `monitoring` | kube-prometheus-stack + Grafana |
| `loki` | Loki + Alloy (логи → Yandex S3) |
| `jaeger` | Jaeger v2 (badger на PVC), UI на jaeger.mortypython.ru |
| `opentelemetry-system` | OTel Operator (Instrumentation / OpenTelemetryCollector CR) |
| `headlamp` | Kubernetes UI (вход SA-token) |
| `weave` | Weave GitOps UI (admin-пароль из секрета) |

Auth без Keycloak: Grafana — admin-пароль из Vault, Headlamp — SA token
(`kubectl -n headlamp create token headlamp`), Weave — admin-пароль
(секрет `weave-gitops-admin-auth` в ns `weave`).

## Validate

```bash
kubectl kustomize infrastructure/base/crds
kubectl kustomize infrastructure/base/controllers
kubectl kustomize helm
```

Полный runbook (bootstrap, Vault, DNS, k3s) — [`AGENTS.md`](./AGENTS.md).
