#!/usr/bin/env bash
set -euo pipefail

# Remote redeploy helper
# - Syncs remote repo via git by default
# - Uses rsync only when explicitly requested
# - Preserves remote .env
# - Falls back to /home/<user>/.env if repo .env is missing
# - Rebuilds/recreates docker compose services

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_ENV_FILE="$SCRIPT_DIR/.env"

load_deploy_config_from_env() {
    if [[ ! -f "$LOCAL_ENV_FILE" ]]; then
        return
    fi

    # Read only deploy-related keys from local .env, ignore all other BCN keys.
    while IFS= read -r line; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        case "$line" in
            DEPLOY_HOST=*|DEPLOY_USER=*|DEPLOY_PORT=*|DEPLOY_REPO_DIR=*|DEPLOY_PASSWORD=*|ALLOW_PARTIAL_ENV=*|DEPLOY_SYNC_MODE=*|DEPLOY_BRANCH=*)
                key="${line%%=*}"
                value="${line#*=}"
                # Keep explicit process environment as highest priority.
                if [[ -n "${!key-}" ]]; then
                    continue
                fi
                value="${value%\"}"
                value="${value#\"}"
                value="${value%\'}"
                value="${value#\'}"
                printf -v "$key" '%s' "$value"
                ;;
        esac
    done < "$LOCAL_ENV_FILE"
}

load_deploy_config_from_env

DEPLOY_HOST="${DEPLOY_HOST:-192.168.0.37}"
DEPLOY_USER="${DEPLOY_USER:-deployment}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
DEPLOY_REPO_DIR="${DEPLOY_REPO_DIR:-/home/${DEPLOY_USER}/broken-cloud-news}"
DEPLOY_PASSWORD="${DEPLOY_PASSWORD:-}"
ALLOW_PARTIAL_ENV="${ALLOW_PARTIAL_ENV:-0}"
DEPLOY_SYNC_MODE="${DEPLOY_SYNC_MODE:-git}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"

usage() {
    cat <<'USAGE'
Usage:
  ./redeploy.sh [--host <ip>] [--user <name>] [--port <n>] [--repo-dir <path>] [--branch <name>] [--sync-mode <git|rsync>] [--allow-partial-env]

Environment overrides:
  DEPLOY_HOST       Default: 192.168.0.37
  DEPLOY_USER       Default: deployment
  DEPLOY_PORT       Default: 22
  DEPLOY_REPO_DIR   Default: /home/<user>/broken-cloud-news
  DEPLOY_PASSWORD   If unset, script prompts securely
  DEPLOY_BRANCH     Default: main
  DEPLOY_SYNC_MODE  Default: git (recommended), fallback: rsync
  ALLOW_PARTIAL_ENV
                  Default: 0 (strict)
                  Set to 1 to bypass strict required-key validation

Example:
  DEPLOY_PASSWORD=2306 ./redeploy.sh --sync-mode git --branch main
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)
            DEPLOY_HOST="$2"
            shift 2
            ;;
        --user)
            DEPLOY_USER="$2"
            shift 2
            ;;
        --port)
            DEPLOY_PORT="$2"
            shift 2
            ;;
        --repo-dir)
            DEPLOY_REPO_DIR="$2"
            shift 2
            ;;
        --branch)
            DEPLOY_BRANCH="$2"
            shift 2
            ;;
        --sync-mode)
            DEPLOY_SYNC_MODE="$2"
            shift 2
            ;;
        --allow-partial-env)
            ALLOW_PARTIAL_ENV=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

case "$DEPLOY_SYNC_MODE" in
    git|rsync)
        ;;
    *)
        echo "Invalid --sync-mode: $DEPLOY_SYNC_MODE (expected: git or rsync)" >&2
        exit 1
        ;;
esac

require_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Missing required command: $cmd" >&2
        exit 1
    fi
}

require_cmd sshpass
require_cmd ssh
if [[ "$DEPLOY_SYNC_MODE" == "rsync" ]]; then
    require_cmd rsync
fi

if [[ -z "$DEPLOY_PASSWORD" ]]; then
    read -rsp "Password for ${DEPLOY_USER}@${DEPLOY_HOST}: " DEPLOY_PASSWORD
    echo ""
fi

export SSHPASS="$DEPLOY_PASSWORD"
REMOTE="${DEPLOY_USER}@${DEPLOY_HOST}"
SSH_OPTS=(-p "$DEPLOY_PORT" -o StrictHostKeyChecking=no)

echo "==> Ensuring remote repo directory exists: ${DEPLOY_REPO_DIR}"
sshpass -e ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p '$DEPLOY_REPO_DIR'"

echo "==> Backing up remote .env (if present)"
sshpass -e ssh "${SSH_OPTS[@]}" "$REMOTE" "
set -euo pipefail
cd '$DEPLOY_REPO_DIR'
if [ -f .env ]; then
  TS=\$(date +%Y%m%d%H%M%S)
  BACKUP_DIR='/home/${DEPLOY_USER}/.env.backups'
  mkdir -p \"\$BACKUP_DIR\"
  cp .env \"\$BACKUP_DIR/bcn.env.\$TS\"
  chmod 600 \"\$BACKUP_DIR/bcn.env.\$TS\"
  echo \"Backed up .env to \$BACKUP_DIR/bcn.env.\$TS\"
else
  echo 'No existing .env to back up'
fi
"

if [[ "$DEPLOY_SYNC_MODE" == "git" ]]; then
    echo "==> Syncing remote repository via git (branch: ${DEPLOY_BRANCH})"
    sshpass -e ssh -tt "${SSH_OPTS[@]}" "$REMOTE" "
set -euo pipefail
cd '$DEPLOY_REPO_DIR'

