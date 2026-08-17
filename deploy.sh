#!/usr/bin/env bash
# ============================================================================
# Prisma AIRS AIGW - Hybrid POC one-command deployer  (OIDC + Redis build)
# ----------------------------------------------------------------------------
# Provisions:
#   * kind cluster (airs-poc)
#   * ingress-nginx (with Basic Auth in front of AIGW)
#   * Keycloak 25 + Postgres 16 (StatefulSets) with realm 'aigw' imported
#   * Redis for Portkey (airs-gw ns) + Redis for agent-api sessions (agent-app ns)
#   * Portkey Gateway (AI + MCP) with JWT validation enabled
#   * 3 MCP servers (filesystem, github, weather)
#   * agent-api (FastAPI + SPA) wired to Keycloak OIDC (BFF) and Redis session store
#   * /etc/hosts entries for aigw.local + mcp-aigw.local + chat.local + keycloak.test
# NO inline AIRS bridge - AIRS is wired through the AIGW control plane manually
# after this deploy succeeds (see README §9).
# Idempotent - safe to re-run.
# ============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ---------- pretty logging ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step() { echo -e "\n${BLUE}==>${NC} ${1}"; }
ok()   { echo -e "${GREEN}   ✓${NC} ${1}"; }
warn() { echo -e "${YELLOW}   !${NC} ${1}"; }
die()  { echo -e "${RED}   ✗${NC} ${1}"; exit 1; }

# ---------- 0. prereqs ----------
step "0/12 Checking prerequisites"
for cmd in docker kind kubectl python3 htpasswd; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    if [[ "$cmd" == "htpasswd" ]]; then
      warn "'htpasswd' not found - will fall back to Python bcrypt"
    else
      die "missing '$cmd' - please install it first"
    fi
  else
    ok "$cmd found"
  fi
done
docker info >/dev/null 2>&1 || die "docker daemon not running - start Docker Desktop"
ok "docker daemon running"

# ---------- 1. .env ----------
step "1/12 Loading .env"
if [[ ! -f .env ]]; then
  warn ".env not found - copying from .env.example"
  cp .env.example .env
  die "edit .env and fill in AIGW_USER, AIGW_PASS, GEMINI_API_KEY, GITHUB_TOKEN, OPENWEATHER_API_KEY then re-run"
fi
# shellcheck disable=SC1091
set -a; source .env; set +a
: "${AIGW_USER:?AIGW_USER not set in .env}"
: "${AIGW_PASS:?AIGW_PASS not set in .env}"
: "${GEMINI_API_KEY:?GEMINI_API_KEY not set in .env}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN not set in .env}"
: "${OPENWEATHER_API_KEY:?OPENWEATHER_API_KEY not set in .env}"
: "${CHAT_USER:=admin}"
: "${CHAT_PASS:?CHAT_PASS not set in .env}"
if [ -z "${CHAT_SESSION_SECRET:-}" ] || [ "$CHAT_SESSION_SECRET" = "CHANGE_ME_LONG_RANDOM_HEX" ]; then
  CHAT_SESSION_SECRET="$(openssl rand -hex 32)"
  warn "CHAT_SESSION_SECRET auto-generated (persist in .env if you want session stability)"
fi
# OIDC (Phase 1-3). All have safe defaults matching keycloak/04-realm-import-configmap.yaml.
: "${OIDC_CLIENT_ID:=aigw-chat}"
: "${OIDC_CLIENT_SECRET:=aigw-chat-client-secret-CHANGE-ME}"
ok "environment loaded (basic-auth user='$AIGW_USER', chat user='$CHAT_USER', oidc client='$OIDC_CLIENT_ID')"

# ---------- 2. kind cluster ----------
step "2/12 Provisioning kind cluster 'airs-poc'"
if kind get clusters | grep -qx "airs-poc"; then
  ok "cluster already exists"
else
  kind create cluster --config kind/cluster.yaml
  ok "cluster created"
fi
kubectl cluster-info --context kind-airs-poc >/dev/null
kubectl config use-context kind-airs-poc >/dev/null

# ---------- 3. ingress-nginx ----------
step "3/12 Installing ingress-nginx"
if ! kubectl get ns ingress-nginx >/dev/null 2>&1; then
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.2/deploy/static/provider/kind/deploy.yaml
fi
kubectl -n ingress-nginx wait --for=condition=Ready pod \
  -l app.kubernetes.io/component=controller --timeout=180s || die "ingress-nginx not ready"
ok "ingress-nginx ready"

# ---------- 4. build & load local images ----------
step "4/12 Building local Docker images"
docker build -q -t mcp-weather:local ./mcp-weather >/dev/null
ok "mcp-weather:local built"
docker build -q -t agent-api:local ./agent-api >/dev/null
ok "agent-api:local built"

# Pre-pull upstream images that some kind nodes can't fetch (corp-MITM'd TLS to GHCR, etc.)
# via docker on the host (Docker Desktop trusts the corp CA), so we can load them into kind.
UPSTREAM_IMAGES=(
  "ghcr.io/github/github-mcp-server:latest"
)
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  arm64|aarch64) DOCKER_PLATFORM="linux/arm64" ;;
  *)             DOCKER_PLATFORM="linux/amd64" ;;
