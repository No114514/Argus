#!/usr/bin/env python3
"""Argus Runner: aiperf.

Runs NVIDIA AIPerf against an OpenAI-compatible endpoint and converts
profile_export_aiperf.json into Argus result.json (schema 2.0).

Contract:
  - business input: JSONL file (--params-file > AG_PARAMS_FILE >
    /workspace/input/params.jsonl)
  - public input: AG_SERVED_URL / AG_SERVED_MODEL_NAME / AG_MODEL_PATH
  - output: $AG_OUTPUT_DIR/result.json + aiperf/ artifacts + logs/
  - exit: 0 succeeded, 1 business failure, 2 validation/input error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NAME = "aiperf"
VERSION = "1.0.0"
SCHEMA_VERSION = "2.0"

DEFAULTS: dict[str, Any] = {
    "tokenizer": "",
    "endpoint_type": "chat",
    "streaming": True,
    "input_tokens_mean": 1024,
    "input_tokens_stddev": 0,
    "output_tokens_mean": 16,
    "output_tokens_stddev": 0,
    "concurrency": 1,
    "request_count": 10,
    "random_seed": 11,
    "request_timeout_seconds": 600,
    "artifact_subdir": "aiperf",
    "allow_request_errors": False,
}

ALLOWED_KEYS = set(DEFAULTS)

# Metrics that are useful in Argus reports. Unknown AIPerf metrics are ignored
# so a future AIPerf version adding fields does not break result validation.
METRIC_KEYS = (
    "time_to_first_token",
    "time_to_second_token",
    "time_to_first_output_token",
    "inter_token_latency",
    "request_latency",
    "output_token_throughput",
    "output_token_throughput_per_user",
    "request_throughput",
    "request_count",
    "error_request_count",
    "request_error_rate",
    "output_sequence_length",
    "input_sequence_length",
    "total_output_tokens",
    "total_input_sequence_length",
    "benchmark_duration",
)

STAT_MAP = (
    ("avg", "mean"),
    ("p50", "p50"),
    ("p90", "p90"),
    ("p95", "p95"),
    ("p99", "p99"),
    ("min", "min"),
    ("max", "max"),
    ("sum", "sum"),
    ("count", "count"),
)

PROXY_KEYS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


class ValidationError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def output_dir() -> Path:
    raw = os.environ.get("AG_OUTPUT_DIR", "/workspace/output")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    (path / "logs").mkdir(parents=True, exist_ok=True)
    return path


def input_path(cli_path: str | None) -> tuple[Path | None, bool]:
    """Return (path, explicitly_requested)."""
    if cli_path:
        return Path(cli_path), True
    env_path = os.environ.get("AG_PARAMS_FILE", "").strip()
    if env_path:
        return Path(env_path), True
    default = Path("/workspace/input/params.jsonl")
    if default.is_file():
        return default, False
    return None, False


def read_params(path: Path | None) -> dict[str, Any]:
    params = dict(DEFAULTS)
    if path is None:
        return params

    if not path.is_file():
        raise ValidationError(f"输入文件不存在或不可读：{path}")

    merged: dict[str, Any] = {}
    # utf-8-sig tolerates a Windows-created BOM; platform input is plain UTF-8.
    with path.open("r", encoding="utf-8-sig") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"输入第 {lineno} 行不是合法 JSON：{exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise ValidationError(f"输入第 {lineno} 行不是 JSON 对象")
            unknown = sorted(set(value) - ALLOWED_KEYS)
            if unknown:
                raise ValidationError(
                    f"输入第 {lineno} 行含未声明字段：{', '.join(unknown)}"
                )
            merged.update(value)

    params.update(merged)
    return validate_params(params)


def _require_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} 必须是 boolean")
    return value


def _require_int(name: str, value: Any, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} 必须是 integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{name} 必须 >= {minimum}")
    return value


def validate_params(params: dict[str, Any]) -> dict[str, Any]:
    tokenizer = params.get("tokenizer")
    if not isinstance(tokenizer, str):
        raise ValidationError("tokenizer 必须是 string")

    endpoint_type = params.get("endpoint_type")
    if not isinstance(endpoint_type, str) or not endpoint_type.strip():
        raise ValidationError("endpoint_type 必须是非空 string")

    params["streaming"] = _require_bool("streaming", params["streaming"])
    for key in (
        "input_tokens_mean",
        "input_tokens_stddev",
        "output_tokens_mean",
        "output_tokens_stddev",
    ):
        params[key] = _require_int(key, params[key], 0)
    params["concurrency"] = _require_int("concurrency", params["concurrency"], 1)
    params["request_count"] = _require_int(
        "request_count", params["request_count"], 1
    )
    params["random_seed"] = _require_int("random_seed", params["random_seed"])
    params["request_timeout_seconds"] = _require_int(
        "request_timeout_seconds", params["request_timeout_seconds"], 0
    )
    params["allow_request_errors"] = _require_bool(
        "allow_request_errors", params["allow_request_errors"]
    )

    subdir = params.get("artifact_subdir")
    if not isinstance(subdir, str) or not subdir.strip():
        raise ValidationError("artifact_subdir 必须是非空 string")
    sub_path = Path(subdir)
    if (
        sub_path.is_absolute()
        or not sub_path.parts
        or ".." in sub_path.parts
    ):
        raise ValidationError("artifact_subdir 必须是不含 .. 的相对路径")
    params["artifact_subdir"] = subdir.strip()
    params["tokenizer"] = tokenizer.strip()
    params["endpoint_type"] = endpoint_type.strip()
    return params


def normalize_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if not url:
        raise ValidationError("缺少 AG_SERVED_URL 或 --url")
    if "://" not in url:
        url = "http://" + url
    # AIPerf expects the server root and appends /v1/... itself. Argus
    # service URLs commonly carry /v1, so strip exactly one trailing /v1.
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


def resolve_tokenizer(params: dict[str, Any]) -> str:
    tokenizer = params["tokenizer"]
    if tokenizer:
        return tokenizer
    model_path = os.environ.get("AG_MODEL_PATH", "").strip()
    if model_path:
        return model_path
    # Let AIPerf auto-detect from the model name when nothing is supplied.
    return ""


def build_command(
    *,
    url: str,
    model: str,
    tokenizer: str,
    params: dict[str, Any],
    artifact_dir: Path,
    api_key: str,
) -> list[str]:
    cmd = [
        "aiperf",
        "profile",
        "--model",
        model,
        "--url",
        url,
        "--endpoint-type",
        params["endpoint_type"],
        "--synthetic-input-tokens-mean",
        str(params["input_tokens_mean"]),
        "--synthetic-input-tokens-stddev",
        str(params["input_tokens_stddev"]),
        "--output-tokens-mean",
        str(params["output_tokens_mean"]),
        "--output-tokens-stddev",
        str(params["output_tokens_stddev"]),
        "--concurrency",
        str(params["concurrency"]),
        "--request-count",
        str(params["request_count"]),
        "--random-seed",
        str(params["random_seed"]),
        "--artifact-dir",
        str(artifact_dir),
        "--ui-type",
        "none",
    ]
    if params["streaming"]:
        cmd.append("--streaming")
    if tokenizer:
        cmd.extend(["--tokenizer", tokenizer])
    if api_key:
        cmd.extend(["--api-key", api_key])
    if params["request_timeout_seconds"] > 0:
        cmd.extend(
            ["--request-timeout-seconds", str(params["request_timeout_seconds"])]
        )
    return cmd


def redact_command(cmd: list[str]) -> str:
    safe = list(cmd)
    if "--api-key" in safe:
        idx = safe.index("--api-key")
        if idx + 1 < len(safe):
            safe[idx + 1] = "***"
    return " ".join(safe)


def run_aiperf(cmd: list[str], log_path: Path) -> int:
    env = os.environ.copy()
    for key in PROXY_KEYS:
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env.setdefault("PYTHONUNBUFFERED", "1")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {redact_command(cmd)}\n")
        log.flush()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
        except FileNotFoundError:
            log.write("aiperf executable not found in image\n")
            return 127

        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        return proc.wait()


def find_export(artifact_dir: Path) -> Path | None:
    exact = artifact_dir / "profile_export_aiperf.json"
    if exact.is_file():
        return exact
    matches = sorted(artifact_dir.rglob("profile_export_aiperf.json"))
    return matches[-1] if matches else None


def to_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def build_metrics(export: dict[str, Any]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for key in METRIC_KEYS:
        block = export.get(key)
        if not isinstance(block, dict):
            continue
        unit = block.get("unit")
        unit = unit if isinstance(unit, str) and unit else "value"
        count = block.get("count")
        sample_size = (
            int(count)
            if isinstance(count, int) and not isinstance(count, bool) and count > 0
            else None
        )
        for source_stat, target_stat in STAT_MAP:
            value = to_float(block.get(source_stat))
            if value is None:
                continue
            identity = (key, target_stat)
            if identity in seen:
                continue
            seen.add(identity)
            item: dict[str, Any] = {
                "name": key,
                "value": value,
                "unit": unit,
                "statistic": target_stat,
            }
            if sample_size is not None:
                item["sample_size"] = sample_size
            metrics.append(item)
    return metrics


def error_payload(
    code: str,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "detail": detail or {},
        "retryable": retryable,
    }


def write_result(
    out: Path,
    *,
    status: str,
    started_at: str,
    workload: dict[str, Any],
    metrics: list[dict[str, Any]],
    error: dict[str, Any] | None,
    artifacts: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    finished_at = utc_now()
    started = datetime.fromisoformat(started_at)
    finished = datetime.fromisoformat(finished_at)
    result = {
        "schema_version": SCHEMA_VERSION,
        "task_id": os.environ.get("AG_TASK_ID", "local"),
        "attempt_no": env_int("AG_ATTEMPT", 1),
        "runner_seq": env_int("AG_RUNNER_SEQ", 1),
        "runner": {
            "name": NAME,
            "version": VERSION,
            "image_ref": os.environ.get("AG_RUNNER_IMAGE", "unknown"),
        },
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": max(0, int((finished - started).total_seconds() * 1000)),
        "workload": workload,
        "metrics": metrics,
        "error": error,
    }
    if artifacts:
        result["artifacts"] = artifacts
    if metadata:
        result["metadata"] = metadata

    target = out / "result.json"
    tmp = out / ".result.json.tmp"
    tmp.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AIPerf and write Argus result.json")
    parser.add_argument("--params-file")
    parser.add_argument("--url")
    parser.add_argument("--model_name")
    parser.add_argument("--model_path")
    args = parser.parse_args()

    out = output_dir()
    started_at = utc_now()

    params_path, explicit = input_path(args.params_file)
    if explicit and params_path is not None and not params_path.is_file():
        # A path supplied explicitly must exist; a missing conventional
        # /workspace/input/params.jsonl is allowed to mean "all defaults".
        message = f"输入文件不存在或不可读：{params_path}"
        print(f"[aiperf] {message}", file=sys.stderr)
        write_result(
            out,
            status="failed",
            started_at=started_at,
            workload={},
            metrics=[],
            error=error_payload("VALIDATION_ERROR", message),
            artifacts=[],
            metadata={},
        )
        return 2

    try:
        params = read_params(params_path)
    except ValidationError as exc:
        message = str(exc)
        print(f"[aiperf] {message}", file=sys.stderr)
        write_result(
            out,
            status="failed",
            started_at=started_at,
            workload={},
            metrics=[],
            error=error_payload("VALIDATION_ERROR", message),
            artifacts=[],
            metadata={},
        )
        return 2

    try:
        url = normalize_url(args.url or os.environ.get("AG_SERVED_URL", ""))
        model = (args.model_name or os.environ.get("AG_SERVED_MODEL_NAME", "")).strip()
        if not model:
            raise ValidationError("缺少 AG_SERVED_MODEL_NAME 或 --model_name")
        tokenizer = resolve_tokenizer(params)
        if args.model_path:
            tokenizer = args.model_path
        if tokenizer.startswith("/") and not Path(tokenizer).exists():
            raise ValidationError(
                f"tokenizer 路径不存在：{tokenizer}；请挂载模型目录或改用 builtin/HF 名称"
            )
        artifact_dir = out / params["artifact_subdir"]
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except ValidationError as exc:
        message = str(exc)
        print(f"[aiperf] {message}", file=sys.stderr)
        write_result(
            out,
            status="failed",
            started_at=started_at,
            workload={},
            metrics=[],
            error=error_payload("VALIDATION_ERROR", message),
            artifacts=[],
            metadata={},
        )
        return 2

    workload = {
        "base_url": url,
        "model_name": model,
        "tokenizer": tokenizer,
        "endpoint_type": params["endpoint_type"],
        "streaming": params["streaming"],
        "input_tokens_mean": params["input_tokens_mean"],
        "input_tokens_stddev": params["input_tokens_stddev"],
        "output_tokens_mean": params["output_tokens_mean"],
        "output_tokens_stddev": params["output_tokens_stddev"],
        "concurrency": params["concurrency"],
        "request_count": params["request_count"],
        "random_seed": params["random_seed"],
    }

    cmd = build_command(
        url=url,
        model=model,
        tokenizer=tokenizer,
        params=params,
        artifact_dir=artifact_dir,
        api_key=os.environ.get("ENDPOINT_API_KEY", "").strip(),
    )
    log_path = out / "logs" / "aiperf.log"
    print(f"[aiperf] {redact_command(cmd)}", flush=True)
    return_code = run_aiperf(cmd, log_path)

    export_path = find_export(artifact_dir)
    export: dict[str, Any] = {}
    if export_path is not None:
        try:
            export = json.loads(export_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            message = f"读取 AIPerf JSON 失败：{exc}"
            write_result(
                out,
                status="failed",
                started_at=started_at,
                workload=workload,
                metrics=[],
                error=error_payload("AIPERF_INVALID_RESULT", message),
                artifacts=[],
                metadata={},
            )
            return 1
    else:
        export = {}

    metrics = build_metrics(export) if export else []
    artifacts: list[dict[str, Any]] = []
    if export_path is not None:
        try:
            rel = export_path.relative_to(out)
        except ValueError:
            rel = export_path
        artifacts.append(
            {
                "name": "aiperf-profile-export",
                "path": str(rel),
                "size_bytes": export_path.stat().st_size,
            }
        )

    metadata = {
        "aiperf_schema_version": export.get("schema_version"),
        "aiperf_version": export.get("aiperf_version"),
        "benchmark_id": export.get("benchmark_id"),
        "aiperf_return_code": return_code,
    }
    metadata = {k: v for k, v in metadata.items() if v is not None}

    if return_code != 0:
        message = f"AIPerf 退出码 {return_code}，详见 logs/aiperf.log"
        write_result(
            out,
            status="failed",
            started_at=started_at,
            workload=workload,
            metrics=metrics,
            error=error_payload(
                "AIPERF_FAILED", message, detail={"exit_code": return_code}
            ),
            artifacts=artifacts,
            metadata=metadata,
        )
        return 1

    if not export:
        message = f"AIPerf 未产出 profile_export_aiperf.json（{artifact_dir}）"
        write_result(
            out,
            status="failed",
            started_at=started_at,
            workload=workload,
            metrics=[],
            error=error_payload("AIPERF_NO_RESULT", message),
            artifacts=[],
            metadata=metadata,
        )
        return 1

    if export.get("was_cancelled") is True:
        message = "AIPerf 报告本次运行被取消"
        write_result(
            out,
            status="failed",
            started_at=started_at,
            workload=workload,
            metrics=metrics,
            error=error_payload("AIPERF_CANCELLED", message),
            artifacts=artifacts,
            metadata=metadata,
        )
        return 1

    if not metrics:
        message = "AIPerf 结果中没有可映射的指标"
        write_result(
            out,
            status="failed",
            started_at=started_at,
            workload=workload,
            metrics=[],
            error=error_payload("AIPERF_NO_METRICS", message),
            artifacts=artifacts,
            metadata=metadata,
        )
        return 1

    error_count = export.get("error_request_count")
    error_count_value = to_float(
        error_count.get("avg") if isinstance(error_count, dict) else None
    )
    if (
        error_count_value is not None
        and error_count_value > 0
        and not params["allow_request_errors"]
    ):
        message = (
            f"AIPerf 有 {int(error_count_value)} 个失败请求；"
            "设置 allow_request_errors=true 可放宽"
        )
        write_result(
            out,
            status="failed",
            started_at=started_at,
            workload=workload,
            metrics=metrics,
            error=error_payload("AIPERF_REQUESTS_FAILED", message),
            artifacts=artifacts,
            metadata=metadata,
        )
        return 1

    write_result(
        out,
        status="succeeded",
        started_at=started_at,
        workload=workload,
        metrics=metrics,
        error=None,
        artifacts=artifacts,
        metadata=metadata,
    )
    print(f"[aiperf] result: {out / 'result.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
