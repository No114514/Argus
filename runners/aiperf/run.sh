#!/bin/bash
# Argus Runner: aiperf
#
# 平台调用形态：entry=["/runner/run.sh"]，argv 为空；业务参数走
# --params-file > AG_PARAMS_FILE > /workspace/input/params.jsonl，
# 公共参数走 AG_SERVED_URL / AG_SERVED_MODEL_NAME / AG_MODEL_PATH。
#
# 独立运行：
#   AG_OUTPUT_DIR=.local/out ./run.sh \
#     --params-file /tmp/params.jsonl \
#     --url http://127.0.0.1:7802 \
#     --model_name qwen3
#
# 退出码：0 成功；1 业务失败；2 参数/输入错误。
set -uo pipefail

exec python3 /runner/runner.py "$@"