esac
for img in "${UPSTREAM_IMAGES[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    warn "pulling $img on host ($DOCKER_PLATFORM) - one-time, ~15MB"
    docker pull --platform "$DOCKER_PLATFORM" "$img" >/dev/null
  fi
  ok "$img present on host"
done

step "5/12 Loading images into kind"
# kind load docker-image sometimes fails on OCI attestation manifests; use the
# streaming docker save | ctr import path instead - it's the most reliable way.
NODES=$(kind get nodes --name airs-poc)
for node in $NODES; do
  # local build (mcp-weather:local)
  docker save mcp-weather:local | docker exec -i "$node" ctr --namespace=k8s.io images import - >/dev/null 2>&1 &&     ok "mcp-weather:local -> $node"
  docker save agent-api:local   | docker exec -i "$node" ctr --namespace=k8s.io images import - >/dev/null 2>&1 && \
    ok "agent-api:local -> $node"
  # upstream pre-pulled
  for img in "${UPSTREAM_IMAGES[@]}"; do
    docker save "$img" | docker exec -i "$node" ctr --namespace=k8s.io images import - >/dev/null 2>&1 &&       ok "$img -> $node"
  done
done

# ---------- 6. namespaces + secrets ----------
step "6/12 Creating namespaces and secrets"
kubectl apply -f manifests/00-namespaces.yaml >/dev/null

# Basic-auth htpasswd file (bcrypt) - used by ingress-nginx auth annotation.
step "     Generating htpasswd (bcrypt) for ingress Basic Auth"
HTPASSWD_FILE="$(mktemp -t aigw-htpasswd.XXXXXX)"
trap 'rm -f "$HTPASSWD_FILE"' EXIT
if command -v htpasswd >/dev/null 2>&1; then
  htpasswd -bBc "$HTPASSWD_FILE" "$AIGW_USER" "$AIGW_PASS" >/dev/null
else
  python3 - "$AIGW_USER" "$AIGW_PASS" "$HTPASSWD_FILE" <<'PY'
import sys, os
user, pw, path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    import bcrypt
except ImportError:
    os.system(f"{sys.executable} -m pip install --quiet bcrypt")
    import bcrypt
h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=10)).decode()
open(path, "w").write(f"{user}:{h}\n")
PY
fi
kubectl -n airs-gw create secret generic basic-auth \
  --from-file=auth="$HTPASSWD_FILE" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "basic-auth secret installed"

kubectl -n airs-gw create secret generic llm-creds \
  --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n airs-gw create secret generic mcp-upstream-creds \
  --from-literal=GITHUB_TOKEN="$GITHUB_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n mcp-servers create secret generic mcp-creds \
  --from-literal=GITHUB_TOKEN="$GITHUB_TOKEN" \
  --from-literal=OPENWEATHER_API_KEY="$OPENWEATHER_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n agent-app create secret generic chat-creds \
  --from-literal=CHAT_USER="$CHAT_USER" \
  --from-literal=CHAT_PASS="$CHAT_PASS" \
  --from-literal=CHAT_SESSION_SECRET="$CHAT_SESSION_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n agent-app create secret generic llm-creds-mirror \
  --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n agent-app create secret generic aigw-basicauth-mirror \
  --from-literal=AIGW_USER="$AIGW_USER" \
  --from-literal=AIGW_PASS="$AIGW_PASS" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# OIDC client credentials (Phase 1). Mirrored into agent-app so agent-api can
# perform the code -> token exchange with Keycloak.  Must match the "secret"
# field of the aigw-chat client in keycloak/04-realm-import-configmap.yaml.
kubectl -n agent-app create secret generic oidc-client \
  --from-literal=OIDC_CLIENT_ID="$OIDC_CLIENT_ID" \
  --from-literal=OIDC_CLIENT_SECRET="$OIDC_CLIENT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "secrets in place (chat-creds, llm-creds-mirror, aigw-basicauth-mirror, oidc-client)"

# ---------- 7. Keycloak (Phase 1) ----------
step "7/12 Deploying Keycloak 25 + Postgres 16"
kubectl apply -f keycloak/00-namespace.yaml            >/dev/null
kubectl apply -f keycloak/01-postgres-secret.yaml      >/dev/null
kubectl apply -f keycloak/02-postgres-statefulset.yaml >/dev/null
kubectl apply -f keycloak/03-keycloak-secret.yaml      >/dev/null
kubectl apply -f keycloak/04-realm-import-configmap.yaml >/dev/null
kubectl apply -f keycloak/05-keycloak-statefulset.yaml >/dev/null
kubectl apply -f keycloak/06-keycloak-ingress.yaml     >/dev/null
ok "keycloak manifests applied - waiting for rollout (first boot ~2 min: JPA migration + realm import)"
kubectl -n keycloak rollout status statefulset/keycloak-db --timeout=180s
kubectl -n keycloak rollout status statefulset/keycloak    --timeout=360s
ok "keycloak ready at http://keycloak.test (realm: aigw)"

