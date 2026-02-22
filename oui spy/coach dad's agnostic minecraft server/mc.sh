#!/usr/bin/env bash
# =============================================================================
# Coach Dad's Minecraft Server — Management Script
# =============================================================================
# Usage: ./mc.sh [command]
# =============================================================================

set -euo pipefail
cd "$(dirname "$0")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

banner() {
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════════════╗"
    echo "  ║   Coach Dad's Agnostic Minecraft Server      ║"
    echo "  ╚══════════════════════════════════════════════╝"
    echo -e "${NC}"
}

usage() {
    banner
    echo -e "  ${BOLD}Usage:${NC} ./mc.sh <command>"
    echo ""
    echo -e "  ${BOLD}Server:${NC}"
    echo -e "  ${GREEN}start${NC}       Start everything (Docker + MLX watchdog)"
    echo -e "  ${GREEN}stop${NC}        Stop everything (Docker + MLX)"
    echo -e "  ${GREEN}restart${NC}     Restart the server"
    echo -e "  ${GREEN}status${NC}      Show server status"
    echo -e "  ${GREEN}logs${NC}        Tail server logs (Ctrl+C to exit)"
    echo -e "  ${GREEN}console${NC}     Open RCON console (type 'quit' to exit)"
    echo -e "  ${GREEN}players${NC}     Show online players"
    echo -e "  ${GREEN}say${NC} <msg>   Broadcast a message in-game"
    echo -e "  ${GREEN}op${NC} <name>   Give a player operator permissions"
    echo -e "  ${GREEN}backup${NC}      Backup world data"
    echo -e "  ${GREEN}update${NC}      Pull latest images and restart"
    echo -e "  ${GREEN}nuke${NC}        Stop server and DELETE all world data"
    echo ""
    echo -e "  ${BOLD}AI / MLX:${NC}"
    echo -e "  ${GREEN}mlx${NC}         Start MLX watchdog (auto-restarts on crash)"
    echo -e "  ${GREEN}mlx-stop${NC}    Stop MLX server and watchdog"
    echo -e "  ${GREEN}mlx-logs${NC}    Tail MLX watchdog log"
    echo -e "  ${GREEN}mlx-status${NC}  Check if MLX server is healthy"
    echo ""
}

require_running() {
    if ! docker compose ps --status running 2>/dev/null | grep -q mc-server; then
        echo -e "${RED}Server is not running.${NC} Start it with: ./mc.sh start"
        exit 1
    fi
}

rcon() {
    docker compose exec mc rcon-cli "$@"
}

