# EvalSpleef Deployment Manifests

This directory contains container build files, single-machine docker compose orchestration, and Kubernetes Helm charts for EvalSpleef.

## Directory Structure

```
deploy/
├── docker/
│   ├── gateway.Dockerfile      # Multi-stage image for spleef-gateway (evalspleef-gateway)
│   ├── control.Dockerfile      # Multi-stage image for spleef CLI & control plane (evalspleef-control)
│   ├── egress.Dockerfile       # Multi-stage image for spleef-egress forward proxy (evalspleef-egress)
│   └── hostd.Dockerfile        # Multi-stage image for spleef-hostd daemon & gateway (evalspleef-hostd)
├── compose/
│   ├── docker-compose.yml      # Single-machine stack (PostgreSQL 16, MinIO, Gateway, Control, Egress, OTel)
│   ├── .env.example            # Environment variable template with secret placeholders
│   ├── egress-policy.yaml      # Egress proxy policy rules
│   └── otel-collector.yaml     # OpenTelemetry collector configuration
├── helm/
│   └── evalspleef/
│       ├── Chart.yaml          # Helm chart definition
│       ├── values.yaml         # Configurable parameters (resources, secretRefs, probes)
│       └── templates/
│           ├── _helpers.tpl    # Shared naming and label templates
│           ├── configmaps.yaml # providers.yaml and models.yaml ConfigMap
│           ├── gateway.yaml    # Model Gateway Deployment & Service
│           ├── control.yaml    # Control Plane Deployment & Service
│           ├── egress.yaml     # Egress Proxy Deployment & Service
│           ├── hostd-gateway.yaml # hostd-gateway Deployment & Service
│           └── networkpolicies.yaml # Ingress & egress network isolation policies
└── README.md
```

## Running the Docker Compose Stack

The compose stack is designed for single-machine deployment (TECH-DESIGN 16.1):

### 1. Configure Secrets

Copy the `.env.example` file to `.env`:

```bash
cp deploy/compose/.env.example deploy/compose/.env
```

Edit `deploy/compose/.env` to configure:
- `POSTGRES_PASSWORD`: password for the PostgreSQL backend.
- `MINIO_ROOT_PASSWORD`: password for MinIO object storage.
- `SPLEEF_GATEWAY_ADMIN_TOKEN`: token for managing gateway virtual keys and budgets.
- Upstream provider credentials (`NOVITA_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `HF_TOKEN`).

### 2. Validate and Launch

Validate the docker compose syntax:

```bash
docker compose -f deploy/compose/docker-compose.yml config
```

Start the services in detached mode:

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
```

### 3. Service Endpoints

- **Gateway Public Data Port**: `http://localhost:8390` (accessible to agents and sandboxes for inference via trial keys).
- **Gateway Admin Port**: `http://127.0.0.1:8391` (loopback only; bearer authenticated via `SPLEEF_GATEWAY_ADMIN_TOKEN`).
- **Postgres**: `127.0.0.1:5432` (loopback only).
- **MinIO Console & API**: `127.0.0.1:9000` and `127.0.0.1:9001` (loopback only).
- **Egress Proxy**: `127.0.0.1:3128` (loopback only).
- **OTel Collector OTLP**: `127.0.0.1:4317` (gRPC) and `127.0.0.1:4318` (HTTP).

To tear down the stack and remove volumes:

```bash
docker compose -f deploy/compose/docker-compose.yml down -v
```

---

## Kubernetes Helm Chart

The Helm chart in `deploy/helm/evalspleef/` provides production Kubernetes manifests for the EvalSpleef control plane, gateway, egress proxy, and hostd-gateway (TECH-DESIGN 16.2).

### Features
- **Zero Raw Secrets in Git**: Uses `secretRefs` referencing Kubernetes Secret objects created out-of-band.
- **Resource Management**: Declares CPU/memory requests and limits for all components.
- **Readiness & Liveness Probes**: Configured for each workload (`/healthz` HTTP for gateway, TCP socket for egress and hostd-gateway, exec for control).
- **Network Policies**: Restricts gateway admin port to control plane pods, allows agent traffic on data port, and controls egress.
- **Non-Root Containers**: Runs with UID/GID 10001 (`spleef`).

### Usage

```bash
# Lint the chart (when helm is available)
helm lint deploy/helm/evalspleef/

# Render templates
helm template evalspleef deploy/helm/evalspleef/

# Install chart
helm install evalspleef deploy/helm/evalspleef/ -f custom-values.yaml
```

---

## Verification Summary

### Verified Here
1. **Docker Compose Validation**:
   - `docker compose -f deploy/compose/docker-compose.yml config` passed with exit code 0.
2. **Dockerfile Image Build**:
   - `docker build -f deploy/docker/gateway.Dockerfile . -t evalspleef/gateway:test` built successfully (exit code 0), verifying uv multi-stage packaging and workspace dependency resolution.
3. **YAML Syntax Verification**:
   - Every YAML file under `deploy/compose/` and `deploy/helm/evalspleef/` (values.yaml, Chart.yaml) parsed cleanly with PyYAML.
   - All Helm templates in `deploy/helm/evalspleef/templates/` were evaluated and parsed against `values.yaml` using a Python script (11 Kubernetes resources successfully parsed).
4. **Clean Docker State**:
   - `docker ps -a --filter label=spleef` returned empty.

### Not Verified on a Real Host
- Helm CLI (`helm lint` / `helm template`) was not installed on the workstation environment; Helm template rendering and Kubernetes resource syntax were validated through an automated Python parsing script.
- The full multi-container stack was not run with live external endpoints (Novita/OpenAI/Anthropic) as this is a build and manifest configuration task.
- `control.Dockerfile`, `egress.Dockerfile`, and `hostd.Dockerfile` were written using the identical multi-stage uv pattern as `gateway.Dockerfile` but were not separately built into image caches to preserve build resources.
