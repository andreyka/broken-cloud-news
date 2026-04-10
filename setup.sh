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
PROJECT_NAME="${BCN_COMPOSE_PROJECT_NAME:-broken-cloud-news}"
COMPOSE_CMD=""

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERROR]${NC} $*"; }
header()  { echo -e "\n${BOLD}=== $* ===${NC}\n"; }

show_banner() {
    cat <<'BANNER'
 ____             _                ____ _                 _ _   _
| __ )  ___  __ _| | _____ _ __   / ___| | ___  _   _  __| | \ | | _____      _____
|  _ \ / _ \/ _` | |/ / _ \ '_ \ | |   | |/ _ \| | | |/ _` |  \| |/ _ \ \ /\ / / __|
| |_) |  __/ (_| |   <  __/ | | || |___| | (_) | |_| | (_| | |\  |  __/\ V  V /\__ \
|____/ \___|\__,_|_|\_\___|_| |_| \____|_|\___/ \__,_|\__,_|_| \_|\___| \_/\_/ |___/
BANNER
}

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

env_quote() {
    local value="${1-}"
    value="${value//$'\n'/\\n}"
    value="${value//\'/\'\"\'\"\'}"
    printf "'%s'" "$value"
}

check_command() {
    if ! command -v "$1" &>/dev/null; then
        err "$1 is not installed. Please install it first."
        return 1
    fi
    ok "$1 found: $(command -v "$1")"
}

detect_compose_command() {
    if docker compose version &>/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        err "Neither 'docker compose' nor 'docker-compose' found."
        exit 1
    fi
}

compose_hint() {
    if [[ -z "$COMPOSE_CMD" ]]; then
        detect_compose_command
    fi
    if [[ "$COMPOSE_CMD" == "docker compose" ]]; then
        echo "docker compose -p $PROJECT_NAME -f $COMPOSE_FILE"
    else
        echo "docker-compose -p $PROJECT_NAME -f $COMPOSE_FILE"
    fi
}

compose_run() {
    if [[ -z "$COMPOSE_CMD" ]]; then
        detect_compose_command
    fi
    if [[ "$COMPOSE_CMD" == "docker compose" ]]; then
        docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
    else
        docker-compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
    fi
}

cleanup_legacy_stack() {
    local legacy_containers
    local legacy_networks

    legacy_containers="$(
        docker ps -a --format '{{.Names}}' \
        | grep -E '^broken_cloud_news-(bcn|postgres|dns_resolver|egress_proxy)-1$' \
        || true
    )"
    legacy_networks="$(
        docker network ls --format '{{.Name}}' \
        | grep -E '^broken_cloud_news_(app_internal|default|egress_public|n8n_network)$' \
        || true
    )"

    if [[ -z "$legacy_containers" && -z "$legacy_networks" ]]; then
        return
    fi

    warn "Detected legacy stack resources from old project naming (broken_cloud_news)."
    if [[ -n "$legacy_containers" ]]; then
        echo "  Containers:"
        while IFS= read -r c; do
            [[ -n "$c" ]] && echo "    - $c"
        done <<< "$legacy_containers"
    fi
    if [[ -n "$legacy_networks" ]]; then
        echo "  Networks:"
        while IFS= read -r n; do
            [[ -n "$n" ]] && echo "    - $n"
        done <<< "$legacy_networks"
    fi

    if ! prompt_yes_no "Remove legacy resources now to avoid network/port conflicts?" "y"; then
        warn "Leaving legacy resources in place; compose startup may fail due to overlap."
        return
    fi

    if [[ -n "$legacy_containers" ]]; then
        while IFS= read -r c; do
            [[ -n "$c" ]] && docker rm -f "$c" >/dev/null 2>&1 || true
        done <<< "$legacy_containers"
    fi
    if [[ -n "$legacy_networks" ]]; then
        while IFS= read -r n; do
            [[ -n "$n" ]] && docker network rm "$n" >/dev/null 2>&1 || true
        done <<< "$legacy_networks"
    fi

    ok "Legacy resources cleaned."
}

