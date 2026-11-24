#!/usr/bin/env bash
# AIassistantCRM_v5.0 one-shot start / stop / status
#
# Usage (run from the project root):
#   bash start_project.sh                 # Start 14 resident processes (12 AVL writers + dimension watcher + API)
#   bash start_project.sh status         # Check whether each process is alive
#   bash start_project.sh stop           # Graceful stop: SIGTERM first so online processes flush call stats, then SIGKILL on timeout
#   DRY_RUN=1 bash start_project.sh      # Print planned actions only: no DB creation, no processes started
#
# Optional environment variables:
#   PYTHON_BIN=/path/to/python      Interpreter override (default: .venv/bin/python, then python3)
#   SKIP_SFTP=1                    Do not start the 12 SFTP writer processes (for local checks without upstream)
#   BOOTSTRAP_AVL_DATE=YYYYMMDD    Before startup, run a one-shot backfill for each of the 12 AVLs on the given date
#   INSTALL_DEPS=1                 Run pip install -r requirements.txt automatically when dependencies are missing
#   SKIP_DEP_CHECK=1               Skip the dependency check
#   SKIP_BOOTSTRAP_DB=1            Do not auto-run --once to create the dimension DB even if it is missing
#
# Online API port: ONLY from ./.env -> CRM_PORT (app_config.load_config());
#                shell environment variables are NOT read.
#
# Artifacts: PID files and stdout go to log/startup/ (ignored by .gitignore);
#            each writer keeps its own logs under runtime/log/, rotated daily and kept for 7 days.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

AVL_TASKS=(F09554 F09555 F09556 F09591 F09627 F09628 F09629 F09630 F09631 F09632 F09633 F09635)
LOG_DIR="$ROOT/log/startup"
DIM_DB="$ROOT/local_excel2db/runtime/sqlite_db/dimensions.db"
ACTION="${1:-start}"
DRY_RUN="${DRY_RUN:-0}"
SKIP_SFTP="${SKIP_SFTP:-0}"
SKIP_DEP_CHECK="${SKIP_DEP_CHECK:-0}"
SKIP_BOOTSTRAP_DB="${SKIP_BOOTSTRAP_DB:-0}"
INSTALL_DEPS="${INSTALL_DEPS:-0}"
BOOTSTRAP_AVL_DATE="${BOOTSTRAP_AVL_DATE:-}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PY="$ROOT/.venv/bin/python"
else
    PY="${PYTHON_BIN:-python3}"
fi

usage() {
    echo "Usage: bash start_project.sh [start|status|stop] (default: start)"
    echo "       DRY_RUN=1 print only; SKIP_SFTP=1 skip the 12 AVL writer processes"
}

