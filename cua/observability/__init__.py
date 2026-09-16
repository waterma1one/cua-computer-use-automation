"""The structured run log and the evidence writer that produces the spec's §10 on-disk
layout.

Pure Python -- no browser driver, no DOM concept -- like every other package
`tests/test_architecture.py` protects: `PURE_PACKAGES` includes `"observability"`, and
this package never imports `cua.replay.engine` either (`cua.observability.EvidenceWriter`
satisfies that module's local `EvidenceSink` Protocol structurally, with no import in
either direction).

Carried note (STATE.md's "Open item for the human", restated here because this package is
where it becomes visible on disk): `cua.surface.snapshot.scrub_protected_values` redacts a
protected value by global substring replacement with a visible `"[REDACTED]"` marker, which
can touch unrelated text around a very short secret. `EvidenceWriter.write_run`'s
sensitive-input masking uses the same literal marker and inherits the same accepted
trade-off -- the repository owner should sign off on this consciously.

Every text write in this package -- `RunLog.event`, and every `EvidenceWriter` method that
writes text -- goes through `cua.policy.redact.RedactingWriter`, which applies the
shape-preserving pattern filter (`cua.policy.redact.redact`) on the way to disk.
"""

from __future__ import annotations

from cua.observability.evidence import EvidenceWriter
from cua.observability.log import RunLog

__all__ = ["EvidenceWriter", "RunLog"]
