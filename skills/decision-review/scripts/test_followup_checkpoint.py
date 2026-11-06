"""Offline observations of the two-round clarification contract; no network."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_review_checkpoint as fixtures
import checkpoint_state as store
import review_checkpoint as review
import stop_checkpoint as stop


class FollowupTest(unittest.TestCase):
    setUp = fixtures.CheckpointTest.setUp
    reply = fixtures.CheckpointTest.reply
    child = fixtures.CheckpointTest.child
    perform_review = fixtures.CheckpointTest.perform_review
    assert_error = fixtures.CheckpointTest.assert_error
    research_packet = fixtures.CheckpointTest.research_packet
    research_runtime = fixtures.CheckpointTest.research_runtime
    search_receipt = fixtures.CheckpointTest.search_receipt
    research_child = fixtures.CheckpointTest.research_child

    def initial(self):
        return self.perform_review(transform=lambda reply: reply["advisors"][0].update(content="还缺原始预算，以及该数字是否已扣除费用？\n请补充。"))[0]

    def supplement(self):
        return {"context_request": "DS询问预算及是否已扣费用，需据本地事实补充。", "supplement": "本地样本定义为净预算80，已扣费用20，不能再扣一次。"}

    def followup(self, first=None, supplement=None, **kwargs):
        first = self.initial() if first is None else first
        return review.followup_checkpoint(self.runtime, self.session, self.cwd, first["checkpoint"]["checkpoint_id"], self.supplement() if supplement is None else supplement, **kwargs)

    def test_second_round_retains_original_result_request_advice_and_supplement(self):
        first = self.initial()
        first_state = store.load_state(self.runtime, self.session)
        original_bytes = Path(first_state["packet_path"]).read_bytes()
        with patch.object(review.subprocess, "run", side_effect=self.child()) as child:
            second = self.followup(first)
        child.assert_called_once()
        sent = json.loads(child.call_args.kwargs["input"])
        state = store.load_state(self.runtime, self.session)
        self.assertEqual(first["consultation"]["round"], 1)
        self.assertTrue(first["consultation"]["followup_allowed"])
        self.assertEqual(second["consultation"]["round"], 2)
        self.assertEqual(second["consultation"]["max_rounds"], 2)
        self.assertFalse(second["consultation"]["followup_allowed"])
        self.assertEqual(first["checkpoint"]["checkpoint_id"], second["checkpoint"]["checkpoint_id"])
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(sent["plan"], self.packet["current_result"])
        self.assertIn(self.packet["original_request"], sent["context"])
        self.assertIn(self.supplement()["supplement"], sent["context"])
        self.assertEqual(state["discussion"]["previous_advisors"][0]["content"], first["advisors"][0]["content"])
        self.assertEqual(Path(state["packet_path"]).read_bytes(), original_bytes)
        self.assertEqual(state["previous_round"]["reply_path"], first_state["reply_path"])
        self.assertTrue(Path(first_state["reply_path"]).is_file())
        self.assertTrue(Path(state["reply_path"]).is_file())
        self.assertNotEqual(first_state["reply_path"], state["reply_path"])

    def test_same_supplement_and_plain_review_reuse_second_reply_then_main_decides(self):
        first = self.initial()
        with patch.object(review.subprocess, "run", side_effect=self.child()):
            second = self.followup(first)
        with patch.object(review.subprocess, "run") as child:
            cached = self.followup(first)
            plain = review.review_checkpoint(self.runtime, self.packet, self.session, self.cwd)
            child.assert_not_called()
        self.assertEqual(cached["request_id"], second["request_id"])
        self.assertEqual(plain["advisors"], second["advisors"])
        self.assertTrue(cached["consultation"]["reused"])
        decision = review.decide_checkpoint(self.runtime, self.session, self.cwd, first["checkpoint"]["checkpoint_id"], {"decision": "补充已到达，实测结果可继续，主助手承担判断。", "action": "continue"})
        self.assertEqual(decision["status"], "decided")
        with patch.object(review.subprocess, "run") as child:
            after_decision = self.followup(first)
            child.assert_not_called()
        self.assertEqual(after_decision["checkpoint"]["status"], "decided")

    def test_different_third_supplement_is_rejected_without_call_or_mutation(self):
        first = self.initial()
        with patch.object(review.subprocess, "run", side_effect=self.child()):
            self.followup(first)
        before = store.load_state(self.runtime, self.session)
        with patch.object(review.subprocess, "run") as child:
            self.assert_error("discussion_round_limit", self.followup, first, dict(self.supplement(), supplement="又一个新问题。"))
            child.assert_not_called()
        self.assertEqual(store.load_state(self.runtime, self.session), before)

    def test_followup_requires_successful_first_round_and_current_scope(self):
        pending = review.register_checkpoint(self.runtime, self.packet, self.session, self.cwd)
        identifier = pending["checkpoint_id"]
        with patch.object(review.subprocess, "run") as child:
            self.assert_error("checkpoint_not_reviewed", review.followup_checkpoint, self.runtime, self.session, self.cwd, identifier, self.supplement())
            child.assert_not_called()
        first = self.initial()
        identifier = first["checkpoint"]["checkpoint_id"]
        other_cwd = self.cwd / "nested"
        other_cwd.mkdir()
        before = store.load_state(self.runtime, self.session)
        with patch.object(review.subprocess, "run") as child:
            self.assert_error("checkpoint_no_longer_active", review.followup_checkpoint, self.runtime, "different-session", self.cwd, identifier, self.supplement())
            self.assert_error("checkpoint_no_longer_active", review.followup_checkpoint, self.runtime, self.session, other_cwd, identifier, self.supplement())
            self.assert_error("out_of_scope", review.followup_checkpoint, self.runtime, self.session, self.directory / "outside", identifier, self.supplement())
            child.assert_not_called()
        self.assertEqual(store.load_state(self.runtime, self.session), before)

    def test_interrupted_superseded_and_skipped_states_cannot_resume(self):
        for status in ("interrupted", "superseded", "skipped"):
            with self.subTest(status=status):
                first = self.initial()
                state = store.load_state(self.runtime, self.session)
                state["status"] = status
                store.save_state(self.runtime, self.session, state)
                with patch.object(review.subprocess, "run") as child:
                    self.assert_error("checkpoint_no_longer_active", self.followup, first)
                    child.assert_not_called()
                self.assertEqual(store.load_state(self.runtime, self.session), state)

    def test_second_round_failure_never_reuses_first_reply_as_success(self):
        for response, error in ((subprocess.CompletedProcess([], 1, b'{}', b''), "tool_failed"), (subprocess.CompletedProcess([], 0, b'{}', b''), "invalid_review_result"), (subprocess.TimeoutExpired("private-unused", 195), "deadline_exceeded")):
            with self.subTest(error=error):
                first = self.initial()
                identifier = first["checkpoint"]["checkpoint_id"]
                if isinstance(response, Exception):
                    kwargs = {"side_effect": response}
                else:
                    kwargs = {"return_value": response}
                with patch.object(review.subprocess, "run", **kwargs) as child:
                    self.assert_error(error, self.followup, first)
                    child.assert_called_once()
                state = store.load_state(self.runtime, self.session)
                self.assertEqual(state["round"], 2)
                self.assertEqual(state["status"], "failed")
                self.assertNotIn("reply_path", state)
                self.assertTrue(Path(state["previous_round"]["reply_path"]).is_file())
                self.assert_error("checkpoint_no_longer_active", review.decide_checkpoint, self.runtime, self.session, self.cwd, identifier, {"decision": "不能把首轮当成补充已复核", "action": "continue"})

    def test_second_round_reuses_search_receipt_and_reports_zero_new_search_calls(self):
        self.runtime = self.research_runtime()
        self.packet = self.research_packet()
        with patch.object(review.subprocess, "run", side_effect=self.research_child()) as child:
            first = review.review_checkpoint(self.runtime, self.packet, self.session, self.cwd)
        self.assertEqual(child.call_count, 2)
        with patch.object(review.subprocess, "run", side_effect=self.child()) as child:
            second = self.followup(first)
        child.assert_called_once()
        self.assertEqual(second["research"]["search_calls_this_run"], 0)
        self.assertTrue(second["research"]["reused"])
        self.assertEqual(first["research"]["receipt_sha256"], second["research"]["receipt_sha256"])
        self.assertIn("[E1]", json.loads(child.call_args.kwargs["input"])["evidence"][-1]["content"])

    def test_changed_route_rejects_followup_before_call(self):
        first = self.initial()
        changed = dict(self.runtime, model="a-different-route")
        with patch.object(review.subprocess, "run") as child:
            self.assert_error("checkpoint_route_changed", review.followup_checkpoint, changed, self.session, self.cwd, first["checkpoint"]["checkpoint_id"], self.supplement())
            child.assert_not_called()
        self.assertEqual(store.load_state(self.runtime, self.session)["round"], 1)

    def test_invalid_private_or_oversized_supplement_never_consumes_round(self):
        first = self.initial()
        invalid = [({}, "invalid_checkpoint_supplement"), ({"context_request": "x", "supplement": ""}, "invalid_checkpoint_supplement"), ({"context_request": "x", "supplement": "Bearer " + "A" * 32}, "credential_material_rejected"), ({"context_request": "x", "supplement": "x" * review.MAX_INPUT_BYTES}, "request_too_large")]
        for supplement, error in invalid:
            with self.subTest(error=error), patch.object(review.subprocess, "run") as child:
                self.assert_error(error, self.followup, first, supplement)
                child.assert_not_called()
            self.assertEqual(store.load_state(self.runtime, self.session)["round"], 1)

    def test_combined_payload_budget_rejects_without_cutting_result_or_consuming_round(self):
        self.packet["current_result"] = "中" * 65000
        first = self.initial()
        before = store.load_state(self.runtime, self.session)
        supplement = dict(self.supplement(), supplement="充" * 23000)
        self.assertLess(len(review._json_bytes(supplement)), review.MAX_INPUT_BYTES)
        with patch.object(review.subprocess, "run") as child:
            self.assert_error("request_too_large", self.followup, first, supplement)
            child.assert_not_called()
        self.assertEqual(store.load_state(self.runtime, self.session), before)
        self.assertEqual(json.loads(Path(before["packet_path"]).read_text())["current_result"], self.packet["current_result"])

    def test_tampered_first_reply_and_original_packet_are_not_forwarded(self):
        for artifact, error in (("reply_path", "invalid_review_result"), ("packet_path", "invalid_checkpoint_packet")):
            with self.subTest(artifact=artifact):
                first = self.initial()
                state = store.load_state(self.runtime, self.session)
                path = Path(state[artifact])
                value = json.loads(path.read_text())
                if artifact == "reply_path":
                    value["advisors"][0]["content"] += " tampered"
                else:
                    value["context"] += " tampered"
                store.write_private_json(path, value)
                with patch.object(review.subprocess, "run") as child:
                    self.assert_error(error, self.followup, first)
                    child.assert_not_called()
                # Restore the immutable original before constructing a new case.
                if artifact == "reply_path":
                    value["advisors"][0]["content"] = value["advisors"][0]["content"].removesuffix(" tampered")
                else:
                    value["context"] = value["context"].removesuffix(" tampered")
                store.write_private_json(path, value)

    def test_late_followup_reply_cannot_resurrect_user_interrupt(self):
        for kind, expected in (("Interrupt", "interrupted"), ("UserPromptSubmit", "superseded")):
            with self.subTest(kind=kind):
                first = self.initial()
                def run(command, **kwargs):
                    stop.handle({"hook_event_name": kind, "session_id": self.session, "cwd": str(self.cwd), "prompt": "这是新的用户指令"}, self.runtime)
                    self.assertEqual(store.load_state(self.runtime, self.session)["status"], expected)
                    return self.child()(command, **kwargs)
                with patch.object(review.subprocess, "run", side_effect=run):
                    self.assert_error("checkpoint_no_longer_active", self.followup, first)
                self.assertEqual(store.load_state(self.runtime, self.session)["status"], expected)

    def test_round2_native_stop_marker_is_preserved_but_interrupt_cancels_it(self):
        first = self.initial()
        def run(command, **kwargs):
            event = {"session_id": self.session, "cwd": str(self.cwd)}
            result = stop.handle(dict(event, hook_event_name="Stop", stop_hook_active=False), self.runtime)
            self.assertEqual(result["decision"], "block")
            marker = stop.continuation_marker(first["checkpoint"]["checkpoint_id"])
            self.assertTrue(result["reason"].startswith(marker))
            stop.handle(dict(event, hook_event_name="UserPromptSubmit", prompt=result["reason"]), self.runtime)
            self.assertEqual(store.load_state(self.runtime, self.session)["status"], "pending")
            self.assertEqual(store.load_state(self.runtime, self.session)["round"], 2)
            stop.handle(dict(event, hook_event_name="Interrupt"), self.runtime)
            self.assertEqual(store.load_state(self.runtime, self.session)["status"], "interrupted")
            self.assertEqual(stop.handle(dict(event, hook_event_name="Stop", stop_hook_active=False), self.runtime), {})
            return self.child()(command, **kwargs)
        with patch.object(review.subprocess, "run", side_effect=run):
            self.assert_error("checkpoint_no_longer_active", self.followup, first)
        self.assertEqual(store.load_state(self.runtime, self.session)["status"], "interrupted")

    def test_interrupt_hook_during_second_reply_persistence_is_not_overwritten(self):
        first = self.initial()
        original_write = review.write_private_json
        def write(path, value):
            raw = original_write(path, value)
            if str(path).endswith(".round2.reply.json"):
                output = stop.handle({"hook_event_name": "Interrupt", "session_id": self.session, "cwd": str(self.cwd)}, self.runtime)
                self.assertEqual(output, {})
                self.assertEqual(store.load_state(self.runtime, self.session)["status"], "interrupted")
            return raw
        with patch.object(review.subprocess, "run", side_effect=self.child()), patch.object(review, "write_private_json", side_effect=write):
            self.assert_error("checkpoint_no_longer_active", self.followup, first)
        self.assertEqual(store.load_state(self.runtime, self.session)["status"], "interrupted")

    def test_same_pending_second_round_can_resume_after_local_process_was_lost(self):
        first = self.initial()
        with patch.object(review.subprocess, "run", side_effect=self.child()):
            second = self.followup(first)
        state = store.load_state(self.runtime, self.session)
        state.update(status="pending")
        state.pop("reply_path")
        state.pop("reply_sha256")
        store.save_state(self.runtime, self.session, state)
        with patch.object(review.subprocess, "run", side_effect=self.child()) as child:
            resumed = self.followup(first)
        child.assert_called_once()
        self.assertEqual(resumed["request_id"], second["request_id"])
        self.assertEqual(resumed["consultation"]["round"], 2)

    def test_cli_lock_refuses_parallel_dispatch_but_does_not_block_interrupt_hook(self):
        first = self.initial()
        with review._session_lock(self.runtime, self.session, self.cwd):
            with self.assertRaises(review.CheckpointError) as caught:
                with review._session_lock(self.runtime, self.session, self.cwd):
                    self.fail("The second dispatcher must not acquire the same session lock.")
            self.assertEqual(caught.exception.code, "checkpoint_busy")
            output = stop.handle({"hook_event_name": "Interrupt", "session_id": self.session, "cwd": str(self.cwd)}, self.runtime)
        self.assertEqual(output, {})
        self.assertEqual(store.load_state(self.runtime, self.session)["status"], "interrupted")
        with patch.object(review.subprocess, "run") as child:
            self.assert_error("checkpoint_no_longer_active", self.followup, first)
            child.assert_not_called()

    def test_cli_followup_busy_reports_public_error_without_model(self):
        first = self.initial()
        path = self.directory / "supplement.json"
        path.write_text(json.dumps(self.supplement()))
        arguments = ["review_checkpoint.py", "followup", "--checkpoint-id", first["checkpoint"]["checkpoint_id"], "--request-file", str(path), "--session-id", self.session, "--cwd", str(self.cwd)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with review._session_lock(self.runtime, self.session, self.cwd), patch.object(sys, "argv", arguments), patch.object(review, "load_runtime", return_value=self.runtime), patch.object(review.subprocess, "run") as child, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = review.main()
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), {"status": "failed", "error": "checkpoint_busy"})
        child.assert_not_called()

    def test_cli_followup_success_writes_second_round_output(self):
        first = self.initial()
        request, output = self.directory / "supplement.json", self.directory / "reply.json"
        request.write_text(json.dumps(self.supplement()))
        arguments = ["review_checkpoint.py", "followup", "--checkpoint-id", first["checkpoint"]["checkpoint_id"], "--request-file", str(request), "--output", str(output), "--session-id", self.session, "--cwd", str(self.cwd)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", arguments), patch.object(review, "load_runtime", return_value=self.runtime), patch.object(review.subprocess, "run", side_effect=self.child()) as child, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = review.main()
        self.assertEqual(status, 0, stderr.getvalue())
        child.assert_called_once()
        result = json.loads(output.read_text())
        self.assertEqual(result, json.loads(stdout.getvalue()))
        self.assertEqual(result["consultation"]["round"], 2)
        self.assertIn("如果这里有缺任何上下文信息请询问我", json.loads(child.call_args.kwargs["input"])["question"])


if __name__ == "__main__":
    unittest.main()
