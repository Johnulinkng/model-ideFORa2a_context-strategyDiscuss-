"""Offline lifecycle checks. These do not claim the product trusts the hook."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import stop_checkpoint as hook


class StopCheckpointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = str(Path(self.temp.name).resolve())
        self.runtime = {"checkpoint_scopes": [self.cwd], "checkpoint_state_dir": self.cwd}
        self.state = {"session_id": "session-a", "cwd": self.cwd, "complexity": "complex",
                      "checkpoint_id": "checkpoint-a", "stage": "delivery", "status": "pending",
                      "continuation_requested": False, "packet_path": self.cwd + "/packet.json"}
        self.event = {"hook_event_name": "Stop", "session_id": "session-a", "cwd": self.cwd,
                      "stop_hook_active": False, "transcript_path": "/must/not/be/read.jsonl"}

    def run_event(self, state=None, event=None):
        with patch.object(hook, "load_state", return_value=state) as load, patch.object(hook, "save_state") as save:
            result = hook.handle(event or self.event, self.runtime)
        return result, load, save

    def test_complex_pending_continues_once_and_keeps_state(self):
        result, _, save = self.run_event(self.state)
        self.assertEqual(result["decision"], "block")
        self.assertIn(self.state["packet_path"], result["reason"])
        self.assertTrue(self.state["continuation_requested"])
        save.assert_called_once()
        repeated, _, repeated_save = self.run_event(self.state)
        self.assertEqual(repeated, {})
        repeated_save.assert_not_called()

    def test_reviewed_feedback_still_needs_main_judgment(self):
        self.state["status"] = "reviewed"
        result, _, _ = self.run_event(self.state)
        self.assertEqual(result["decision"], "block")

    def test_no_registration_and_simple_tasks_do_not_continue(self):
        for state in (None, dict(self.state, complexity="simple")):
            with self.subTest(state=state):
                result, _, save = self.run_event(state)
                self.assertEqual(result, {})
                save.assert_not_called()

    def test_finished_failed_or_inactive_states_do_not_continue(self):
        for status in ("decided", "failed", "interrupted", "superseded"):
            with self.subTest(status=status):
                result, _, save = self.run_event(dict(self.state, status=status))
                self.assertEqual(result, {})
                save.assert_not_called()

    def test_active_continuation_does_not_loop(self):
        result, _, save = self.run_event(self.state, dict(self.event, stop_hook_active=True))
        self.assertEqual(result, {})
        save.assert_not_called()

    def test_scope_boundary_and_different_session_do_not_continue(self):
        result, load, _ = self.run_event(self.state, dict(self.event, cwd=self.cwd + "-other"))
        self.assertEqual(result, {})
        load.assert_not_called()
        result, _, save = self.run_event(dict(self.state, session_id="session-b"))
        self.assertEqual(result, {})
        save.assert_not_called()

    def test_new_user_prompt_supersedes_old_pending_checkpoint(self):
        result, _, save = self.run_event(self.state, dict(self.event, hook_event_name="UserPromptSubmit", prompt="新任务"))
        self.assertEqual(result, {})
        self.assertEqual(self.state["status"], "superseded")
        save.assert_called_once()

    def test_own_continuation_prompt_preserves_review(self):
        self.state["continuation_requested"] = True
        prompt = hook.continuation_marker(self.state["checkpoint_id"]) + " 继续本检查点"
        result, _, save = self.run_event(self.state, dict(self.event, hook_event_name="UserPromptSubmit", prompt=prompt))
        self.assertEqual(result, {})
        save.assert_not_called()
        self.assertEqual(self.state["status"], "pending")

    def test_user_interrupt_cancels_pending_without_continuing(self):
        result, _, save = self.run_event(self.state, dict(self.event, hook_event_name="Interrupt"))
        self.assertEqual(result, {})
        self.assertEqual(self.state["status"], "interrupted")
        save.assert_called_once()

    def test_other_events_and_invalid_input_are_ignored(self):
        for event in ([], None, {}, dict(self.event, hook_event_name="SubagentStop")):
            with self.subTest(event=event):
                result, load, save = self.run_event(self.state, event or {"hook_event_name": "Unknown"})
                self.assertEqual(result, {})
                load.assert_not_called()
                save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
