"""Control-file tar builder - the ONE deliberate code copy in this package.

Phase 12.6, section 4.2: ``DockerOrchestrator`` reproduces LocalExecutor's
control-mode sequence ``create -> put_archive(control_files) -> start``, which
needs the identical tar. A runner host must not need ``backend/app`` on its
PYTHONPATH for one ~30-line function, and a shared installable package would
drag the backend's dependency tree onto every runner node.

So the function is COPIED, and the copy is pinned by
``tests/test_control_archive_parity.py``, which extracts the backend's
``build_control_archive`` out of
``backend/app/services/execution/local_executor.py`` and asserts BYTE equality
for the same input. That test is unconditional: it fails loudly if the backend
version moves, is renamed, or changes tar shape. A cheaper R3 instrument than a
shared package, with the same drift protection.

Byte determinism note: ``tarfile.TarInfo`` defaults ``mtime``/``uid``/``gid``
to 0 and ``uname``/``gname`` to "", and neither side sets them. That is why two
independent builders can be byte-identical at all - and why neither side may
start stamping a timestamp without the other.
"""
from __future__ import annotations

import io
import json
import tarfile
from typing import Sequence

#: Directory (relative to /workspace) the control files land in. Must match
#: ``local_executor.CONTROL_CONFIG_DIR``.
CONTROL_CONFIG_DIR = ".control"


def build_control_archive(files: Sequence[tuple[str, dict]]) -> bytes:
    """Build the in-memory tar delivering one or more `.control/<name>` files.

    Byte-for-byte identical to ``local_executor.build_control_archive``.
    Extracted by ``container.put_archive("/workspace", ...)`` onto the
    created-but-not-started step container, so secrets (the step JWT, the
    provider API key) never appear in ``docker inspect`` env. Every file is
    mode 0600. Tar entries carry no uid/gid: the image entrypoint's chown of
    /workspace/.control owns in-container readability.

    An agent step ships TWO entries in ONE tar: the step config and the agent
    config. One put_archive keeps them atomic with respect to container start.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        dir_info = tarfile.TarInfo(CONTROL_CONFIG_DIR)
        dir_info.type = tarfile.DIRTYPE
        dir_info.mode = 0o700
        tar.addfile(dir_info)

        for filename, config in files:
            payload = json.dumps(config, indent=2).encode("utf-8")
            file_info = tarfile.TarInfo(f"{CONTROL_CONFIG_DIR}/{filename}")
            file_info.size = len(payload)
            file_info.mode = 0o600
            tar.addfile(file_info, io.BytesIO(payload))
    return buf.getvalue()


def control_files_to_entries(control_files: dict) -> list[tuple[str, dict]]:
    """Turn ``execute_step.config.control_files`` into tar entries.

    The wire keys files by ABSOLUTE in-container path
    (``/workspace/.control/<step_execution_id>.json``); the tar builder wants
    basenames under ``.control/``. Insertion order is preserved rather than
    sorted: the backend emits the step config first and the agent config
    second, JSON preserves object order, and the local path tars them in that
    same order - re-sorting here would break byte parity for agent steps.

    A path outside the control root is a hard error: it would mean the backend
    is trying to write somewhere this tar cannot reach, and silently relocating
    it would put a step's config where the runtime will not look for it.
    """
    entries: list[tuple[str, dict]] = []
    prefix = f"/workspace/{CONTROL_CONFIG_DIR}/"
    for path, payload in (control_files or {}).items():
        text = str(path)
        if not text.startswith(prefix) or "/" in text[len(prefix):]:
            raise ValueError(
                f"control file path {text!r} is not directly under {prefix!r}; "
                "the control archive can only deliver files into that directory"
            )
        entries.append((text[len(prefix):], payload))
    return entries


__all__ = ["CONTROL_CONFIG_DIR", "build_control_archive", "control_files_to_entries"]
