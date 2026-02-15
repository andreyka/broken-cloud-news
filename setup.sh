#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Broken Cloud News - End-to-End Setup Script
# ============================================================================
# Usage:
#   ./setup.sh              Interactive setup (prompts for all tokens)
#   ./setup.sh --reset      Tear down containers, remove volumes, and re-setup
#   ./setup.sh --nuke       Remove everything (volumes, .env, images) and exit
#   ./setup.sh --check      Validate current .env and test connectivity
# ============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yaml"

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERROR]${NC} $*"; }
header()  { echo -e "\n${BOLD}=== $* ===${NC}\n"; }

REPLY_VALUE=""
prompt_value() {
    local varname="$1"
    local description="$2"
    local default="${3:-}"
    local secret="${4:-false}"

    if [[ -n "$default" ]]; then
        echo -en "  ${CYAN}$description${NC}\n"
        echo -en "  ${BOLD}default:${NC} $default\n"
        echo -en "  ${BOLD}enter value (or press Enter for default):${NC} "
    else
        echo -en "  ${CYAN}$description${NC}\n"
        echo -en "  ${BOLD}enter value (or press Enter to skip):${NC} "
    fi

    if [[ "$secret" == "true" ]]; then
        read -rs value
        echo ""
    else
        read -r value
    fi

    if [[ -z "$value" ]]; then
        value="$default"
    fi

    # Show feedback
    if [[ -z "$value" ]]; then
        echo -e "  ${YELLOW}→ skipped (not set)${NC}\n"
    elif [[ "$secret" == "true" ]]; then
        # Mask secret values: show first 4 chars + asterisks
        local masked
        if [[ ${#value} -gt 4 ]]; then
            masked="${value:0:4}$(printf '*%.0s' $(seq 1 $((${#value} - 4))))"
        else
            masked="****"
        fi
        echo -e "  ${GREEN}→ set: ${masked}${NC}\n"
    else
        echo -e "  ${GREEN}→ set: ${value}${NC}\n"
    fi

    REPLY_VALUE="$value"
}

prompt_yes_no() {
    local question="$1"
    local default="${2:-y}"
    local yn

    if [[ "$default" == "y" ]]; then
        echo -en "${CYAN}$question${NC} [Y/n]: "
    else
        echo -en "${CYAN}$question${NC} [y/N]: "
    fi

    read -r yn
    yn="${yn:-$default}"
    [[ "$yn" =~ ^[Yy] ]]
}

check_command() {
    if ! command -v "$1" &>/dev/null; then
        err "$1 is not installed. Please install it first."
        return 1
    fi
    ok "$1 found: $(command -v "$1")"
}

# --------------------------------------------------------------------------
# Docker volume management
# --------------------------------------------------------------------------

docker_stop() {
    info "Stopping containers..."
    docker compose -f "$COMPOSE_FILE" down 2>/dev/null || docker-compose -f "$COMPOSE_FILE" down 2>/dev/null || true
    ok "Containers stopped."
}

docker_remove_volumes() {
    info "Removing Docker volumes..."
    docker compose -f "$COMPOSE_FILE" down -v 2>/dev/null || docker-compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    ok "Volumes removed. Database will be re-initialized on next start."
}

docker_remove_images() {
    info "Removing built images..."
    docker compose -f "$COMPOSE_FILE" down --rmi local 2>/dev/null || docker-compose -f "$COMPOSE_FILE" down --rmi local 2>/dev/null || true
    ok "Local images removed."
}

# --------------------------------------------------------------------------
# --nuke: full teardown
# --------------------------------------------------------------------------

cmd_nuke() {
    header "FULL TEARDOWN"
    warn "This will remove ALL containers, volumes, images, and .env"
    if ! prompt_yes_no "Are you sure?" "n"; then
        info "Aborted."
        exit 0
    fi

    docker_stop
    docker_remove_volumes
    docker_remove_images

    if [[ -f "$ENV_FILE" ]]; then
        rm "$ENV_FILE"
        ok "Removed .env"
    fi

    ok "Full teardown complete."
}

# --------------------------------------------------------------------------
# --reset: teardown + re-setup
# --------------------------------------------------------------------------

cmd_reset() {
    header "RESET"
    warn "This will destroy the database volume and re-run setup."
    if ! prompt_yes_no "Continue?" "n"; then
        info "Aborted."
        exit 0
    fi

    docker_stop
    docker_remove_volumes
    info "Volumes cleared. Proceeding to setup...\n"
    cmd_setup
}

# --------------------------------------------------------------------------
# --check: validate .env and test connectivity
# --------------------------------------------------------------------------

cmd_check() {
    header "CONFIGURATION CHECK"

    if [[ ! -f "$ENV_FILE" ]]; then
        err ".env file not found. Run ./setup.sh first."
        exit 1
    fi

    source "$ENV_FILE" 2>/dev/null || true

    local issues=0

    # Database
    if [[ -n "${BCN_DATABASE_URL:-}" ]]; then
        ok "BCN_DATABASE_URL is set"
    else
        err "BCN_DATABASE_URL is missing"; ((issues++))
    fi

    # LLM
    if [[ -n "${BCN_LLM_BASE_URL:-}" ]]; then
        ok "BCN_LLM_BASE_URL = $BCN_LLM_BASE_URL"
        if curl -sf --connect-timeout 5 "${BCN_LLM_BASE_URL}/models" >/dev/null 2>&1; then
            ok "LLM endpoint is reachable"
        else
            warn "LLM endpoint is not reachable (may be on local network)"
        fi
    else
        warn "BCN_LLM_BASE_URL not set (using default)"
    fi

    # Browserless
    if [[ -n "${BCN_BROWSERLESS_URL:-}" ]]; then
        ok "BCN_BROWSERLESS_URL = $BCN_BROWSERLESS_URL"
        if curl -sf --connect-timeout 5 "${BCN_BROWSERLESS_URL}/json/version" >/dev/null 2>&1; then
            ok "Browserless endpoint is reachable"
        else
            warn "Browserless endpoint is not reachable (may start with docker-compose)"
        fi
    else
        warn "BCN_BROWSERLESS_URL not set (using default http://browserless:3000)"
    fi

    # GitHub
    if [[ -n "${BCN_GITHUB_TOKEN:-}" && "${BCN_GITHUB_TOKEN}" != "ghp_xxxxxxxxxxxx" ]]; then
        ok "BCN_GITHUB_TOKEN is set"
        local gh_status
        gh_status=$(curl -sf -o /dev/null -w "%{http_code}" \
            -H "Authorization: bearer ${BCN_GITHUB_TOKEN}" \
            https://api.github.com/user 2>/dev/null || echo "000")
        if [[ "$gh_status" == "200" ]]; then
            ok "GitHub token is valid"
        else
            warn "GitHub token validation returned HTTP $gh_status"
        fi
    else
        err "BCN_GITHUB_TOKEN is missing or placeholder"; ((issues++))
    fi

    # Apify
    if [[ -n "${BCN_APIFY_TOKEN:-}" && "${BCN_APIFY_TOKEN}" != "apify_api_xxxxxxxxxxxx" ]]; then
        ok "BCN_APIFY_TOKEN is set"
    else
        warn "BCN_APIFY_TOKEN is missing (Twitter collection disabled)"
    fi

    # Telegram
    if [[ -n "${BCN_TELEGRAM_BOT_TOKEN:-}" && "${BCN_TELEGRAM_BOT_TOKEN}" != "123456:ABC-xxxxxxxxxxxx" ]]; then
        ok "BCN_TELEGRAM_BOT_TOKEN is set"
        if [[ -n "${BCN_TELEGRAM_CHAT_ID:-}" ]]; then
            ok "BCN_TELEGRAM_CHAT_ID = ${BCN_TELEGRAM_CHAT_ID}"
        else
            warn "BCN_TELEGRAM_CHAT_ID is missing"
        fi
    else
        warn "Telegram not configured (distribution disabled)"
    fi

    # Email
    if [[ -n "${BCN_SMTP_HOST:-}" ]]; then
        ok "Email configured: ${BCN_SMTP_HOST}:${BCN_SMTP_PORT:-587}"
    else
        warn "Email not configured (distribution disabled)"
    fi

    # Slack
    if [[ -n "${BCN_SLACK_WEBHOOK_URL:-}" && "${BCN_SLACK_WEBHOOK_URL}" != "https://hooks.slack.com/services/T00/B00/xxxx" ]]; then
        ok "Slack webhook is set"
    else
        warn "Slack not configured (distribution disabled)"
    fi

    echo ""
    if [[ $issues -gt 0 ]]; then
        err "$issues critical issue(s) found. Run ./setup.sh to fix."
        exit 1
    else
        ok "Configuration looks good!"
    fi

    # Check if containers are running
    header "CONTAINER STATUS"
    docker compose -f "$COMPOSE_FILE" ps 2>/dev/null || docker-compose -f "$COMPOSE_FILE" ps 2>/dev/null || warn "Could not check container status"
}

# --------------------------------------------------------------------------
# Interactive setup
# --------------------------------------------------------------------------

cmd_setup() {
    header "Broken Cloud News - Setup"

    # Prerequisites
    header "Checking Prerequisites"
    check_command docker
    check_command curl

    # Determine compose command
    if docker compose version &>/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        err "Neither 'docker compose' nor 'docker-compose' found."
        exit 1
    fi
    ok "Using: $COMPOSE_CMD"

    # If .env exists, offer to keep it
    if [[ -f "$ENV_FILE" ]]; then
        warn ".env already exists."
        if ! prompt_yes_no "Overwrite it with fresh config?" "n"; then
            info "Keeping existing .env. Skipping to Docker setup..."
            start_services
            return
        fi
    fi

    # ------------------------------------------------------------------
    # Collect tokens
    # ------------------------------------------------------------------

    header "Database Configuration"
    info "The default PostgreSQL credentials work with docker-compose out of the box."
    prompt_value BCN_DATABASE_URL "PostgreSQL connection URL" \
        "postgresql://broken_cloud_news_agent_db:cloud_security_agent@localhost:5432/broken_cloud_news"
    DB_URL="$REPLY_VALUE"

    header "LLM Configuration (Qwen on DGX Spark)"
    info "OpenAI-compatible API endpoint for the analysis LLM."
    prompt_value BCN_LLM_BASE_URL "LLM API base URL" "http://192.168.0.9:8000/v1"
    LLM_URL="$REPLY_VALUE"
    prompt_value BCN_LLM_MODEL "Model name" "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"
    LLM_MODEL="$REPLY_VALUE"

    header "ComfyUI Configuration (Flux image generation)"
    prompt_value BCN_COMFYUI_URL "ComfyUI API URL" "http://192.168.0.9:8188"
    COMFYUI_URL="$REPLY_VALUE"

    header "Browserless Configuration (headless Chromium)"
    info "Used for scraping full article content from RSS feed URLs."
    info "Default works with docker-compose (browserless service on port 3000)."
    prompt_value BCN_BROWSERLESS_URL "Browserless API URL" "http://browserless:3000"
    BROWSERLESS_URL="$REPLY_VALUE"

    header "GitHub Token (for GHSA advisory collection)"
    info "Create a token at: https://github.com/settings/tokens"
    info "No special scopes required — only public advisory data is read."
    prompt_value BCN_GITHUB_TOKEN "GitHub token (ghp_...)" "" "true"
    GH_TOKEN="$REPLY_VALUE"

    header "Apify Token (for Twitter/X collection)"
    info "Sign up at: https://apify.com and get your API token."
    info "Leave blank to skip Twitter collection."
    prompt_value BCN_APIFY_TOKEN "Apify API token" "" "true"
    APIFY_TOKEN="$REPLY_VALUE"

    # -- Distribution channels --
    header "Distribution Channels"
    info "Configure at least one channel, or skip all for collect/analyze only.\n"

    # Telegram
    TELEGRAM_TOKEN=""
    TELEGRAM_CHAT_ID=""
    if prompt_yes_no "Configure Telegram distribution?" "y"; then
        info "1. Talk to @BotFather on Telegram to create a bot"
        info "2. Add the bot to your target group/channel"
        info "3. Get the chat ID (use @userinfobot or API getUpdates)"
        prompt_value BCN_TELEGRAM_BOT_TOKEN "Bot token (123456:ABC-...)" "" "true"
        TELEGRAM_TOKEN="$REPLY_VALUE"
        prompt_value BCN_TELEGRAM_CHAT_ID "Chat ID (e.g. -1001234567890)" ""
        TELEGRAM_CHAT_ID="$REPLY_VALUE"
    fi

    # Email
    SMTP_HOST=""
    SMTP_PORT="587"
    SMTP_USER=""
    SMTP_PASS=""
    EMAIL_FROM=""
    EMAIL_RECIPIENTS=""
    if prompt_yes_no "Configure Email distribution?" "n"; then
        info "For Gmail: use smtp.gmail.com with an App Password"
        info "Generate at: https://myaccount.google.com/apppasswords"
        prompt_value BCN_SMTP_HOST "SMTP host" "smtp.gmail.com"
        SMTP_HOST="$REPLY_VALUE"
        prompt_value BCN_SMTP_PORT "SMTP port" "587"
        SMTP_PORT="$REPLY_VALUE"
        prompt_value BCN_SMTP_USER "SMTP username (email)" ""
        SMTP_USER="$REPLY_VALUE"
        prompt_value BCN_SMTP_PASSWORD "SMTP password" "" "true"
        SMTP_PASS="$REPLY_VALUE"
        prompt_value BCN_EMAIL_FROM "From address" "Broken Cloud News <$SMTP_USER>"
        EMAIL_FROM="$REPLY_VALUE"
        prompt_value BCN_EMAIL_RECIPIENTS 'Recipients JSON (e.g. ["a@b.com"])' '[]'
        EMAIL_RECIPIENTS="$REPLY_VALUE"
    fi

    # Slack
    SLACK_WEBHOOK=""
    if prompt_yes_no "Configure Slack distribution?" "n"; then
        info "Create an Incoming Webhook at: https://api.slack.com/messaging/webhooks"
        prompt_value BCN_SLACK_WEBHOOK_URL "Slack webhook URL" "" "true"
        SLACK_WEBHOOK="$REPLY_VALUE"
    fi

    # ------------------------------------------------------------------
    # Write .env
    # ------------------------------------------------------------------

    header "Writing .env"

    cat > "$ENV_FILE" <<ENVEOF
# Broken Cloud News - Configuration
# Generated by setup.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Database
BCN_DATABASE_URL=$DB_URL

# LLM (OpenAI-compatible API)
BCN_LLM_BASE_URL=$LLM_URL
BCN_LLM_MODEL=$LLM_MODEL

# ComfyUI (Flux image generation)
BCN_COMFYUI_URL=$COMFYUI_URL

# Browserless (headless Chromium for content scraping)
BCN_BROWSERLESS_URL=$BROWSERLESS_URL

# GitHub API token (GHSA collection)
BCN_GITHUB_TOKEN=$GH_TOKEN

# Apify API token (Twitter/X collection)
BCN_APIFY_TOKEN=$APIFY_TOKEN

# Telegram distribution
BCN_TELEGRAM_BOT_TOKEN=$TELEGRAM_TOKEN
BCN_TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID

# Email distribution (SMTP)
BCN_SMTP_HOST=$SMTP_HOST
BCN_SMTP_PORT=$SMTP_PORT
BCN_SMTP_USER=$SMTP_USER
BCN_SMTP_PASSWORD=$SMTP_PASS
BCN_EMAIL_FROM=$EMAIL_FROM
BCN_EMAIL_RECIPIENTS=$EMAIL_RECIPIENTS

# Slack distribution
BCN_SLACK_WEBHOOK_URL=$SLACK_WEBHOOK

# Scheduling (uncomment to override defaults)
# BCN_GHSA_INTERVAL_HOURS=4
# BCN_RSS_INTERVAL_HOURS=2
# BCN_TWITTER_INTERVAL_HOURS=6
# BCN_ANALYST_INTERVAL_MINUTES=15
# BCN_DISTRIBUTE_HOUR=9
# BCN_DISTRIBUTE_MINUTE=0
ENVEOF

    chmod 600 "$ENV_FILE"
    ok ".env written (mode 600)"

    start_services
}

# --------------------------------------------------------------------------
# Start Docker services
# --------------------------------------------------------------------------

start_services() {
    header "Starting Docker Services"

    if ! prompt_yes_no "Start services now?" "y"; then
        info "Skipped. Start manually with: docker compose up -d"
        show_next_steps
        return
    fi

    info "Building and starting containers..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d --build

    info "Waiting for PostgreSQL to be ready..."
    local retries=30
    while [[ $retries -gt 0 ]]; do
        if $COMPOSE_CMD -f "$COMPOSE_FILE" exec -T postgres pg_isready -U broken_cloud_news_agent_db -d broken_cloud_news >/dev/null 2>&1; then
            ok "PostgreSQL is ready!"
            break
        fi
        retries=$((retries - 1))
        sleep 2
    done

    if [[ $retries -eq 0 ]]; then
        err "PostgreSQL did not become ready in time."
        err "Check logs: $COMPOSE_CMD -f '$COMPOSE_FILE' logs postgres"
        exit 1
    fi

    info "Waiting for Browserless (headless Chromium) to be ready..."
    retries=15
    while [[ $retries -gt 0 ]]; do
        if curl -sf --connect-timeout 2 "http://localhost:3000/json/version" >/dev/null 2>&1; then
            ok "Browserless is ready!"
            break
        fi
        retries=$((retries - 1))
        sleep 2
    done

    if [[ $retries -eq 0 ]]; then
        warn "Browserless did not respond in time (may still be starting)."
        warn "Check logs: $COMPOSE_CMD -f '$COMPOSE_FILE' logs browserless"
    fi

    echo ""
    $COMPOSE_CMD -f "$COMPOSE_FILE" ps

    show_next_steps
}

show_next_steps() {
    header "Setup Complete"
    echo -e "
${BOLD}Quick commands:${NC}
  ${GREEN}docker compose up -d${NC}           Start all services
  ${GREEN}docker compose logs -f bcn${NC}     Follow application logs
  ${GREEN}docker compose down${NC}            Stop services
  ${GREEN}./setup.sh --check${NC}             Validate configuration
  ${GREEN}./setup.sh --reset${NC}             Reset DB volume + re-setup
  ${GREEN}./setup.sh --nuke${NC}              Remove everything

${BOLD}Manual pipeline (without Docker app container):${NC}
  ${GREEN}pip install -e .${NC}               Install BCN locally
  ${GREEN}bcn collect${NC}                    Collect news items
  ${GREEN}bcn analyze${NC}                    Analyze with LLM
  ${GREEN}bcn write${NC}                      Generate briefing
  ${GREEN}bcn distribute${NC}                 Send to channels
  ${GREEN}bcn pipeline${NC}                   Run full cycle once
  ${GREEN}bcn run${NC}                        Start daemon (agents + scheduler)
"
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

main() {
    cd "$SCRIPT_DIR"

    case "${1:-}" in
        --nuke)
            cmd_nuke
            ;;
        --reset)
            cmd_reset
            ;;
        --check)
            cmd_check
            ;;
        --help|-h)
            echo "Usage: $0 [--reset|--nuke|--check|--help]"
            echo ""
            echo "  (no args)   Interactive setup - prompts for all tokens"
            echo "  --reset     Remove DB volume and re-run setup"
            echo "  --nuke      Remove everything (containers, volumes, images, .env)"
            echo "  --check     Validate .env and test connectivity"
            exit 0
            ;;
        "")
            cmd_setup
            ;;
        *)
            err "Unknown option: $1"
            echo "Run '$0 --help' for usage."
            exit 1
            ;;
    esac
}

main "$@"
