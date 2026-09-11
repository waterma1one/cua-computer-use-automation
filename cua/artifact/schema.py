"""Export an `Artifact` as a JSON Schema tool definition.

Spec S4.2 decision 5: "`inputs` and `outputs` are JSON Schema, generated from Pydantic.
The artifact is already a tool definition; the catalog exports it verbatim, so there is
no second schema to drift from the one replay validates against." `export_tool_schema`
builds the exported schema from the artifact's *declared* `inputs`/`outputs`
(`InputSpec`/`OutputSpec`, both JSON-Schema-shaped by construction) -- never from
`Artifact.model_json_schema()`, which would export the shape of the `Artifact` Pydantic
model itself (its `steps`, `provenance`, `app`, ...). Those are two entirely different
schemas, and producing the wrong one would violate the no-mechanics rule below in the
same stroke, by exporting exactly the fields that rule forbids.

**This function formats. It does not validate or gate.** (Controller ruling E12.) A
capability whose `validate()` call still yields errors, whose `verified` flag is false,
or whose registry `status` is `draft` can still be passed to `export_tool_schema` and
will still produce a well-formed tool schema back -- on purpose, so the catalog can
render a draft artifact for human review without this module silently deciding policy on
its behalf. Before offering the result of this function to a calling agent as something
that may actually be invoked, the caller MUST separately check all three of:

1. `cua.artifact.validate.validate(artifact)` carries no `error`-level finding;
2. `artifact.verified` is `True`;
3. the registry's `status` for `(artifact.id, artifact.version)` is not `draft`.

Phase 9 builds the catalog and owns enforcing this gate; nothing in this module or
`cua/artifact/` enforces it.

**The export leaks no mechanics.** What a calling agent receives is the contract -- what
the capability takes and returns -- never how it is achieved. This module reads only
`name`, `description`, `inputs`, and `outputs` off the artifact; it never touches
`steps`, `provenance`, `success`, `recovery`, or any locator. An agent that could see the
steps could try to reason about them, and the artifact is deliberately the only thing
that knows the mechanics.

**E13 (controller ruling): `sensitive` and `redact` are exported, not withheld.** An
earlier version of this module treated them as log-time concerns and left them out. That
was wrong, and E13 corrects it: S4.2 decision 6 says "the capability author declares
sensitivity; the log writer honours it" -- naming one consumer that must honour the flag,
not the only one permitted to see it. The calling agent is the party that actually
sources and constructs a sensitive argument, and it decides what to do with it *before*
this system's own log writer ever gets a look -- its own transcript, its own upstream
logging, its own display to a user are all outside this system's control. An agent never
told an argument is sensitive has no reason to treat it carefully, and that is a leak
this system cannot close after the fact from inside its own log writer.

This does not reopen the no-mechanics rule above: `sensitive` (on an input) and `redact`
(on an output) describe *what kind of thing the value is* -- a constraint on the
argument, exactly the same kind of fact `pattern` states -- never *how the capability
achieves its result*. `locator` and `steps` are mechanics; `sensitive` is contract, like
`type` and `pattern`. JSON Schema has no standard keyword for either flag, which is fine
here: an unrecognised key is inert to any validator, so unlike `pattern` it carries no
obligation to be independently enforceable by `jsonschema.validate` -- it only has to be
visible to a reader. Each flag uses the same field name as the source model
(`InputSpec.sensitive`, `OutputSpec.redact`) for zero translation cost, and is emitted
only when `True`, so a property with nothing to declare stays exactly as clean as before.

Like the rest of `cua/artifact/`, this module is pure data shaping: no I/O, no Playwright
import, no DOM reference, no CSS selector or XPath.
"""

from __future__ import annotations

from typing import Any

from cua.artifact.models import Artifact, InputSpec, OutputSpec


def _input_property(spec: InputSpec) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": spec.type}
    if spec.pattern is not None:
        prop["pattern"] = spec.pattern
    if spec.sensitive:
        prop["sensitive"] = True
    return prop


def _output_property(spec: OutputSpec) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": spec.type}
    if spec.format is not None:
        prop["format"] = spec.format
    if spec.redact:
        prop["redact"] = True
    return prop


def export_tool_schema(artifact: Artifact) -> dict[str, Any]:
    """Format `artifact` as a JSON Schema tool definition.

    Built entirely from the declared `name`, `description`, `inputs`, and `outputs` --
    see the module docstring for why nothing else on the artifact is read, and for E13's
    reasoning on why `sensitive`/`redact` are exported alongside `type`/`pattern`/
    `format` rather than withheld. Does not validate or gate; see the module docstring
    for the three checks a caller must run before treating the result as callable.
    """
    input_properties = {name: _input_property(spec) for name, spec in artifact.inputs.items()}
    required = [name for name, spec in artifact.inputs.items() if spec.required]
    output_properties = {name: _output_property(spec) for name, spec in artifact.outputs.items()}

    return {
        "name": artifact.name,
        "description": artifact.description,
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": input_properties,
            "required": required,
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": output_properties,
        },
    }
