"""Offline evidence-selection contract tests, independent of live services."""

import hashlib
import json
from unittest.mock import patch
import unittest

import checkpoint_state as store
import review_checkpoint as review
import test_review_checkpoint as fixtures


class RelevantSelectionTest(unittest.TestCase):
    setUp = fixtures.CheckpointTest.setUp
    reply = fixtures.CheckpointTest.reply
    child = fixtures.CheckpointTest.child
    assert_error = fixtures.CheckpointTest.assert_error
    research_packet = fixtures.CheckpointTest.research_packet
    research_runtime = fixtures.CheckpointTest.research_runtime
    search_receipt = fixtures.CheckpointTest.search_receipt
    research_child = fixtures.CheckpointTest.research_child

    def selected_receipt(self):
        packet = self.research_packet()
        packet["research"]["queries"] = ["Stop stop_hook_active continue false"]
        receipt = self.search_receipt(packet["research"]["queries"])
        source = receipt["queries"][0]["results"][0]
        prefix = "索引，尚非正文。\n" * 1700
        first = "### Stop\n\nstop_hook_active表示本轮是否已续跑。不能把SubagentStop当成本轮Stop。\n"
        middle = "无关段落。\n" * 200
        last = "continue false有优先级，不能忽略这项例外。\n"
        original = prefix + first + middle + last
        raw = original.encode("utf-8")
        first_start = len(prefix.encode("utf-8"))
        last_start = len((prefix + first + middle).encode("utf-8"))
        spans = [
            {"start_byte": first_start, "end_byte": first_start + len(first.encode("utf-8"))},
            {"start_byte": last_start, "end_byte": len(raw)},
        ]
        source.update({
            "content": first + review.EXCERPT_GAP.decode("utf-8") + last,
            "content_excerpted": True,
            "original_content_bytes": len(raw),
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "content_selection": {
                "policy": review.SEARCH_CONTRACT["excerpt_policy"],
                "status": "matched", "query_terms": ["stop", "stop_hook_active", "continue", "false"],
                "matched_terms": ["stop", "stop_hook_active", "continue", "false"],
                "matched_blocks": 3, "omitted_matching_blocks": 1,
                "truncated": False, "spans": spans,
            },
        })
        return packet, receipt, original

    def test_disjoint_spans_preserve_late_clauses_negations_and_wire_content(self):
        packet, receipt, original = self.selected_receipt()
        source = receipt["queries"][0]["results"][0]
        selected = source["content_selection"]
        self.assertGreater(selected["spans"][0]["start_byte"], 12000)
        raw = original.encode("utf-8")
        rebuilt = review.EXCERPT_GAP.join(raw[item["start_byte"]:item["end_byte"]] for item in selected["spans"]).decode("utf-8")
        self.assertEqual(rebuilt, source["content"])
        self.assertIn("不能把SubagentStop当成本轮Stop", rebuilt)
        self.assertIn("不能忽略这项例外", rebuilt)
        review._validate_search_receipt(receipt, packet)
        with patch.object(review.subprocess, "run", side_effect=self.research_child(receipt)) as child:
            result = review.review_checkpoint(self.research_runtime(), packet, self.session, self.cwd)
        self.assertEqual(child.call_count, 2)
        wire = json.loads(child.call_args.kwargs["input"])
        external = json.loads(wire["evidence"][-1]["content"].split("\n", 1)[1])
        self.assertEqual(external["sources"][0]["content"], source["content"])
        self.assertEqual(external["sources"][0]["content_selection"], selected)
        self.assertEqual(wire["plan"], packet["current_result"])
        self.assertEqual(result["research"]["sources"][0]["content_selection"]["omitted_matching_blocks"], 1)

    def test_empty_and_no_match_are_honest_metadata_states(self):
        packet, receipt, _ = self.selected_receipt()
        source = receipt["queries"][0]["results"][0]
        source.update(content="", content_excerpted=False, original_content_bytes=0, content_sha256=hashlib.sha256(b"").hexdigest())
        source["content_selection"].update(status="empty", matched_terms=[], matched_blocks=0, omitted_matching_blocks=0, spans=[])
        review._validate_search_receipt(receipt, packet)
        source.update(content="Background only", content_excerpted=False, original_content_bytes=len(b"Background only"), content_sha256=hashlib.sha256(b"Background only").hexdigest())
        source["content_selection"].update(status="no_match", spans=[{"start_byte": 0, "end_byte": len(b"Background only")}])
        review._validate_search_receipt(receipt, packet)
        source["content_selection"].update(status="matched")
        self.assert_error("invalid_search_response", review._validate_search_receipt, receipt, packet)

    def test_truncated_matching_window_allowed_but_full_copy_cannot_claim_truncated(self):
        packet, receipt, _ = self.selected_receipt()
        source = receipt["queries"][0]["results"][0]
        source["content_selection"]["truncated"] = True
        review._validate_search_receipt(receipt, packet)
        source["summary_selection"]["truncated"] = True
        self.assert_error("invalid_search_response", review._validate_search_receipt, receipt, packet)

    def test_old_or_missing_selection_policy_prevents_model_consultation(self):
        for policy in (None, "utf8-rune-head-v1"):
            with self.subTest(policy=policy):
                packet, receipt, _ = self.selected_receipt()
                source = receipt["queries"][0]["results"][0]
                if policy is None:
                    del source["content_selection"]
                else:
                    source["content_selection"]["policy"] = policy
                with patch.object(review.subprocess, "run", side_effect=self.research_child(receipt)) as child:
                    self.assert_error("invalid_search_response", review.review_checkpoint, self.research_runtime(), packet, self.session, self.cwd)
                child.assert_called_once()
                self.assertEqual(store.load_state(self.research_runtime(), self.session)["status"], "failed")

    def test_reordered_overlapping_out_of_range_and_wrong_length_spans_fail(self):
        mutations = [
            lambda s: s["spans"].reverse(),
            lambda s: s["spans"][1].update(start_byte=s["spans"][0]["start_byte"]),
            lambda s: s["spans"][0].update(start_byte=-1),
            lambda s: s["spans"][1].update(end_byte=1 << 30),
            lambda s: s["spans"][0].update(end_byte=s["spans"][0]["end_byte"] - 1),
            lambda s: s["spans"][0].update(start_byte=True),
        ]
        for mutation in mutations:
            packet, receipt, _ = self.selected_receipt()
            mutation(receipt["queries"][0]["results"][0]["content_selection"])
            self.assert_error("invalid_search_response", review._validate_search_receipt, receipt, packet)

    def test_gap_markers_and_utf8_segment_boundaries_are_checked(self):
        packet, receipt, _ = self.selected_receipt()
        source = receipt["queries"][0]["results"][0]
        source["content"] = source["content"].replace(review.EXCERPT_GAP.decode(), "\n\n[gap changed]\n\n")
        self.assert_error("invalid_search_response", review._validate_search_receipt, receipt, packet)
        packet, receipt, _ = self.selected_receipt()
        source = receipt["queries"][0]["results"][0]
        # Transfer a byte from the first Chinese-containing segment to the
        # second while keeping total byte count; the first segment is now invalid.
        first = source["content_selection"]["spans"][0]
        first["end_byte"] -= 2
        source["content_selection"]["spans"][1]["start_byte"] -= 2
        self.assert_error("invalid_search_response", review._validate_search_receipt, receipt, packet)

    def test_inconsistent_match_counts_terms_and_omissions_fail(self):
        mutations = [
            lambda s: s.update(status="empty"),
            lambda s: s.update(status="no_match"),
            lambda s: s.update(matched_terms=[]),
            lambda s: s.update(matched_terms=["not_a_query_term"]),
            lambda s: s.update(matched_terms=["stop", "stop"]),
            lambda s: s.update(omitted_matching_blocks=4),
            lambda s: s.update(matched_blocks=True),
            lambda s: s.update(omitted_matching_blocks=-1),
        ]
        for mutation in mutations:
            packet, receipt, _ = self.selected_receipt()
            mutation(receipt["queries"][0]["results"][0]["content_selection"])
            self.assert_error("invalid_search_response", review._validate_search_receipt, receipt, packet)

    def test_invalid_json_status_type_is_a_safe_search_failure_before_model(self):
        packet, receipt, _ = self.selected_receipt()
        receipt["queries"][0]["results"][0]["content_selection"]["status"] = []
        with patch.object(review.subprocess, "run", side_effect=self.research_child(receipt)) as child:
            self.assert_error("invalid_search_response", review.review_checkpoint, self.research_runtime(), packet, self.session, self.cwd)
        child.assert_called_once()
        self.assertEqual(store.load_state(self.research_runtime(), self.session)["status"], "failed")

    def test_selection_policy_change_invalidates_previously_saved_advice(self):
        packet, receipt, _ = self.selected_receipt()
        runtime = self.research_runtime()
        with patch.object(review.subprocess, "run", side_effect=self.research_child(receipt)):
            first = review.review_checkpoint(runtime, packet, self.session, self.cwd)
        with patch.object(review.subprocess, "run") as child:
            cached = review.review_checkpoint(runtime, packet, self.session, self.cwd)
            child.assert_not_called()
        self.assertTrue(cached["consultation"]["reused"])
        with patch.dict(review.SEARCH_CONTRACT, {"excerpt_policy": "future-policy-v2"}):
            new = review.register_checkpoint(runtime, packet, self.session, self.cwd)
        self.assertNotEqual(new["checkpoint_id"], first["checkpoint"]["checkpoint_id"])
        self.assertNotEqual(new["route_fingerprint"], first["checkpoint"]["route_fingerprint"])
        self.assertEqual(new["status"], "pending")


if __name__ == "__main__":
    unittest.main()
