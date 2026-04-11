#!/bin/bash
# =============================================================================
# scripts/_common.sh
# Shared bootstrap for all WHG orchestration scripts
# =============================================================================
# Sourced by es.sh, symphonym.sh, cluster.sh, ingest.sh.
# Guard against double-sourcing.

if [ -n "$_COMMON_SH_LOADED" ]; then
    return 0 2>/dev/null || true
fi
_COMMON_SH_LOADED=1

# --- Bootstrap: minimal hardcoded path for initial install ---
IX1_BASE="/ix1/ishi"

# --- Load Environment Variables (if available) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

# --- Conda Environment Setup ---
# Admins are renaming 'whcdh' to 'ishi'. Fallback for transition period.
CONDA_SETUP_PATH="/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
if [ ! -f "$CONDA_SETUP_PATH" ]; then
    CONDA_SETUP_PATH="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
fi
CONDA_BIN_PATH="$(dirname "$CONDA_SETUP_PATH")/../bin"

# Ensure PATH includes Java if available
if [ -n "$JAVA_HOME" ] && [ -d "$JAVA_HOME/bin" ]; then
    export PATH="$JAVA_HOME/bin:$PATH"
fi

activate_environment() {
    cat <<EOF
# --- ENV SETUP ---
CONDA_SETUP="$CONDA_SETUP_PATH"
if [ -f "\$CONDA_SETUP" ]; then
    source "\$CONDA_SETUP"
else
    export PATH="$CONDA_BIN_PATH:\$PATH"
fi

conda activate whg
export PYTHONPATH="/ix1/ishi/elastic:${PYTHONPATH}"

echo "Environment: $(conda info --envs | grep '*' | awk '{print $1}')"
echo "Python: $(which python)"
echo "------------------------------------------------"
EOF
}

# Helper: curl with elastic credentials when security is enabled
es_curl() {
    local ELASTIC_PASS_FILE="${IX1_BASE}/es/config/elastic.password"
    if [ -f "$ELASTIC_PASS_FILE" ]; then
        curl -s -u "elastic:$(cat "$ELASTIC_PASS_FILE")" "$@"
    else
        curl -s "$@"
    fi
}

