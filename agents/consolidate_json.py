"""Fan-in consolidator for the single-pass Ollama swarm (JSON knowledge base).

Collects ``incoming_findings/*_finding.json`` from every ``agent-state/<agent>``
branch and appends them to the canonical ``knowledge_base.json`` on the
``central-storage`` branch, alongside a human-readable Markdown dashboard.

Environment variables
---------------------
INCOMING_DIR  Directory holding *_finding.json payloads (default: incoming_findings)
KB_JSON_PATH  Canonical JSON knowledge base (default: knowledge_base.json)
REPORT_PATH   Markdown dashboard (default: SWARM_REPORT.md)
"""

from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

INCOMING_DIR = Path(os.environ.get("INCOMING_DIR", "incoming_findings"))
KB_JSON_PATH = Path(os.environ.get("KB_JSON_PATH", "knowledge_base.json"))
REPORT_PATH = Path(os.environ.get("REPORT_PATH", "SWARM_REPORT.md"))


def load_knowledge_base() -> dict:
    if KB_JSON_PATH.exists():
        try:
            return json.loads(KB_JSON_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[consolidate] {KB_JSON_PATH} was unreadable; starting a new store.")
    return {"last_updated": None, "entries": []}


def render_report(knowledge_base: dict, new_findings: list[dict]) -> str:
    lines = [
        "# Centralized Multi-Agent Knowledge Store",
        "",
        f"**Last Updated:** `{knowledge_base['last_updated']}` "
        f"| **Total Findings:** `{len(knowledge_base['entries'])}` "
        f"| **Findings This Run:** `{len(new_findings)}`",
        "",
        "## Latest Run Findings",
        "",
    ]
    for item in new_findings:
        lines.append(f"### Agent: `{item['agent_id']}` (Model: `{item['model_used']}`)")
        lines.append("")
        lines.append(f"**Task:** {item['task']}")
        lines.append("")
        lines.append(f"**Summary:** {item['summary']}")
        lines.append("")

        findings = item.get("key_findings", []) or []
        if findings:
            lines.append("**Key Findings:**")
            lines.extend(f"- {entry}" for entry in findings)
            lines.append("")

        risks = item.get("risks_or_open_questions", []) or []
        if risks:
            lines.append("**Risks / Open Questions:**")
            lines.extend(f"- {entry}" for entry in risks)
            lines.append("")

        lines.extend(["---", ""])

    return "\n".join(lines)


def main() -> int:
    knowledge_base = load_knowledge_base()

    new_findings: list[dict] = []
    for filepath in sorted(glob.glob(str(INCOMING_DIR / "*_finding.json"))):
        try:
            finding = json.loads(Path(filepath).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[consolidate] Skipping malformed payload {filepath}: {exc}")
            continue
        knowledge_base["entries"].append(finding)
        new_findings.append(finding)

    knowledge_base["last_updated"] = datetime.now(timezone.utc).isoformat()

    KB_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    KB_JSON_PATH.write_text(json.dumps(knowledge_base, indent=2), encoding="utf-8")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(knowledge_base, new_findings), encoding="utf-8")

    print(
        f"[consolidate] Appended {len(new_findings)} finding(s) to {KB_JSON_PATH} "
        f"(total: {len(knowledge_base['entries'])}) -> report at {REPORT_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
