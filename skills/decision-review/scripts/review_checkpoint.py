#!/usr/bin/env python3
"""Review a complex task's plan, milestone or proposed delivery with DS."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit
import uuid

from checkpoint_state import current_session_id, in_scope, load_runtime, load_state, save_state, state_path, state_transaction, write_private_json


MAX_INPUT_BYTES = 256 << 10
MAX_REPLY_BYTES = 2 << 20
MAX_ROUNDS = 2
STAGES = {"plan", "milestone", "delivery"}
REVIEW_INSTRUCTION = "请用较为简洁的表达来回答我。"
SAFE_CODES = {"invalid_arguments", "request_too_large", "invalid_request", "existing_plan_required", "invalid_gateway_config", "invalid_deepseek_route", "gateway_initialization_failed", "review_failed", "deadline_exceeded", "canceled", "advisors_unavailable", "consultation_failed", "invalid_review_result", "tool_failed", "invalid_query", "unsafe_query", "invalid_deepseek_config", "search_unavailable", "search_http_error", "response_too_large", "invalid_search_response"}
SEARCH_CONTRACT = {"count": 3, "need_content": True, "content_excerpt_bytes": 12000, "summary_excerpt_bytes": 4000, "excerpt_policy": "query-paragraph-v1"}
EXCERPT_GAP = b"\n\n[... omitted ...]\n\n"
CORE_REVIEW_QUESTION = "这里是目前的上下文、我的验收结果和最开始的请求。你看看这个结果是否能满足我最开始的请求意图；如果按照业界标准，这样的结果/指标是否已经达成；这里的见解和结论有没有问题？如果这里有缺任何上下文信息请询问我。请用较为简洁的表达来回答我。"
CREDENTIAL_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
)
FIELDS = {"complexity", "complexity_reason", "stage", "original_request", "context", "progress", "current_result", "acceptance", "constraints", "evidence", "previous_review", "open_questions", "review_question", "codex_assessment", "research"}


class CheckpointError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_json(path, limit=MAX_INPUT_BYTES):
    if str(path) == "-":
        raw = sys.stdin.buffer.read(limit + 1)
    else:
        with Path(path).open("rb") as stream:
            raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise CheckpointError("request_too_large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise CheckpointError("invalid_request") from None


def _is_complex(packet):
    return isinstance(packet, dict) and packet.get("complexity") == "complex" and isinstance(packet.get("complexity_reason"), str) and bool(packet["complexity_reason"].strip())


def _strings(value, *, nonempty=False, maximum=32):
    return isinstance(value, list) and (bool(value) or not nonempty) and len(value) <= maximum and all(isinstance(item, str) and item.strip() for item in value)


def _unsafe_query(query):
    # A few recognizable private identifiers, not a complete privacy classifier.
    patterns = (
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        r"(?<![:/\w])/(?:Users|home|private|tmp|var|etc|opt|Volumes|mnt|srv|root)(?:/|\b)",
        r"\b[A-Za-z]:[\\/]|\\\\[^\s\\]+\\|file://",
        r"\b(?:localhost|[A-Za-z0-9.-]+\.(?:local|internal|lan|ts\.net))\b",
    )
    if any(pattern.search(query) for pattern in CREDENTIAL_PATTERNS) or any(re.search(pattern, query, re.IGNORECASE) for pattern in patterns):
        return True
    for token in re.findall(r"(?<![A-Za-z0-9])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9])|\[?[0-9A-Fa-f:]*:[0-9A-Fa-f:]+\]?", query):
        try:
            address = ipaddress.ip_address(token.strip("[]"))
        except ValueError:
            continue
        if address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
            return True
    return False


def _validate_research(packet):
    research = packet.get("research")
    if research is None:
        return
    if not isinstance(research, dict) or set(research) != {"required", "queries", "freshness_key"} or research.get("required") is not True:
        raise CheckpointError("invalid_research_request")
    queries = research.get("queries")
    if not _strings(queries, nonempty=True, maximum=2) or any(len(query) > 128 for query in queries):
        raise CheckpointError("invalid_query")
    if not isinstance(research.get("freshness_key"), str) or not research["freshness_key"].strip():
        raise CheckpointError("invalid_research_request")
    if any(_unsafe_query(query) for query in queries):
        raise CheckpointError("unsafe_query")


def _validate_packet(packet):
    if not isinstance(packet, dict) or set(packet) - FIELDS:
        raise CheckpointError("invalid_checkpoint_packet")
    if packet.get("stage") not in STAGES:
        raise CheckpointError("invalid_checkpoint_packet")
    for field in ("original_request", "context", "progress", "current_result"):
        if not isinstance(packet.get(field), str) or not packet[field].strip():
            raise CheckpointError("invalid_checkpoint_packet")
    if not _strings(packet.get("acceptance"), nonempty=True):
        raise CheckpointError("invalid_checkpoint_packet")
    if "constraints" in packet and not _strings(packet["constraints"]):
        raise CheckpointError("invalid_checkpoint_packet")
    evidence = packet.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 32:
        raise CheckpointError("invalid_checkpoint_packet")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"source", "content"} or any(not isinstance(item[key], str) or not item[key].strip() for key in ("source", "content")):
            raise CheckpointError("invalid_checkpoint_packet")
    previous = packet.get("previous_review")
    if previous is not None and not isinstance(previous, (str, dict)):
        raise CheckpointError("invalid_checkpoint_packet")
    questions = packet.get("open_questions")
    if questions is not None and not isinstance(questions, str) and not _strings(questions):
        raise CheckpointError("invalid_checkpoint_packet")
    if "review_question" in packet and (not isinstance(packet["review_question"], str) or not packet["review_question"].strip()):
        raise CheckpointError("invalid_checkpoint_packet")
    if "codex_assessment" in packet and (not isinstance(packet["codex_assessment"], str) or not packet["codex_assessment"].strip()):
        raise CheckpointError("invalid_checkpoint_packet")
    _validate_research(packet)
    if packet.get("research") and len(evidence) >= 32:
        raise CheckpointError("research_evidence_limit_exceeded")
    raw = _json_bytes(packet)
    if len(raw) > MAX_INPUT_BYTES:
        raise CheckpointError("request_too_large")
    if any(pattern.search(raw.decode("utf-8")) for pattern in CREDENTIAL_PATTERNS):
        raise CheckpointError("credential_material_rejected")
    return raw


def _validate_selection(source, field):
    selection = source.get(field + "_selection")
    fields = {"policy", "status", "query_terms", "matched_terms", "matched_blocks", "omitted_matching_blocks", "truncated", "spans"}
    if not isinstance(selection, dict) or set(selection) != fields or selection.get("policy") != SEARCH_CONTRACT["excerpt_policy"] or not isinstance(selection.get("status"), str) or selection["status"] not in {"matched", "no_match", "empty"} or type(selection.get("truncated")) is not bool:
        raise CheckpointError("invalid_search_response")
    for key in ("query_terms", "matched_terms"):
        terms = selection.get(key)
        if not isinstance(terms, list) or len(terms) > 32 or any(not isinstance(term, str) or not term.strip() or len(term) > 64 for term in terms) or len(set(terms)) != len(terms):
            raise CheckpointError("invalid_search_response")
    if not set(selection["matched_terms"]).issubset(selection["query_terms"]):
        raise CheckpointError("invalid_search_response")
    matched, omitted = selection.get("matched_blocks"), selection.get("omitted_matching_blocks")
    if type(matched) is not int or type(omitted) is not int or not 0 <= omitted <= matched:
        raise CheckpointError("invalid_search_response")
    original = source["original_" + field + "_bytes"]
    status = selection["status"]
    if (status == "empty") != (original == 0) or (status == "matched") != bool(matched) or bool(selection["matched_terms"]) != bool(matched):
        raise CheckpointError("invalid_search_response")
    spans = selection.get("spans")
    if not isinstance(spans, list) or len(spans) > 2048 or bool(spans) != bool(original):
        raise CheckpointError("invalid_search_response")
    raw, cursor, previous_end = source[field].encode("utf-8"), 0, -1
    for index, span in enumerate(spans):
        if not isinstance(span, dict) or set(span) != {"start_byte", "end_byte"}:
            raise CheckpointError("invalid_search_response")
        start, end = span.get("start_byte"), span.get("end_byte")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= original or start <= previous_end:
            raise CheckpointError("invalid_search_response")
        if index:
            if raw[cursor:cursor + len(EXCERPT_GAP)] != EXCERPT_GAP:
                raise CheckpointError("invalid_search_response")
            cursor += len(EXCERPT_GAP)
        length = end - start
        try:
            raw[cursor:cursor + length].decode("utf-8")
        except UnicodeError:
            raise CheckpointError("invalid_search_response") from None
        cursor += length
        previous_end = end
    if cursor != len(raw):
        raise CheckpointError("invalid_search_response")
    if not source[field + "_excerpted"] and (selection["truncated"] or omitted or spans != ([] if not original else [{"start_byte": 0, "end_byte": original}])):
        raise CheckpointError("invalid_search_response")


def _validate_search_receipt(receipt, packet):
    expected = packet["research"]["queries"]
    if not isinstance(receipt, dict) or receipt.get("status") != "complete" or type(receipt.get("search_calls")) is not int or receipt["search_calls"] != len(expected):
        raise CheckpointError("invalid_search_response")
    searched_at = receipt.get("searched_at")
    if not isinstance(searched_at, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", searched_at) is None:
        raise CheckpointError("invalid_search_response")
    try:
        # Python 3.9 accepts microseconds; Go emits RFC3339Nano. Normalize only
        # for validation and retain the exact upstream timestamp in the receipt.
        parse_time = re.sub(r"\.(\d+)", lambda match: "." + match.group(1)[:6].ljust(6, "0"), searched_at)
        timestamp = datetime.fromisoformat(parse_time.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError:
        raise CheckpointError("invalid_search_response") from None
    queries = receipt.get("queries")
    if not isinstance(queries, list) or len(queries) != len(expected):
        raise CheckpointError("invalid_search_response")
    for query, original in zip(queries, expected):
        if not isinstance(query, dict) or query.get("query") != original or not isinstance(query.get("results"), list) or len(query["results"]) > SEARCH_CONTRACT["count"]:
            raise CheckpointError("invalid_search_response")
        for source in query["results"]:
            if not isinstance(source, dict) or not isinstance(source.get("title"), str):
                raise CheckpointError("invalid_search_response")
            title_missing = source.get("title_missing", False)
            if type(title_missing) is not bool or title_missing != (not source["title"].strip()):
                raise CheckpointError("invalid_search_response")
            if not isinstance(source.get("url"), str):
                raise CheckpointError("invalid_search_response")
            try:
                url = urlsplit(source["url"])
                if url.scheme not in ("https", "http") or not url.hostname or url.username is not None or url.password is not None:
                    raise ValueError("public source URL required")
            except ValueError:
                raise CheckpointError("invalid_search_response") from None
            if not isinstance(source.get("date"), str):
                raise CheckpointError("invalid_search_response")
            for field, limit in (("content", SEARCH_CONTRACT["content_excerpt_bytes"]), ("summary", SEARCH_CONTRACT["summary_excerpt_bytes"])):
                text, excerpted = source.get(field), source.get(field + "_excerpted")
                original_bytes, original_hash = source.get("original_" + field + "_bytes"), source.get(field + "_sha256")
                if not isinstance(text, str) or type(excerpted) is not bool or type(original_bytes) is not int or not isinstance(original_hash, str) or re.fullmatch(r"[0-9a-f]{64}", original_hash) is None:
                    raise CheckpointError("invalid_search_response")
                size = len(text.encode("utf-8"))
                if size > limit or original_bytes < size or (excerpted and original_bytes <= size) or (not excerpted and (original_bytes != size or original_hash != _sha(text.encode("utf-8")))):
                    raise CheckpointError("invalid_search_response")
                _validate_selection(source, field)
    if len(_json_bytes(receipt)) > MAX_REPLY_BYTES:
        raise CheckpointError("response_too_large")


def _research_sources(receipt):
    sources = []
    for query in receipt["queries"]:
        for result in query["results"]:
            item = dict(result, id="[E" + str(len(sources) + 1) + "]", query=query["query"])
            sources.append(item)
    return sources


def _research_metadata(receipt, packet, path, digest):
    sources = _research_sources(receipt)
    return {"status": "complete" if sources else "no_results", "provider": "deepseek-search", "search_calls": receipt["search_calls"], "searched_at": receipt["searched_at"], "freshness_key": packet["research"]["freshness_key"], "source_count": len(sources), "unique_source_count": len({source["url"] for source in sources}), "sources": [{key: value for key, value in source.items() if key not in {"content", "summary"}} for source in sources], "receipt_path": str(path), "receipt_sha256": digest}


def _run_research(runtime, packet, session_id, state):
    binary, config = runtime.get("search_binary"), runtime.get("gateway_config")
    if not isinstance(binary, str) or not Path(binary).is_absolute() or not isinstance(config, str) or not Path(config).is_absolute():
        raise CheckpointError("invalid_search_runtime")
    _active_state(runtime, session_id, state["checkpoint_id"])
    try:
        process = subprocess.run([binary, "-config", config], input=json.dumps({"queries": packet["research"]["queries"]}, ensure_ascii=False).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=95)
    except OSError:
        raise CheckpointError("search_unavailable") from None
    if process.returncode != 0:
        raise CheckpointError(_failure_code(process))
    if len(process.stdout) > MAX_REPLY_BYTES:
        raise CheckpointError("response_too_large")
    try:
        receipt = json.loads(process.stdout)
    except (ValueError, TypeError):
        raise CheckpointError("invalid_search_response") from None
    _validate_search_receipt(receipt, packet)
    path = state_path(runtime, session_id).parent / (state["checkpoint_id"] + ".search.json")
    raw = write_private_json(path, receipt)
    with state_transaction(runtime, session_id):
        latest = _active_state(runtime, session_id, state["checkpoint_id"], state["request_id"])
        latest.update(search_receipt_path=str(path), search_receipt_sha256=_sha(raw), research=_research_metadata(receipt, packet, path, _sha(raw)))
        save_state(runtime, session_id, latest)
    return receipt


def _cached_research(packet, state):
    if not packet.get("research"):
        return None
    path = state.get("search_receipt_path")
    expected = Path(state["packet_path"]).parent / (state["checkpoint_id"] + ".search.json")
    if not isinstance(path, str) or Path(path) != expected:
        raise CheckpointError("invalid_search_response")
    receipt = _read_json(path, MAX_REPLY_BYTES)
    _validate_search_receipt(receipt, packet)
    digest = _sha(_json_bytes(receipt))
    if digest != state.get("search_receipt_sha256") or _research_metadata(receipt, packet, path, digest) != state.get("research"):
        raise CheckpointError("invalid_search_response")
    return receipt


def _review_output(result, state, reused=False):
    output = dict(result, checkpoint=_metadata(state))
    output["consultation"] = {"round": state.get("round", 1), "max_rounds": MAX_ROUNDS, "followup_allowed": state.get("round", 1) < MAX_ROUNDS and state["status"] == "reviewed", "reused": reused}
    if state.get("research"):
        output["research"] = dict(state["research"], reused=reused or bool(state.get("discussion")), search_calls_this_run=0 if reused or state.get("discussion") else state["research"]["search_calls"])
    return output


def legacy_request(packet, request_id, search_receipt=None, discussion=None):
    """Keep the actual result verbatim while adapting to the existing gateway."""
    context = ["检查点：" + packet["stage"], "最初的请求：\n" + packet["original_request"], "必要上下文：\n" + packet["context"], "中间关键过程与证据：\n" + packet["progress"], "验收条件：\n" + "\n".join("- " + item for item in packet["acceptance"])]
    if packet.get("codex_assessment"):
        context.append("Codex当前结论、见解与依据：\n" + packet["codex_assessment"])
    if packet.get("previous_review"):
        previous = packet["previous_review"]
        context.append("上一轮意见、主助手判断与剩余分歧：\n" + (previous if isinstance(previous, str) else json.dumps(previous, ensure_ascii=False, sort_keys=True)))
    if packet.get("open_questions"):
        questions = packet["open_questions"]
        context.append("这轮待讨论问题：\n" + (questions if isinstance(questions, str) else "\n".join("- " + item for item in questions)))
    if discussion is not None:
        context.append("上一轮顾问原文及Codex补充的上下文（本次为最后一轮咨询）：\n" + json.dumps(discussion, ensure_ascii=False, sort_keys=True))
    question = CORE_REVIEW_QUESTION
    if packet.get("review_question"):
        question += "\n\n本轮额外关注：\n" + packet["review_question"]
    evidence = list(packet.get("evidence", []))
    if search_receipt is not None:
        sources = _research_sources(search_receipt)
        context.append("本轮实际公开检索：provider=deepseek-search；searched_at=" + search_receipt["searched_at"] + "；search_calls=" + str(search_receipt["search_calls"]) + "；返回来源数=" + str(len(sources)) + "。" + ("检索未返回资料。" if not sources else "搜索资料见evidence。"))
        evidence.append({"source": "DeepSeek Search公开检索资料", "content": "以下为本轮搜索资料及选段回执（关键词命中，不保证完整）。\n" + json.dumps({"searched_at": search_receipt["searched_at"], "search_calls": search_receipt["search_calls"], "sources": sources}, ensure_ascii=False)})
    value = {"request_id": request_id, "question": question, "context": "\n\n".join(context), "plan": packet["current_result"], "constraints": packet.get("constraints", []), "evidence": evidence, "return_review_only": True}
    # Match the child helper's own JSON encoding budget, including added flags.
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_INPUT_BYTES:
        raise CheckpointError("request_too_large")
    return value


def _metadata(state):
    return {key: state[key] for key in ("checkpoint_id", "request_id", "stage", "status", "complexity", "packet_path", "packet_sha256", "result_sha256", "route_fingerprint", "continuation_requested", "round") if key in state}


def _route_fingerprint(runtime):
    # Bind cached advice to public routing choices, never to credential contents.
    route = {key: runtime.get(key) or "" for key in ("model", "gateway_config", "binary", "search_binary")}
    if any(not isinstance(value, str) for value in route.values()):
        raise CheckpointError("invalid_checkpoint_runtime")
    route["search_contract"] = SEARCH_CONTRACT
    route["review_question"] = CORE_REVIEW_QUESTION
    route["review_footer"] = REVIEW_INSTRUCTION
    route["max_rounds"] = MAX_ROUNDS
    return _sha(json.dumps(route, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def register_checkpoint(runtime, packet, session_id, cwd):
    if session_id and in_scope(runtime, cwd):
        with state_transaction(runtime, session_id):
            return _register_checkpoint(runtime, packet, session_id, cwd)
    return _register_checkpoint(runtime, packet, session_id, cwd)


def _register_checkpoint(runtime, packet, session_id, cwd):
    if not _is_complex(packet):
        # An explicit simple-task assessment clears only this session/cwd's
        # unfinished checkpoint, even when UserPromptSubmit is unavailable.
        if session_id and in_scope(runtime, cwd):
            previous = load_state(runtime, session_id)
            if previous and previous.get("session_id") == session_id and previous.get("cwd") == str(Path(cwd).resolve()) and previous.get("status") in {"pending", "reviewed"}:
                previous.update(status="skipped", skip_reason="not_complex", skipped_at=_now())
                save_state(runtime, session_id, previous)
        return {"status": "skipped", "reason": "not_complex"}
    if not in_scope(runtime, cwd):
        return {"status": "skipped", "reason": "out_of_scope"}
    raw = _validate_packet(packet)
    identifier = "checkpoint-" + uuid.uuid4().hex
    legacy_request(packet, identifier)  # Reject oversized mapping before state or DS.
    cwd = str(Path(cwd).resolve())
    previous = load_state(runtime, session_id)
    digest = _sha(raw)
    route_fingerprint = _route_fingerprint(runtime)
    if previous and previous.get("session_id") == session_id and previous.get("cwd") == cwd and previous.get("packet_sha256") == digest and previous.get("route_fingerprint") == route_fingerprint and previous.get("status") in {"pending", "reviewed", "decided"}:
        return previous
    directory = state_path(runtime, session_id).parent
    packet_path = directory / (identifier + ".packet.json")
    write_private_json(packet_path, packet)
    state = {"session_id": session_id, "cwd": cwd, "complexity": "complex", "complexity_reason": packet["complexity_reason"], "checkpoint_id": identifier, "request_id": identifier, "round": 1, "stage": packet["stage"], "status": "pending", "continuation_requested": False, "packet_path": str(packet_path), "result_sha256": _sha(packet["current_result"].encode("utf-8")), "packet_sha256": digest, "route_fingerprint": route_fingerprint, "registered_at": _now()}
    save_state(runtime, session_id, state)
    return state


def _validate_reply(result, packet, state, search_receipt=None):
    if not isinstance(result, dict) or result.get("request_id") != state["request_id"] or result.get("status") != "reviewed" or result.get("mode") != "plan_review" or result.get("plan_source") != "provided" or result.get("decision") is not None:
        raise CheckpointError("invalid_review_result")
    advice = result.get("advisors")
    if not isinstance(advice, list) or len(advice) != 1 or not isinstance(advice[0], dict):
        raise CheckpointError("invalid_review_result")
    item = advice[0]
    content = item.get("content")
    if item.get("name") != "deepseek" or item.get("status") != "success" or item.get("provider", "") not in ("", "deepseek") or not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 64 << 10:
        raise CheckpointError("invalid_review_result")
    legacy = legacy_request(packet, state["request_id"], search_receipt, state.get("discussion"))
    supplied_context = result.get("consult_context")
    if result.get("plan") != packet["current_result"] or not isinstance(supplied_context, str) or legacy["context"] not in supplied_context or packet["current_result"] not in supplied_context or not supplied_context.endswith(REVIEW_INSTRUCTION):
        raise CheckpointError("invalid_review_result")
    if search_receipt is not None and any(source["id"] not in supplied_context or source["url"] not in supplied_context or source["content_sha256"] not in supplied_context or source["summary_sha256"] not in supplied_context for source in _research_sources(search_receipt)):
        raise CheckpointError("invalid_review_result")


def _active_state(runtime, session_id, checkpoint_id, request_id=None):
    state = load_state(runtime, session_id)
    if not state or state.get("checkpoint_id") != checkpoint_id or state.get("status") not in {"pending", "reviewed", "decided"}:
        raise CheckpointError("checkpoint_no_longer_active")
    if request_id is not None and state.get("request_id") != request_id:
        raise CheckpointError("checkpoint_no_longer_active")
    return state


def _failure_code(process):
    try:
        failure = json.loads(process.stdout)
        if isinstance(failure, dict) and isinstance(failure.get("error"), str) and failure["error"] in SAFE_CODES:
            return failure["error"]
    except (ValueError, TypeError):
        pass
    # The child prints only a public code in parentheses, never echo its text.
    text = process.stderr.decode("utf-8", errors="replace") if isinstance(process.stderr, bytes) else ""
    for code in SAFE_CODES:
        if "(" + code + ")" in text or "（" + code + "）" in text:
            return code
    return "tool_failed"


def review_checkpoint(runtime, packet, session_id, cwd):
    state = register_checkpoint(runtime, packet, session_id, cwd)
    if state["status"] == "skipped":
        return state
    return _review_state(runtime, packet, session_id, state)


def _review_state(runtime, packet, session_id, state):
    if state["status"] in {"reviewed", "decided"}:
        receipt = _cached_research(packet, state)
        reply = _read_json(state["reply_path"], MAX_REPLY_BYTES)
        if _sha(_json_bytes(reply)) != state.get("reply_sha256"):
            raise CheckpointError("invalid_review_result")
        _validate_reply(reply, packet, state, receipt)
        return _review_output(reply, state, reused=True)
    helper = Path(__file__).resolve().with_name("review_plan.py")
    try:
        receipt = (_cached_research(packet, state) if state.get("discussion") else _run_research(runtime, packet, session_id, state)) if packet.get("research") else None
        request = legacy_request(packet, state["request_id"], receipt, state.get("discussion"))
        _active_state(runtime, session_id, state["checkpoint_id"], state["request_id"])
        process = subprocess.run([sys.executable, str(helper), "--request-file", "-"], input=json.dumps(request, ensure_ascii=False).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=195)
        if process.returncode != 0:
            raise CheckpointError(_failure_code(process))
        if len(process.stdout) > MAX_REPLY_BYTES:
            raise CheckpointError("invalid_review_result")
        try:
            result = json.loads(process.stdout)
        except (ValueError, TypeError):
            raise CheckpointError("invalid_review_result") from None
        _validate_reply(result, packet, state, receipt)
    except subprocess.TimeoutExpired:
        failure = "deadline_exceeded"
    except CheckpointError as error:
        failure = error.code
    except OSError:
        failure = "tool_failed"
    else:
        suffix = ".round2.reply.json" if state.get("round", 1) == 2 else ".reply.json"
        reply_path = state_path(runtime, session_id).parent / (state["checkpoint_id"] + suffix)
        raw = write_private_json(reply_path, result)
        with state_transaction(runtime, session_id):
            latest = _active_state(runtime, session_id, state["checkpoint_id"], state["request_id"])
            latest.update(status="reviewed", reply_path=str(reply_path), reply_sha256=_sha(raw), reviewed_at=_now())
            save_state(runtime, session_id, latest)
        return _review_output(result, latest)
    with state_transaction(runtime, session_id):
        latest = _active_state(runtime, session_id, state["checkpoint_id"], state["request_id"])
        latest.update(status="failed", error=failure, failed_at=_now())
        save_state(runtime, session_id, latest)
    raise CheckpointError(failure)


def followup_checkpoint(runtime, session_id, cwd, checkpoint_id, supplement):
    """One main-assistant initiated clarification; retain the original packet."""
    if not in_scope(runtime, cwd):
        raise CheckpointError("out_of_scope")
    with state_transaction(runtime, session_id):
        packet, state = _prepare_followup(runtime, session_id, cwd, checkpoint_id, supplement)
    return _review_state(runtime, packet, session_id, state)


def _prepare_followup(runtime, session_id, cwd, checkpoint_id, supplement):
    state = _active_state(runtime, session_id, checkpoint_id)
    if state.get("cwd") != str(Path(cwd).resolve()) or state.get("session_id") != session_id:
        raise CheckpointError("checkpoint_no_longer_active")
    if state.get("route_fingerprint") != _route_fingerprint(runtime):
        raise CheckpointError("checkpoint_route_changed")
    if not isinstance(supplement, dict) or set(supplement) != {"context_request", "supplement"} or any(not isinstance(value, str) or not value.strip() for value in supplement.values()):
        raise CheckpointError("invalid_checkpoint_supplement")
    raw = _json_bytes(supplement)
    if len(raw) > MAX_INPUT_BYTES:
        raise CheckpointError("request_too_large")
    if any(pattern.search(raw.decode("utf-8")) for pattern in CREDENTIAL_PATTERNS):
        raise CheckpointError("credential_material_rejected")
    digest = _sha(raw)
    packet = _read_json(state["packet_path"])
    if _sha(_validate_packet(packet)) != state.get("packet_sha256"):
        raise CheckpointError("invalid_checkpoint_packet")
    if state.get("round", 1) >= MAX_ROUNDS:
        if digest == state.get("supplement_sha256") and state["status"] in {"pending", "reviewed", "decided"}:
            return packet, state
        raise CheckpointError("discussion_round_limit")
    if state["status"] != "reviewed":
        raise CheckpointError("checkpoint_not_reviewed")
    first = _review_state(runtime, packet, session_id, state)
    # The main assistant supplies the natural-language reason; no question regex.
    discussion = dict(supplement, previous_advisors=[{key: item[key] for key in ("name", "provider", "actual_model", "content") if key in item} for item in first["advisors"]])
    request_id = checkpoint_id + "-round-2"
    receipt = _cached_research(packet, state)
    request = legacy_request(packet, request_id, receipt, discussion)
    if any(pattern.search(json.dumps(request, ensure_ascii=False)) for pattern in CREDENTIAL_PATTERNS):
        raise CheckpointError("credential_material_rejected")
    latest = _active_state(runtime, session_id, checkpoint_id, state["request_id"])
    previous = {key: state[key] for key in ("request_id", "reply_path", "reply_sha256", "reviewed_at")}
    latest.update(status="pending", round=2, request_id=request_id, discussion=discussion, supplement_sha256=digest, previous_round=previous, followup_at=_now())
    latest.pop("reply_path", None)
    latest.pop("reply_sha256", None)
    save_state(runtime, session_id, latest)
    return packet, latest


@contextmanager
def _session_lock(runtime, session_id, cwd, enabled=True):
    """Serialize CLI dispatch; hooks remain free to signal user interruption."""
    if not enabled or not in_scope(runtime, cwd):
        yield
        return
    path = state_path(runtime, session_id).parent / "consultation.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CheckpointError("checkpoint_busy") from None
        yield
    finally:
        os.close(descriptor)


def decide_checkpoint(runtime, session_id, cwd, checkpoint_id, judgment):
    if not in_scope(runtime, cwd):
        raise CheckpointError("out_of_scope")
    with state_transaction(runtime, session_id):
        return _decide_checkpoint(runtime, session_id, cwd, checkpoint_id, judgment)


def _decide_checkpoint(runtime, session_id, cwd, checkpoint_id, judgment):
    state = _active_state(runtime, session_id, checkpoint_id)
    if state.get("cwd") != str(Path(cwd).resolve()) or state.get("session_id") != session_id or state.get("status") != "reviewed":
        raise CheckpointError("checkpoint_not_reviewed")
    if not isinstance(judgment, dict) or set(judgment) != {"decision", "action"} or not isinstance(judgment.get("decision"), str) or not judgment["decision"].strip() or judgment.get("action") not in {"continue", "deliver"}:
        raise CheckpointError("invalid_checkpoint_decision")
    if judgment["action"] == "deliver" and state.get("stage") != "delivery":
        raise CheckpointError("delivery_checkpoint_required")
    if any(pattern.search(json.dumps(judgment, ensure_ascii=False)) for pattern in CREDENTIAL_PATTERNS):
        raise CheckpointError("credential_material_rejected")
    state.update(status="decided", decision=judgment["decision"], action=judgment["action"], decided_at=_now())
    save_state(runtime, session_id, state)
    return _metadata(state)


def main():
    parser = argparse.ArgumentParser(description="复杂任务阶段成果与交付前的 DS 检查点")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("register", "review"):
        command = commands.add_parser(name)
        command.add_argument("--request-file", required=True)
        if name == "review":
            command.add_argument("--output", type=Path)
        command.add_argument("--session-id", default=current_session_id())
        command.add_argument("--cwd", default=str(Path.cwd()))
    command = commands.add_parser("decide")
    command.add_argument("--checkpoint-id", required=True)
    command.add_argument("--decision-file", required=True)
    command.add_argument("--session-id", default=current_session_id())
    command.add_argument("--cwd", default=str(Path.cwd()))
    command = commands.add_parser("followup")
    command.add_argument("--checkpoint-id", required=True)
    command.add_argument("--request-file", required=True)
    command.add_argument("--output", type=Path)
    command.add_argument("--session-id", default=current_session_id())
    command.add_argument("--cwd", default=str(Path.cwd()))
    args = parser.parse_args()
    try:
        runtime = load_runtime()
        packet = _read_json(args.request_file) if args.command != "decide" else None
        with _session_lock(runtime, args.session_id, args.cwd, enabled=args.command in {"decide", "followup"} or _is_complex(packet)):
            if args.command == "decide":
                result = decide_checkpoint(runtime, args.session_id, args.cwd, args.checkpoint_id, _read_json(args.decision_file))
            elif args.command == "followup":
                result = followup_checkpoint(runtime, args.session_id, args.cwd, args.checkpoint_id, packet)
            else:
                result = register_checkpoint(runtime, packet, args.session_id, args.cwd) if args.command == "register" else review_checkpoint(runtime, packet, args.session_id, args.cwd)
                if args.command == "register" and result.get("status") != "skipped":
                    result = _metadata(result)
            if args.command in {"review", "followup"} and args.output:
                write_private_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CheckpointError as error:
        print(json.dumps({"status": "failed", "error": error.code}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "failed", "error": "checkpoint_tool_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