database_mode_from_env() {
    local mode="${BCN_SETUP_DATABASE_MODE:-}"
    local url="${BCN_DATABASE_URL:-}"
    if [[ "$mode" == "managed" || "$mode" == "docker" ]]; then
        echo "$mode"
        return
    fi

    if [[ "$url" =~ @postgres([:/]|$) ]] \
        || [[ "$url" =~ @localhost([:/]|$) ]] \
        || [[ "$url" =~ @127\.0\.0\.1([:/]|$) ]]; then
        echo "docker"
    else
        echo "managed"
    fi
}

warn_managed_db_sslmode() {
    local db_url="$1"
    if [[ "$db_url" != *"sslmode="* ]]; then
        warn "Managed DB URL has no sslmode parameter. Add '?sslmode=require' unless your provider says otherwise."
    fi
}

validate_database_access() {
    local output=""
    if ! output="$(compose_run exec -T bcn sh -lc "python - <<'PY'
import asyncio
import os

import asyncpg

REQUIRED = (
    ('news_items', 'relevance_score'),
    ('briefings', 'id'),
    ('generation_runs', 'id'),
)

async def main() -> int:
    url = os.environ.get('BCN_DATABASE_URL', '')
    if not url:
        print('missing_env:BCN_DATABASE_URL')
        return 2

    conn = await asyncpg.connect(url)
    try:
        await conn.execute('SELECT 1')
        for table, column in REQUIRED:
            row = await conn.fetchrow(
                'SELECT 1 FROM information_schema.columns WHERE table_name = \$1 AND column_name = \$2',
                table,
                column,
            )
            if row is None:
                print(f'missing_schema:{table}.{column}')
                return 3
    finally:
        await conn.close()
    return 0

try:
    raise SystemExit(asyncio.run(main()))
except Exception as exc:
    print(f'exception:{type(exc).__name__}:{exc}')
    raise
PY
" 2>&1)"; then
        err "Database connectivity/schema validation failed."
        echo "$output"
        return 1
    fi

    ok "Database connectivity and baseline schema checks passed."
}

# --------------------------------------------------------------------------
# Docker volume management
# --------------------------------------------------------------------------

docker_stop() {
    info "Stopping containers..."
    compose_run down 2>/dev/null || true
    ok "Containers stopped."
}

docker_remove_volumes() {
    info "Removing Docker volumes..."
    compose_run down -v 2>/dev/null || true
    ok "Volumes removed. Database will be re-initialized on next start."
}

