"""Offline checkpoint lifecycle tests; no actual credentials or network calls."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get("CHECKPOINT_SCRIPTS_DIR", str(Path(__file__).resolve().parent)))
import checkpoint_state as store
import review_checkpoint as review


PRIVATE = "private-child-output-must-never-be-printed"


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ds-checkpoint-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.cwd = self.directory / "project"
        self.cwd.mkdir()
        self.runtime = {"checkpoint_state_dir": str(self.directory / "state"), "checkpoint_scopes": [str(self.cwd)]}
        self.session = "offline-session-1"
        self.packet = {
            "complexity": "complex",
            "complexity_reason": "有跨阶段状态和交付验收，需要一起判断。",
            "stage": "milestone",
            "original_request": "原始目标：保留结果原文，再推进任务。",
            "context": "必须沿用当前主助手；不部署、不联网测试。",
            "progress": "阶段事实：已复现问题，模型赞同不算验证。",
            "current_result": "  实际阶段成果原文。\n保留空格与换行。\n",
            "acceptance": ["成功与失败均留证据", "保持实际成果原文"],
            "constraints": ["只改本地测试文件"],
            "evidence": [{"source": "local-test", "content": "本地观察证据：计数为1。"}],
            "previous_review": {"advice": "补边界", "decision": "已采纳，等待验证"},
            "open_questions": ["现有证据是否足够？"],
        }

    def reply(self, legacy):
        return {
            "request_id": legacy["request_id"],
            "status": "reviewed", "mode": "plan_review", "plan_source": "provided",
            "plan": legacy["plan"],
            "consult_context": legacy["context"] + "\n\n" + legacy["plan"] + "\n\n" + "\n\n".join(item["content"] for item in legacy.get("evidence", [])) + "\n\n" + review.REVIEW_INSTRUCTION,
            "advisors": [{"name": "deepseek", "provider": "deepseek", "status": "success", "actual_model": "offline-ds", "content": "  DS 原文：可以继续，但要补验证。\n保留这段换行。\n"}],
        }

    def child(self, transform=None):
        def run(command, **kwargs):
            self.assertEqual(command[-2:], ["--request-file", "-"])
            self.assertIsInstance(command, list)
            legacy = json.loads(kwargs["input"])
            result = self.reply(legacy)
            if transform:
                transform(result)
            return subprocess.CompletedProcess(command, 0, json.dumps(result, ensure_ascii=False).encode("utf-8"), b"")
        return run

    def perform_review(self, packet=None, transform=None):
        with patch.object(review.subprocess, "run", side_effect=self.child(transform)) as child:
            result = review.review_checkpoint(self.runtime, self.packet if packet is None else packet, self.session, self.cwd)
        return result, child

    def research_packet(self):
        return dict(self.packet, codex_assessment="Codex判断：当前实现方向成立，尚需核对外部依据。", research={"required": True, "queries": ["公开标准的版本及适用范围"], "freshness_key": "2026-11-06/standards-v1"})

    def research_runtime(self):
        return dict(self.runtime, search_binary="/offline/search-cli", gateway_config="/offline/gateway.yaml")

    def search_receipt(self, queries, empty=False):
        result = {"status": "complete", "searched_at": "2026-11-06T03:04:05.123456789Z", "search_calls": len(queries), "queries": []}
        for index, query in enumerate(queries):
            content, summary = "公开来源正文：适用范围须核对原文。", "公开搜索摘要。"
            source = {"title": "官方资料" + str(index + 1), "url": "https://example.org/standard/" + str(index + 1), "date": "2026-01-02", "summary": summary, "content": content}
            for key in ("content", "summary"):
                source.update({key + "_excerpted": False, "original_" + key + "_bytes": len(source[key].encode()), key + "_sha256": hashlib.sha256(source[key].encode()).hexdigest()})
                source[key + "_selection"] = {"policy": review.SEARCH_CONTRACT["excerpt_policy"], "status": "no_match", "query_terms": [], "matched_terms": [], "matched_blocks": 0, "omitted_matching_blocks": 0, "truncated": False, "spans": [{"start_byte": 0, "end_byte": len(source[key].encode())}]}
            result["queries"].append({"query": query, "results": [] if empty else [source]})
        return result

    def research_child(self, receipt=None, *, after_search=None):
        def run(command, **kwargs):
            if command[0] == "/offline/search-cli":
                self.assertEqual(command[1:], ["-config", "/offline/gateway.yaml"])
                sent = json.loads(kwargs["input"])
                self.assertEqual(set(sent), {"queries"})
                response = receipt if receipt is not None else self.search_receipt(sent["queries"])
                if after_search:
                    after_search()
                return subprocess.CompletedProcess(command, 0, json.dumps(response, ensure_ascii=False).encode(), b"")
            return self.child()(command, **kwargs)
        return run

    def assert_error(self, code, function, *args):
        with self.assertRaises(review.CheckpointError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_register_creates_explicit_pending_private_state_and_complete_packet(self):
        saved = review.register_checkpoint(self.runtime, self.packet, self.session, self.cwd)
        self.assertEqual(saved["status"], "pending")
        self.assertFalse(saved["continuation_requested"])
        self.assertEqual(saved["cwd"], str(self.cwd.resolve()))
        self.assertEqual(saved["session_id"], self.session)
        packet_path = Path(saved["packet_path"])
        self.assertEqual(json.loads(packet_path.read_text()), self.packet)
        self.assertEqual(saved["packet_sha256"], hashlib.sha256(packet_path.read_bytes()).hexdigest())
        self.assertEqual(saved["result_sha256"], hashlib.sha256(self.packet["current_result"].encode()).hexdigest())
        self.assertEqual(stat.S_IMODE(packet_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(store.state_path(self.runtime, self.session).stat().st_mode), 0o600)

    def test_state_is_partitioned_by_session_and_atomically_replaces_permissions(self):
        path = store.state_path(self.runtime, "../session/escape")
        self.assertEqual(path.parent.parent, Path(self.runtime["checkpoint_state_dir"]))
        self.assertNotEqual(path, store.state_path(self.runtime, "another-session"))
        store.save_state(self.runtime, self.session, {"status": "pending"})
        current = store.state_path(self.runtime, self.session)
        current.chmod(0o644)
        store.save_state(self.runtime, self.session, {"status": "reviewed"})
        self.assertEqual(store.load_state(self.runtime, self.session), {"status": "reviewed"})
        self.assertEqual(stat.S_IMODE(current.stat().st_mode), 0o600)
        self.assertFalse(list(current.parent.glob(".checkpoint-*")))

    def test_scope_uses_resolved_path_boundaries(self):
        self.assertTrue(store.in_scope(self.runtime, self.cwd / "nested"))
        self.assertFalse(store.in_scope(self.runtime, str(self.cwd) + "-other"))
        outside = self.directory / "outside"
        outside.mkdir()
        (self.cwd / "link").symlink_to(outside, target_is_directory=True)
        self.assertFalse(store.in_scope(self.runtime, self.cwd / "link"))

    def test_default_session_uses_codex_session_then_thread(self):
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "session", "CODEX_THREAD_ID": "thread"}):
            self.assertEqual(store.current_session_id(), "session")
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "", "CODEX_THREAD_ID": "thread"}):
            self.assertEqual(store.current_session_id(), "thread")

    def test_simple_task_skips_without_model_or_new_state(self):
        with patch.object(review.subprocess, "run") as child:
            for packet in ({"complexity": "simple"}, {"complexity": "complex", "complexity_reason": "  "}):
                result = review.review_checkpoint(self.runtime, packet, self.session, self.cwd)
                self.assertEqual(result["status"], "skipped")
            child.assert_not_called()
        self.assertIsNone(store.load_state(self.runtime, self.session))

    def test_explicit_simple_task_clears_only_same_session_cwd_unfinished_state(self):
        first = review.register_checkpoint(self.runtime, self.packet, self.session, self.cwd)
        with patch.object(review.subprocess, "run") as child:
            result = review.review_checkpoint(self.runtime, {"complexity": "simple"}, self.session, self.cwd)
            child.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        saved = store.load_state(self.runtime, self.session)
        self.assertEqual(saved["status"], "skipped")
        self.assertEqual(saved["checkpoint_id"], first["checkpoint_id"])
        other_session = "unrelated-session"
        review.register_checkpoint(self.runtime, self.packet, other_session, self.cwd)
        review.register_checkpoint(self.runtime, {"complexity": "simple"}, self.session, self.cwd)
        self.assertEqual(store.load_state(self.runtime, other_session)["status"], "pending")

    def test_scope_outside_does_not_mutate_other_project_pending_state(self):
        saved = review.register_checkpoint(self.runtime, self.packet, self.session, self.cwd)
        outside = self.directory / "outside"
        with patch.object(review.subprocess, "run") as child:
            result = review.review_checkpoint(self.runtime, {"complexity": "simple"}, self.session, outside)
            complex_result = review.review_checkpoint(self.runtime, self.packet, self.session, outside)
            child.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(complex_result["reason"], "out_of_scope")
        self.assertEqual(store.load_state(self.runtime, self.session), saved)

    def test_every_required_source_and_actual_result_reaches_legacy_wire_for_both_stages(self):
        for stage in ("milestone", "delivery"):
            with self.subTest(stage=stage):
                self.packet["stage"] = stage
                result, child = self.perform_review()
                child.assert_called_once()
                sent = json.loads(child.call_args.kwargs["input"])
                self.assertEqual(sent["plan"], self.packet["current_result"])
                for key in ("original_request", "context", "progress"):
                    self.assertIn(self.packet[key], sent["context"])
                for item in self.packet["acceptance"]:
                    self.assertIn(item, sent["context"])
                self.assertIn(self.packet["previous_review"]["decision"], sent["context"])
                self.assertIn(self.packet["open_questions"][0], sent["context"])
                self.assertEqual(sent["evidence"], self.packet["evidence"])
                self.assertTrue(sent["return_review_only"])
                self.assertEqual(result["advisors"][0]["content"], self.reply(sent)["advisors"][0]["content"])
                self.assertEqual(store.load_state(self.runtime, self.session)["status"], "reviewed")

    def test_missing_original_request_progress_or_result_is_rejected_before_model(self):
        for key in ("original_request", "context", "progress", "current_result", "acceptance"):
            with self.subTest(key=key):
                packet = dict(self.packet)
                del packet[key]
                with patch.object(review.subprocess, "run") as child:
                    self.assert_error("invalid_checkpoint_packet", review.review_checkpoint, self.runtime, packet, self.session, self.cwd)
                    child.assert_not_called()
        self.assertIsNone(store.load_state(self.runtime, self.session))

    def test_oversized_packet_or_mapped_payload_rejected_without_truncation_or_model(self):
        packet = dict(self.packet, current_result="x" * review.MAX_INPUT_BYTES)
        with patch.object(review.subprocess, "run") as child:
            self.assert_error("request_too_large", review.review_checkpoint, self.runtime, packet, self.session, self.cwd)
            child.assert_not_called()
        # Fill a valid packet to its byte budget; mapping labels then exceed it.
        packet = dict(self.packet, current_result="x")
        count = review.MAX_INPUT_BYTES - len(review._json_bytes(packet))
        packet["current_result"] += "x" * count
        self.assertEqual(len(review._validate_packet(packet)), review.MAX_INPUT_BYTES)
        with patch.object(review.subprocess, "run") as child:
            self.assert_error("request_too_large", review.review_checkpoint, self.runtime, packet, self.session, self.cwd)
            child.assert_not_called()
        self.assertIsNone(store.load_state(self.runtime, self.session))

    def test_request_file_and_stdin_reads_are_bounded(self):
        large = self.directory / "large.json"
        large.write_bytes(b"x" * (review.MAX_INPUT_BYTES + 1024))
        self.assert_error("request_too_large", review._read_json, large)
        class LimitedInput(io.BytesIO):
            def read(self, size=-1):
                self.last_size = size
                return super().read(size)
        stream = LimitedInput(large.read_bytes())
        with patch.object(sys, "stdin", type("Input", (), {"buffer": stream})()):
            self.assert_error("request_too_large", review._read_json, "-")
        self.assertEqual(stream.last_size, review.MAX_INPUT_BYTES + 1)

    def test_common_real_credential_shapes_are_rejected_without_printing_content(self):
        for material in ("Bearer " + "A" * 32, "sk-" + "B" * 32, "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key"):
            with self.subTest(material=material[:10]):
                packet = dict(self.packet, current_result=material)
                with patch.object(review.subprocess, "run") as child:
                    self.assert_error("credential_material_rejected", review.review_checkpoint, self.runtime, packet, self.session, self.cwd)
                    child.assert_not_called()
        self.assertIsNone(store.load_state(self.runtime, self.session))

    def test_repeated_review_or_decided_packet_reuses_original_reply_and_continuation_flag(self):
        initial = review.register_checkpoint(self.runtime, self.packet, self.session, self.cwd)
        initial["continuation_requested"] = True
        store.save_state(self.runtime, self.session, initial)
        first, child = self.perform_review()
        child.assert_called_once()
        self.assertTrue(first["checkpoint"]["continuation_requested"])
        with patch.object(review.subprocess, "run") as child:
            cached = review.review_checkpoint(self.runtime, self.packet, self.session, self.cwd)
            child.assert_not_called()
        self.assertEqual(cached["advisors"], first["advisors"])
        review.decide_checkpoint(self.runtime, self.session, self.cwd, initial["checkpoint_id"], {"decision": "采纳补边界建议，继续测试。", "action": "continue"})
        with patch.object(review.subprocess, "run") as child:
            cached = review.review_checkpoint(self.runtime, self.packet, self.session, self.cwd)
            child.assert_not_called()
        self.assertEqual(cached["checkpoint"]["status"], "decided")
        self.assertTrue(cached["checkpoint"]["continuation_requested"])

    def test_materially_changed_packet_creates_new_checkpoint(self):
        first, _ = self.perform_review()
        changed = dict(self.packet, progress="新的证据：测试全部通过。")
        second = review.register_checkpoint(self.runtime, changed, self.session, self.cwd)
        self.assertNotEqual(first["checkpoint"]["checkpoint_id"], second["checkpoint_id"])
        self.assertEqual(second["status"], "pending")
        self.assertFalse(second["continuation_requested"])

    def test_cached_advice_is_bound_to_model_and_public_config_paths(self):
        baseline = dict(self.runtime, model="offline-ds-v1", gateway_config="/test/gateway-one.yaml", binary="/test/review-one", search_binary="/test/search-one")
        for key, changed_value in (("model", "offline-ds-v2"), ("gateway_config", "/test/gateway-two.yaml"), ("binary", "/test/review-two"), ("search_binary", "/test/search-two")):
            with self.subTest(route_field=key):
                session = self.session + "-" + key
                with patch.object(review.subprocess, "run", side_effect=self.child()) as child:
                    first = review.review_checkpoint(baseline, self.packet, session, self.cwd)
                    child.assert_called_once()
                with patch.object(review.subprocess, "run") as child:
                    reused = review.review_checkpoint(baseline, self.packet, session, self.cwd)
                    child.assert_not_called()
                self.assertEqual(reused["checkpoint"]["checkpoint_id"], first["checkpoint"]["checkpoint_id"])
                changed = dict(baseline, **{key: changed_value})
                registered = review.register_checkpoint(changed, self.packet, session, self.cwd)
                self.assertNotEqual(registered["checkpoint_id"], first["checkpoint"]["checkpoint_id"])
                self.assertNotEqual(registered["route_fingerprint"], first["checkpoint"]["route_fingerprint"])
                self.assertEqual(registered["packet_sha256"], first["checkpoint"]["packet_sha256"])
                self.assertEqual(registered["status"], "pending")
                with patch.object(review.subprocess, "run", side_effect=self.child()) as child:
                    refreshed = review.review_checkpoint(changed, self.packet, session, self.cwd)
                    child.assert_called_once()
                self.assertEqual(refreshed["checkpoint"]["route_fingerprint"], registered["route_fingerprint"])

    def test_mismatched_id_mutated_result_or_replacement_decision_fails_closed(self):
        mutations = (
            lambda result: result.update(request_id="wrong-task"),
            lambda result: result.update(plan="silently truncated"),
            lambda result: result.update(decision={}),
            lambda result: result.update(consult_context="missing original context"),
            lambda result: result["advisors"][0].update(content="  "),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                with patch.object(review.subprocess, "run", side_effect=self.child(mutate)):
                    self.assert_error("invalid_review_result", review.review_checkpoint, self.runtime, self.packet, self.session, self.cwd)
                self.assertEqual(store.load_state(self.runtime, self.session)["status"], "failed")

    def test_timeout_and_child_failure_save_only_safe_codes(self):
        for failure in (subprocess.TimeoutExpired([PRIVATE], 195, output=PRIVATE.encode()), OSError(PRIVATE)):
            with self.subTest(kind=type(failure).__name__):
                with patch.object(review.subprocess, "run", side_effect=failure):
                    with self.assertRaises(review.CheckpointError) as caught:
                        review.review_checkpoint(self.runtime, self.packet, self.session, self.cwd)
                self.assertIn(caught.exception.code, {"deadline_exceeded", "tool_failed"})
                saved = store.load_state(self.runtime, self.session)
                self.assertEqual(saved["status"], "failed")
                self.assertNotIn(PRIVATE, json.dumps(saved))
        process = subprocess.CompletedProcess([], 1, PRIVATE.encode(), ("prefix " + PRIVATE).encode())
        with patch.object(review.subprocess, "run", return_value=process):
            self.assert_error("tool_failed", review.review_checkpoint, self.runtime, self.packet, self.session, self.cwd)

    def test_interrupted_superseded_or_explicitly_simple_state_is_never_resurrected(self):
        for status in ("interrupted", "superseded", "skipped"):
            with self.subTest(status=status):
                def run(command, **kwargs):
                    latest = store.load_state(self.runtime, self.session)
                    latest["status"] = status
                    store.save_state(self.runtime, self.session, latest)
                    return self.child()(command, **kwargs)
                with patch.object(review.subprocess, "run", side_effect=run):
                    self.assert_error("checkpoint_no_longer_active", review.review_checkpoint, self.runtime, self.packet, self.session, self.cwd)
                self.assertEqual(store.load_state(self.runtime, self.session)["status"], status)

    def test_decide_requires_reviewed_state_matching_id_and_real_main_judgment(self):
        state = review.register_checkpoint(self.runtime, self.packet, self.session, self.cwd)
        judgment = {"decision": "继续", "action": "continue"}
        self.assert_error("checkpoint_not_reviewed", review.decide_checkpoint, self.runtime, self.session, self.cwd, state["checkpoint_id"], judgment)
        self.perform_review()
        self.assert_error("checkpoint_no_longer_active", review.decide_checkpoint, self.runtime, self.session, self.cwd, "wrong-id", judgment)
        self.assert_error("invalid_checkpoint_decision", review.decide_checkpoint, self.runtime, self.session, self.cwd, state["checkpoint_id"], {"decision": "  ", "action": "continue"})
        self.assert_error("delivery_checkpoint_required", review.decide_checkpoint, self.runtime, self.session, self.cwd, state["checkpoint_id"], {"decision": "交付", "action": "deliver"})

    def test_delivery_decision_is_saved_after_successful_delivery_review(self):
        self.packet["stage"] = "delivery"
        result, _ = self.perform_review()
        decision = "已采纳补测试建议，验证通过后交付；未采纳扩大范围建议。"
        saved = review.decide_checkpoint(self.runtime, self.session, self.cwd, result["checkpoint"]["checkpoint_id"], {"decision": decision, "action": "deliver"})
        self.assertEqual(saved["status"], "decided")
        state = store.load_state(self.runtime, self.session)
        self.assertEqual(state["decision"], decision)
        self.assertEqual(state["action"], "deliver")

    def test_cli_failure_emits_only_safe_error_and_success_emits_metadata(self):
        request = self.directory / "request.json"
        request.write_text(json.dumps(dict(self.packet, current_result="Bearer " + "A" * 32)))
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = ["review_checkpoint.py", "review", "--request-file", str(request), "--session-id", self.session, "--cwd", str(self.cwd)]
        with patch.object(sys, "argv", arguments), patch.object(review, "load_runtime", return_value=self.runtime), patch.object(review.subprocess, "run") as child, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = review.main()
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["error"], "credential_material_rejected")
        self.assertNotIn("A" * 32, stderr.getvalue())
        child.assert_not_called()
        request.write_text(json.dumps(self.packet))
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments[1] = "register"
        with patch.object(sys, "argv", arguments), patch.object(review, "load_runtime", return_value=self.runtime), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = review.main()
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["status"], "pending")

    def test_user_review_question_and_assessment_are_preserved_with_extra_question(self):
        packet = dict(self.packet, codex_assessment="当前Codex判断及依据。", review_question="另外检查恢复策略。")
        legacy = review.legacy_request(packet, "offline-id")
        self.assertTrue(legacy["question"].startswith(review.CORE_REVIEW_QUESTION))
        self.assertTrue(legacy["question"].endswith(packet["review_question"]))
        self.assertIn(packet["codex_assessment"], legacy["context"])
        for dimension in ("最开始的请求意图", "业界标准", "见解和结论", "较为简洁"):
            self.assertIn(dimension, legacy["question"])

    def test_prompt_change_invalidates_cached_advice_for_same_packet(self):
        first, _ = self.perform_review()
        for field in ("CORE_REVIEW_QUESTION", "REVIEW_INSTRUCTION"):
            with self.subTest(field=field), patch.object(review, field, getattr(review, field) + " 新版本"):
                registered = review.register_checkpoint(self.runtime, self.packet, self.session, self.cwd)
                self.assertNotEqual(registered["checkpoint_id"], first["checkpoint"]["checkpoint_id"])
                self.assertEqual(registered["packet_sha256"], first["checkpoint"]["packet_sha256"])
                self.assertNotEqual(registered["route_fingerprint"], first["checkpoint"]["route_fingerprint"])

    def test_simple_task_with_research_still_skips_all_external_calls(self):
        packet = dict(self.research_packet(), complexity="simple")
        packet["research"]["queries"] = ["person@example.com"]
        with patch.object(review.subprocess, "run") as child:
            result = review.review_checkpoint(self.research_runtime(), packet, self.session, self.cwd)
            child.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(store.load_state(self.runtime, self.session))

    def test_research_query_contract_and_unicode_budget_are_checked_before_calls(self):
        packet = self.research_packet()
        packet["research"]["queries"] = ["中" * 128]
        review._validate_packet(packet)
        invalid = [dict(packet["research"], queries=[]), dict(packet["research"], queries=["x"] * 3), dict(packet["research"], queries=["中" * 129]), dict(packet["research"], queries=["  "]), dict(packet["research"], freshness_key=""), {"required": True, "queries": ["public query"]}]
        for research in invalid:
            with self.subTest(research=research):
                with patch.object(review.subprocess, "run") as child:
                    with self.assertRaises(review.CheckpointError):
                        review.review_checkpoint(self.research_runtime(), dict(packet, research=research), self.session, self.cwd)
                    child.assert_not_called()
        self.assertIsNone(store.load_state(self.runtime, self.session))

    def test_queries_with_recognizable_private_identifiers_are_rejected_locally(self):
        queries = ["person@example.com", "/Users/example/private/repository", r"C:\private\project", "file:///tmp/config", "00000000-0000-0000-0000-000000000001", "10.2.3.4", "192.168.1.10", "127.0.0.1", "[::1]", "[fd12::1]", "service.internal", "host.ts.net", "Bearer " + "A" * 32, "sk-" + "B" * 32]
        for query in queries:
            with self.subTest(query=query[:20]):
                packet = self.research_packet()
                packet["research"]["queries"] = [query]
                with patch.object(review.subprocess, "run") as child:
                    self.assert_error("unsafe_query", review.review_checkpoint, self.research_runtime(), packet, self.session, self.cwd)
                    child.assert_not_called()
        self.assertIsNone(store.load_state(self.runtime, self.session))

    def test_required_search_runs_before_review_and_sends_only_public_queries(self):
        packet = self.research_packet()
        packet["research"]["queries"].append("公开验收指标的环境及样本量")
        with patch.object(review.subprocess, "run", side_effect=self.research_child()) as child:
            result = review.review_checkpoint(self.research_runtime(), packet, self.session, self.cwd)
        self.assertEqual(child.call_count, 2)
        search, consult = child.call_args_list
        self.assertEqual(search.args[0][0], "/offline/search-cli")
        self.assertEqual(json.loads(search.kwargs["input"]), {"queries": packet["research"]["queries"]})
        sent = json.loads(consult.kwargs["input"])
        self.assertEqual(sent["plan"], packet["current_result"])
        self.assertIn(packet["codex_assessment"], sent["context"])
        external = sent["evidence"][-1]
        self.assertIn("DeepSeek Search", external["source"])
        self.assertEqual(json.loads(external["content"].split("\n", 1)[1])["sources"][0]["content"], self.search_receipt(packet["research"]["queries"])["queries"][0]["results"][0]["content"])
        self.assertIn("[E1]", external["content"])
        self.assertIn("[E2]", external["content"])
        metadata = result["research"]
        self.assertEqual(metadata["provider"], "deepseek-search")
        self.assertEqual(metadata["source_count"], 2)
        self.assertEqual(metadata["search_calls"], 2)
        self.assertEqual(metadata["search_calls_this_run"], 2)
        self.assertFalse(metadata["reused"])
        self.assertEqual(metadata["sources"][0]["id"], "[E1]")
        receipt = Path(metadata["receipt_path"])
        self.assertEqual(hashlib.sha256(receipt.read_bytes()).hexdigest(), metadata["receipt_sha256"])
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_required_search_failure_timeout_and_unavailable_never_call_review(self):
        for response, error in ((subprocess.CompletedProcess([], 1, b'{"status":"failed","error":"search_http_error"}', PRIVATE.encode()), "search_http_error"), (subprocess.CompletedProcess([], 1, PRIVATE.encode(), PRIVATE.encode()), "tool_failed"), (subprocess.CompletedProcess([], 0, b'{}', b''), "invalid_search_response")):
            with self.subTest(error=error):
                with patch.object(review.subprocess, "run", return_value=response) as child:
                    self.assert_error(error, review.review_checkpoint, self.research_runtime(), self.research_packet(), self.session, self.cwd)
                    child.assert_called_once()
                state = store.load_state(self.runtime, self.session)
                self.assertEqual(state["status"], "failed")
                self.assertNotIn(PRIVATE, json.dumps(state))
        for exception, code in ((OSError(PRIVATE), "search_unavailable"), (subprocess.TimeoutExpired(PRIVATE, 95), "deadline_exceeded")):
            with patch.object(review.subprocess, "run", side_effect=exception) as child:
                self.assert_error(code, review.review_checkpoint, self.research_runtime(), self.research_packet(), self.session, self.cwd)
                child.assert_called_once()

    def test_no_search_results_continue_with_explicit_insufficient_evidence(self):
        packet = self.research_packet()
        receipt = self.search_receipt(packet["research"]["queries"], empty=True)
        with patch.object(review.subprocess, "run", side_effect=self.research_child(receipt)) as child:
            result = review.review_checkpoint(self.research_runtime(), packet, self.session, self.cwd)
        self.assertEqual(child.call_count, 2)
        sent = json.loads(child.call_args.kwargs["input"])
        self.assertIn("返回来源数=0", sent["context"])
        self.assertEqual(result["research"]["status"], "no_results")
        self.assertEqual(result["research"]["source_count"], 0)
        self.assertEqual(result["research"]["search_calls"], 1)

    def test_same_research_version_reuses_receipt_but_new_freshness_key_searches_again(self):
        runtime, packet = self.research_runtime(), self.research_packet()
        with patch.object(review.subprocess, "run", side_effect=self.research_child()) as child:
            first = review.review_checkpoint(runtime, packet, self.session, self.cwd)
            self.assertEqual(child.call_count, 2)
        with patch.object(review.subprocess, "run") as child:
            reused = review.review_checkpoint(runtime, packet, self.session, self.cwd)
            child.assert_not_called()
        self.assertEqual(reused["research"]["searched_at"], first["research"]["searched_at"])
        self.assertEqual(reused["research"]["receipt_sha256"], first["research"]["receipt_sha256"])
        self.assertTrue(reused["research"]["reused"])
        self.assertEqual(reused["research"]["search_calls_this_run"], 0)
        packet["research"]["freshness_key"] = "2026-11-07/standards-v2"
        with patch.object(review.subprocess, "run", side_effect=self.research_child()) as child:
            refreshed = review.review_checkpoint(runtime, packet, self.session, self.cwd)
            self.assertEqual(child.call_count, 2)
        self.assertNotEqual(refreshed["checkpoint"]["checkpoint_id"], first["checkpoint"]["checkpoint_id"])

    def test_cached_research_receipt_and_source_metadata_are_revalidated(self):
        for changed in ("receipt", "metadata"):
            with self.subTest(changed=changed):
                session = self.session + changed
                with patch.object(review.subprocess, "run", side_effect=self.research_child()):
                    result = review.review_checkpoint(self.research_runtime(), self.research_packet(), session, self.cwd)
                state = store.load_state(self.runtime, session)
                if changed == "receipt":
                    path = Path(result["research"]["receipt_path"])
                    receipt = json.loads(path.read_text())
                    receipt["queries"][0]["results"][0]["title"] = "被改写的来源标题"
                    store.write_private_json(path, receipt)
                else:
                    state["research"]["source_count"] += 1
                    store.save_state(self.runtime, session, state)
                with patch.object(review.subprocess, "run") as child:
                    self.assert_error("invalid_search_response", review.review_checkpoint, self.research_runtime(), self.research_packet(), session, self.cwd)
                    child.assert_not_called()

    def test_search_augmented_budget_rejects_before_review_without_cutting_result(self):
        packet = self.research_packet()
        identifier = "checkpoint-" + "0" * 32
        before_size = len(json.dumps(review.legacy_request(packet, identifier), ensure_ascii=False).encode())
        packet["current_result"] += "x" * (review.MAX_INPUT_BYTES - before_size - 100)
        original = packet["current_result"]
        with patch.object(review.subprocess, "run", side_effect=self.research_child()) as child:
            self.assert_error("request_too_large", review.review_checkpoint, self.research_runtime(), packet, self.session, self.cwd)
            child.assert_called_once()
        state = store.load_state(self.runtime, self.session)
        self.assertEqual(state["status"], "failed")
        self.assertTrue(Path(state["search_receipt_path"]).is_file())
        self.assertEqual(json.loads(Path(state["packet_path"]).read_text())["current_result"], original)

    def test_full_original_evidence_array_rejects_research_before_any_call(self):
        packet = self.research_packet()
        packet["evidence"] = [{"source": str(index), "content": "证据"} for index in range(32)]
        with patch.object(review.subprocess, "run") as child:
            self.assert_error("research_evidence_limit_exceeded", review.review_checkpoint, self.research_runtime(), packet, self.session, self.cwd)
            child.assert_not_called()

    def test_supersession_during_search_prevents_starting_review(self):
        def supersede():
            state = store.load_state(self.runtime, self.session)
            state["status"] = "superseded"
            store.save_state(self.runtime, self.session, state)
        with patch.object(review.subprocess, "run", side_effect=self.research_child(after_search=supersede)) as child:
            self.assert_error("checkpoint_no_longer_active", review.review_checkpoint, self.research_runtime(), self.research_packet(), self.session, self.cwd)
            child.assert_called_once()
        self.assertEqual(store.load_state(self.runtime, self.session)["status"], "superseded")

    def test_search_receipt_identity_hashes_excerpt_flags_and_urls_fail_closed(self):
        packet = self.research_packet()
        mutations = [lambda data: data["queries"][0].update(query="another query"), lambda data: data.update(search_calls=0), lambda data: data.update(searched_at="2026-11-06 03:04:05"), lambda data: data["queries"][0]["results"][0].update(content_sha256="0" * 64), lambda data: data["queries"][0]["results"][0].update(summary_excerpted=True), lambda data: data["queries"][0]["results"][0].update(url="https://user:pass@example.org"), lambda data: data["queries"][0]["results"].extend(data["queries"][0]["results"] * 3)]
        for mutate in mutations:
            receipt = self.search_receipt(packet["research"]["queries"])
            mutate(receipt)
            with patch.object(review.subprocess, "run", side_effect=self.research_child(receipt)) as child:
                self.assert_error("invalid_search_response", review.review_checkpoint, self.research_runtime(), packet, self.session, self.cwd)
                child.assert_called_once()

    def test_external_excerpts_keep_original_hashes_lengths_and_explicit_markers(self):
        packet = self.research_packet()
        receipt = self.search_receipt(packet["research"]["queries"])
        source = receipt["queries"][0]["results"][0]
        for key in ("content", "summary"):
            original = source[key] + "这是被截取的公开原文部分。"
            source.update({key + "_excerpted": True, "original_" + key + "_bytes": len(original.encode()), key + "_sha256": hashlib.sha256(original.encode()).hexdigest()})
            source[key + "_selection"]["spans"] = [{"start_byte": 0, "end_byte": len(source[key].encode())}]
        with patch.object(review.subprocess, "run", side_effect=self.research_child(receipt)):
            result = review.review_checkpoint(self.research_runtime(), packet, self.session, self.cwd)
        metadata = result["research"]["sources"][0]
        self.assertTrue(metadata["content_excerpted"])
        self.assertTrue(metadata["summary_excerpted"])
        self.assertEqual(metadata["content_sha256"], source["content_sha256"])
        self.assertEqual(metadata["original_content_bytes"], source["original_content_bytes"])

    def test_search_titles_may_be_missing_only_with_honest_flag_and_valid_url(self):
        packet = self.research_packet()
        for index, title in enumerate(("", "  \t")):
            with self.subTest(allowed_title=repr(title)):
                receipt = self.search_receipt(packet["research"]["queries"])
                source = receipt["queries"][0]["results"][0]
                source.update(title=title, title_missing=True)
                with patch.object(review.subprocess, "run", side_effect=self.research_child(receipt)) as child:
                    result = review.review_checkpoint(self.research_runtime(), packet, self.session + str(index), self.cwd)
                self.assertEqual(child.call_count, 2)
                recorded = result["research"]["sources"][0]
                self.assertEqual(recorded["title"], title)
                self.assertIs(recorded["title_missing"], True)
                self.assertEqual(recorded["url"], source["url"])
        mutations = [
            lambda source: source.update(title=""),
            lambda source: source.update(title="", title_missing=False),
            lambda source: source.update(title="", title_missing="true"),
            lambda source: source.update(title_missing=True),
            lambda source: source.update(title="", title_missing=True, url=""),
            lambda source: (source.update(title="", title_missing=True), source.pop("url")),
        ]
        for mutate in mutations:
            with self.subTest(invalid_title_shape=mutate):
                receipt = self.search_receipt(packet["research"]["queries"])
                mutate(receipt["queries"][0]["results"][0])
                with patch.object(review.subprocess, "run", side_effect=self.research_child(receipt)) as child:
                    self.assert_error("invalid_search_response", review.review_checkpoint, self.research_runtime(), packet, self.session, self.cwd)
                    child.assert_called_once()


if __name__ == "__main__":
    unittest.main()
