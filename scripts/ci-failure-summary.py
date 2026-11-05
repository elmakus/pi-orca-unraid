#!/usr/bin/env python3
"""Explain a failed GitHub Actions build using only fixed, non-sensitive metadata."""
import json
import os
from pathlib import Path
import re
import sys

STEPS = (
    ("Resolver boundary tests", "RESOLVER_TEST"),
    ("Stream recovery unit tests", "STREAM_UNIT_TEST"),
    ("Resolve immutable build inputs", "INPUT_RESOLUTION"),
    ("Build Docker image", "DOCKER_BUILD"),
    ("Run isolated runtime smoke test", "RUNTIME_SMOKE"),
    ("Inventory Debian packages", "DEBIAN_INVENTORY"),
    ("Stream recovery integration test", "STREAM_E2E"),
    ("Publish and promote tested image", "PACKAGE_PROMOTION"),
    ("Record publication result", "RESULT_RECORD"),
    ("Send monitoring heartbeat", "HEARTBEAT"),
)
ALLOWED_STATUS = {"success", "failure", "skipped", "cancelled", "unknown"}


def safe_label(value):
    """Defensively reject arbitrary text (including secrets) from diagnostic files."""
    if not isinstance(value, str) or not 1 <= len(value) <= 140:
        return "unavailable"
    if not re.fullmatch(r"[A-Za-z0-9 ._:/;()=<>+,-]+", value):
        return "redacted"
    return value


def make_summary(statuses, diagnostic):
    failed = [name for name, env in STEPS if statuses.get(env) == "failure"]
    first = failed[0] if failed else "Workflow failed without a named failed stage"
    lines = ["## Pi-Orca build diagnostics", "", f"**Failed step:** {first}", "",
             "| Stage | Result |", "| --- | --- |"]
    for name, key in STEPS:
        value = statuses.get(key, "unknown")
        status = value if value in ALLOWED_STATUS else "unknown"
        lines.append(f"| {name} | {status} |")
    lines += ["", "### Runtime failure details"]
    if diagnostic and diagnostic.get("result") == "failure":
        for label, key in (("Precise stage", "stage"), ("Failure category", "category"),
                           ("Safe reason", "reason")):
            lines.append(f"- **{label}:** {safe_label(diagnostic.get(key))}")
        lines.append("- Captured command output, tokens and credentials are intentionally excluded.")
    elif statuses.get("RUNTIME_SMOKE") == "failure":
        lines.append("- Smoke test failed without a diagnostic file; inspect the smoke-step log for its last stage.")
    else:
        lines.append("- Runtime smoke test was not the failing stage or was not executed.")
    lines += ["", "**Publication is blocked if any required test fails.**",
              "The running Unraid container is not changed by this workflow.", ""]
    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: ci-failure-summary.py DIAGNOSTIC_JSON")
    statuses = {key: os.getenv(key, "unknown") for _, key in STEPS}
    diagnostic = None
    path = Path(sys.argv[1])
    if path.exists():
        try:
            candidate = json.loads(path.read_text())
            if isinstance(candidate, dict):
                diagnostic = candidate
        except (OSError, ValueError):
            pass
    message = make_summary(statuses, diagnostic)
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(message)
    # GitHub Actions job annotation shows the first failed stage without opening step logs.
    failed = next((name for name, key in STEPS if statuses[key] == "failure"), "unknown stage")
    print("BUILD_FAILURE_STAGE:", failed)
    if diagnostic and diagnostic.get("result") == "failure":
        print("RUNTIME_SMOKE_STAGE:", safe_label(diagnostic.get("stage")))
        print("RUNTIME_SMOKE_REASON:", safe_label(diagnostic.get("reason")))
    print("::error title=Pi-Orca build failure::Check the Pi-Orca build diagnostics job summary")


if __name__ == "__main__":
    main()
