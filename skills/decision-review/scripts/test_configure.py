"""Local project binding tests; no network or real credentials."""
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch

import configure
import checkpoint_state


class ConfigureTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / "first project"
        self.project.mkdir()
        self.runtime = self.root / "skill/runtime.json"
        self.binary = self.root / "existing-tool"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        self.config = self.root / "private-config"
        self.config.write_text("opaque private configuration; never read by configure")

    def initialize(self, **kwargs):
        return configure.configure(self.runtime, self.project, binary=self.binary,
                                   gateway_config=self.config, state_dir=self.root / "state", **kwargs)

    def test_initialize_private_runtime_and_exact_project_scope(self):
        self.initialize()
        value = json.loads(self.runtime.read_text())
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o600)
        self.assertEqual(value["checkpoint_scopes"], [str(self.project)])
        self.assertTrue(checkpoint_state.in_scope(value, self.project / "child"))
        self.assertFalse(checkpoint_state.in_scope(value, self.root / "another"))
        self.assertNotIn("opaque private", self.runtime.read_text())

    def test_add_project_reuses_connection_and_is_idempotent(self):
        self.initialize()
        another = self.root / "second"
        another.mkdir()
        configure.configure(self.runtime, another)
        configure.configure(self.runtime, another)
        value = json.loads(self.runtime.read_text())
        self.assertEqual(len(value["checkpoint_scopes"]), 2)
        self.assertEqual(value["binary"], str(self.binary))

    def test_profile_imports_connection_without_old_scopes_or_credentials(self):
        profile = self.root / "prior.json"
        profile.write_text(json.dumps({"binary": str(self.binary), "gateway_config": str(self.config), "checkpoint_scopes": ["/an-old-project"], "api_key": "not-a-real-key", "unrelated": "private"}))
        configure.configure(self.runtime, self.project, profile=profile)
        value = json.loads(self.runtime.read_text())
        self.assertEqual(value["checkpoint_scopes"], [str(self.project)])
        self.assertNotIn("api_key", value)
        self.assertNotIn("unrelated", value)

    def test_missing_dependency_and_root_scope_fail_without_output_file(self):
        with self.assertRaises(ValueError):
            configure.configure(self.runtime, self.project)
        with self.assertRaises(ValueError):
            configure.configure(self.runtime, Path(self.root.anchor), binary=self.binary, gateway_config=self.config)
        self.assertFalse(self.runtime.exists())

    def test_non_executable_tool_and_invalid_model_do_not_overwrite_runtime(self):
        self.initialize()
        before = self.runtime.read_bytes()
        self.binary.chmod(0o600)
        with self.assertRaises(ValueError):
            self.initialize()
        self.binary.chmod(0o700)
        with self.assertRaises(ValueError):
            self.initialize(model={"bad": "value"})
        self.assertEqual(before, self.runtime.read_bytes())

    def test_hook_printing_quotes_paths_and_does_not_install_anything(self):
        python, script = self.root / "python with spaces", self.root / "skill with spaces/stop.py"
        value = configure.hook_config(script, python)
        self.assertEqual(set(value["hooks"]), {"UserPromptSubmit", "Stop", "Interrupt"})
        for hooks in value["hooks"].values():
            self.assertEqual(shlex.split(hooks[0]["hooks"][0]["command"]), [str(python), str(script)])
        self.assertFalse(self.runtime.exists())

    def test_concurrent_project_registration_does_not_lose_a_scope(self):
        self.initialize()
        projects = [self.root / "second", self.root / "third"]
        for project in projects:
            project.mkdir()
        start = threading.Barrier(2)
        write = configure.write_private_json
        def delayed_write(path, value):
            time.sleep(0.05)
            return write(path, value)
        def register(project):
            start.wait(timeout=5)
            return configure.configure(self.runtime, project)
        with patch.object(configure, "write_private_json", side_effect=delayed_write), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, projects))
        self.assertTrue(all(value["status"] == "configured" for value in results))
        self.assertEqual(set(json.loads(self.runtime.read_text())["checkpoint_scopes"]), {str(self.project), *(str(p) for p in projects)})

    def test_missing_named_profile_is_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            self.initialize(profile=self.root / "missing-profile.json")
        self.assertFalse(self.runtime.exists())


if __name__ == "__main__":
    unittest.main()
