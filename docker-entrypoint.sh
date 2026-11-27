#!/bin/sh
# AIassistantCRM_v5.0 容器入口：PID 1，负责信号转发与首次初始化。
#   api   —— 只起在线 API（默认）
#   full  —— 12 个 AVL 写入进程 + 维表轮询 + 在线 API（共 14 个进程）
#   shell —— 进入容器调试
#   其它  —— 原样 exec，例如 python -m local_excel2db --once
#
# 支持整目录挂载：-v /host/AIassistantCRM_v5.0:/app
#   · 代码 / 各包 .env / excels/ 全部来自宿主机，改完重启容器即生效
#   · 解释器与 site-packages 在 /usr/local，不会被 /app 覆盖
#   · 本脚本装在 /usr/local/bin，也不会被 /app 覆盖
#   · runtime/ log/ 等产物会写回宿主机目录；不想污染可再嵌套挂命名卷
#
# 可选环境变量：
#   CRM_PORT=9100      覆盖 /app/.env 的在线端口（会写回挂载目录）
#   SKIP_ENV_WRITE=1   不改写已有 /app/.env（自行维护 CRM_PORT 时用）
set -eu

cd /app

# 整目录挂载最常见的踩坑：挂错代码版本或挂成空目录
if [ ! -f app_config.py ] || [ ! -f local_excel2db/registry.json ]; then
    echo "[entrypoint][ERROR] /app 不是本项目根目录" >&2
    echo "  期望包含: app_config.py requirements.txt main.py IOP/ call_stat/ local_excel2db/ sftp_avl2db/ services/ excels/" >&2
    echo "  请挂载 codeload tarball 解出的目录（不要用其它代码快照）" >&2
    exit 2
fi

write_root_env() {
    _port="$1"
    _tmp="$(mktemp)"
    if [ -f .env ] && grep -q '^CRM_PORT=' .env; then
        sed "s/^CRM_PORT=.*/CRM_PORT=${_port}/" .env > "$_tmp"
    elif [ -f .env ]; then
        cat .env > "$_tmp"
        printf 'CRM_PORT=%s\n' "$_port" >> "$_tmp"
    else
        printf '# 在线服务端口\nCRM_PORT=%s\n' "$_port" > "$_tmp"
    fi
    cat "$_tmp" > .env
    rm -f "$_tmp"
}

if [ ! -f .env ]; then
    write_root_env "${CRM_PORT:-8989}"
elif [ -n "${CRM_PORT:-}" ] && [ "${SKIP_ENV_WRITE:-0}" != "1" ]; then
    echo "[entrypoint] CRM_PORT=${CRM_PORT} 写入 /app/.env（整目录挂载时会同步改宿主机文件；SKIP_ENV_WRITE=1 可禁用）"
    write_root_env "$CRM_PORT"
elif [ -n "${CRM_PORT:-}" ]; then
    echo "[entrypoint] SKIP_ENV_WRITE=1，端口以挂载目录 .env 为准（忽略 CRM_PORT=${CRM_PORT}）"
fi

mkdir -p log/startup \
         call_stat/runtime call_stat/runtime/export \
         local_excel2db/runtime/sqlite_db local_excel2db/runtime/state local_excel2db/runtime/log \
         sftp_avl2db/runtime/sqlite_db sftp_avl2db/runtime/local_files \
         sftp_avl2db/runtime/state sftp_avl2db/runtime/log

# 整目录挂载会遮蔽镜像里预灌的 dimensions.db，首次启动自动补建
if [ ! -s local_excel2db/runtime/sqlite_db/dimensions.db ]; then
    echo "[entrypoint] dimensions.db 缺失，首次导入 excels/*.xlsx（约数十秒）..."
    python3 -m local_excel2db --once || echo "[warn] dimensions.db 初始化失败，API 将降级返回" >&2
fi

MODE="${1:-api}"
case "$MODE" in
  api)
    shift 2>/dev/null || true
    exec python3 main.py "$@"
    ;;

  full)
    shift 2>/dev/null || true
    AVL_TASKS="F09554 F09555 F09556 F09591 F09627 F09628 F09629 F09630 F09631 F09632 F09633 F09635"
    pids=""
    for avl in $AVL_TASKS; do
        python3 -m sftp_avl2db --task "$avl" &
        pids="$pids $!"
    done
    python3 -m local_excel2db &
    pids="$pids $!"

    python3 main.py &
    api_pid=$!

    terminate() {
        trap - TERM INT
        kill -TERM $pids "$api_pid" 2>/dev/null || true
        wait 2>/dev/null || true
    }
    trap terminate TERM INT
    # API 是主进程，它退出则容器退出，其余后台进程由 trap 收尾
    wait "$api_pid"
    ;;

  shell)
    exec /bin/sh
    ;;

  *)
    exec "$@"
    ;;
esac
