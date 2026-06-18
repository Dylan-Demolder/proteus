#!/usr/bin/env bash
# Proteus Proxy Health Watchdog
# Silent no_agent cron: detects proxy failure, auto-bypasses, auto-restores.
# Runs every minute — only produces output when toggling state.

set -euo pipefail

PROXY_URL="http://127.0.0.1:8787/readyz"
STATE_FILE="$HOME/.hermes/proteus-proxy-state"
CONFIG_FILE="$HOME/.hermes/config.yaml"
HERMES_BIN="/usr/local/lib/hermes-agent/venv/bin/hermes"

# Determine current proxy health
if curl -sf --max-time 3 "$PROXY_URL" >/dev/null 2>&1; then
    HEALTHY=true
else
    HEALTHY=false
fi

# Read previous state (default: healthy — assume proxy is up until proven otherwise)
if [[ -f "$STATE_FILE" ]]; then
    PREVIOUS=$(cat "$STATE_FILE" | tr -d '[:space:]')
else
    PREVIOUS="healthy"
fi

# Check if Hermes is currently routed through the proxy
if grep -q "base_url:.*8787" "$CONFIG_FILE" 2>/dev/null; then
    ROUTING_THROUGH_PROXY=true
else
    ROUTING_THROUGH_PROXY=false
fi

# Track consecutive failures
FAIL_FILE="$HOME/.hermes/proteus-proxy-fails"
if [[ "$HEALTHY" == "true" ]]; then
    echo 0 > "$FAIL_FILE"
else
    COUNT=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)
    COUNT=$((COUNT + 1))
    echo "$COUNT" > "$FAIL_FILE"
fi

CONSECUTIVE_FAILS=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)

# --- DECISION LOGIC ---

# 1) Proxy is healthy but we're bypassing it → restore proxy routing
if [[ "$HEALTHY" == "true" && "$ROUTING_THROUGH_PROXY" == "false" && "$PREVIOUS" == "unhealthy" ]]; then
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ⚡ Proteus proxy recovered — restoring proxy routing"
    "$HERMES_BIN" config set model.base_url "http://127.0.0.1:8787/v1" 2>&1 || true
    echo "healthy" > "$STATE_FILE"
    echo 0 > "$FAIL_FILE"
    exit 0
fi

# 2) Proxy is unhealthy for 2+ consecutive checks → bypass proxy
if [[ "$HEALTHY" == "false" && "$ROUTING_THROUGH_PROXY" == "true" && "$CONSECUTIVE_FAILS" -ge 2 ]]; then
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ⚠️  Proteus proxy down (${CONSECUTIVE_FAILS}s) — bypassing proxy"
    "$HERMES_BIN" config set model.base_url "" 2>&1 || true
    echo "unhealthy" > "$STATE_FILE"
    exit 0
fi

# 3) Update state when transitioning back to healthy (smooth transition)
if [[ "$HEALTHY" == "true" && "$PREVIOUS" == "unhealthy" && "$ROUTING_THROUGH_PROXY" == "true" ]]; then
    # Proxy is already being used — just update the state marker
    echo "healthy" > "$STATE_FILE"
fi

# No output = silent (no_agent cron only delivers on stdout)
exit 0
