# Keycloak deployment for AIGW OAuth 2.0 E2E integration

Keycloak 25 + PostgreSQL 16 with persistent volumes, deployed as StatefulSets
in the `keycloak` namespace. On first startup, Keycloak imports the `aigw`
realm from the ConfigMap in [`04-realm-import-configmap.yaml`](04-realm-import-configmap.yaml:1).

## Apply order

Apply once in order; the whole stack stands up in ~2 min on a healthy kind
cluster:

```bash
kubectl apply -f keycloak/00-namespace.yaml
kubectl apply -f keycloak/01-postgres-secret.yaml
kubectl apply -f keycloak/02-postgres-statefulset.yaml
kubectl apply -f keycloak/03-keycloak-secret.yaml
kubectl apply -f keycloak/04-realm-import-configmap.yaml
kubectl apply -f keycloak/05-keycloak-statefulset.yaml
kubectl apply -f keycloak/06-keycloak-ingress.yaml

# Wait for both StatefulSets
kubectl -n keycloak rollout status statefulset/keycloak-db --timeout=180s
kubectl -n keycloak rollout status statefulset/keycloak    --timeout=300s
```

## Prerequisite on the host

Add to `/etc/hosts`:

```
127.0.0.1  chat.local aigw.local mcp-aigw.local keycloak.local
```

Then flush DNS:
```bash
sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder
```

## Verification

```bash
# 1. OIDC discovery works
curl -s http://keycloak.local/realms/aigw/.well-known/openid-configuration | jq .issuer
# expected: "http://keycloak.local/realms/aigw"

# 2. JWKS fetchable (this is the URL AIGW will hit)
curl -s http://keycloak.local/realms/aigw/protocol/openid-connect/certs | jq '.keys | length'
# expected: 2 or 3 (RS256, RS512, ES256 signing keys)

# 3. Try direct grant with alice (skips browser flow, useful for smoke test)
curl -s -X POST http://keycloak.local/realms/aigw/protocol/openid-connect/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'grant_type=password' \
  -d 'client_id=aigw-chat' \
  -d 'client_secret=aigw-chat-client-secret-CHANGE-ME' \
  -d 'username=alice' \
  -d 'password=alice' \
  -d 'scope=openid email aigw-api' | jq

# 4. Decode the access_token and confirm claims
TOKEN=$(curl -s -X POST http://keycloak.local/realms/aigw/protocol/openid-connect/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'grant_type=password&client_id=aigw-chat&client_secret=aigw-chat-client-secret-CHANGE-ME&username=alice&password=alice&scope=openid email aigw-api' | jq -r .access_token)

echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq
# Expected fields:
#   "iss": "http://keycloak.local/realms/aigw"
#   "aud": "aigw-api"
#   "preferred_username": "alice"
#   "email": "alice@example.local"
#   "groups": ["gemini-users"]
#   "scope": "openid email aigw-api"
```

## Test users

| Username | Password  | Groups                    | Purpose                     |
|----------|-----------|---------------------------|-----------------------------|
| alice    | alice     | gemini-users              | Happy path Gemini caller    |
| bob      | bob       | (none)                    | Should be denied by RBAC    |
| kcadmin  | kcadmin   | admins, gemini-users      | Budget-bypass admin path    |

Master realm admin (for Keycloak admin console at http://keycloak.local/):
| admin    | kc-admin-poc-CHANGE-ME |

## Secrets to rotate before non-POC use

- `01-postgres-secret.yaml` : `POSTGRES_PASSWORD`
- `03-keycloak-secret.yaml` : `KEYCLOAK_ADMIN_PASSWORD` and `OIDC_CLIENT_SECRET`
- `04-realm-import-configmap.yaml` : same `secret` for `aigw-chat` client + all user passwords

Any change to the `aigw-chat.secret` field in the realm import JSON MUST match
the `OIDC_CLIENT_SECRET` in the k8s Secret.

## Persistence

Data lives in two PVCs (`local-path` provisioner on kind):

| PVC                          | Size | Purpose                     |
|------------------------------|------|-----------------------------|
| data-keycloak-db-0           | 5Gi  | Postgres row data           |
| data-keycloak-0              | 2Gi  | Keycloak runtime data       |

The realm import ConfigMap is imported once by Keycloak on **initial** startup.
Later edits to the ConfigMap are NOT re-imported unless you clear the DB.

To re-import after editing the realm JSON:
```bash
kubectl -n keycloak scale sts keycloak --replicas=0
kubectl -n keycloak delete pod keycloak-db-0                # optional: forces DB restart
kubectl -n keycloak scale sts keycloak --replicas=1
```

Or use `kcadm.sh` from inside the Keycloak pod to update the realm live.

## Cleanup

```bash
kubectl delete -f keycloak/
kubectl -n keycloak delete pvc --all
```
