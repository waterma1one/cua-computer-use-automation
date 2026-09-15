"""The structured trace log: one JSON object per line, never a captured `LogRecord`.

E12: the evidence trail is built entirely from explicit `.event(**fields)` calls with
caller-supplied structured fields. Nothing here attaches a `logging.Handler` or reads
`logging.LogRecord` -- a stray `logger.info(...)` call anywhere else in the codebase must
never silently become a trace line, and a caller's own log lines never leak into a run's
evidence merely because they happened to be emitted while a step was replaying.
"""

from __future__ import annotations

import json
from pathlib import Path

from cua.surface.snapshot import scrub_protected_values


class RunLog:
    """Appends one JSON object per `event()` call to `path`, as newline-delimited JSON.

    `path`'s parent directories are created on construction, so a caller never has to
    `mkdir` before the first `event()`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, **fields: object) -> None:
        """Appends one line to the trace file, one JSON object per call.

        A `snapshot_yaml` field, if present, is passed through `scrub_protected_values`
        before serialisation -- a raw accessibility-tree snapshot embedded in a trace
        event (e.g. on a `timed_out` branch outcome) must never carry a credential
        verbatim into `trace.jsonl`, exactly as it must not in the `snapshots/` directory
        `EvidenceWriter.frame` writes.
        """
        scrubbed = dict(fields)
        snapshot_yaml = scrubbed.get("snapshot_yaml")
        if isinstance(snapshot_yaml, str):
            scrubbed["snapshot_yaml"] = scrub_protected_values(snapshot_yaml)
        with self._path.open("a") as handle:
            handle.write(json.dumps(scrubbed))
            handle.write("\n")
