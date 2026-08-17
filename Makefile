# ---------------------------------------------------------------------------
# AIGW hybrid POC - convenience targets  (Basic-Auth build, no AIRS bridge)
# ---------------------------------------------------------------------------
SHELL := /bin/bash
ROOT  := $(shell pwd)

# read AIGW_USER/AIGW_PASS from .env for curl targets (best-effort)
AIGW_USER ?= $(shell grep -E '^AIGW_USER=' .env 2>/dev/null | cut -d= -f2-)
AIGW_PASS ?= $(shell grep -E '^AIGW_PASS=' .env 2>/dev/null | cut -d= -f2-)
CURL_AUTH := -u $(AIGW_USER):$(AIGW_PASS)

.PHONY: help up down redeploy status logs logs-gw logs-mcp \
        curl-health-noauth curl-health curl-chat \
        test agent venv install-agent-deps reload-config clean

help: ## show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} \
	     /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## deploy everything (kind + workloads + ingress + BasicAuth)
	./deploy.sh

down: ## delete the kind cluster and clean /etc/hosts
	./teardown.sh

redeploy: down up ## down then up

status: ## show pods + services + ingress
	@echo "--- namespaces ---"    && kubectl get ns   | grep -E "airs-gw|mcp-servers|agent-app|ingress-nginx" || true
	@echo "--- airs-gw ---"       && kubectl -n airs-gw get pods,svc
	@echo "--- mcp-servers ---"   && kubectl -n mcp-servers get pods,svc
	@echo "--- ingress ---"       && kubectl -n airs-gw get ingress

logs: ## tail every workload log
	@kubectl -n airs-gw     logs -l app=portkey-gateway -f --max-log-requests=6 --prefix=true &
	@kubectl -n mcp-servers logs -l app=mcp-weather     -f --max-log-requests=6 --prefix=true &
	@kubectl -n mcp-servers logs -l app=mcp-filesystem  -f --max-log-requests=6 --prefix=true &
	@kubectl -n mcp-servers logs -l app=mcp-github      -f --max-log-requests=6 --prefix=true &
	@wait

logs-gw: ## tail the Portkey gateway only
	kubectl -n airs-gw logs deploy/portkey-gateway -f

logs-mcp: ## tail all 3 MCP servers
	kubectl -n mcp-servers logs -l app.kubernetes.io/part-of=airs-aigw-poc -f --prefix=true

curl-health-noauth: ## hit the AI Gateway WITHOUT credentials - expect HTTP 401
	@curl -sS -o /dev/null -w '%{http_code}\n' http://aigw.local/

curl-health: ## hit the AI Gateway WITH credentials - expect HTTP 200
	@curl -sS -o /dev/null -w '%{http_code}\n' $(CURL_AUTH) http://aigw.local/

curl-chat: ## fire a benign chat request through the gateway (with auth)
	curl -sS $(CURL_AUTH) http://aigw.local/v1/chat/completions \
	  -H 'Content-Type: application/json' \
	  -d '{"model":"gemini-2.0-flash","messages":[{"role":"user","content":"Say hello in one short sentence."}]}' | jq . || true

venv: ## create a Python venv for the agent
	python3 -m venv .venv
	@echo "activate with:  source .venv/bin/activate"

install-agent-deps: ## install the agent's python deps into current venv
	pip install -r agent/requirements.txt

test: ## run the 5 smoke tests
	@if [[ -z "$$VIRTUAL_ENV" ]]; then echo ">> tip: source .venv/bin/activate first"; fi
	python3 agent/test_prompts.py

agent: ## interactive REPL against the gateway
	python3 agent/agent.py

reload-config: ## edit configs/portkey-config.json then rerun this to hot-swap the ConfigMap and restart
	kubectl -n airs-gw create configmap portkey-config \
	  --from-file=config.json=configs/portkey-config.json \
	  --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n airs-gw rollout restart deploy/portkey-gateway
	kubectl -n airs-gw rollout status  deploy/portkey-gateway

clean: ## docker cleanup (local images built by this repo)
	-docker rmi mcp-weather:local
