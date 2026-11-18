#!/bin/bash
# 本地便捷运行：只 run，不 build。
# 平台执行走 run.sh；本脚本只是把输出目录和输入文件摆好。
set -uo pipefail

cd "$(dirname "$0")"

OUT="${AG_OUTPUT_DIR:-.local/out}"
PARAMS="${AG_PARAMS_FILE:-.local/params.jsonl}"
mkdir -p "${OUT}"

if [ ! -f "${PARAMS}" ]; then
  mkdir -p "$(dirname "${PARAMS}")"
  cat > "${PARAMS}" <<'JSONL'
{"tokenizer":"","endpoint_type":"chat","streaming":true,"input_tokens_mean":1024,"input_tokens_stddev":0,"output_tokens_mean":16,"output_tokens_stddev":0,"concurrency":1,"request_count":10,"random_seed":11}
JSONL
fi

exec env AG_OUTPUT_DIR="${OUT}" bash ./run.sh --params-file "${PARAMS}" "$@"
