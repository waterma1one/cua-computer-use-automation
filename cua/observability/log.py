"""The structured trace log: one JSON object per line, never a captured `LogRecord`.

E12: the evidence trail is built entirely from explicit `.event(**fields)` calls with
caller-supplied structured fields. Nothing here attaches a `logging.Handler` or reads
`logging.LogRecord` -- a stray `logger.info(...)` call anywhere else in the codebase must
never silently become a trace line, and a caller's own log lines never leak into a run's
evidence merely because they happened to be emitted while a step was replaying.

E8: every line reaches disk through `cua.policy.redact.RedactingWriter`, so the pattern
filter cannot be bypassed by a call site that forgets to scrub.
"""

from __future__ import annotations

import json
from pathlib import Path

from cua.policy.redact import RedactingWriter, mask_field, redact_leaves
from cua.surface.snapshot import scrub_protected_values


class RunLog:
    """Appends one JSON object per `event()` call to `path`, as newline-delimited JSON,
    through `RedactingWriter` (phase 5, E8) so every line is pattern-filtered on the way
    to disk.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._writer = RedactingWriter()

    def event(self, **fields: object) -> None:
        """Appends one line to the trace file, one JSON object per call.

        Three layers, in order: a `snapshot_yaml` field is passed through
        `scrub_protected_values` (a credential never reaches the trace verbatim); a `value`
        field is masked through `mask_field` when the event carries `redact=True` (§6.5:
        field-level `redact` is honoured by the log writer, and the replay engine tags its
        `bound` events with the output's declared flag -- E9; the masked value is always
        emitted as a string, whatever type the caller passed in); then every string leaf
        goes through the pattern filter (`redact_leaves`), and the serialised line goes
        through `RedactingWriter` once more, which is a no-op on already-filtered text and
        is what makes "every write is filtered" true by construction rather than by
        discipline.
        """
        scrubbed = dict(fields)
        snapshot_yaml = scrubbed.get("snapshot_yaml")
        if isinstance(snapshot_yaml, str):
            scrubbed["snapshot_yaml"] = scrub_protected_values(snapshot_yaml)
        value = scrubbed.get("value")
        if scrubbed.get("redact") is True and value is not None:
            scrubbed["value"] = mask_field(str(value))
        self._writer.put_line(self._path, json.dumps(redact_leaves(scrubbed)))
