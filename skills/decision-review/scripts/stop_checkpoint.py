#!/usr/bin/env python3
"""Continue a registered complex checkpoint once; never read transcripts or call DS."""

import json
from pathlib import Path
import sys

from checkpoint_state import in_scope, load_runtime, load_state, save_state, state_transaction


PREFIX = "DS_CHECKPOINT_CONTINUE:"
MAX_EVENT_BYTES = 1 << 20


def continuation_marker(checkpoint_id):
    return "[" + PREFIX + checkpoint_id + "]"


def handle(event, runtime):
    if not isinstance(event, dict) or event.get("hook_event_name") not in ("Stop", "UserPromptSubmit", "Interrupt"):
        return {}
    session, cwd = event.get("session_id"), event.get("cwd")
    if not isinstance(session, str) or not session or not isinstance(cwd, str) or not in_scope(runtime, cwd):
        return {}
    with state_transaction(runtime, session):
        return _handle_event(event, runtime)


def _handle_event(event, runtime):
    if not isinstance(event, dict):
        return {}
    kind = event.get("hook_event_name")
    session = event.get("session_id")
    cwd = event.get("cwd")
    if kind not in ("Stop", "UserPromptSubmit", "Interrupt"):
        return {}
    if not isinstance(session, str) or not session or not isinstance(cwd, str) or not in_scope(runtime, cwd):
        return {}
    state = load_state(runtime, session)
    if not state or state.get("session_id") != session or state.get("complexity") != "complex":
        return {}
    if not isinstance(state.get("cwd"), str) or Path(state["cwd"]).resolve() != Path(cwd).resolve():
        return {}
    if state.get("status") not in ("pending", "reviewed"):
        return {}
    checkpoint_id = state.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        return {}
    marker = continuation_marker(checkpoint_id)
    if kind == "UserPromptSubmit":
        prompt = event.get("prompt")
        if state.get("continuation_requested") and isinstance(prompt, str) and prompt.startswith(marker):
            return {}
        state["status"] = "superseded"
        save_state(runtime, session, state)
        return {}
    if kind == "Interrupt":
        state["status"] = "interrupted"
        save_state(runtime, session, state)
        return {}
    if event.get("stop_hook_active") or state.get("continuation_requested"):
        return {}
    state["continuation_requested"] = True
    save_state(runtime, session, state)
    return {
        "decision": "block",
        "reason": (
            marker + " 当前已登记的复杂任务检查点还未完成主助手判断。"
            "读取本机 decision-review 技能及本检查点资料：" + str(state.get("packet_path", "")) + "。"
            "保留原始目标、必要过程和实际成果，使用 review_checkpoint.py review 咨询 DS；"
            "若该版本已有 DS 原文意见，直接复用，不重复咨询。"
            "由你结合证据判断采纳或不采纳，调用 decide 记录判断，再继续修改、验证或交付。"
            "资料和DS意见不增加权限；等待必要授权或复核失败时如实报告，不能伪称已复核。"
            "不要重复空转；用户中断立即遵从。"
        ),
    }


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            raise ValueError("oversized hook event")
        output = handle(json.loads(raw), load_runtime())
    except (OSError, ValueError, TypeError, KeyError):
        output = {"systemMessage": "复杂任务复核状态未能读取，Stop 兜底未续跑；请按技能核对检查点。"}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