# ---------- 8. gateway config -> ConfigMap ----------
step "8/12 Loading Portkey config into ConfigMap"
kubectl -n airs-gw create configmap portkey-config \
  --from-file=config.json=configs/portkey-config.json \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "portkey-config ConfigMap applied"

# ---------- 9. workloads ----------
step "9/12 Applying application manifests"
kubectl apply -f manifests/20-redis.yaml           >/dev/null   # Portkey cache
kubectl apply -f manifests/25-redis-sessions.yaml  >/dev/null   # agent-api session store (Phase 4)
kubectl apply -f manifests/50-mcp-filesystem.yaml  >/dev/null
kubectl apply -f manifests/51-mcp-github.yaml      >/dev/null
kubectl apply -f manifests/52-mcp-weather.yaml     >/dev/null
kubectl apply -f manifests/40-portkey-gateway.yaml >/dev/null
kubectl apply -f manifests/60-ingress.yaml         >/dev/null
kubectl apply -f manifests/70-agent-api.yaml       >/dev/null
ok "manifests applied"

step "10/12 Waiting for rollouts"
kubectl -n airs-gw     rollout status deploy/redis           --timeout=120s
kubectl -n agent-app   rollout status deploy/redis           --timeout=120s
kubectl -n mcp-servers rollout status deploy/mcp-filesystem  --timeout=240s
kubectl -n mcp-servers rollout status deploy/mcp-github      --timeout=240s
kubectl -n mcp-servers rollout status deploy/mcp-weather     --timeout=180s
kubectl -n airs-gw     rollout status deploy/portkey-gateway --timeout=240s
kubectl -n agent-app   rollout status deploy/agent-api       --timeout=180s
ok "all workloads Ready"

# Force a rollout restart of agent-api so it picks up the freshly-built image
# even when the tag ('local') did not change.
step "11/12 Restarting agent-api to pick up newly built image"
kubectl -n agent-app rollout restart deploy/agent-api >/dev/null
kubectl -n agent-app rollout status  deploy/agent-api --timeout=180s
ok "agent-api restarted"

# ---------- 12. hosts file ----------
step "12/12 Ensuring /etc/hosts has aigw.local, mcp-aigw.local, chat.local, keycloak.test"
HOSTS_LINE="127.0.0.1 aigw.local mcp-aigw.local chat.local keycloak.test"
if ! grep -qE "^\s*127\.0\.0\.1\s+.*keycloak\.test" /etc/hosts; then
  warn "adding hosts entry (needs sudo)"
  echo "$HOSTS_LINE" | sudo tee -a /etc/hosts >/dev/null
  # macOS DNS cache flush (no-op on Linux)
  if command -v dscacheutil >/dev/null 2>&1; then
    sudo dscacheutil -flushcache 2>/dev/null || true
    sudo killall -HUP mDNSResponder 2>/dev/null || true
  fi
fi
ok "/etc/hosts OK"

# ---------- summary ----------
echo -e "\n${GREEN}=========================================================================${NC}"
echo -e "${GREEN} DEPLOY COMPLETE${NC}"
echo -e "${GREEN}=========================================================================${NC}"
echo -e " Chat UI (OIDC) : http://chat.local/                    (login via Keycloak)"
echo -e "                    - alice / alice     (group: gemini-users)"
echo -e "                    - bob   / bob       (no groups - should be denied by SCM)"
echo -e "                    - kcadmin / kcadmin (groups: admins, gemini-users)"
echo -e " Chat UI (legacy) : http://chat.local/?legacy=1         (login: $CHAT_USER)"
echo -e ""
echo -e " Keycloak admin  : http://keycloak.test/                (admin / kc-admin-poc-CHANGE-ME)"
echo -e " OIDC discovery  : http://keycloak.test/realms/aigw/.well-known/openid-configuration"
echo -e ""
echo -e " AI Gateway      : http://aigw.local/v1/chat/completions   (Basic Auth: $AIGW_USER  +  Bearer JWT from Keycloak)"
echo -e " MCP Gateway     : http://mcp-aigw.local/{filesystem|github|weather}/mcp"
echo -e ""
echo -e " Auth test - no creds should return 401:"
echo -e "   curl -i http://aigw.local/"
echo -e " OIDC smoke test (direct grant):"
echo -e "   curl -s -X POST http://keycloak.test/realms/aigw/protocol/openid-connect/token \\"
echo -e "     -d 'grant_type=password&client_id=$OIDC_CLIENT_ID&client_secret=$OIDC_CLIENT_SECRET&username=alice&password=alice&scope=openid email aigw-api' | jq"
echo -e ""
echo -e " Run smoke tests : ${YELLOW}make test${NC}"
echo -e " Interactive REPL: ${YELLOW}make agent${NC}"
echo -e " Tail all logs   : ${YELLOW}make logs${NC}"
echo -e " Tear it all down: ${YELLOW}./teardown.sh${NC}"