case "${1:-help}" in
    start)
        banner
        echo -e "${YELLOW}Starting Docker containers...${NC}"
        docker compose up -d
        echo -e "${GREEN}Server is starting up. First boot takes ~2 minutes.${NC}"
        echo -e "${YELLOW}Starting MLX watchdog...${NC}"
        # Kill any old watchdog
        pkill -f "mlx_watchdog.sh" 2>/dev/null || true
        sleep 1
        nohup bash ./mlx_watchdog.sh > /dev/null 2>&1 &
        echo -e "${GREEN}MLX watchdog running in background (auto-restarts on crash).${NC}"
        echo -e "Watch logs with: ${CYAN}./mc.sh logs${NC} or ${CYAN}./mc.sh mlx-logs${NC}"
        ;;

    stop)
        banner
        echo -e "${YELLOW}Stopping everything...${NC}"
        # Stop MLX watchdog and server
        pkill -f "mlx_watchdog.sh" 2>/dev/null || true
        pkill -f "mlx_lm.server" 2>/dev/null || true
        docker compose down
        echo -e "${GREEN}Server and MLX stopped.${NC}"
        ;;

    restart)
        banner
        echo -e "${YELLOW}Restarting server...${NC}"
        docker compose restart mc
        echo -e "${GREEN}Server restarting.${NC}"
        ;;

    status)
        banner
        echo -e "${BOLD}Container Status:${NC}"
        docker compose ps
        echo ""
        if docker compose ps --status running 2>/dev/null | grep -q mc-server; then
            echo -e "${BOLD}Server Info:${NC}"
            rcon "list" 2>/dev/null || echo -e "${YELLOW}Server is still starting up...${NC}"
        fi
        ;;

    logs)
        docker compose logs -f mc
        ;;

    tunnel-logs)
        docker compose logs -f tunnel
        ;;

    console)
        banner
        echo -e "${CYAN}Opening RCON console. Type 'quit' or Ctrl+C to exit.${NC}"
        require_running
        docker compose exec mc rcon-cli
        ;;

    players)
        require_running
        rcon "list"
        ;;

    say)
        shift
        require_running
        if [ $# -eq 0 ]; then
            echo -e "${RED}Usage: ./mc.sh say <message>${NC}"
            exit 1
        fi
        rcon "say $*"
        echo -e "${GREEN}Message sent.${NC}"
        ;;

    op)
        require_running
        if [ -z "${2:-}" ]; then
            echo -e "${RED}Usage: ./mc.sh op <playername>${NC}"
            exit 1
        fi
        rcon "op $2"
        echo -e "${GREEN}$2 is now an operator.${NC}"
        ;;

    backup)
        banner
        BACKUP_DIR="backups/$(date +%Y-%m-%d_%H%M%S)"
        echo -e "${YELLOW}Backing up world data to ${BACKUP_DIR}...${NC}"
        # Tell server to save and pause writes
        if docker compose ps --status running 2>/dev/null | grep -q mc-server; then
            rcon "save-all flush" 2>/dev/null || true
            rcon "save-off" 2>/dev/null || true
            sleep 2
        fi
        mkdir -p "$BACKUP_DIR"
        cp -r data/* "$BACKUP_DIR/"
        # Resume saves
        if docker compose ps --status running 2>/dev/null | grep -q mc-server; then
            rcon "save-on" 2>/dev/null || true
        fi
        SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
        echo -e "${GREEN}Backup complete! ${SIZE} saved to ${BACKUP_DIR}${NC}"
        ;;

    update)
        banner
        echo -e "${YELLOW}Pulling latest images...${NC}"
        docker compose pull
        echo -e "${YELLOW}Restarting with new images...${NC}"
        docker compose up -d
        echo -e "${GREEN}Updated and restarted.${NC}"
        ;;

    nuke)
        banner
        echo -e "${RED}${BOLD}WARNING: This will DELETE your world data permanently!${NC}"
        read -p "Type 'yes i am sure' to confirm: " confirm
        if [ "$confirm" = "yes i am sure" ]; then
            docker compose down 2>/dev/null || true
            rm -rf data/*
            echo -e "${GREEN}World data deleted. Run ./mc.sh start for a fresh world.${NC}"
        else
            echo -e "${YELLOW}Cancelled.${NC}"
        fi
        ;;

    mlx)
        banner
        echo -e "${YELLOW}Starting MLX watchdog...${NC}"
        pkill -f "mlx_watchdog.sh" 2>/dev/null || true
        pkill -f "mlx_lm.server" 2>/dev/null || true
        sleep 2
        bash ./mlx_watchdog.sh
        ;;

    mlx-stop)
        banner
        echo -e "${YELLOW}Stopping MLX...${NC}"
        pkill -f "mlx_watchdog.sh" 2>/dev/null || true
        pkill -f "mlx_lm.server" 2>/dev/null || true
        echo -e "${GREEN}MLX server and watchdog stopped.${NC}"
        ;;

    mlx-logs)
        tail -f mlx_watchdog.log
        ;;

    mlx-status)
        banner
        if curl -sf --max-time 3 http://localhost:8800/v1/models > /dev/null 2>&1; then
            echo -e "${GREEN}MLX server is healthy!${NC}"
            echo -e "${BOLD}Loaded models:${NC}"
            curl -s http://localhost:8800/v1/models | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('data', []):
    print(f'  • {m[\"id\"]}')
" 2>/dev/null || echo "  (could not parse model list)"
        else
            echo -e "${RED}MLX server is NOT responding.${NC}"
        fi
        if pgrep -f "mlx_watchdog.sh" > /dev/null 2>&1; then
            echo -e "${GREEN}Watchdog is running.${NC}"
        else
            echo -e "${YELLOW}Watchdog is NOT running.${NC} Start with: ./mc.sh mlx"
        fi
        ;;

    help|--help|-h|*)
        usage
        ;;
esac
