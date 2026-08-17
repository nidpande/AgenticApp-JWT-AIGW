# Prisma AIRS AIGW Hybrid Deployment — Complete End-to-End Guide

**Version:** 1.0 (verified working August 2026)
**Audience:** Anyone building this stack from scratch on a fresh macOS laptop.
**Outcome:** A browser-accessible chat UI where a LangGraph agent uses **Gemini via SCM-managed Portkey Enterprise Gateway** and **3 MCP tool servers** (filesystem, GitHub, weather) — all running locally on `kind`.

---

## Table of contents

1. [Architecture at a glance](#1-architecture-at-a-glance)
2. [Prerequisites](#2-prerequisites)
3. [External accounts + credentials you must obtain first](#3-external-accounts--credentials-you-must-obtain-first)
4. [Repository layout](#4-repository-layout)
5. [Quick start (TL;DR)](#5-quick-start-tldr)
6. [Step-by-step deployment](#6-step-by-step-deployment)
7. [SCM manual configuration (control plane)](#7-scm-manual-configuration-control-plane)
8. [Verification](#8-verification)
9. [Common failure modes & fixes](#9-common-failure-modes--fixes)
10. [Uninstall / teardown](#10-uninstall--teardown)
11. [Reference: every file and what it does](#11-reference-every-file-and-what-it-does)

---

## 1. Architecture at a glance

```
                              ┌────────────────────────────────────────┐
                              │  SCM (Strata Cloud Manager)             │
                              │  https://stratacloudmanager.paloaltonetworks.com  │
                              │                                          │
                              │  • AI Gateway registration               │
                              │  • API Keys (workspace-scoped)           │
                              │  • Integrations  (e.g. @geminiapi)       │
                              │  • Optional: AIRS AI Security Profiles   │
                              └───────────────────────────────────────┬─┘
                                                                        │  poll every 60s
                                                                        │  (config sync)
                                                                        ▼
Browser (http://127.0.0.1:8080)
   │
   │  cookie session (admin login)
   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  kind cluster  (docker containers acting as k8s nodes)                   │
│                                                                          │
│   ┌────── ns: agent-app ─────────┐                                       │
│   │  agent-api (LangGraph)        │                                       │
│   │   • /api/login /api/chat      │                                       │
│   │   • 55 MCP tools loaded via   │                                       │
│   │     custom mcp_httpx client   │                                       │
│   │   • routes LLM to Portkey     │                                       │
│   └──┬──────────────┬────────────┘                                       │
│      │              │                                                     │
│      │              └─── direct cluster DNS to MCP servers                │
│      │                                                                    │
│      ▼                                                                    │
│   ┌── ns: airs-gw ────────────────────────────────────┐                   │
│   │  Portkey Enterprise Gateway :8787                 │                   │
│   │  (registry.portkey.ai/portkeyai/gateway_enterprise:2.13.0)           │
│   │  • Bootstrapped with ORG_ID + CLIENT_AUTH         │                   │
│   │  • Validates SCM API key on inbound requests      │                   │
│   │  • Resolves @geminiapi → real Gemini API key      │                   │
│   │  Redis :6379 (session + cache)                    │                   │
│   └──┬────────────────────────────────────────────────┘                   │
│      │                                                                    │
│      ▼ HTTPS out (through corp MITM)                                      │
│   ┌── Google Gemini API (generativelanguage.googleapis.com) ──┐           │
│   └────────────────────────────────────────────────────────────┘          │
│                                                                          │
│   ┌── ns: mcp-servers ────────────────────────────────┐                   │
│   │  mcp-filesystem  :8080/mcp   (14 tools)           │                   │
│   │  mcp-github      :8080/mcp   (39 tools)           │                   │
│   │  mcp-weather     :8080/mcp   ( 2 tools)           │                   │
│   │  All fronted by supergateway (stateless Streamable-HTTP) │            │
│   └────────────────────────────────────────────────────┘                  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Two logically-separate traffic paths

| Path        | What flows                                     | Route                                                                                       |
|-------------|------------------------------------------------|---------------------------------------------------------------------------------------------|
| **LLM**     | `POST /v1/chat/completions`                    | agent-api → `portkey-gateway.airs-gw:8787` → (SCM validates key + integration) → Gemini |
| **Tools**   | JSON-RPC `initialize / tools/list / tools/call` | agent-api → `mcp-<name>.mcp-servers.svc.cluster.local:8080/mcp` (direct, bypasses SCM's MCP Gateway) |

### Why we bypass Portkey MCP Gateway (:8788)

The enterprise gateway also exposes a Portkey **MCP Gateway** on `:8788`, but it only proxies MCP servers that are **registered in SCM as workspaces/servers**. For a POC with local `supergateway`-wrapped MCP servers, registration is impractical (their URLs aren't public). We keep LLM traffic policy-controlled via SCM but let agent-api call MCP directly over cluster DNS.

---

## 2. Prerequisites

Install these once (macOS-focused; Linux equivalents work too):

```bash
brew install --cask docker            # Docker Desktop (start it once so daemon runs)
brew install kind kubectl python@3.11 htpasswd jq
```

Minimum resources: 8 GB RAM allocated to Docker Desktop, 4 CPUs, ~30 GB disk.

Verify:
```bash
docker info               # daemon reachable
kind version              # v0.24+
kubectl version --client  # v1.30+
python3 --version         # 3.11+
```

If you're on a **corporate laptop with a MITM proxy**, exports for the shell:
```bash
export HTTPS_PROXY=http://your-proxy:port
export NO_PROXY=localhost,127.0.0.1,.svc.cluster.local
```
The stack itself uses `NODE_TLS_REJECT_UNAUTHORIZED=0` in the Portkey pod and `verify=False` in the weather MCP to survive the MITM cert (POC-only workaround).

---

## 3. External accounts + credentials you must obtain first

Gather these BEFORE running `deploy.sh`. You'll paste them into `.env`.

| Credential                | Where to get it | Used for |
|---------------------------|-----------------|----------|
| **Gemini API key**        | https://aistudio.google.com/apikey — create in a GCP project that has *Generative Language API* enabled, and does NOT have the key restricted away from that API. | LLM traffic |
| **GitHub Personal Access Token** | https://github.com/settings/personal-access-tokens/new — fine-grained token with read access to any repos you want to query. | github MCP tool |
| **OpenWeather API key**   | https://openweathermap.org/api — free tier is fine. | weather MCP tool |
| **SCM AI Gateway bootstrap creds** | SCM → *Insights → AI Gateway → Bootstrap Data* (only visible after AI Gateway subscription is enabled on your deployment profile). You need `aigw-org-id` (UUID) and `aigw-client-auth` (28-char opaque token). | Portkey Enterprise gateway control-plane sync |
| **Portkey registry pull creds** | Provided by your PANW SE — a docker `registry.portkey.ai` username + password/token. | Pull `gateway_enterprise:2.13.0` |
| **SCM API key**           | SCM → *AI Access → API Keys → Create* (after registering the gateway). This is what the agent presents on every LLM call. | agent-api → Portkey auth |
| **Gemini API key inside an SCM Integration** | SCM → *AI Access → Integrations → Create → provider=Google, slug=`geminiapi`, paste same Gemini key from above*. The gateway resolves `@geminiapi` from the request header to this integration's real key. | Gemini secret storage |

---

## 4. Repository layout

```
AIGW/
├── deploy.sh                       # one-command bootstrap (kind + all workloads)
├── teardown.sh                     # delete cluster + /etc/hosts entries
├── Makefile                        # convenience targets (make up / down / status / logs)
├── .env.example                    # template with every required variable
├── .env                            # <-- YOU CREATE this (git-ignored)
├── README.md                       # short pitch
├── kind/
│   └── cluster.yaml                # kind cluster shape (1 cp + 2 workers, host ports 80/443)
├── manifests/                      # k8s manifests applied by deploy.sh
│   ├── 00-namespaces.yaml
│   ├── 20-redis.yaml
│   ├── 40-portkey-gateway.yaml     # patched later to enterprise image + SCM env
│   ├── 50-mcp-filesystem.yaml
│   ├── 51-mcp-github.yaml
│   ├── 52-mcp-weather.yaml
│   ├── 60-ingress.yaml             # optional external ingress
│   └── 70-agent-api.yaml
├── configs/
│   └── portkey-config.json         # used only in OSS mode
├── agent-api/                      # LangGraph chat backend + static UI
│   ├── Dockerfile
│   ├── requirements.txt            # fastapi + langgraph + langchain-openai + httpx
│   └── app/
│       ├── main.py                 # FastAPI routes /api/login /api/chat /api/me
│       ├── agent_runner.py         # ChatSession + LLM + tool wiring
│       ├── mcp_httpx.py            # CUSTOM stateless Streamable-HTTP MCP client
│       └── static/index.html       # login + chat UI
├── mcp-weather/                    # custom Python MCP server (OpenWeather wrapper)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── server.py                   # FastMCP + verify=False httpx.AsyncClient
└── docs/
    ├── SCM-CONTROL-PLANE-INTEGRATION.md
    └── DEPLOYMENT-GUIDE.md          # <-- this document
```

---

## 5. Quick start (TL;DR)

For someone in a hurry who has the repo cloned and every credential ready:

```bash
cd /path/to/AIGW

# 1. Create .env from template and fill in every value
cp .env.example .env
$EDITOR .env         # populate ALL keys (see section 3)

# 2. One-shot bootstrap (kind cluster + workloads + ingress + basic auth)
./deploy.sh

# 3. Register the gateway with SCM control plane (swaps OSS -> enterprise image)
bash - <<'END'
source .env
kubectl -n airs-gw create secret generic aigw-scm-creds \
  --from-literal=PORTKEY_CLIENT_AUTH="$AIGW_CLIENT_AUTH"
kubectl -n airs-gw create secret docker-registry airsgatewayregistrycredentials \
  --docker-server=registry.portkey.ai \
  --docker-username="$PORTKEY_REGISTRY_USER" \
  --docker-password="$PORTKEY_REGISTRY_PASS"
# Patch the Deployment to use the enterprise image + SCM env vars (details in section 7)
END

# 4. Manual SCM UI steps (section 7)  → get NP-GW-API key + geminiapi integration slug

# 5. Wire agent-api to use SCM API key + @geminiapi
kubectl -n agent-app create secret generic llm-creds-mirror \
  --from-literal=GEMINI_API_KEY="<SCM-API-KEY>" -o yaml --dry-run=client | kubectl apply -f -
kubectl -n agent-app rollout restart deploy/agent-api

# 6. Port-forward + open browser
kubectl -n agent-app port-forward svc/agent-api 8080:8000 &
open http://127.0.0.1:8080/     # login with CHAT_USER / CHAT_PASS from .env
```

---

## 6. Step-by-step deployment

### Step 6.1 — Prepare `.env`

Create the file:

```bash
cd /path/to/AIGW
cp .env.example .env
```

Fill in EVERY value. Below is a fully-annotated template — copy this and replace angle-bracket values:

```dotenv
# ============ AIGW ingress Basic Auth (OSS phase only) ==================
AIGW_USER=aigwuser
AIGW_PASS=<random-string-of-your-choice>

# ============ LLM & tool provider credentials ===========================
GEMINI_API_KEY=<from https://aistudio.google.com/apikey>
GEMINI_MODEL=gemini-2.5-flash
GITHUB_TOKEN=<GitHub fine-grained PAT>
OPENWEATHER_API_KEY=<https://openweathermap.org/api>

# ============ Chat UI admin login =======================================
CHAT_USER=admin
CHAT_PASS=<random-strong-password>
CHAT_SESSION_SECRET=<32-hex; run: openssl rand -hex 32>

# ============ SCM control plane wiring (section 7) ======================
AIGW_ORG_ID=<UUID from SCM AI Gateway Bootstrap page>
AIGW_CLIENT_AUTH=<28-char opaque token from same page>
AIGW_ALBUS_BASEPATH=https://mp.us.prod.airs-gw.portkey.ai/api
AIGW_CONTROL_PLANE_BASEPATH=https://aigw.portkey.ai/v1

# ============ Portkey registry (for enterprise image pull) ==============
PORTKEY_REGISTRY_USER=<from PANW SE>
PORTKEY_REGISTRY_PASS=<from PANW SE>
```

### Step 6.2 — Run `./deploy.sh`

This idempotent script performs the base deployment (OSS mode):

1. Checks Docker + kind + kubectl + python3 are present.
2. Sources `.env`.
3. Creates the kind cluster `airs-poc` (1 control-plane + 2 workers, host ports 80/443 mapped).
4. Installs `ingress-nginx` (kind flavour).
5. Builds `mcp-weather:local` and `agent-api:local` from source.
6. Pre-pulls `ghcr.io/github/github-mcp-server:latest` on the host (bypasses corp-MITM pull issues in the kind nodes).
7. Streams all images into every kind node via `docker save | ctr images import`.
8. Applies every manifest in `manifests/*.yaml`.
9. Creates the ingress Basic-Auth secret (`aigw-basicauth`).
10. Adds `aigw.local` and `mcp-aigw.local` to `/etc/hosts` (requires sudo prompt).
11. Waits for all Deployments to be Available.

Expected end state:
```
kubectl get pods -A | grep -vE "kube-system|local-path"
# ingress-nginx-controller  Running
# airs-gw/portkey-gateway   Running     ← still OSS at this point
# airs-gw/redis             Running
# mcp-servers/mcp-filesystem Running
# mcp-servers/mcp-github    Running
# mcp-servers/mcp-weather   Running
# agent-app/agent-api       Running
```

At this point the stack works **without SCM** using the local `configs/portkey-config.json` file. Continue to section 7 to switch it to SCM-managed enterprise mode.

---

## 7. SCM manual configuration (control plane)

This section is UI-heavy; there is no way to script it end-to-end because SCM issues credentials that only exist after human clicks. Do these in order.

### 7.1 — Enable AI Gateway subscription on your deployment profile

1. Sign in to Strata Cloud Manager.
2. **Manage → Deployment Profile → your profile**.
3. Toggle **AI Gateway** ON. This is the license gate; without it the AI Gateway menu items don't appear.

### 7.2 — Register the on-prem gateway and get bootstrap credentials

1. **Insights → AI Gateway → Data Planes → Register New**.
2. Give it a name (e.g. `NP-GW`).
3. SCM shows **Bootstrap Data**:
   ```
   aigw-org-id      = <UUID>
   aigw-client-auth = <28-char opaque token>
   ```
4. Paste both into `.env`:
   ```dotenv
   AIGW_ORG_ID=<uuid>
   AIGW_CLIENT_AUTH=<28-char-token>
   ```

### 7.3 — Get Portkey registry pull credentials

Ask your PANW SE for `registry.portkey.ai` username + password. Add to `.env`:
```dotenv
PORTKEY_REGISTRY_USER=<value>
PORTKEY_REGISTRY_PASS=<value>
```

### 7.4 — Create the two Kubernetes secrets

```bash
source .env

# imagePullSecret so the pod can pull the enterprise image
kubectl -n airs-gw create secret docker-registry airsgatewayregistrycredentials \
  --docker-server=registry.portkey.ai \
  --docker-username="$PORTKEY_REGISTRY_USER" \
  --docker-password="$PORTKEY_REGISTRY_PASS" \
  --dry-run=client -o yaml | kubectl apply -f -

# SCM control-plane secret containing the client-auth token
kubectl -n airs-gw create secret generic aigw-scm-creds \
  --from-literal=PORTKEY_CLIENT_AUTH="$AIGW_CLIENT_AUTH" \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 7.5 — Pre-pull the enterprise image on the host + stream to kind

```bash
docker login registry.portkey.ai -u "$PORTKEY_REGISTRY_USER" -p "$PORTKEY_REGISTRY_PASS"
docker pull registry.portkey.ai/portkeyai/gateway_enterprise:2.13.0

for node in $(kind get nodes --name airs-poc); do
  docker save registry.portkey.ai/portkeyai/gateway_enterprise:2.13.0 \
    | docker exec -i "$node" ctr --namespace=k8s.io images import -
done
```

### 7.6 — Patch the Portkey Deployment to enterprise mode

```bash
source .env

kubectl -n airs-gw patch deploy portkey-gateway --type=strategic -p "$(cat <<END
spec:
  template:
    spec:
      imagePullSecrets:
        - name: airsgatewayregistrycredentials
      containers:
        - name: gateway
          image: registry.portkey.ai/portkeyai/gateway_enterprise:2.13.0
          livenessProbe:
            tcpSocket: {port: 8787}
          readinessProbe:
            tcpSocket: {port: 8787}
          env:
            - {name: ORGANISATIONS_TO_SYNC, value: "$AIGW_ORG_ID"}
            - {name: ALBUS_BASEPATH, value: "$AIGW_ALBUS_BASEPATH"}
            - {name: CONTROL_PLANE_BASEPATH, value: "$AIGW_CONTROL_PLANE_BASEPATH"}
            - {name: ANALYTICS_STORE, value: "control_plane"}
            - {name: LOG_STORE, value: "control_plane"}
            - {name: CACHE_STORE, value: "redis"}
            - {name: REDIS_URL, value: "redis://redis.airs-gw.svc.cluster.local:6379"}
            - {name: NODE_TLS_REJECT_UNAUTHORIZED, value: "0"}
            - name: PORTKEY_CLIENT_AUTH
              valueFrom:
                secretKeyRef:
                  name: aigw-scm-creds
                  key: PORTKEY_CLIENT_AUTH
END
)"

kubectl -n airs-gw rollout status deploy/portkey-gateway --timeout=120s
```

> **Why `tcpSocket` and not `httpGet`?** The enterprise image's `GET /` returns 401 (auth required); httpGet health probes would flap.
> **Why `NODE_TLS_REJECT_UNAUTHORIZED=0`?** The Node.js runtime inside the pod otherwise rejects the corporate MITM cert on outbound TLS to `mp.us.prod.airs-gw.portkey.ai`.

### 7.7 — Verify the gateway registered

In SCM **Insights → AI Gateway → Data Planes**, your `NP-GW` entry should show **Last Sync ≈ 60s ago**. If it still shows `--`, the pod logs will explain why — usually a bad `AIGW_CLIENT_AUTH` value.

### 7.8 — Create an API Key in SCM

1. **AI Access → API Keys → Create**.
2. Name it (e.g. `NP-GW-API`).
3. Attach it to a workspace (create one if needed, e.g. `default`).
4. Copy the generated key (**you will only see it once**), e.g. `5vZYPhhEs8faMbPU22v5Vbkzk3lr`.

### 7.9 — Create an Integration for Gemini

1. **AI Access → Integrations → Create**.
2. **Provider:** Google.
3. **Slug:** exactly `geminiapi` (must match `x-portkey-provider: @geminiapi` header).
4. **API Key:** paste the same Gemini key from your `.env` (`GEMINI_API_KEY`).
5. Attach to the same workspace as the API key.
6. Save.

### 7.10 — Wire agent-api to use the SCM API key + `@geminiapi`

The Python code in [`agent-api/app/agent_runner.py`](../agent-api/app/agent_runner.py) is already set up to send `x-portkey-provider: @geminiapi`. All you need to update is the API key mirror secret:

```bash
kubectl -n agent-app delete secret llm-creds-mirror --ignore-not-found
kubectl -n agent-app create secret generic llm-creds-mirror \
  --from-literal=GEMINI_API_KEY="<paste-SCM-API-key-here>"

kubectl -n agent-app rollout restart deploy/agent-api
kubectl -n agent-app rollout status deploy/agent-api --timeout=90s
```

`GEMINI_API_KEY` in that secret is a misnomer for backwards compat — its value is now the SCM API key. The Gemini key itself lives only inside the SCM Integration; agent-api never touches it directly.

---

## 8. Verification

### 8.1 — Open the chat UI

```bash
kubectl -n agent-app port-forward --address 127.0.0.1 svc/agent-api 8080:8000 &
open http://127.0.0.1:8080/
```

Log in with `CHAT_USER` / `CHAT_PASS` from `.env`.

### 8.2 — Verify each traffic path

Try these prompts in the UI in order:

| Prompt                                       | Expected behaviour                                                          |
|----------------------------------------------|-----------------------------------------------------------------------------|
| `hi`                                         | Plain LLM reply (no tools). Confirms **LLM path** works.                    |
| `what is the weather in Bangalore`           | Tool call `weather__get_weather({city:"Bangalore"})` → real OpenWeather JSON → summary reply. Confirms **weather MCP + agent-api ↔ MCP path**. |
| `list the files in /data using your tools`   | Tool call `filesystem__list_directory({path:"/data"})` → real ConfigMap-mounted files. Confirms **filesystem MCP path**. |
| `list open issues in the octocat/hello-world github repo` | Tool call `github__list_issues(...)` → real GitHub API result. Confirms **github MCP path** (uses your PAT). |

### 8.3 — CLI smoke test (equivalent, no browser)

```bash
CU=$(grep '^CHAT_USER=' .env | cut -d= -f2)
CP=$(grep '^CHAT_PASS=' .env | cut -d= -f2)

curl -sS -X POST http://127.0.0.1:8080/api/login \
  -H 'Content-Type: application/json' -c /tmp/c.jar \
  -d "{\"username\":\"$CU\",\"password\":\"$CP\"}"

curl -sS http://127.0.0.1:8080/api/me -b /tmp/c.jar | jq .

curl -sS -X POST http://127.0.0.1:8080/api/chat \
  -H 'Content-Type: application/json' -b /tmp/c.jar \
  -d '{"message":"what is the weather in Bangalore"}' | jq .
```

Expected: `/api/me` shows a `tools:[...]` array of 55 entries, and `/api/chat` returns a reply plus a `tool_calls` array.

### 8.4 — Confirm SCM sees the traffic

In SCM **Insights → AI Gateway → Logs**, each `/api/chat` should produce a request record with:
- Model: `gemini-2.5-flash`
- Integration: `geminiapi`
- Latency + token counts

If nothing appears, the gateway isn't routing through SCM (see 9.4).

---

## 9. Common failure modes & fixes

### 9.1 — `deploy.sh` fails at pip install with `ResolutionImpossible`

**Cause:** Over-pinned `langchain-core` conflicts with `langgraph`.
**Fix:** Ensure [`agent-api/requirements.txt`](../agent-api/requirements.txt) does NOT pin `langchain-core`. Let it come in transitively via `langchain==0.3.7`.

### 9.2 — Chat returns `google error: API key not valid`

**Cause:** Your GCP project restricts the Gemini key away from the Generative Language API, or the API isn't enabled.
**Fix:**
1. https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com — click ENABLE.
2. https://console.cloud.google.com/apis/credentials — edit the key → API restrictions → add *Generative Language API* (or select "Don't restrict key").

### 9.3 — Portkey pod CrashLoopBackOff after switching to enterprise image

**Cause A:** Wrong image pull creds → `ImagePullBackOff`. Check `kubectl -n airs-gw describe pod` for `401 Unauthorized`. Fix: recreate `airsgatewayregistrycredentials`.
**Cause B:** Liveness probe using `httpGet /` on the enterprise image → gets 401 and gets killed. Fix: use `tcpSocket: {port: 8787}` (step 7.6 already does this).
**Cause C:** Corp MITM breaking outbound TLS → pod logs show `fetch failed` against Albus. Fix: `NODE_TLS_REJECT_UNAUTHORIZED=0` in the env (already in step 7.6).

### 9.4 — LLM call fails with `Inline provider names are not allowed when block_inline_config is enabled`

**Cause:** Enterprise gateway blocks inline `provider:google` and requires a saved integration reference.
**Fix:** In agent-api, ensure `default_headers={"x-portkey-provider": "@geminiapi"}` is set on the `ChatOpenAI` (already in [`agent-api/app/agent_runner.py`](../agent-api/app/agent_runner.py:44)). Do NOT also pass `x-portkey-api-key` — the Bearer key on the OpenAI client suffices.

### 9.5 — `session bootstrapped with 0 MCP tools`

**Cause:** `langchain-mcp-adapters` + upstream `mcp` package version incompatibility with `supergateway`'s **stateless** Streamable-HTTP mode.
**Fix (already applied):** Use the custom [`agent-api/app/mcp_httpx.py`](../agent-api/app/mcp_httpx.py) client and DO NOT install `langchain-mcp-adapters` or `mcp` — remove them from `requirements.txt`.

### 9.6 — Weather tool returns `SSL: CERTIFICATE_VERIFY_FAILED`

**Cause:** Corporate MITM cert intercepting httpx→OpenWeather TLS.
**Fix:** [`mcp-weather/server.py`](../mcp-weather/server.py:40) has `httpx.AsyncClient(timeout=10.0, verify=False)` (POC-only). Rebuild + reload:
```bash
docker build -t mcp-weather:local ./mcp-weather
docker save mcp-weather:local | docker exec -i airs-poc-worker  ctr -n k8s.io images import -
docker save mcp-weather:local | docker exec -i airs-poc-worker2 ctr -n k8s.io images import -
docker save mcp-weather:local | docker exec -i airs-poc-control-plane ctr -n k8s.io images import -
kubectl -n mcp-servers rollout restart deploy/mcp-weather
```

### 9.7 — GitHub MCP tools return `401 Unauthorized`

**Cause:** `GITHUB_TOKEN` env var not injected into agent-api.
**Fix:**
```bash
kubectl -n agent-app create secret generic github-token-mirror \
  --from-literal=GITHUB_TOKEN="$GITHUB_TOKEN" --dry-run=client -o yaml | kubectl apply -f -

kubectl -n agent-app set env deploy/agent-api --from=secret/github-token-mirror
kubectl -n agent-app rollout restart deploy/agent-api
```
Then [`agent-api/app/agent_runner.py`](../agent-api/app/agent_runner.py:60) sends it as `Authorization: Bearer $GITHUB_TOKEN` to the github MCP.

### 9.8 — Docker build appears to succeed but old code persists in pod

**Cause:** Silent `docker build` failure at pip step leaves the previous image tagged.
**Fix:** Always inspect: `docker build -t agent-api:local ./agent-api 2>&1 | tail -20`. On success confirm with `docker run --rm agent-api:local ls /app/app/` shows all expected .py files. Only THEN stream to nodes and rollout.

### 9.9 — Port-forward drops after a while

Common with kubectl port-forward. Wrap it:
```bash
while true; do
  kubectl -n agent-app port-forward --address 127.0.0.1 svc/agent-api 8080:8000 || true
  sleep 2
done
```

---

## 10. Uninstall / teardown

```bash
./teardown.sh
```

That script:
1. `kind delete cluster --name airs-poc`
2. Removes the `aigw.local` and `mcp-aigw.local` lines from `/etc/hosts` (sudo prompt).

Optional cleanup:
```bash
docker rmi agent-api:local mcp-weather:local registry.portkey.ai/portkeyai/gateway_enterprise:2.13.0
docker system prune -f
```

On the SCM side, delete the `NP-GW` gateway registration + `NP-GW-API` key + `geminiapi` integration in the UI to keep your tenant clean.

---

## 11. Reference: every file and what it does

### Manifests (applied by `deploy.sh` in numeric order)

| File                                      | Purpose |
|-------------------------------------------|---------|
| [`manifests/00-namespaces.yaml`](../manifests/00-namespaces.yaml) | Creates `airs-gw`, `mcp-servers`, `agent-app` namespaces. |
| [`manifests/20-redis.yaml`](../manifests/20-redis.yaml) | Redis (session store + Portkey cache). |
| [`manifests/40-portkey-gateway.yaml`](../manifests/40-portkey-gateway.yaml) | Deployment + Service for the gateway. Starts as OSS; patched to enterprise in section 7.6. |
| [`manifests/50-mcp-filesystem.yaml`](../manifests/50-mcp-filesystem.yaml) | Reference MCP server (`@modelcontextprotocol/server-filesystem`) wrapped in `supergateway` Streamable-HTTP. Mounts a ConfigMap at `/data`. |
| [`manifests/51-mcp-github.yaml`](../manifests/51-mcp-github.yaml) | GitHub MCP (`ghcr.io/github/github-mcp-server`) wrapped in supergateway. Needs `GITHUB_TOKEN` secret. |
| [`manifests/52-mcp-weather.yaml`](../manifests/52-mcp-weather.yaml) | Custom `mcp-weather:local` (see [`mcp-weather/server.py`](../mcp-weather/server.py)). |
| [`manifests/60-ingress.yaml`](../manifests/60-ingress.yaml) | Optional public routes at `aigw.local` / `mcp-aigw.local` behind Basic Auth. |
| [`manifests/70-agent-api.yaml`](../manifests/70-agent-api.yaml) | Deployment + Service for the chat backend. |

### Code

| File                                        | Purpose |
|--------------------------------------------|---------|
| [`agent-api/app/main.py`](../agent-api/app/main.py) | FastAPI routes: `POST /api/login` (cookie session), `GET /api/me`, `POST /api/chat`, plus `/` static UI. |
| [`agent-api/app/agent_runner.py`](../agent-api/app/agent_runner.py) | `ChatSession` class. Builds `ChatOpenAI` pointing at Portkey with `x-portkey-provider: @geminiapi`. Loads tools via `mcp_httpx.load_tools_from_servers()`. Uses `langgraph.prebuilt.create_react_agent`. |
| [`agent-api/app/mcp_httpx.py`](../agent-api/app/mcp_httpx.py) | ~200-line stateless Streamable-HTTP MCP client. Does the JSON-RPC handshake (`initialize` → `notifications/initialized` → `tools/list` → `tools/call`) with proper SSE body parsing. Converts each remote tool's JSON-Schema to a pydantic model and wraps as a LangChain `StructuredTool`. Written because `langchain-mcp-adapters` + `mcp>=1.9` demand stateful sessions that our `supergateway`-fronted servers don't provide. |
| [`agent-api/app/static/index.html`](../agent-api/app/static/index.html) | Single-file login + chat UI. Force-hides the login overlay on `POST /api/login` success (no round-trip to `/api/me` needed). |
| [`mcp-weather/server.py`](../mcp-weather/server.py) | Custom FastMCP server exposing `get_weather(city)` and `health()`. Uses `httpx.AsyncClient(verify=False)` to survive corporate MITM cert (POC). |

### Scripts

| File | Purpose |
|------|---------|
| [`deploy.sh`](../deploy.sh) | Idempotent bootstrap. Safe to re-run. Deploys the OSS-mode stack. Enterprise/SCM switch is a manual follow-up (section 7). |
| [`teardown.sh`](../teardown.sh) | Deletes the kind cluster and cleans `/etc/hosts`. |
| [`Makefile`](../Makefile) | `make up` / `make down` / `make status` / `make logs` / etc. |

### Config

| File | Purpose |
|------|---------|
| [`configs/portkey-config.json`](../configs/portkey-config.json) | Portkey OSS-mode config (`provider: google`, virtual key). Only used before the SCM switch. |

---

## Appendix A — What's inside `.env.example` (full annotated list)

```dotenv
# ================ OSS phase: ingress Basic Auth ==========================
AIGW_USER=aigwuser              # any username
AIGW_PASS=                      # any strong password

# ================ LLM ====================================================
GEMINI_API_KEY=                 # https://aistudio.google.com/apikey
GEMINI_MODEL=gemini-2.5-flash   # 2.0-flash is retired

# ================ MCP tool credentials ===================================
GITHUB_TOKEN=                   # GitHub fine-grained PAT
OPENWEATHER_API_KEY=            # https://openweathermap.org/api

# ================ Chat app admin =========================================
CHAT_USER=admin
CHAT_PASS=                      # strong random
CHAT_SESSION_SECRET=            # openssl rand -hex 32

# ================ SCM (section 7) ========================================
AIGW_ORG_ID=                    # UUID from SCM Bootstrap page
AIGW_CLIENT_AUTH=               # 28-char token from same page
AIGW_ALBUS_BASEPATH=https://mp.us.prod.airs-gw.portkey.ai/api
AIGW_CONTROL_PLANE_BASEPATH=https://aigw.portkey.ai/v1

# ================ Portkey registry (section 7) ===========================
PORTKEY_REGISTRY_USER=          # from PANW SE
PORTKEY_REGISTRY_PASS=          # from PANW SE
```

---

## Appendix B — Minimum working `deploy.sh` sequence (if you'd rather do it by hand)

```bash
# 0. prereqs already installed (docker, kind, kubectl, python3)
source .env

# 1. cluster
kind create cluster --config kind/cluster.yaml

# 2. ingress-nginx
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.2/deploy/static/provider/kind/deploy.yaml
kubectl -n ingress-nginx wait --for=condition=Ready pod -l app.kubernetes.io/component=controller --timeout=180s

# 3. build local images
docker build -t mcp-weather:local ./mcp-weather
docker build -t agent-api:local   ./agent-api

# 4. pre-pull upstream MCP image
docker pull ghcr.io/github/github-mcp-server:latest

# 5. stream all images into every kind node
for node in $(kind get nodes --name airs-poc); do
  for img in agent-api:local mcp-weather:local ghcr.io/github/github-mcp-server:latest; do
    docker save "$img" | docker exec -i "$node" ctr --namespace=k8s.io images import -
  done
done

# 6. secrets required by the manifests
kubectl create ns airs-gw agent-app mcp-servers --dry-run=client -o yaml | kubectl apply -f -
kubectl -n airs-gw create configmap portkey-config --from-file=configs/portkey-config.json
kubectl -n agent-app create secret generic chat-creds \
  --from-literal=CHAT_USER="$CHAT_USER" --from-literal=CHAT_PASS="$CHAT_PASS" \
  --from-literal=CHAT_SESSION_SECRET="$CHAT_SESSION_SECRET"
kubectl -n agent-app create secret generic llm-creds-mirror --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY"
kubectl -n agent-app create secret generic github-token-mirror --from-literal=GITHUB_TOKEN="$GITHUB_TOKEN"
kubectl -n mcp-servers create secret generic weather-secrets --from-literal=OPENWEATHER_API_KEY="$OPENWEATHER_API_KEY"
kubectl -n mcp-servers create secret generic github-token --from-literal=GITHUB_TOKEN="$GITHUB_TOKEN"
htpasswd -bnBC 10 "$AIGW_USER" "$AIGW_PASS" | kubectl -n airs-gw create secret generic aigw-basicauth --from-file=auth=/dev/stdin

# 7. apply manifests
kubectl apply -f manifests/

# 8. wait
for d in airs-gw/portkey-gateway airs-gw/redis mcp-servers/mcp-filesystem mcp-servers/mcp-github mcp-servers/mcp-weather agent-app/agent-api; do
  ns=${d%/*}; dep=${d#*/}
  kubectl -n "$ns" rollout status deploy/"$dep" --timeout=180s
done

# 9. port-forward + open
kubectl -n agent-app port-forward --address 127.0.0.1 svc/agent-api 8080:8000 &
open http://127.0.0.1:8080/
```
