# aiperf：AIPerf 性能测试 Runner

对已部署的 OpenAI 兼容 LLM 服务执行 [NVIDIA AIPerf](https://github.com/ai-dynamo/aiperf)
`profile`，并把 `profile_export_aiperf.json` 映射成 Argus `result.json`（schema 2.0）。

## 目录结构

```text
runners/aiperf/
├── runner.yaml        # 平台契约：role/entry/参数/资源/镜像
├── run.sh             # 入口，exec python3 runner.py
├── runner.py          # 运行 AIPerf、解析 JSON、写 result.json
├── build/Dockerfile   # Python 3.12 + aiperf==<build arg>
├── run-local.sh       # 可选，本地便捷运行
├── tests/             # 纯 stdlib 单测
└── README.md
```

## 平台调用契约

平台执行 `entry: ["/runner/run.sh"]`，argv 恒为空；输入三通道：

```text
--params-file <path> > $AG_PARAMS_FILE > /workspace/input/params.jsonl
```

公共参数：

```text
AG_SERVED_URL / AG_SERVED_MODEL_NAME / AG_MODEL_PATH
```

产物：

```text
$AG_OUTPUT_DIR/result.json
$AG_OUTPUT_DIR/aiperf/profile_export_aiperf.json   # AIPerf artifact
$AG_OUTPUT_DIR/logs/aiperf.log
```

退出码：`0` 成功；`1` 业务失败；`2` 参数/输入错误。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `tokenizer` | 空 | `--tokenizer`；缺省依次用 `AG_MODEL_PATH`、模型名；本地路径必须存在 |
| `endpoint_type` | `chat` | AIPerf `--endpoint-type` |
| `streaming` | `true` | 是否流式；流式才有 TTFT/ITL |
| `input_tokens_mean` | `1024` | 合成输入 token 均值 |
| `input_tokens_stddev` | `0` | 合成输入 token 标准差 |
| `output_tokens_mean` | `16` | 输出 token 均值 |
| `output_tokens_stddev` | `0` | 输出 token 标准差 |
| `concurrency` | `1` | 并发 |
| `request_count` | `10` | 请求数 |
| `random_seed` | `11` | 随机种子 |
| `request_timeout_seconds` | `600` | 单请求超时；`<=0` 用 AIPerf 默认 |
| `artifact_subdir` | `aiperf` | artifact 在输出目录下的相对子目录 |
| `allow_request_errors` | `false` | 有失败请求时是否仍按成功返回 |

示例输入（JSONL 单行）：

```json
{"tokenizer":"/models/Qwen3-32B-W8A8","endpoint_type":"chat","streaming":true,"input_tokens_mean":1024,"input_tokens_stddev":0,"output_tokens_mean":16,"output_tokens_stddev":0,"concurrency":1,"request_count":10,"random_seed":11}
```

## 本地运行

不需要容器时：

```bash
cd runners/aiperf
chmod +x run.sh
echo '{"tokenizer":"/models/Qwen3-32B-W8A8","concurrency":1,"request_count":10}' \
  > /tmp/params.jsonl

AG_OUTPUT_DIR=.local/out \
AG_SERVED_URL=http://127.0.0.1:7802 \
AG_SERVED_MODEL_NAME=qwen3 \
./run.sh --params-file /tmp/params.jsonl
```

检查：

```bash
jq . .local/out/result.json
tail -n 50 .local/out/logs/aiperf.log
```

前提是本机已安装 `aiperf`，且 tokenizer 路径存在。

## 构建镜像

```bash
cd runners/aiperf
docker build -f build/Dockerfile \
  --build-arg AIPERF_VERSION=0.13.0 \
  -t registry.dev.huawei.com/argus/aiperf:1.0.0 .
```

把 `runner.yaml.image` 改成实际镜像；CI 也按 `runners/*/build/Dockerfile` 发现构建。

## result.json 指标

按 AIPerf 原生 metric 名输出，保留 `unit` 与统计量：

```text
time_to_first_token / time_to_second_token / time_to_first_output_token
inter_token_latency / request_latency
output_token_throughput / output_token_throughput_per_user
request_throughput / request_count / error_request_count / request_error_rate
input_sequence_length / output_sequence_length
total_input_sequence_length / total_output_tokens / benchmark_duration
```

AIPerf 的 `avg` 映射为 Argus `mean`；`p50/p90/p95/p99/min/max/sum` 原样映射。
`count` 映射为 `sample_size`。缺失的 metric 不补零，避免把“没有数据”误读成 0。

## 失败分类

| code | 场景 |
|---|---|
| `VALIDATION_ERROR` | 参数/输入/URL/model/tokenizer 不合法 |
| `AIPERF_FAILED` | AIPerf 非零退出 |
| `AIPERF_NO_RESULT` | 未产出 `profile_export_aiperf.json` |
| `AIPERF_INVALID_RESULT` | JSON 无法解析 |
| `AIPERF_NO_METRICS` | 没有可映射指标 |
| `AIPERF_CANCELLED` | AIPerf 报告 `was_cancelled=true` |
| `AIPERF_REQUESTS_FAILED` | 有失败请求且 `allow_request_errors=false` |

## 注意

- AIPerf 版本由 Docker build arg 固定；升级版本要同时 bump `runner.yaml.version`。
- 本地 tokenizer 目录必须挂载进 Runner Pod；否则用 `builtin` 或 HF 模型名，或去掉 `tokenizer` 让 AIPerf 自动推断。
- 平台默认不会挂载 `/models`；部署模式下优先使用 `AG_MODEL_PATH`，并确认该路径在 Runner 容器内可见。
- 不要把 `ENDPOINT_API_KEY` 写进日志或 `result.json`。
