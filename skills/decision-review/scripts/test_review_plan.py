"""Offline regression checks for the host-side DS review helper.

Run with ``python3 -m unittest discover -s scripts -p 'test_review_plan.py'``
from the skill directory. No real gateway configuration or network is used.
Set ``REVIEW_PLAN_MODULE`` to run the same checks against another helper file.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


MAX_INPUT_BYTES = 256 * 1024
PRIVATE_SENTINEL = "private-child-data-must-not-appear"
HELPER_PATH = Path(os.environ.get("REVIEW_PLAN_MODULE", Path(__file__).with_name("review_plan.py"))).resolve()
SPEC = importlib.util.spec_from_file_location("review_plan_under_test", HELPER_PATH)
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class RecordingInput(io.BytesIO):
    """Record how much the helper asks to read and actually consumes."""

    def __init__(self, raw):
        super().__init__(raw)
        self.read_sizes = []
        self.returned_bytes = 0

    def read(self, size=-1):
        self.read_sizes.append(size)
        result = super().read(size)
        self.returned_bytes += len(result)
        return result


class ReviewPlanTest(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory(prefix="review-plan-test-")
        self.addCleanup(self.temp_directory.cleanup)
        self.directory = Path(self.temp_directory.name)
        self.request_path = self.directory / "request.json"
        self.binary_path = self.directory / "fake-review-binary"
        self.config_path = self.directory / "fake-gateway.yaml"
        self.binary_path.touch()
        self.config_path.write_text("test fixture only, not real credentials\n")
        self.request = {
            "request_id": "offline-regression-task",
            "question": "这个方案可以继续吗？",
            "context": "用本地模拟验证，不联网。",
            "plan": "先确认问题，再修复和验收。",
        }
        self.result = {
            "request_id": self.request["request_id"],
            "status": "reviewed",
            "mode": "plan_review",
            "plan_source": "provided",
            "advisors": [{
                "name": "deepseek",
                "status": "success",
                "provider": "deepseek",
                "actual_model": "offline-test-model",
                "content": "  第一条意见：保留现有范围。\n第二条：补充边界验收。\n",
            }],
        }

    def raw_request(self, size=None):
        raw = json.dumps(self.request, ensure_ascii=False).encode("utf-8")
        if size is not None:
            self.assertLessEqual(len(raw), size)
            raw += b" " * (size - len(raw))
        return raw

    def invoke(self, raw=None, *, stdin=False, response=None, side_effect=None):
        """Run main with synthetic input, a fake runtime and a mocked child."""
        stream = RecordingInput(self.raw_request() if raw is None else raw)
        stdout, stderr = io.StringIO(), io.StringIO()
        runtime_path = HELPER_PATH.resolve().parents[1] / "runtime.json"
        runtime = json.dumps({
            "binary": str(self.binary_path),
            "gateway_config": str(self.config_path),
            "model": "offline-test-alias",
        })

        def fake_read_text(path, *args, **kwargs):
            self.assertEqual(path, runtime_path, "unexpected configuration read")
            return runtime

        def fake_open(path, *args, **kwargs):
            self.assertEqual(path, self.request_path, "unexpected file read")
            return stream

        if response is None:
            response = subprocess.CompletedProcess(
                [], 0, json.dumps(self.result, ensure_ascii=False).encode("utf-8"), b""
            )
        argv = ["review_plan.py", "--request-file", "-" if stdin else str(self.request_path)]
        with (
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdin", SimpleNamespace(buffer=stream)),
            patch.object(Path, "read_text", fake_read_text),
            patch.object(Path, "open", fake_open),
            patch.object(HELPER.subprocess, "run", return_value=response, side_effect=side_effect) as run,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = HELPER.main()
        return SimpleNamespace(
            code=code,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            stream=stream,
            child=run,
        )

    def assert_failed_without_success(self, outcome):
        self.assertNotEqual(outcome.code, 0)
        self.assertEqual(outcome.stdout, "")
        self.assertTrue(outcome.stderr)
        self.assertNotIn(PRIVATE_SENTINEL, outcome.stdout + outcome.stderr)

    def test_file_read_is_bounded_before_rejecting_oversize(self):
        outcome = self.invoke(self.raw_request(MAX_INPUT_BYTES + 4096))
        self.assert_failed_without_success(outcome)
        outcome.child.assert_not_called()
        self.assertEqual(outcome.stream.read_sizes, [MAX_INPUT_BYTES + 1])
        self.assertEqual(outcome.stream.returned_bytes, MAX_INPUT_BYTES + 1)

    def test_stdin_read_is_bounded_before_rejecting_oversize(self):
        outcome = self.invoke(self.raw_request(MAX_INPUT_BYTES + 4096), stdin=True)
        self.assert_failed_without_success(outcome)
        outcome.child.assert_not_called()
        self.assertEqual(outcome.stream.read_sizes, [MAX_INPUT_BYTES + 1])
        self.assertEqual(outcome.stream.returned_bytes, MAX_INPUT_BYTES + 1)

    def test_exact_input_limit_accepts_valid_json_for_file_and_stdin(self):
        for stdin in (False, True):
            with self.subTest(stdin=stdin):
                outcome = self.invoke(self.raw_request(MAX_INPUT_BYTES), stdin=stdin)
                self.assertEqual(outcome.code, 0, outcome.stderr)
                outcome.child.assert_called_once()
                self.assertEqual(json.loads(outcome.stdout), self.result)

    def test_one_byte_over_input_limit_rejects_before_child_for_file_and_stdin(self):
        for stdin in (False, True):
            with self.subTest(stdin=stdin):
                outcome = self.invoke(self.raw_request(MAX_INPUT_BYTES + 1), stdin=stdin)
                self.assert_failed_without_success(outcome)
                outcome.child.assert_not_called()

    def test_child_timeout_has_safe_deadline_classification(self):
        expired = subprocess.TimeoutExpired(
            ["fake-review-binary"], 180,
            output=PRIVATE_SENTINEL.encode("utf-8"),
            stderr=PRIVATE_SENTINEL.encode("utf-8"),
        )
        outcome = self.invoke(side_effect=expired)
        self.assert_failed_without_success(outcome)
        outcome.child.assert_called_once()
        self.assertIn("deadline_exceeded", outcome.stderr)

    def test_invalid_utf8_fails_before_child_without_timeout_classification(self):
        for stdin in (False, True):
            with self.subTest(stdin=stdin):
                outcome = self.invoke(b'{"question":"\xe4\xb8","plan":"p"}', stdin=stdin)
                self.assert_failed_without_success(outcome)
                outcome.child.assert_not_called()
                self.assertNotIn("deadline_exceeded", outcome.stderr)

    def test_child_start_failure_is_not_misclassified_as_timeout(self):
        outcome = self.invoke(side_effect=OSError(PRIVATE_SENTINEL))
        self.assert_failed_without_success(outcome)
        outcome.child.assert_called_once()
        self.assertNotIn("deadline_exceeded", outcome.stderr)

    def test_success_preserves_feedback_and_forces_review_only(self):
        self.request["return_review_only"] = False
        outcome = self.invoke()
        self.assertEqual(outcome.code, 0, outcome.stderr)
        self.assertEqual(outcome.stderr, "")
        self.assertEqual(json.loads(outcome.stdout), self.result)
        outcome.child.assert_called_once()
        arguments, kwargs = outcome.child.call_args
        self.assertIsInstance(arguments[0], list)
        sent = json.loads(kwargs["input"])
        self.assertTrue(sent["return_review_only"])
        self.assertEqual(sent["context"], self.request["context"])
        self.assertEqual(sent["plan"], self.request["plan"])

    def test_gateway_public_error_codes_are_preserved(self):
        for code in ("deadline_exceeded", "canceled", "advisors_unavailable", "invalid_request"):
            with self.subTest(code=code):
                response = subprocess.CompletedProcess(
                    [], 1, json.dumps({"error": code}).encode("utf-8"), PRIVATE_SENTINEL.encode("utf-8")
                )
                outcome = self.invoke(response=response)
                self.assert_failed_without_success(outcome)
                self.assertIn(code, outcome.stderr)

    def test_untrusted_gateway_error_and_stderr_are_not_echoed(self):
        response = subprocess.CompletedProcess(
            [], 1, json.dumps({"error": PRIVATE_SENTINEL}).encode("utf-8"), PRIVATE_SENTINEL.encode("utf-8")
        )
        outcome = self.invoke(response=response)
        self.assert_failed_without_success(outcome)
        self.assertIn("tool_failed", outcome.stderr)

    def test_malformed_child_stdout_is_not_echoed(self):
        for return_code in (0, 1):
            with self.subTest(return_code=return_code):
                response = subprocess.CompletedProcess(
                    [], return_code, PRIVATE_SENTINEL.encode("utf-8"), PRIVATE_SENTINEL.encode("utf-8")
                )
                outcome = self.invoke(response=response)
                self.assert_failed_without_success(outcome)


if __name__ == "__main__":
    unittest.main()
