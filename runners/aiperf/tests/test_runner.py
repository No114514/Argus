"""Pure-stdlib unit tests for the aiperf runner helper functions."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner  # noqa: E402


class TestParams(unittest.TestCase):
    def test_defaults(self) -> None:
        params = runner.validate_params(dict(runner.DEFAULTS))
        self.assertEqual(params["endpoint_type"], "chat")
        self.assertTrue(params["streaming"])
        self.assertEqual(params["request_count"], 10)

    def test_unknown_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "params.jsonl"
            path.write_text('{"nope": 1}\n', encoding="utf-8")
            with self.assertRaises(runner.ValidationError):
                runner.read_params(path)

    def test_bad_concurrency(self) -> None:
        params = dict(runner.DEFAULTS)
        params["concurrency"] = 0
        with self.assertRaises(runner.ValidationError):
            runner.validate_params(params)

    def test_artifact_subdir_rejects_escape(self) -> None:
        params = dict(runner.DEFAULTS)
        params["artifact_subdir"] = "../outside"
        with self.assertRaises(runner.ValidationError):
            runner.validate_params(params)


class TestMetrics(unittest.TestCase):
    def test_avg_maps_to_mean_and_sample_size(self) -> None:
        export = {
            "time_to_first_token": {
                "unit": "ms",
                "avg": 125.5,
                "p50": 120.0,
                "p99": 150.0,
                "count": 10,
            },
            "request_latency": {"unit": "ms", "avg": 500.0},
            "output_token_throughput": {"unit": "tokens/sec", "avg": 1000.0},
            "request_count": {"unit": "requests", "avg": 10.0},
        }
        metrics = runner.build_metrics(export)
        names = {(m["name"], m["statistic"]) for m in metrics}
        self.assertIn(("time_to_first_token", "mean"), names)
        self.assertIn(("time_to_first_token", "p50"), names)
        self.assertIn(("time_to_first_token", "count"), names)
        self.assertIn(("request_latency", "mean"), names)
        ttft_mean = next(
            m
            for m in metrics
            if m["name"] == "time_to_first_token" and m["statistic"] == "mean"
        )
        self.assertEqual(ttft_mean["sample_size"], 10)

    def test_missing_metric_is_not_zero_filled(self) -> None:
        metrics = runner.build_metrics({"request_latency": {"unit": "ms", "avg": 1.0}})
        self.assertEqual(
            [m["name"] for m in metrics],
            ["request_latency"],
        )


class TestUrl(unittest.TestCase):
    def test_strips_v1_and_scheme(self) -> None:
        self.assertEqual(
            runner.normalize_url("http://127.0.0.1:7802/v1/"),
            "http://127.0.0.1:7802",
        )
        self.assertEqual(
            runner.normalize_url("127.0.0.1:7802"),
            "http://127.0.0.1:7802",
        )


if __name__ == "__main__":
    unittest.main()
