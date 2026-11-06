#!/usr/bin/env python3
"""Return DS feedback to the current assistant without manual API operation."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

MAX_INPUT_BYTES = 256 << 10


def main():
    parser = argparse.ArgumentParser(description="已有方案交 DS 复核，意见回到当前主助手")
    parser.add_argument("--request-file", required=True, help="任务 JSON 的绝对路径；- 表示标准输入")
    parser.add_argument("--output", type=Path, help="可选的本地 JSON 结果文件")
    args = parser.parse_args()
    try:
        runtime = json.loads((Path(__file__).resolve().parents[1] / "runtime.json").read_text())
        binary, config = Path(runtime["binary"]), Path(runtime["gateway_config"])
        if not binary.is_file() or not config.is_file():
            print("本机 DS 复核工具或已有网关配置尚未就绪。", file=sys.stderr)
            return 1
        if args.request_file == "-":
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        else:
            with Path(args.request_file).open("rb") as stream:
                raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("oversized request")
        request = json.loads(raw)
        if not isinstance(request, dict) or not isinstance(request.get("question"), str) or not request["question"].strip() or not isinstance(request.get("plan"), str) or not request["plan"].strip():
            raise ValueError("question and existing plan required")
        request["return_review_only"] = True
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_INPUT_BYTES:
            raise ValueError("oversized request")
        command = [str(binary), "-config", str(config)]
        if runtime.get("model"):
            if not isinstance(runtime["model"], str):
                raise ValueError("invalid model alias")
            command.extend(["-model", runtime["model"]])
        process = subprocess.run(command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if process.returncode != 0:
            code = "tool_failed"
            try:
                failure = json.loads(process.stdout)
                known = {"invalid_arguments", "request_too_large", "invalid_request", "existing_plan_required", "invalid_gateway_config", "invalid_deepseek_route", "gateway_initialization_failed", "review_failed", "deadline_exceeded", "canceled", "advisors_unavailable", "consultation_failed", "invalid_review_result"}
                if isinstance(failure, dict) and failure.get("error") in known:
                    code = failure["error"]
            except (ValueError, TypeError):
                pass
            print("DS 复核未完成（{}），当前方案仍标记为未复核。".format(code), file=sys.stderr)
            return 1
        result = json.loads(process.stdout)
        if not isinstance(result, dict):
            raise ValueError("invalid review envelope")
        advice = result.get("advisors", [])
        if not isinstance(advice, list) or len(advice) != 1 or not isinstance(advice[0], dict):
            raise ValueError("invalid review advice")
        content = advice[0].get("content")
        if result.get("status") != "reviewed" or result.get("mode") != "plan_review" or result.get("plan_source") != "provided" or result.get("decision") or advice[0].get("name") != "deepseek" or advice[0].get("status") != "success" or advice[0].get("provider", "") not in ("", "deepseek") or not isinstance(content, str) or not content.strip():
            raise ValueError("invalid review contract")
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(output + "\n")
        print(output)
        return 0
    except subprocess.TimeoutExpired:
        print("DS 复核未完成（deadline_exceeded），当前方案仍标记为未复核。", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("DS 复核未完成，请检查任务资料或本机工具状态；不要把它当作已复核。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
