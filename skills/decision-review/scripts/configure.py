#!/usr/bin/env python3
"""Bind an existing local gateway toolchain to a project, without reading keys."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import sys

from checkpoint_state import write_private_json


def _file(value, executable=False):
    path = Path(value).expanduser().resolve()
    if not path.is_file() or (executable and not os.access(path, os.X_OK)):
        raise ValueError("required local tool/config file is unavailable")
    return str(path)


def configure(runtime_path, project, *, binary=None, gateway_config=None,
              search_binary=None, model=None, state_dir=None, profile=None):
    runtime_path = Path(runtime_path).resolve()
    runtime_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(runtime_path.parent / ".runtime.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _configure(runtime_path, project, binary=binary, gateway_config=gateway_config,
                          search_binary=search_binary, model=model, state_dir=state_dir, profile=profile)
    finally:
        os.close(descriptor)


def _configure(runtime_path, project, *, binary=None, gateway_config=None,
               search_binary=None, model=None, state_dir=None, profile=None):
    runtime = {}
    source = Path(profile).expanduser().resolve() if profile else runtime_path
    if profile and not source.is_file():
        raise ValueError("runtime profile does not exist")
    if source.is_file():
        if source.stat().st_size > 16384:
            raise ValueError("oversized runtime configuration")
        data = json.loads(source.read_text())
        if not isinstance(data, dict):
            raise ValueError("invalid runtime configuration")
        # Copy only connection metadata, never arbitrary credential fields.
        runtime = {key: data[key] for key in ("binary", "gateway_config", "search_binary", "model", "checkpoint_state_dir", "checkpoint_scopes") if key in data}
        if profile:
            runtime.pop("checkpoint_scopes", None)
    root = Path(project).expanduser().resolve()
    if not root.is_dir() or root == Path(root.anchor):
        raise ValueError("a concrete project directory is required")
    scopes = runtime.get("checkpoint_scopes", [])
    if not isinstance(scopes, list) or any(not isinstance(path, str) or not Path(path).is_absolute() for path in scopes):
        raise ValueError("invalid project scopes")
    runtime["checkpoint_scopes"] = sorted(set(scopes + [str(root)]))
    runtime["binary"] = _file(binary or runtime.get("binary", ""), executable=True)
    runtime["gateway_config"] = _file(gateway_config or runtime.get("gateway_config", ""))
    search = search_binary or runtime.get("search_binary")
    if search:
        runtime["search_binary"] = _file(search, executable=True)
    chosen_model = model if model is not None else runtime.get("model")
    if chosen_model is not None:
        if not isinstance(chosen_model, str) or not chosen_model.strip() or len(chosen_model) > 128:
            raise ValueError("invalid model alias")
        runtime["model"] = chosen_model
    state = Path(state_dir or runtime.get("checkpoint_state_dir") or Path.home() / ".local/state/decision-review").expanduser().resolve()
    runtime["checkpoint_state_dir"] = str(state)
    write_private_json(runtime_path, runtime)
    return {"status": "configured", "runtime_path": str(runtime_path), "project": str(root), "search_available": bool(search)}


def hook_config(script=None, python=None):
    script = Path(script or Path(__file__).with_name("stop_checkpoint.py")).resolve()
    command = shlex.join([str(Path(python or sys.executable).resolve()), str(script)])
    return {"description": "Only registered complex checkpoints; no transcript reads or network calls.",
            "hooks": {name: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
                      for name in ("UserPromptSubmit", "Stop", "Interrupt")}}


def main():
    parser = argparse.ArgumentParser(description="Configure this skill's local toolchain and project scope")
    parser.add_argument("--project", default=str(Path.cwd()))
    parser.add_argument("--binary")
    parser.add_argument("--gateway-config")
    parser.add_argument("--search-binary")
    parser.add_argument("--model")
    parser.add_argument("--state-dir")
    parser.add_argument("--profile-file", help="Existing runtime JSON; imports connection metadata, not project scopes")
    parser.add_argument("--print-hooks", action="store_true", help="Print configuration only; never install or trust it")
    args = parser.parse_args()
    try:
        if args.print_hooks:
            result = hook_config()
        else:
            result = configure(Path(__file__).resolve().parents[1] / "runtime.json", args.project,
                               binary=args.binary, gateway_config=args.gateway_config,
                               search_binary=args.search_binary, model=args.model,
                               state_dir=args.state_dir, profile=args.profile_file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "failed", "error": "invalid_local_configuration"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
