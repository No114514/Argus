# Argus Runner: aiperf
#
# 构建上下文 = runners/aiperf：
#   cd runners/aiperf
#   docker build -f build/Dockerfile \
#     --build-arg AIPERF_VERSION=0.13.0 \
#     -t registry.dev.huawei.com/argus/aiperf:1.0.0 .
#
# AIPerf 版本固定在 build arg；升级时同时更新 runner.yaml.version。
FROM python:3.12-slim

ARG AIPERF_VERSION=0.13.0
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "aiperf==${AIPERF_VERSION}" \
 && groupadd -g 10001 runner \
 && useradd -u 10001 -g runner -m -s /sbin/nologin runner \
 && mkdir -p /workspace/input /workspace/output /runner \
 && chown -R 10001:10001 /workspace /runner

COPY run.sh /runner/run.sh
COPY runner.py /runner/runner.py
RUN chmod 0755 /runner/run.sh /runner/runner.py

USER 10001:10001
WORKDIR /workspace

ENTRYPOINT ["/runner/run.sh"]
