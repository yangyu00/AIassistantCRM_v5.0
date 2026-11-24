#!/usr/bin/env bash
# AIassistantCRM_v5.0 一键启动 / 停止 / 状态
#
# 用法（在项目根目录执行）：
#   bash 启动项目.sh                 # 启动 14 个常驻进程（12 AVL 写入 + 维表 watcher + API）
#   bash 启动项目.sh status         # 查看各进程是否存活
#   bash 启动项目.sh stop           # 优雅停止；先 SIGTERM 让在线进程 flush 调用量，超时再 SIGKILL
#   DRY_RUN=1 bash 启动项目.sh      # 只打印将要做什么，不建库、不起进程
#
# 可选环境变量：
#   PYTHON_BIN=/path/to/python      指定解释器（默认优先 .venv/bin/python，其次 python3）
#   SKIP_SFTP=1                    不起 12 个 SFTP 写入进程（无上游时本机验证用）
#   BOOTSTRAP_AVL_DATE=YYYYMMDD    启动前对 12 个 AVL 各做一次指定日期的单次补跑
#   INSTALL_DEPS=1                 依赖缺失时自动执行 pip install -r requirements.txt
#   SKIP_DEP_CHECK=1               跳过依赖检查
#   SKIP_BOOTSTRAP_DB=1            即使维表库不存在也不自动 --once 建库
#
# 产物：PID 与 stdout 落在 log/startup/（已被 .gitignore 忽略）；
#       各写入侧自己的日志仍在各自 runtime/log/ 下，按天轮转保留 7 天。
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
    echo "用法: bash 启动项目.sh [start|status|stop]（默认 start）"
    echo "      DRY_RUN=1 只打印不执行；SKIP_SFTP=1 跳过 12 个 AVL 写入进程"
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
        echo "无启动记录（$LOG_DIR 不存在）"
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
    [[ "$any" == "1" ]] || echo "无启动记录"
    return 0
}

stop() {
    [[ -d "$LOG_DIR" ]] || { echo "无启动记录"; return 0; }
    local f pid pending
    echo "发送 SIGTERM ..."
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
            echo "  $f 超时未退出 → SIGKILL $pid"
            kill -KILL "$pid" 2>/dev/null || true
        fi
        rm -f "$f"
    done
    echo "已停止。日志保留在 $LOG_DIR"
}

launch() {
    local name="$1"; shift
    local pid_file="$LOG_DIR/$name.pid"
    if [[ -f "$pid_file" ]] && alive "$pid_file"; then
        echo "  [跳过] $name 已在运行 pid=$(cat "$pid_file")"
        return 0
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] $name → $PY $*"
        return 0
    fi
    nohup "$PY" "$@" >> "$LOG_DIR/$name.log" 2>&1 < /dev/null &
    local pid=$!
    disown "$pid" 2>/dev/null || true
    echo "$pid" > "$pid_file"
    printf '  [启动] %-18s pid=%-8s → log/startup/%s.log\n' "$name" "$pid" "$name"
}

start() {
    [[ "$DRY_RUN" == "1" ]] || mkdir -p "$LOG_DIR"

    # 防止重复启动：写入侧有单实例锁，重复实例会直接退出；API 会端口冲突。
    local f
    for f in $(pid_files); do
        if alive "$f"; then
            echo "已有进程在运行（$(basename "$f" .pid)）。请先执行：bash 启动项目.sh stop"
            return 1
        fi
        rm -f "$f"
    done

    command -v "$PY" >/dev/null 2>&1 || { echo "找不到 Python：$PY"; return 2; }
    case "$(uname -s)" in
        Linux|Darwin|*BSD) ;;
        *) echo "  [警告] 写入侧依赖 fcntl 文件锁，仅支持 Linux / Unix" ;;
    esac

    if [[ "$SKIP_DEP_CHECK" != "1" ]]; then
        local mods='fastapi, uvicorn, pydantic, aiohttp, loguru, openpyxl'
        [[ "$SKIP_SFTP" == "1" ]] || mods="$mods, paramiko"
        if ! "$PY" -c "import $mods" 2>/dev/null; then
            if [[ "$INSTALL_DEPS" == "1" ]]; then
                echo "[依赖] 自动安装 requirements.txt ..."
                "$PY" -m pip install -r requirements.txt
            else
                echo "[依赖] 缺少运行依赖。请先：python -m pip install -r requirements.txt"
                echo "       或加 INSTALL_DEPS=1 让脚本自动安装。"
                return 2
            fi
        fi
    fi

    for f in IOP/.env call_stat/.env services/.env sftp_avl2db/.env; do
        [[ -f "$f" ]] || echo "  [警告] 缺少 $f（配置可能回落到内置默认值）"
    done

    # 1/3 维表库：在线侧的卖点话术 / 辅助画像全靠它，缺了会大面积 code=2
    echo "[1/3] 维表库"
    if [[ ! -s "$DIM_DB" && "$SKIP_BOOTSTRAP_DB" != "1" ]]; then
        echo "  dimensions.db 不存在 → python -m local_excel2db --once"
        if [[ "$DRY_RUN" == "1" ]]; then
            echo "  [dry-run] $PY -m local_excel2db --once"
        else
            "$PY" -m local_excel2db --once
        fi
    else
        echo "  已存在，跳过初始化（强制重导：python -m local_excel2db --once --force）"
    fi

    # 2/3 业务库首次灌数：需要上游真实文件日期，默认不猜、交给常驻进程自动拉
    echo "[2/3] 业务库补跑"
    if [[ -n "$BOOTSTRAP_AVL_DATE" ]]; then
        [[ "$BOOTSTRAP_AVL_DATE" =~ ^[0-9]{8}$ ]] || {
            echo "  BOOTSTRAP_AVL_DATE 必须是 YYYYMMDD"; return 2; }
        for avl in "${AVL_TASKS[@]}"; do
            if [[ "$DRY_RUN" == "1" ]]; then
                echo "  [dry-run] $PY -m sftp_avl2db --task $avl --date $BOOTSTRAP_AVL_DATE"
            elif "$PY" -m sftp_avl2db --task "$avl" --date "$BOOTSTRAP_AVL_DATE"; then
                echo "  $avl 补跑完成"
            else
                echo "  [警告] $avl 补跑失败（上游无该日期文件或 SFTP 不可达），不阻断启动"
            fi
        done
    else
        echo "  未设 BOOTSTRAP_AVL_DATE，跳过；常驻进程会自动拉取到达的 AVL 文件"
    fi

    # 3/3 常驻进程：先写入侧，最后在线 API
    echo "[3/3] 启动常驻进程（共 14 个；SKIP_SFTP=1 时 2 个）"
    if [[ "$SKIP_SFTP" == "1" ]]; then
        echo "  [跳过] SKIP_SFTP=1，不启动 12 个 AVL 写入进程"
    else
        local avl
        for avl in "${AVL_TASKS[@]}"; do
            launch "avl_$avl" -m sftp_avl2db --task "$avl"
        done
    fi
    launch dim_watcher -m local_excel2db
    launch api main.py

    if [[ "$DRY_RUN" != "1" ]]; then
        echo
        echo "在线 API : http://0.0.0.0:8989/api/speechQryService"
        echo "查看状态 : bash 启动项目.sh status"
        echo "停止全部 : bash 启动项目.sh stop"
        echo "启动日志 : $LOG_DIR/*.log"
    fi
}

case "$ACTION" in
    start)  start ;;
    status) status ;;
    stop)   stop ;;
    -h|--help|help) usage ;;
    *) usage; exit 2 ;;
esac
