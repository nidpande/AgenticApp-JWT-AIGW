# Registering your AIGW Data Plane with the SCM (Strata Cloud Manager) Control Plane

**Applies to:** the local POC deployed by `deploy.sh` (Portkey OSS gateway running in `airs-gw` namespace)
**Model per official fspec:** SCM is the UI front, Portkey control plane is the API back. Your on-prem gateway is keyed by a `tenant_service_group_id` (TSG id = organisation id) and a `client_auth` token. The gateway pulls its configuration blob from the Portkey control plane on demand and every 30 s.

---

## Recap: what already runs (data plane side)

You have Portkey OSS Gateway running as a Deployment in `airs-gw`. It currently reads its config from a **local ConfigMap** (`configs/portkey-config.json`). That config is NOT wired to any SCM/Portkey control plane; it's a self-contained POC.

To go hybrid, we replace that local ConfigMap-driven mode with **control-plane-managed mode** using additional environment variables (`PORTKEY_CLIENT_AUTH`, `ORGANISATIONS_TO_SYNC`) plus the two control-plane basepaths that tell the gateway where to fetch config from.

---

## Step 1 — Get the two credentials from SCM (UI work, no CLI)

1. Sign in to Strata Cloud Manager with your tenant.
2. Open the AI Gateway section (menu: Insights → AI Gateway OR Configuration → AI Gateway).
3. Enable the AI Gateway feature in the deployment profile if it isn't already:
   - Manage → Deployment Profile → your profile
   - Enable AI Gateway subscription (this is the license gate)
4. SCM shows Bootstrap Data for on-prem gateways. Two values you need:
   ```
   aigw-org-id      = <UUID>
   aigw-client-auth = <28-char opaque token>
   ```
   Equivalent to `ORGANISATIONS_TO_SYNC` and `PORTKEY_CLIENT_AUTH` env vars.
5. Copy both — you'll paste into `.env` in step 2.

---

## Step 2 — Add the credentials to your local `.env`

```bash
cd /path/to/AIGW

cat >> .env <<END

# --- SCM control plane wiring (from SCM AI Gateway bootstrap page) ---
AIGW_ORG_ID=<paste aigw-org-id UUID from SCM>
AIGW_CLIENT_AUTH=<paste aigw-client-auth 28-char token from SCM>
# Prod tenant:
AIGW_ALBUS_BASEPATH=https://mp.us.prod.airs-gw.portkey.ai/api
AIGW_CONTROL_PLANE_BASEPATH=https://aigw.portkey.ai/v1
END
```

For a QA/dev tenant, use these instead:
```
AIGW_ALBUS_BASEPATH=https://mp.us.qa.airs-gw.portkeydev.com/api
AIGW_CONTROL_PLANE_BASEPATH=https://aigw.portkeydev.com/v1
```

---

## Step 3 — Patch the Portkey Deployment to use control-plane mode

```bash
cd /path/to/AIGW
set -a; source .env; set +a

# Store the sensitive client-auth as a Secret
kubectl -n airs-gw create secret generic aigw-scm-creds \
  --from-literal=PORTKEY_CLIENT_AUTH="$AIGW_CLIENT_AUTH" \
  --dry-run=client -o yaml | kubectl apply -f -

# Patch the Deployment with control-plane env vars
kubectl -n airs-gw set env deploy/portkey-gateway \
  ORGANISATIONS_TO_SYNC="$AIGW_ORG_ID" \
  ALBUS_BASEPATH="$AIGW_ALBUS_BASEPATH" \
  CONTROL_PLANE_BASEPATH="$AIGW_CONTROL_PLANE_BASEPATH" \
  ANALYTICS_STORE=control_plane \
  LOG_STORE=control_plane \
  CACHE_STORE=redis \
  REDIS_URL="redis://redis.airs-gw.svc.cluster.local:6379"

# Attach the client-auth from the Secret
kubectl -n airs-gw set env deploy/portkey-gateway --from=secret/aigw-scm-creds

kubectl -n airs-gw rollout status deploy/portkey-gateway --timeout=90s
```

---

## Step 4 — Verify registration succeeded

### 4a. Data-plane liveness
```bash
kubectl -n airs-gw exec deploy/portkey-gateway -- \
  curl -sS -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8787/
```

### 4b. Check pod logs for registration
```bash
kubectl -n airs-gw logs deploy/portkey-gateway --tail=100 | \
  grep -iE "control.plane|register|org|sync|albus"
```
Look for successful sync from `ALBUS_BASEPATH`. `401/403` = wrong client-auth. Timeout = corporate proxy blocking (add `HTTPS_PROXY`, `NO_PROXY=svc.cluster.local`).

### 4c. Confirm on SCM UI
Back in SCM AI Gateway page — your gateway should now appear under Registered Gateways with:
- Org id matching
- Last sync timestamp within 30s
- Health = healthy

### 4d. End-to-end test through the chat UI
No code change needed on `agent-api`. Open http://127.0.0.1:8080/ → send any prompt → request should succeed AND appear in SCM under Insights → AI Gateway → Logs.

---

## Step 5 — Attach AIRS AI Security Profile to the gateway (this is the whole point)

1. In SCM → Manage → AI Security Profile, use the AIRS AI Profile you already have.
2. Attach that profile to your registered gateway's config (org + workspace) in the Portkey control plane view.
3. Save. Gateway pulls the guardrail policy within 30s and starts enforcing:
   - Prompt injection detection
   - PII/secret detection & masking
   - URL/domain deny-lists
   - Response content filtering
4. Test with a prompt that trips a guardrail (e.g., fake credit card number). Should be blocked in the chat UI and logged in SCM.

---

## Rollback

```bash
kubectl -n airs-gw set env deploy/portkey-gateway \
  ORGANISATIONS_TO_SYNC- ALBUS_BASEPATH- CONTROL_PLANE_BASEPATH- \
  ANALYTICS_STORE- LOG_STORE- CACHE_STORE- REDIS_URL- PORTKEY_CLIENT_AUTH-
kubectl -n airs-gw rollout status deploy/portkey-gateway --timeout=90s
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `401 Unauthorized` from ALBUS | Wrong `PORTKEY_CLIENT_AUTH` — regenerate on SCM |
| `unknown organization` | Wrong `ORGANISATIONS_TO_SYNC` — copy UUID exactly |
| `ETIMEDOUT` reaching `aigw.portkey.ai` | Corporate proxy blocking egress; set `HTTPS_PROXY` on pod |
| Chat UI works but SCM shows no logs | Set `ANALYTICS_STORE=control_plane` and `LOG_STORE=control_plane` |
| Rate-limit not enforced | Attach rate-limit policy in SCM → AI Gateway → Policies |

---

## References
- Palo Alto Networks internal fspec: AIRS(VM) AI Gateway Integration (Confluence 917178554)
- Portkey OSS Gateway docs: https://docs.portkey.ai
