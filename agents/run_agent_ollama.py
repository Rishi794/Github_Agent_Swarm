"""Single-pass agent reasoning against a local Ollama server.

This is the lightweight alternative to the llama.cpp debate engine
(``debate_runner.py``): one model load, one tool-enabled inference per agent, and
findings written to ``output/<agent_id>_finding.json``.

Environment variables
---------------------
AGENT_ID             Agent role, must be a key of system_prompts.json (required)
TASK_PROMPT          Task or research question (required)
MODEL_NAME           Ollama model tag (default: qwen3:8b)
SYSTEM_PROMPTS_PATH  Persona file (default: agents/system_prompts.json)
CENTRAL_MEMORY_PATH  JSON knowledge base used as read-only context
OLLAMA_BASE_URL      OpenAI-compatible endpoint (default: http://127.0.0.1:11434/v1)
OLLAMA_API_KEY       Dummy API key (default: ollama)
ENABLE_TOOLS         "0" to skip the tool loop (default: enabled)
MAX_TOOL_ITERATIONS  Tool-call budget (default: 3)
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_tools import run_tool_loop, tool_definitions  # noqa: E402

from openai import OpenAI  # noqa: E402
from pydantic import BaseModel, ConfigDict  # noqa: E402

SYSTEM_PROMPTS_PATH = Path(
    os.environ.get("SYSTEM_PROMPTS_PATH", os.environ.get("PROMPTS_PATH", "agents/system_prompts.json"))
)
OUTPUT_DIR = Path("output")
CENTRAL_MEMORY = Path(os.environ.get("CENTRAL_MEMORY_PATH", "central_memory/knowledge_base.json"))
MEMORY_TAIL = int(os.environ.get("CENTRAL_MEMORY_TAIL", "5"))

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class AgentFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    key_findings: list[str] = []
    risks_or_open_questions: list[str] = []
    confidence_score: float = 0.0


def load_central_memory(path: Path) -> str:
    """Return the most recent findings, trimmed to keep the CPU prompt cheap."""
    if not path.exists():
        return "No prior centralized findings exist yet."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "Prior centralized memory could not be parsed."
    return json.dumps(data.get("entries", [])[-MEMORY_TAIL:], indent=2)


def extract_json(text: str) -> str:
    fenced = _FENCE_RE.search(text)
    if fenced:
        return fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def structured_finding(client: OpenAI, model_name: str, messages: list[dict]) -> AgentFinding:
    try:
        response = client.beta.chat.completions.parse(
            model=model_name,
            messages=messages,
            response_format=AgentFinding,
            temperature=0.2,
        )
        parsed = response.choices[0].message.parsed
        if parsed is not None:
            return parsed
    except Exception as exc:  # noqa: BLE001 - fall back for servers without json_schema support
        print(f"[agent] Structured parse unavailable ({type(exc).__name__}: {exc}); using JSON mode.")

    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return AgentFinding.model_validate_json(extract_json(response.choices[0].message.content or "{}"))


def main() -> int:
    agent_id = os.environ["AGENT_ID"]
    task_prompt = os.environ["TASK_PROMPT"]
    model_name = os.environ.get("MODEL_NAME", "qwen3:8b")

    with open(SYSTEM_PROMPTS_PATH, "r", encoding="utf-8") as handle:
        personas = json.load(handle)
    persona = personas.get(agent_id, {"role": agent_id, "system_prompt": "Analyze the task thoroughly."})

    persona_tools = persona.get("tools")
    if isinstance(persona_tools, list) and persona_tools and not os.environ.get("TOOL_ALLOWLIST"):
        os.environ["TOOL_ALLOWLIST"] = ",".join(persona_tools)

    tools_enabled = os.environ.get("ENABLE_TOOLS", "1") != "0" and bool(tool_definitions())
    max_tool_iterations = int(os.environ.get("MAX_TOOL_ITERATIONS", "3"))

    base_prompt = persona.get("system_prompt") or (
        f"You are the {persona.get('role', agent_id)}. Focus: {persona.get('focus', '')}"
    )
    available = [tool["function"]["name"] for tool in tool_definitions()] if tools_enabled else []
    system_instruction = (
        f"{base_prompt}\n"
        + (f"Available tools: {', '.join(available)}.\n" if available else "")
        + "Review the shared central memory and analyze the task. "
        "Respond strictly with valid JSON matching the requested schema. /no_think"
    )
    user_content = (
        f"### Task Objective\n{task_prompt}\n\n"
        f"### Prior Centralized Storage Context\n{load_central_memory(CENTRAL_MEMORY)}"
    )

    client = OpenAI(
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
        api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
    )

    print(f"[{agent_id}] Running inference with Pareto model: {model_name} (tools={tools_enabled}) ...")
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": user_content},
    ]

    tool_trace: list[dict] = []
    if tools_enabled:
        messages = run_tool_loop(
            client, model_name, messages, agent_id, max_iterations=max_tool_iterations, trace=tool_trace
        )

    finding = structured_finding(client, model_name, messages)

    payload = finding.model_dump()
    payload["agent_id"] = agent_id
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    payload["model_used"] = model_name
    payload["task"] = task_prompt
    payload["tools_used"] = sorted({entry["call"] for entry in tool_trace})
    payload["tool_names"] = sorted({entry["tool"] for entry in tool_trace})
    payload["tool_call_count"] = len(tool_trace)
    payload["tool_trace"] = tool_trace

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_file = OUTPUT_DIR / f"{agent_id}_finding.json"
    output_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"[{agent_id}] Saved findings to {output_file} ({len(tool_trace)} tool call(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
