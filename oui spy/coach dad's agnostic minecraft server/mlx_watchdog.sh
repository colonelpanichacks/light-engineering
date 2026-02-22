#!/usr/bin/env bash
# =============================================================================
# MLX Server Watchdog — auto-restarts the MLX LLM server if it crashes
# =============================================================================
# Preloads the 7B model (Oracle default). The 3B model loads on demand.
# Logs to mlx_watchdog.log in this directory.
# =============================================================================

set -uo pipefail
cd "$(dirname "$0")"

MLX_HOST="0.0.0.0"
MLX_PORT="8800"
DEFAULT_MODEL="mlx-community/Qwen2.5-7B-Instruct-4bit"
LOG_FILE="mlx_watchdog.log"
HEALTH_URL="http://localhost:${MLX_PORT}/v1/models"
CHECK_INTERVAL=10
RESTART_DELAY=3

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo -e "$msg" | tee -a "$LOG_FILE"
}

cleanup() {
    log "${YELLOW}Watchdog shutting down...${NC}"
    if [ -n "${MLX_PID:-}" ] && kill -0 "$MLX_PID" 2>/dev/null; then
        log "Stopping MLX server (PID $MLX_PID)..."
        kill "$MLX_PID" 2>/dev/null
        wait "$MLX_PID" 2>/dev/null
    fi
    exit 0
}
trap cleanup SIGINT SIGTERM

start_mlx() {
    log "${CYAN}Starting MLX server (model: ${DEFAULT_MODEL}, port: ${MLX_PORT})...${NC}"
    python3 -m mlx_lm.server \
        --host "$MLX_HOST" \
        --port "$MLX_PORT" \
        --model "$DEFAULT_MODEL" \
        >> "$LOG_FILE" 2>&1 &
    MLX_PID=$!
    log "${GREEN}MLX server started (PID: $MLX_PID)${NC}"

    # Wait for it to become healthy
    for i in $(seq 1 30); do
        if curl -sf --max-time 3 "$HEALTH_URL" > /dev/null 2>&1; then
            log "${GREEN}MLX server is healthy!${NC}"
            return 0
        fi
        if ! kill -0 "$MLX_PID" 2>/dev/null; then
            log "${RED}MLX server died during startup.${NC}"
            return 1
        fi
        sleep 1
    done
    log "${YELLOW}MLX server not responding after 30s, but process is alive — continuing.${NC}"
    return 0
}

# Kill any existing MLX server on our port
existing_pid=$(lsof -ti :"$MLX_PORT" 2>/dev/null || true)
if [ -n "$existing_pid" ]; then
    log "${YELLOW}Killing existing process on port ${MLX_PORT} (PID: $existing_pid)${NC}"
    kill "$existing_pid" 2>/dev/null || true
    sleep 2
fi

echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       MLX Server Watchdog — Running          ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
log "Watchdog starting. Model: $DEFAULT_MODEL | Port: $MLX_PORT"

start_mlx
CRASH_COUNT=0

while true; do
    sleep "$CHECK_INTERVAL"

    # Check if process is alive
    if ! kill -0 "$MLX_PID" 2>/dev/null; then
        CRASH_COUNT=$((CRASH_COUNT + 1))
        log "${RED}MLX server crashed! (crash #${CRASH_COUNT})${NC}"
        log "Restarting in ${RESTART_DELAY}s..."
        sleep "$RESTART_DELAY"
        start_mlx
        continue
    fi

    # Health check via HTTP
    if ! curl -sf --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
        log "${YELLOW}MLX server not responding to health check — waiting...${NC}"
        sleep 5
        if ! curl -sf --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
            log "${RED}MLX server unresponsive after retry. Killing and restarting...${NC}"
            CRASH_COUNT=$((CRASH_COUNT + 1))
            kill "$MLX_PID" 2>/dev/null || true
            wait "$MLX_PID" 2>/dev/null || true
            sleep "$RESTART_DELAY"
            start_mlx
        fi
    fi
done