docker_remove_images() {
    info "Removing built images..."
    compose_run down --rmi local 2>/dev/null || true
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
    local runtime_issues=0
    local db_mode=""

    # Database
    if [[ -n "${BCN_DATABASE_URL:-}" ]]; then
        ok "BCN_DATABASE_URL is set"
        db_mode="$(database_mode_from_env)"
        ok "Database mode: $db_mode"
        if [[ "$db_mode" == "managed" ]]; then
            warn_managed_db_sslmode "${BCN_DATABASE_URL}"
        elif [[ "${BCN_DATABASE_URL}" == *"@localhost:"* ]]; then
            warn "Database URL uses localhost. In Docker mode prefer host 'postgres'."
        fi
    else
        err "BCN_DATABASE_URL is missing"; ((issues++))
    fi

    # LLM
    if [[ -n "${BCN_LLM_PROVIDER:-}" ]]; then
        ok "BCN_LLM_PROVIDER = ${BCN_LLM_PROVIDER}"
    else
        warn "BCN_LLM_PROVIDER not set (defaulting to openai_compat)"
    fi
    if [[ -n "${BCN_LLM_BASE_URL:-}" ]]; then
        ok "BCN_LLM_BASE_URL = $BCN_LLM_BASE_URL"
        if [[ "${BCN_LLM_BASE_URL}" == http://model_bridge:* ]]; then
            info "LLM endpoint uses the internal model bridge; host-side curl validation is skipped."
        elif curl -sf --connect-timeout 5 "${BCN_LLM_BASE_URL}/models" >/dev/null 2>&1; then
            ok "LLM endpoint is reachable"
        else
            warn "LLM endpoint is not reachable from the host (may still work from containers)"
        fi
    else
        warn "BCN_LLM_BASE_URL not set (using default)"
    fi
    if [[ -n "${MODEL_BRIDGE_UPSTREAM:-}" ]]; then
        ok "MODEL_BRIDGE_UPSTREAM = ${MODEL_BRIDGE_UPSTREAM}"
    fi

    # ComfyUI
    if [[ -n "${BCN_COMFYUI_URL:-}" ]]; then
        ok "BCN_COMFYUI_URL = ${BCN_COMFYUI_URL}"
        if [[ "${BCN_COMFYUI_URL}" == http://comfy_bridge:* ]]; then
            info "ComfyUI endpoint uses internal comfy_bridge; host-side curl validation is skipped."
        fi
    else
        warn "BCN_COMFYUI_URL not set"
    fi
    if [[ -n "${COMFYUI_BRIDGE_UPSTREAM:-}" ]]; then
        ok "COMFYUI_BRIDGE_UPSTREAM = ${COMFYUI_BRIDGE_UPSTREAM}"
    fi

    # GitHub
    if [[ -n "${BCN_GITHUB_TOKEN:-}" && "${BCN_GITHUB_TOKEN}" != "ghp_xxxxxxxxxxxx" && "${BCN_GITHUB_TOKEN}" != "<github-token>" ]]; then
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

    # X API (Twitter)
    if [[ -n "${BCN_TWITTER_BEARER_TOKEN:-}" && "${BCN_TWITTER_BEARER_TOKEN}" != "AAAA..." ]]; then
        ok "BCN_TWITTER_BEARER_TOKEN is set"
    else
        warn "BCN_TWITTER_BEARER_TOKEN is missing (Twitter collection disabled)"
    fi

    # Telegram
    if [[ -n "${BCN_TELEGRAM_BOT_TOKEN:-}" && "${BCN_TELEGRAM_BOT_TOKEN}" != "123456:ABC-xxxxxxxxxxxx" && "${BCN_TELEGRAM_BOT_TOKEN}" != "<telegram-bot-token>" ]]; then
        ok "BCN_TELEGRAM_BOT_TOKEN is set"
        if [[ -n "${BCN_TELEGRAM_CHAT_ID:-}" ]]; then
            ok "BCN_TELEGRAM_CHAT_ID = ${BCN_TELEGRAM_CHAT_ID}"
        else
            warn "BCN_TELEGRAM_CHAT_ID is missing"
        fi
    else
        warn "Telegram not configured (distribution disabled)"
    fi

    # Discord
    if [[ -n "${BCN_DISCORD_BOT_TOKEN:-}" ]]; then
        ok "BCN_DISCORD_BOT_TOKEN is set"
        if [[ -n "${BCN_DISCORD_CHANNEL_ID:-}" ]]; then
            ok "BCN_DISCORD_CHANNEL_ID = ${BCN_DISCORD_CHANNEL_ID}"
        else
            warn "BCN_DISCORD_CHANNEL_ID is missing"
        fi
    else
        warn "Discord not configured (distribution disabled)"
    fi

    # Ghost
    if [[ "${BCN_GHOST_ENABLED:-false}" == "true" ]]; then
        if [[ -n "${BCN_GHOST_ADMIN_API_URL:-}" && -n "${BCN_GHOST_ADMIN_API_KEY:-}" ]]; then
            ok "Ghost publishing is configured"
        else
            err "Ghost is enabled but BCN_GHOST_ADMIN_API_URL / BCN_GHOST_ADMIN_API_KEY is incomplete"; ((issues++))
        fi
    else
        warn "Ghost not configured (distribution disabled)"
    fi

    # Substack
    if [[ "${BCN_SUBSTACK_ENABLED:-false}" == "true" ]]; then
        if [[ -n "${BCN_SUBSTACK_SID:-}" && -n "${BCN_SUBSTACK_PUBLICATION_URL:-}" ]]; then
            ok "Substack publishing is configured"
        else
            err "Substack is enabled but BCN_SUBSTACK_SID / BCN_SUBSTACK_PUBLICATION_URL is incomplete"; ((issues++))
        fi
    else
        warn "Substack not configured (distribution disabled)"
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

    # AI review
    if [[ -n "${BCN_AI_REVIEW_API_KEY:-}" || -n "${OPENAI_API_KEY:-}" ]]; then
        ok "AI review API key is configured"
        ok "BCN_AI_REVIEW_MODEL = ${BCN_AI_REVIEW_MODEL:-gpt-5.4}"
    else
        warn "AI review API key not configured (review lab auto-review disabled)"
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
    if command -v docker >/dev/null 2>&1; then
        detect_compose_command
        compose_run ps || warn "Could not check container status"
        if compose_run ps --services --filter status=running 2>/dev/null | grep -qx "bcn"; then
            info "Running in-container DB/schema validation..."
            if ! validate_database_access; then
                warn "If this is a reused or stale DB volume, run ./setup.sh --reset."
                runtime_issues=$((runtime_issues + 1))
            fi
            if compose_run exec -T bcn python - <<'PY' >/dev/null 2>&1
import asyncio
import httpx
import os

async def main():
    base = os.environ.get("BCN_LLM_BASE_URL", "").rstrip("/")
    if not base:
        raise SystemExit(0)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base}/models")
        resp.raise_for_status()

asyncio.run(main())
PY
            then
                ok "In-container LLM validation passed."
            else
                warn "In-container LLM validation failed."
            fi
        else
            warn "bcn container is not running; skipped runtime DB/schema validation."
        fi
    else
        warn "Docker not found; skipped container status checks."
    fi

    if [[ $runtime_issues -gt 0 ]]; then
        exit 1
    fi
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

    detect_compose_command
    ok "Using: $COMPOSE_CMD"
    cleanup_legacy_stack

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
    DB_MODE="docker"
    DB_URL=""
    PG_USER="postgres"
    PG_PASSWORD="postgres"
    PG_DB="broken_cloud_news"
    if prompt_yes_no "Use a managed/external PostgreSQL database?" "n"; then
        DB_MODE="managed"
        info "Managed DB mode skips local postgres startup and connects directly."
        info "Example: postgresql://user:password@db.example.com:5432/broken_cloud_news?sslmode=require"
        prompt_value BCN_DATABASE_URL "Managed PostgreSQL connection URL" "" "true"
        DB_URL="$REPLY_VALUE"
        if [[ -z "$DB_URL" ]]; then
            err "Managed DB mode requires BCN_DATABASE_URL."
            exit 1
        fi
        warn_managed_db_sslmode "$DB_URL"
    else
        DB_MODE="docker"
        info "Using docker-compose PostgreSQL service. Host must be 'postgres' for in-container app access."
        prompt_value BCN_DATABASE_URL "PostgreSQL connection URL (docker mode)" \
            "postgresql://postgres:postgres@postgres:5432/broken_cloud_news"
        DB_URL="$REPLY_VALUE"
    fi

    header "LLM Configuration"
    info "This config is written for the Docker stack. Use bridge service URLs for local host-proxied models."
    prompt_value BCN_LLM_PROVIDER "LLM provider (openai_compat / vertexai / openai)" "openai_compat"
    LLM_PROVIDER="$REPLY_VALUE"
    if [[ "$LLM_PROVIDER" == "openai_compat" ]]; then
        prompt_value BCN_LLM_BASE_URL "LLM API base URL (inside Docker network)" "http://model_bridge:8000/v1"
        LLM_URL="$REPLY_VALUE"
        prompt_value MODEL_BRIDGE_UPSTREAM "Model bridge upstream (host-local or remote OpenAI-compatible endpoint)" "http://host.docker.internal:8000"
        MODEL_BRIDGE_UPSTREAM="$REPLY_VALUE"
        prompt_value BCN_LLM_API_KEY "LLM API key (leave blank for unauthenticated local upstreams)" "" "true"
        LLM_API_KEY="$REPLY_VALUE"
        prompt_value BCN_LLM_MODEL "Model name" "Qwen/Qwen3-VL-30B-A3B-Instruct"
        LLM_MODEL="$REPLY_VALUE"
    else
        prompt_value BCN_LLM_BASE_URL "LLM API base URL" ""
        LLM_URL="$REPLY_VALUE"
        prompt_value BCN_LLM_API_KEY "LLM API key (optional if your provider uses external auth)" "" "true"
        LLM_API_KEY="$REPLY_VALUE"
        prompt_value BCN_LLM_MODEL "Model name" "gemini-3.1-pro-preview"
        LLM_MODEL="$REPLY_VALUE"
        MODEL_BRIDGE_UPSTREAM=""
    fi
    prompt_value BCN_LLM_MODEL_COVER "Optional cover model override (leave blank to use BCN default)" ""
    LLM_COVER_MODEL="$REPLY_VALUE"

    header "ComfyUI Configuration"
    prompt_value BCN_COMFYUI_URL "ComfyUI API URL (inside Docker network)" "http://comfy_bridge:8188"
    COMFYUI_URL="$REPLY_VALUE"
    prompt_value COMFYUI_BRIDGE_UPSTREAM "ComfyUI bridge upstream (host-local ComfyUI endpoint)" "http://host.docker.internal:8188"
    COMFYUI_BRIDGE_UPSTREAM="$REPLY_VALUE"

    header "GitHub Token (for GHSA advisory collection)"
    info "Create a token at: https://github.com/settings/tokens"
    info "No special scopes required — only public advisory data is read."
    prompt_value BCN_GITHUB_TOKEN "GitHub token (ghp_...)" "" "true"
    GH_TOKEN="$REPLY_VALUE"

    header "X API Bearer Token (for Twitter/X collection)"
    info "Get a bearer token from: https://developer.x.com/en/portal/dashboard"
    info "Leave blank to skip Twitter collection."
    prompt_value BCN_TWITTER_BEARER_TOKEN "X API Bearer Token" "" "true"
    TWITTER_BEARER_TOKEN="$REPLY_VALUE"

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

    # Discord
    DISCORD_TOKEN=""
    DISCORD_CHANNEL_ID=""
    if prompt_yes_no "Configure Discord distribution?" "y"; then
        info "1. Create a Discord bot and copy its token"
        info "2. Invite bot to your server/channel with message permissions"
        info "3. Enable developer mode and copy the target channel ID"
        prompt_value BCN_DISCORD_BOT_TOKEN "Discord bot token" "" "true"
        DISCORD_TOKEN="$REPLY_VALUE"
        prompt_value BCN_DISCORD_CHANNEL_ID "Discord channel ID" ""
        DISCORD_CHANNEL_ID="$REPLY_VALUE"
    fi

    # Ghost
    GHOST_ENABLED="false"
    GHOST_ADMIN_API_URL=""
    GHOST_ADMIN_API_KEY=""
    if prompt_yes_no "Configure Ghost publishing?" "n"; then
        GHOST_ENABLED="true"
        prompt_value BCN_GHOST_ADMIN_API_URL "Ghost Admin API URL (e.g. https://yourpub.ghost.io)" ""
        GHOST_ADMIN_API_URL="$REPLY_VALUE"
        prompt_value BCN_GHOST_ADMIN_API_KEY "Ghost Admin API key (<id>:<secret>)" "" "true"
        GHOST_ADMIN_API_KEY="$REPLY_VALUE"
    fi

    # Substack
    SUBSTACK_ENABLED="false"
    SUBSTACK_SID=""
    SUBSTACK_PUBLICATION_URL=""
    if prompt_yes_no "Configure Substack publishing?" "n"; then
        SUBSTACK_ENABLED="true"
        prompt_value BCN_SUBSTACK_SID "Substack SID cookie" "" "true"
        SUBSTACK_SID="$REPLY_VALUE"
        prompt_value BCN_SUBSTACK_PUBLICATION_URL "Substack publication URL (e.g. https://yourpub.substack.com)" ""
        SUBSTACK_PUBLICATION_URL="$REPLY_VALUE"
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

    header "AI Review"
    AI_REVIEW_API_KEY=""
    AI_REVIEW_MODEL="gpt-5.4"
    AI_REVIEW_REASONING_EFFORT="high"
    AI_REVIEW_AUTO_ENABLED="false"
    if prompt_yes_no "Configure OpenAI-backed AI review?" "n"; then
        prompt_value BCN_AI_REVIEW_API_KEY "OpenAI API key for AI review" "" "true"
        AI_REVIEW_API_KEY="$REPLY_VALUE"
        prompt_value BCN_AI_REVIEW_MODEL "AI review model" "gpt-5.4"
        AI_REVIEW_MODEL="$REPLY_VALUE"
        prompt_value BCN_AI_REVIEW_REASONING_EFFORT "AI review reasoning effort" "high"
        AI_REVIEW_REASONING_EFFORT="$REPLY_VALUE"
        if [[ -n "$AI_REVIEW_API_KEY" ]]; then
            AI_REVIEW_AUTO_ENABLED="true"
        else
            warn "AI review key was blank; automatic AI review will stay disabled."
        fi
    fi

    # ------------------------------------------------------------------
    # Write .env
    # ------------------------------------------------------------------

    header "Writing .env"

    cat > "$ENV_FILE" <<ENVEOF
# Broken Cloud News - Configuration
# Generated by setup.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Database
BCN_SETUP_DATABASE_MODE=$(env_quote "$DB_MODE")
BCN_POSTGRES_USER=$(env_quote "$PG_USER")
BCN_POSTGRES_PASSWORD=$(env_quote "$PG_PASSWORD")
BCN_POSTGRES_DB=$(env_quote "$PG_DB")
BCN_DATABASE_URL=$(env_quote "$DB_URL")

# LLM
BCN_LLM_PROVIDER=$(env_quote "$LLM_PROVIDER")
BCN_LLM_BASE_URL=$(env_quote "$LLM_URL")
BCN_LLM_MODEL=$(env_quote "$LLM_MODEL")
BCN_LLM_API_KEY=$(env_quote "$LLM_API_KEY")
BCN_LLM_MODEL_COVER=$(env_quote "$LLM_COVER_MODEL")
MODEL_BRIDGE_UPSTREAM=$(env_quote "$MODEL_BRIDGE_UPSTREAM")

# ComfyUI bridge
BCN_COMFYUI_URL=$(env_quote "$COMFYUI_URL")
COMFYUI_BRIDGE_UPSTREAM=$(env_quote "$COMFYUI_BRIDGE_UPSTREAM")

# GitHub API token (GHSA collection)
BCN_GITHUB_TOKEN=$(env_quote "$GH_TOKEN")

# X API Bearer Token (Twitter/X collection)
BCN_TWITTER_BEARER_TOKEN=$(env_quote "$TWITTER_BEARER_TOKEN")

# Telegram distribution
BCN_TELEGRAM_BOT_TOKEN=$(env_quote "$TELEGRAM_TOKEN")
BCN_TELEGRAM_CHAT_ID=$(env_quote "$TELEGRAM_CHAT_ID")

# Discord distribution
BCN_DISCORD_BOT_TOKEN=$(env_quote "$DISCORD_TOKEN")
BCN_DISCORD_CHANNEL_ID=$(env_quote "$DISCORD_CHANNEL_ID")

# Ghost distribution
BCN_GHOST_ENABLED=$(env_quote "$GHOST_ENABLED")
BCN_GHOST_ADMIN_API_URL=$(env_quote "$GHOST_ADMIN_API_URL")
BCN_GHOST_ADMIN_API_KEY=$(env_quote "$GHOST_ADMIN_API_KEY")

# Substack distribution
BCN_SUBSTACK_ENABLED=$(env_quote "$SUBSTACK_ENABLED")
BCN_SUBSTACK_SID=$(env_quote "$SUBSTACK_SID")
BCN_SUBSTACK_PUBLICATION_URL=$(env_quote "$SUBSTACK_PUBLICATION_URL")

# Email distribution (SMTP)
BCN_SMTP_HOST=$(env_quote "$SMTP_HOST")
BCN_SMTP_PORT=$(env_quote "$SMTP_PORT")
BCN_SMTP_USER=$(env_quote "$SMTP_USER")
BCN_SMTP_PASSWORD=$(env_quote "$SMTP_PASS")
BCN_EMAIL_FROM=$(env_quote "$EMAIL_FROM")
BCN_EMAIL_RECIPIENTS=$(env_quote "$EMAIL_RECIPIENTS")

# Slack distribution
BCN_SLACK_WEBHOOK_URL=$(env_quote "$SLACK_WEBHOOK")

# AI review
BCN_AI_REVIEW_API_KEY=$(env_quote "$AI_REVIEW_API_KEY")
BCN_AI_REVIEW_MODEL=$(env_quote "$AI_REVIEW_MODEL")
BCN_AI_REVIEW_REASONING_EFFORT=$(env_quote "$AI_REVIEW_REASONING_EFFORT")
BCN_AI_REVIEW_AUTO_ENABLED=$(env_quote "$AI_REVIEW_AUTO_ENABLED")

# Scheduling (uncomment to override defaults)
# BCN_GHSA_INTERVAL_HOURS=4
# BCN_REDDIT_INTERVAL_HOURS=3
# BCN_RSS_INTERVAL_HOURS=2
# BCN_TWITTER_INTERVAL_HOURS=6
# BCN_ANALYST_INTERVAL_MINUTES=15
# BCN_DISTRIBUTE_HOUR=9
# BCN_DISTRIBUTE_HOURS=9,13,19
# BCN_DISTRIBUTE_MINUTE=0
# BCN_DISTRIBUTE_TIMEZONE=UTC
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

    if [[ -f "$ENV_FILE" ]]; then
        source "$ENV_FILE" 2>/dev/null || true
    fi
    local db_mode
    db_mode="$(database_mode_from_env)"

    if ! prompt_yes_no "Start services now?" "y"; then
        info "Skipped. Start manually with: $(compose_hint) up -d"
        show_next_steps
        return
    fi

    if [[ "$db_mode" == "managed" ]]; then
        info "Starting split stack in managed DB mode (without local postgres dependency)..."
        compose_run up -d --build egress_proxy dns_resolver model_bridge comfy_bridge
        compose_run up -d --build --no-deps scheduler bcn ingest_worker evaluation_worker dashboard
    else
        info "Building and starting containers..."
        compose_run up -d --build

        info "Waiting for PostgreSQL to be ready..."
        local retries=30
        local pg_user="${BCN_POSTGRES_USER:-postgres}"
        local pg_db="${BCN_POSTGRES_DB:-broken_cloud_news}"
        while [[ $retries -gt 0 ]]; do
            if compose_run exec -T postgres pg_isready -U "$pg_user" -d "$pg_db" >/dev/null 2>&1; then
                ok "PostgreSQL is ready!"
                break
            fi
            retries=$((retries - 1))
            sleep 2
        done

        if [[ $retries -eq 0 ]]; then
            err "PostgreSQL did not become ready in time."
            err "Check logs: $(compose_hint) logs postgres"
            exit 1
        fi
    fi

    info "Validating database connectivity and schema from the app container..."
    local db_check_retries=12
    while [[ $db_check_retries -gt 0 ]]; do
        if validate_database_access >/dev/null 2>&1; then
            ok "Database connectivity and baseline schema checks passed."
            break
        fi
        db_check_retries=$((db_check_retries - 1))
        sleep 2
    done
    if [[ $db_check_retries -eq 0 ]]; then
        validate_database_access
        err "Database validation failed. For stale local DB state, run ./setup.sh --reset."
        exit 1
    fi

    echo ""
    compose_run ps

    show_next_steps
}

show_next_steps() {
    local compose_cmd
    compose_cmd="$(compose_hint)"
    header "Setup Complete"
    echo -e "
${BOLD}Quick commands:${NC}
  ${GREEN}${compose_cmd} up -d${NC}           Start all services
  ${GREEN}${compose_cmd} logs -f scheduler bcn ingest_worker evaluation_worker${NC}
                                Follow control-plane and worker logs
  ${GREEN}${compose_cmd} down${NC}            Stop services
  ${GREEN}./setup.sh --check${NC}             Validate configuration
  ${GREEN}./setup.sh --reset${NC}             Reset DB volume + re-setup
  ${GREEN}./setup.sh --nuke${NC}              Remove everything

${BOLD}Current worker topology:${NC}
  ${GREEN}${compose_cmd} exec -T bcn python -m bcn workflow-jobs --limit 10${NC}
                                Inspect queue state
  ${GREEN}${compose_cmd} exec -T bcn python -m bcn workflow-lanes list${NC}
                                Inspect lane pause controls
  ${GREEN}${compose_cmd} exec -T bcn python -m bcn distribute --briefing-id <uuid>${NC}
                                Retry distribution for one draft briefing

${BOLD}Local compatibility mode (advanced):${NC}
  ${GREEN}bcn scheduler${NC}                  Run enqueue-only scheduler locally
  ${GREEN}bcn worker --lane publish${NC}      Run publish worker locally
  ${GREEN}bcn worker --lane collection --lane analysis${NC}
                                Run ingest worker locally
  ${GREEN}bcn worker --lane evaluation${NC}   Run evaluation worker locally
  ${GREEN}bcn run${NC}                        Compatibility mode (single process)
"
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

main() {
    cd "$SCRIPT_DIR"
    show_banner
    echo ""

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