pid_files() {
    shopt -s nullglob
    local f=("$LOG_DIR"/*.pid)
    shopt -u nullglob
    [[ ${#f[@]} -gt 0 ]] && printf '%s\n' "${f[@]}"
    return 0
}

alive() {
    local pid
    pid="$(cat "$1" 2>/dev/null || true)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

status() {
    if [[ ! -d "$LOG_DIR" ]]; then
        echo "No startup records ($LOG_DIR does not exist)"
        return 0
    fi
    local any=0 f name pid
    for f in $(pid_files); do
        any=1
        name="$(basename "$f" .pid)"
        pid="$(cat "$f" 2>/dev/null || true)"
        if alive "$f"; then
            printf '  %-18s pid=%-8s RUNNING\n' "$name" "$pid"
        else
            printf '  %-18s pid=%-8s DEAD\n' "$name" "$pid"
        fi
    done
    [[ "$any" == "1" ]] || echo "No startup records"
    return 0
}

stop() {
    [[ -d "$LOG_DIR" ]] || { echo "No startup records"; return 0; }
    local f pid pending
    echo "Sending SIGTERM ..."
    for f in $(pid_files); do
        if alive "$f"; then
            kill -TERM "$(cat "$f")" 2>/dev/null || true
        fi
    done
    for _ in {1..10}; do
        pending=0
        for f in $(pid_files); do alive "$f" && pending=1; done
        [[ "$pending" == "0" ]] && break
        sleep 1
    done
    for f in $(pid_files); do
        if alive "$f"; then
            pid="$(cat "$f")"
            echo "  $f did not exit in time -> SIGKILL $pid"
            kill -KILL "$pid" 2>/dev/null || true
        fi
        rm -f "$f"
    done
    echo "Stopped. Logs kept in $LOG_DIR"
}

launch() {
    local name="$1"; shift
    local pid_file="$LOG_DIR/$name.pid"
    if [[ -f "$pid_file" ]] && alive "$pid_file"; then
        echo "  [skip] $name already running pid=$(cat "$pid_file")"
        return 0
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] $name -> $PY $*"
        return 0
    fi
    nohup "$PY" "$@" >> "$LOG_DIR/$name.log" 2>&1 < /dev/null &
    local pid=$!
    disown "$pid" 2>/dev/null || true
    echo "$pid" > "$pid_file"
    printf '  [start] %-18s pid=%-8s -> log/startup/%s.log\n' "$name" "$pid" "$name"
}

start() {
    [[ "$DRY_RUN" == "1" ]] || mkdir -p "$LOG_DIR"

    # Avoid duplicate startup: writers hold a single-instance lock (a second instance exits),
    # and the API would hit a port conflict.
    local f
    for f in $(pid_files); do
        if alive "$f"; then
            echo "Process already running ($(basename "$f" .pid)). Run first: bash start_project.sh stop"
            return 1
        fi
        rm -f "$f"
    done

    command -v "$PY" >/dev/null 2>&1 || { echo "Python not found: $PY"; return 2; }

    # 端口与 main.py 共用根 .env 的同一个解析入口，避免两处漂移。
    if ! API_PORT="$("$PY" -c 'from app_config import resolve_port; print(resolve_port())')"; then
        echo "端口配置解析失败：请检查项目根 .env 的 CRM_PORT（需为 1~65535 的整数）"
        return 2
    fi
    case "$(uname -s)" in
        Linux|Darwin|*BSD) ;;
        *) echo "  [warning] Writer side relies on fcntl file locks; Linux / Unix only" ;;
    esac

    if [[ "$SKIP_DEP_CHECK" != "1" ]]; then
        local mods='fastapi, uvicorn, pydantic, aiohttp, loguru, openpyxl'
        [[ "$SKIP_SFTP" == "1" ]] || mods="$mods, paramiko"
        if ! "$PY" -c "import $mods" 2>/dev/null; then
            if [[ "$INSTALL_DEPS" == "1" ]]; then
                echo "[deps] Installing requirements.txt automatically ..."
                "$PY" -m pip install -r requirements.txt
            else
                echo "[deps] Missing runtime dependencies. Run first: python -m pip install -r requirements.txt"
                echo "       Or set INSTALL_DEPS=1 to install them automatically."
                return 2
            fi
        fi
    fi

    for f in IOP/.env call_stat/.env services/.env sftp_avl2db/.env; do
        [[ -f "$f" ]] || echo "  [warning] Missing $f (config may fall back to built-in defaults)"
    done

    # 1/3 Dimension DB: online selling-point scripts / auxiliary profiles depend on it;
    # without it most requests return code=2.
    echo "[1/3] Dimension DB"
    if [[ ! -s "$DIM_DB" && "$SKIP_BOOTSTRAP_DB" != "1" ]]; then
        echo "  dimensions.db missing -> python -m local_excel2db --once"
        if [[ "$DRY_RUN" == "1" ]]; then
            echo "  [dry-run] $PY -m local_excel2db --once"
        else
            "$PY" -m local_excel2db --once
        fi
    else
        echo "  Already present, skipping init (force re-import: python -m local_excel2db --once --force)"
    fi

    # 2/3 First business DB load: needs a real upstream file date, so it is not guessed;
    # resident processes fetch files automatically instead.
    echo "[2/3] Business DB backfill"
    if [[ -n "$BOOTSTRAP_AVL_DATE" ]]; then
        [[ "$BOOTSTRAP_AVL_DATE" =~ ^[0-9]{8}$ ]] || {
            echo "  BOOTSTRAP_AVL_DATE must be YYYYMMDD"; return 2; }
        for avl in "${AVL_TASKS[@]}"; do
            if [[ "$DRY_RUN" == "1" ]]; then
                echo "  [dry-run] $PY -m sftp_avl2db --task $avl --date $BOOTSTRAP_AVL_DATE"
            elif "$PY" -m sftp_avl2db --task "$avl" --date "$BOOTSTRAP_AVL_DATE"; then
                echo "  $avl backfill done"
            else
                echo "  [warning] $avl backfill failed (no upstream file for that date or SFTP unreachable); startup continues"
            fi
        done
    else
        echo "  BOOTSTRAP_AVL_DATE not set, skipping; resident processes will fetch AVL files as they arrive"
    fi

    # 3/3 Resident processes: writers first, online API last
    echo "[3/3] Starting resident processes (14 total; 2 when SKIP_SFTP=1)"
    if [[ "$SKIP_SFTP" == "1" ]]; then
        echo "  [skip] SKIP_SFTP=1, not starting the 12 AVL writer processes"
    else
        local avl
        for avl in "${AVL_TASKS[@]}"; do
            launch "avl_$avl" -m sftp_avl2db --task "$avl"
        done
    fi
    launch dim_watcher -m local_excel2db
    launch api main.py

    echo
    echo "Online API : http://0.0.0.0:${API_PORT}/api/speechQryService"
    if [[ "$DRY_RUN" != "1" ]]; then
        echo "Status     : bash start_project.sh status"
        echo "Stop all   : bash start_project.sh stop"
        echo "Start logs : $LOG_DIR/*.log"
    fi
}

case "$ACTION" in
    start)  start ;;
    status) status ;;
    stop)   stop ;;
    -h|--help|help) usage ;;
    *) usage; exit 2 ;;
esac
