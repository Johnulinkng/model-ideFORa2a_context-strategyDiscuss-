"""Private, atomic state shared by checkpoint tools and the local Stop hook."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def load_runtime():
    value = json.loads((Path(__file__).resolve().parents[1] / "runtime.json").read_text())
    if not isinstance(value, dict):
        raise ValueError("invalid runtime")
    return value


def current_session_id():
    return os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or ""


def state_path(runtime, session_id):
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session required")
    directory = runtime.get("checkpoint_state_dir")
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError("absolute checkpoint state directory required")
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return Path(directory) / key / "pending.json"


def load_state(runtime, session_id):
    path = state_path(runtime, session_id)
    try:
        with path.open("rb") as stream:
            raw = stream.read((1 << 20) + 1)
    except FileNotFoundError:
        return None
    if len(raw) > 1 << 20:
        raise ValueError("oversized checkpoint state")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid checkpoint state")
    return value


def write_private_json(path, value):
    """Replace a complete JSON file with mode 0600; never expose partial JSON."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return raw


def save_state(runtime, session_id, value):
    if not isinstance(value, dict):
        raise ValueError("invalid checkpoint state")
    write_private_json(state_path(runtime, session_id), value)


@contextmanager
def state_transaction(runtime, session_id):
    """Protect short state read/modify/write operations, never network calls."""
    path = state_path(runtime, session_id).parent / "state.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def in_scope(runtime, cwd):
    try:
        candidate = Path(cwd).resolve()
        scopes = runtime.get("checkpoint_scopes", [])
        if not isinstance(scopes, list):
            return False
        for configured in scopes:
            if not isinstance(configured, str) or not Path(configured).is_absolute():
                continue
            scope = Path(configured).resolve()
            if candidate == scope or scope in candidate.parents:
                return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return False
