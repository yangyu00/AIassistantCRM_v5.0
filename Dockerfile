# syntax=docker/dockerfile:1
# AIassistantCRM_v5.0 —— 在线 API + 写入侧一体化镜像
#
# 构建：docker build -t aiassistantcrm:v5.0 .
# 运行（默认只起在线 API，适合本地联调）：
#   docker run -d --name crm -p 8990:8990 aiassistantcrm:v5.0
# 运行（起 12 个 AVL 写入进程 + 维表轮询 + 在线 API，共 14 个进程）：
#   docker run -d --name crm -p 8990:8990 aiassistantcrm:v5.0 full
#
# 整目录挂载（推荐：改代码 / 改 .env 不用重建镜像，runtime 与 log 落宿主机）：
#   docker run -d --name crm -p 8991:8991 \
#     -v /opt/AIassistantCRM_v5.0:/app \
#     -e CRM_PORT=8991 \
#     aiassistantcrm:v5.0 full
#   挂进去的目录必须是 codeload tarball 解出的那套代码；运行时产物可用
#   命名卷嵌套挂载隔离（见 docker-entrypoint.sh 顶部注释）。
# 也可以只挂单个配置文件：
#   -v /your/conf/IOP/.env:/app/IOP/.env -v /your/conf/sftp_avl2db/.env:/app/sftp_avl2db/.env
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PYTHONPATH=/app \
    TZ=Asia/Shanghai

WORKDIR /app

# 上游 HTTPS / SFTP 需要 CA 证书；其余依赖全部有预编译 wheel，无需编译工具链
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 项目各组件只认包内 .env、不读系统环境变量；这里用仓库自带的 .env.example
# 初始化默认配置，并预生成维表库，让镜像开箱即可响应请求。
RUN set -eu; \
    for d in IOP call_stat services sftp_avl2db; do \
        [ -f "$d/.env" ] || cp "$d/.env.example" "$d/.env"; \
    done; \
    [ -f .env ] || printf 'CRM_PORT=8990\n' > .env; \
    mkdir -p log/startup \
             call_stat/runtime call_stat/runtime/export \
             local_excel2db/runtime/sqlite_db local_excel2db/runtime/state local_excel2db/runtime/log \
             sftp_avl2db/runtime/sqlite_db sftp_avl2db/runtime/local_files \
             sftp_avl2db/runtime/state sftp_avl2db/runtime/log; \
    python -m local_excel2db --once; \
    install -m 0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 8990

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["api"]
