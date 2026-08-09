"""Recursive `{{var}}` rendering for pipeline payloads.

Lets a UI submit a *template* payload (steps + context with placeholders) plus a
flat variable scope, and have the Bridge render it server-side before execution.

Deliberately stricter than `src/templates.py`, which leaves unknown variables
in place: here every unresolved name is reported back so the caller can be
rejected with 400. A pipeline prompt that still contains a literal `{{uid}}`
would make the agent actually run `aws s3 cp ... s3://bucket/{{uid}}/` and
silently create a garbage path.
"""

import re
import secrets
from datetime import date
from pathlib import Path

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _render(obj, scope: dict, missing: list, path: str):
    """Recursively substitute `{{var}}` in every string reachable from obj.

    Unresolved names are appended to `missing` as (name, json_path) and left
    verbatim in the output, so error messages can point at the exact field.
    """
    if isinstance(obj, str):
        def sub(m):
            name = m.group(1)
            if name in scope:
                return str(scope[name])
            missing.append((name, path))
            return m.group(0)
        return _VAR_RE.sub(sub, obj)
    if isinstance(obj, list):
        return [_render(v, scope, missing, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, dict):
        return {k: _render(v, scope, missing, f"{path}.{k}" if path else k)
                for k, v in obj.items()}
    return obj


def build_scope(input_text: str = "", variables: dict | None = None,
                uid: str = "") -> dict:
    """Flat variable scope, lowest to highest precedence: auto → input → vars."""
    scope = {
        "uid": uid or secrets.token_hex(4),
        "date": date.today().isoformat(),
        "input": input_text,
    }
    scope.update(variables or {})
    return scope


def render_payload(steps: list, context: dict, input_text: str = "",
                   variables: dict | None = None, uid: str = "") -> tuple:
    """Render a pipeline payload.

    Returns (steps, context, uid, missing). `missing` is a list of
    (variable_name, json_path) for every placeholder that had no value.
    """
    scope = build_scope(input_text, variables, uid)
    missing: list = []
    steps = _render(steps, scope, missing, "steps")
    context = _render(context, scope, missing, "context")
    return steps, context, scope["uid"], missing


def extract_artifacts(steps: list) -> list:
    """Pop `artifact` off each step, returning a list aligned with steps.

    Steps without a declaration get None, so index i of the result always
    corresponds to step i.
    """
    return [s.pop("artifact", None) or None for s in steps]


def resolve_artifacts(artifacts: list, steps: list, shared_cwd: str = "") -> list:
    """Pair artifact declarations with step outcomes for API readback.

    `type: file` patterns are resolved against `shared_cwd` (glob supported);
    any other type just echoes the rendered pattern.
    """
    out = []
    for i, art in enumerate(artifacts or []):
        if not art:
            continue
        step = steps[i] if i < len(steps) else {}
        entry = {
            "step": i,
            "agent": step.get("agent", ""),
            "type": art.get("type", ""),
            "label": art.get("label", ""),
            "pattern": art.get("pattern", ""),
        }
        if entry["type"] == "file" and shared_cwd and entry["pattern"]:
            matches = sorted(
                str(p) for p in Path(shared_cwd).glob(entry["pattern"]) if p.is_file()
            )
            entry["exists"] = bool(matches)
            if matches:
                entry["path"] = matches[0]
        elif entry["pattern"]:
            entry["url"] = entry["pattern"]
        out.append(entry)
    return out


def format_missing(missing: list) -> str:
    """One-line, de-duplicated summary of unresolved variables for a 400 body."""
    seen, out = set(), []
    for name, path in missing:
        key = (name, path)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{{{{{name}}}}} at {path}")
    return "unresolved variables: " + ", ".join(out)
