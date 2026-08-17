#!/usr/bin/env bash
# Tear down the Prisma AIRS AIGW POC completely.
set -Eeuo pipefail
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step() { echo -e "\n${BLUE}==>${NC} ${1}"; }
ok()   { echo -e "${GREEN}   ✓${NC} ${1}"; }
warn() { echo -e "${YELLOW}   !${NC} ${1}"; }

step "Deleting kind cluster 'airs-poc'"
if kind get clusters | grep -qx "airs-poc"; then
  kind delete cluster --name airs-poc
  ok "cluster deleted"
else
  warn "cluster not present"
fi

step "Removing /etc/hosts entry"
if grep -qE "127\.0\.0\.1\s+aigw\.local\s+mcp-aigw\.local" /etc/hosts; then
  warn "removing hosts entry (needs sudo)"
  sudo sed -i.bak '/127\.0\.0\.1 aigw\.local mcp-aigw\.local/d' /etc/hosts
  ok "hosts cleaned"
else
  warn "no hosts entry to remove"
fi

step "Optional: remove local docker images"
echo "   docker rmi airs-bridge:local mcp-weather:local  # run manually if desired"

echo -e "\n${GREEN}Teardown complete.${NC}\n"