if [ ! -d .git ]; then
  echo 'ERROR: Remote directory is not a git repository.'
  echo 'Run with --sync-mode rsync once, or clone the repository on remote first.'
  exit 6
fi

origin_url=\$(git remote get-url origin 2>/dev/null || true)
if echo \"\$origin_url\" | grep -q '^https://github.com/'; then
  ssh_origin=\"git@github.com:\${origin_url#https://github.com/}\"
  git remote set-url origin \"\$ssh_origin\"
  echo \"Updated origin to SSH URL: \$ssh_origin\"
fi

if git show-ref --verify --quiet 'refs/heads/${DEPLOY_BRANCH}'; then
  git checkout '${DEPLOY_BRANCH}'
else
  git checkout -b '${DEPLOY_BRANCH}' 'origin/${DEPLOY_BRANCH}'
fi
git fetch --prune origin
git pull --ff-only origin '${DEPLOY_BRANCH}'
echo \"Remote HEAD after git sync: \$(git rev-parse --short HEAD)\"
"
else
    echo "==> Syncing local repository to remote host via rsync (fallback mode)"
    rsync -az --delete \
        --filter='P .env' \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='.ruff_cache' \
        --exclude='.mypy_cache' \
        --exclude='.env' \
        -e "sshpass -e ssh -p ${DEPLOY_PORT} -o StrictHostKeyChecking=no" \
        "$SCRIPT_DIR/" \
        "${REMOTE}:${DEPLOY_REPO_DIR}/"
fi

echo "==> Rebuilding and restarting compose services on remote host"
sshpass -e ssh "${SSH_OPTS[@]}" "$REMOTE" "
set -euo pipefail
cd '$DEPLOY_REPO_DIR'

if [ ! -f .env ] && [ -f '/home/${DEPLOY_USER}/.env' ]; then
  cp '/home/${DEPLOY_USER}/.env' .env
  chmod 600 .env
  echo 'Recovered .env from /home/${DEPLOY_USER}/.env'
fi

if [ ! -f .env ]; then
  echo 'ERROR: .env is missing in repo and parent home directory.'
  echo 'Create it first, then rerun redeploy.sh.'
  exit 2
fi

if ! grep -q '^BCN_DATABASE_URL=' .env; then
  echo 'ERROR: BCN_DATABASE_URL is missing in .env.'
  echo 'Set BCN_DATABASE_URL, then rerun redeploy.sh.'
  exit 3
fi

# Containerized app cannot reach localhost postgres inside the same container.
if grep -qE '^BCN_DATABASE_URL=.*@(localhost|127\\.0\\.0\\.1|::1):5432/' .env; then
  sed -i 's#@localhost:5432/#@postgres:5432/#g; s#@127\\.0\\.0\\.1:5432/#@postgres:5432/#g; s#@::1:5432/#@postgres:5432/#g' .env
  echo 'Patched BCN_DATABASE_URL host to postgres for dockerized runtime'
fi

bcn_key_count=\$(grep -c '^BCN_' .env || true)
echo \"Detected BCN key count: \$bcn_key_count\"
if [ \"\$bcn_key_count\" -lt 8 ] && [ '${ALLOW_PARTIAL_ENV}' != '1' ]; then
  echo 'ERROR: .env looks incomplete (too few BCN_* keys).'
  echo 'Refusing deploy in strict mode.'
  echo 'Use --allow-partial-env only for intentional partial deployments.'
  exit 4
fi

required_keys=(
  BCN_DATABASE_URL
  BCN_LLM_PROVIDER
  BCN_LLM_BASE_URL
  BCN_LLM_MODEL
  BCN_LLM_API_KEY
)
missing_required=0
for key in \"\${required_keys[@]}\"; do
  line=\$(grep -E \"^\${key}=\" .env | head -n1 || true)
  if [ -z \"\$line\" ]; then
    echo \"ERROR: Missing required key: \$key\"
    missing_required=1
    continue
  fi
  value=\${line#*=}
  if [ -z \"\$value\" ]; then
    echo \"ERROR: Empty required key: \$key\"
    missing_required=1
  fi
done
if [ \"\$missing_required\" -ne 0 ] && [ '${ALLOW_PARTIAL_ENV}' != '1' ]; then
  echo 'ERROR: Required keys are missing/empty; refusing deploy in strict mode.'
  echo 'Use --allow-partial-env only for intentional partial deployments.'
  exit 5
fi

for key in BCN_GITHUB_TOKEN BCN_TWITTER_BEARER_TOKEN BCN_TELEGRAM_BOT_TOKEN BCN_TELEGRAM_CHAT_ID BCN_DISCORD_BOT_TOKEN BCN_DISCORD_CHANNEL_ID BCN_COMFYUI_URL; do
  if ! grep -q \"^\${key}=\" .env; then
    echo \"WARN: Optional key is missing: \${key}\"
  fi
done

echo '=== effective non-secret config preview ==='
for key in BCN_DATABASE_URL BCN_LLM_PROVIDER BCN_LLM_BASE_URL BCN_LLM_MODEL BCN_LLM_PROVIDER_COVER BCN_LLM_BASE_URL_COVER BCN_LLM_MODEL_COVER BCN_DISTRIBUTE_HOURS BCN_DISTRIBUTE_MINUTE BCN_DISTRIBUTE_TIMEZONE; do
  line=\$(grep -E \"^\${key}=\" .env | head -n1 || true)
  if [ -n \"\$line\" ]; then
    echo \"\$line\"
  fi
done

echo '=== compose up ==='
docker compose up -d --build --force-recreate
echo '=== compose ps ==='
docker compose ps
echo '=== recent bcn logs ==='
docker compose logs --since 3m bcn | tail -n 120 || true
"

unset SSHPASS
echo "==> Redeploy completed"
