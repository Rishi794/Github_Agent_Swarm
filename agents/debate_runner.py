"""Multi-round, tool-enabled debate engine.

Each agent (one GitHub Actions matrix leg) runs this script. It talks to a
**single** locally hosted ``llama-server`` for the entire debate and, between
rounds, synchronizes with peers through isolated Git branches
(``agent-state/<agent>``) so no two runners ever push to the same ref.

Round r protocol
----------------
1. System prompt = persona from ``agents/system_prompts.json`` (+ round directive).
2. Tool loop: the model may call ``search_web`` / ``query_arxiv`` / ``fetch_url`` /
   ``compute`` repeatedly (budget: ``max_tool_iterations``) before answering.
3. Structured turn: schema-constrained JSON thesis for this round.
4. Commit ``output/round_r.json`` to ``agent-state/<agent_id>`` and force-push.
5. Poll ``origin/agent-state/<peer>`` until every peer published the same
   ``run_id`` for round r (Git barrier), then continue to round r+1.

Environment variables
---------------------
AGENT_ID             Agent role, must be a key of system_prompts.json (required)
TASK_PROMPT          Debate topic (required)
GITHUB_RUN_ID        Run identifier used to reject stale peer state (required)
ALL_AGENTS           Comma separated roster (default: researcher,security_auditor,synthesizer)
CONFIG_PATH          Path to model_config.json (default: external_config/model_config.json)
SYSTEM_PROMPTS_PATH  Persona file (default: agents/system_prompts.json)
RAG_CONTEXT_PATH     Retrieved vector memory from rag_query.py (default: /tmp/rag_context.json)
LLAMA_BASE_URL       OpenAI-compatible endpoint of the local server (default: http://127.0.0.1:8080/v1)
LLAMA_API_KEY        Dummy API key for llama-server (default: llama-cpp)
ROUNDS_OVERRIDE      Optional integer overriding debate_rounds from the config
GIT_REMOTE           Git remote name (default: origin)
TOOL_ALLOWLIST       Set automatically per persona; restricts exposed tools
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make `import agent_tools` work whether this file is executed as a script
# (`python agents/debate_runner.py`) or imported as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_tools import run_tool_loop, tool_definitions  # noqa: E402
from model_output import (  # noqa: E402
    coerce_confidence,
    coerce_str_list,
    coerce_text,
    parse_model_json,
    salvage_from_exception,
    task_identity,
)

from openai import OpenAI  # noqa: E402
from pydantic import BaseModel, ConfigDict, field_validator  # noqa: E402

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "external_config/model_config.json"))
SYSTEM_PROMPTS_PATH = Path(
    os.environ.get("SYSTEM_PROMPTS_PATH", os.environ.get("PROMPTS_PATH", "agents/system_prompts.json"))
)
RAG_CONTEXT_PATH = Path(os.environ.get("RAG_CONTEXT_PATH", "/tmp/rag_context.json"))
OUTPUT_DIR = Path("output")

DEFAULT_AGENTS = ["researcher", "security_auditor", "synthesizer"]
DEFAULT_PEER_TIMEOUT_SEC = 1800
POLL_INTERVAL_SEC = 6


FINAL_TURN_INSTRUCTION = (
    "Produce your answer for this round now, as the closing turn of the conversation.\n"
    "Output EXACTLY ONE JSON object — no markdown, no headings, no code fences, "
    "no commentary before or after it.\n"
    "The object must match this shape:\n"
    "{\n"
    '  "summary": "<your position in 2-4 sentences>",\n'
    '  "key_findings": ["<specific finding>", "..."],\n'
    '  "peer_critiques_and_rebuttals": ["<critique or rebuttal; empty in round 1>", "..."],\n'
    '  "remaining_risks": ["<risk or unresolved question>", "..."],\n'
    '  "confidence_score": <number from 0 to 100>\n'
    "}\n"
    "Use [] for empty lists. Never invent sources, measurements, or tool results."
)


class DebateTurnOutput(BaseModel):
    """Schema the local model is constrained to produce each round."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    key_findings: list[str] = []
    peer_critiques_and_rebuttals: list[str] = []
    remaining_risks: list[str] = []
    confidence_score: float = 0.0

    @field_validator("summary", mode="before")
    @classmethod
    def _summary_text(cls, value):  # noqa: ANN001 - pydantic before-validator
        return coerce_text(value)

    @field_validator("key_findings", "peer_critiques_and_rebuttals", "remaining_risks", mode="before")
    @classmethod
    def _string_lists(cls, value):  # noqa: ANN001 - pydantic before-validator
        return coerce_str_list(value)

    @field_validator("confidence_score", mode="before")
    @classmethod
    def _confidence(cls, value):  # noqa: ANN001 - pydantic before-validator
        return coerce_confidence(value)


# --------------------------------------------------------------------------- #
# Shell helpers
# --------------------------------------------------------------------------- #
def run_cmd(cmd: list[str], check: bool = True) -> str:
    """Run a command and return stripped stdout (raises on failure when check=True)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def git(*args: str, check: bool = True) -> str:
    return run_cmd(["git", *args], check=check)


# --------------------------------------------------------------------------- #
# Branch-based synchronization
# --------------------------------------------------------------------------- #
def state_branch(agent_id: str) -> str:
    return f"agent-state/{agent_id}"


def prepare_agent_branch(agent_id: str) -> None:
    """Create the agent's own state branch once, before round 1."""
    branch = state_branch(agent_id)
    git("checkout", "-B", branch)
    print(f"[{agent_id}] Working on isolated state branch: {branch}")


def push_round_to_agent_branch(agent_id: str, round_num: int, run_id: str) -> None:
    """Commit this round's output and force-push it to the agent's own branch."""
    branch = state_branch(agent_id)
    remote = os.environ.get("GIT_REMOTE", "origin")

    git("add", "-f", str(OUTPUT_DIR / f"round_{round_num}.json"), str(OUTPUT_DIR / "latest.json"))
    git("commit", "-m", f"debate({agent_id}): round {round_num} [run {run_id}]", check=False)
    git("push", remote, branch, "--force")

    print(f"[{agent_id}] Pushed Round {round_num} -> {remote}/{branch}")


def wait_for_peer_rounds(
    agent_id: str,
    all_agents: list[str],
    round_num: int,
    run_id: str,
    timeout_sec: int,
) -> list[dict]:
    """Git-based barrier: block until every peer publishes `round_num` for `run_id`."""
    remote = os.environ.get("GIT_REMOTE", "origin")
    peers = [peer for peer in all_agents if peer != agent_id]
    collected: dict[str, dict] = {}
    deadline = time.time() + timeout_sec

    print(f"[{agent_id}] Barrier: waiting for {peers} to publish Round {round_num} ...")
    while len(collected) < len(peers):
        for peer in peers:
            if peer in collected:
                continue

            git("fetch", remote, state_branch(peer), check=False)
            raw = git(
                "show",
                f"refs/remotes/{remote}/{state_branch(peer)}:output/round_{round_num}.json",
                check=False,
            )
            if not raw:
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if str(payload.get("run_id")) == str(run_id) and payload.get("round_num") == round_num:
                collected[peer] = payload
                print(f"[{agent_id}] Synchronized Round {round_num} from peer: {peer}")

        if len(collected) < len(peers):
            if time.time() > deadline:
                missing = sorted(set(peers) - set(collected))
                raise TimeoutError(
                    f"[{agent_id}] Timed out after {timeout_sec}s waiting for peers "
                    f"on round {round_num}: {missing}"
                )
            time.sleep(POLL_INTERVAL_SEC)

    return list(collected.values())


# --------------------------------------------------------------------------- #
# Structured generation
# --------------------------------------------------------------------------- #
def structured_turn(
    client: OpenAI,
    model_alias: str,
    messages: list[dict],
    temperature: float,
) -> DebateTurnOutput:
    """Request a schema-constrained turn, degrading at every step instead of raising.

    Prefers OpenAI-style ``response_format`` json_schema parsing, salvages the
    object out of wrapped output when the server ignores it, then falls back to
    plain JSON-object mode with the schema restated in the prompt. A round that
    returns prose still produces a usable turn instead of killing the run.
    """
    messages = [*messages, {"role": "user", "content": FINAL_TURN_INSTRUCTION}]

    try:
        response = client.beta.chat.completions.parse(
            model=model_alias,
            messages=messages,
            response_format=DebateTurnOutput,
            temperature=temperature,
        )
        parsed = response.choices[0].message.parsed
        if parsed is not None:
            return parsed
    except Exception as exc:  # noqa: BLE001 - degrade gracefully on any server-side refusal
        salvaged = parse_model_json(salvage_from_exception(exc), DebateTurnOutput)
        if salvaged is not None:
            print("[debate] Schema endpoint returned wrapped JSON; extracted the object it embedded.")
            return salvaged
        print(f"[debate] Structured parse unavailable ({type(exc).__name__}); using JSON mode.")

    try:
        response = client.chat.completions.create(
            model=model_alias,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - a bad request must not kill the debate
        print(f"[debate] JSON-mode request failed ({type(exc).__name__}: {exc}).")
        content = ""

    turn = parse_model_json(content, DebateTurnOutput)
    if turn is not None:
        return turn

    print("[debate] Model never produced a parseable JSON object; carrying its prose forward.")
    return DebateTurnOutput(summary=coerce_text(content)[:4000], confidence_score=0.0)


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #
def build_round_instructions(round_num: int, total_rounds: int, tools_enabled: bool) -> str:
    if round_num == 1:
        instructions = (
            "This is Round 1 (Initial Thesis). Formulate your independent analysis based on "
            "the Task Objective and the Retrieved Vector Memory. "
            "Leave peer_critiques_and_rebuttals empty."
        )
        if tools_enabled:
            instructions += (
                " Use your tools (`search_web` and/or `query_arxiv`) to ground your thesis in "
                "current external evidence before answering."
            )
        return instructions

    instructions = (
        f"This is Debate Round {round_num} of {total_rounds}. Critically examine your peers' "
        f"Round {round_num - 1} findings, which are provided below.\n"
        "1. Point out specific flaws, blind spots, or contradictions in peer proposals.\n"
        "2. Defend or revise your own prior position where peers raised valid counterarguments.\n"
        "3. Converge toward a battle-tested technical consensus."
    )
    if tools_enabled:
        instructions += (
            "\nUse your tools to verify any disputed technical claim (specs, limits, CVEs, "
            "benchmarks) rather than asserting it from memory."
        )
    return instructions


def build_system_prompt(persona: dict, round_instructions: str, tools_enabled: bool) -> str:
    base = persona.get("system_prompt") or (
        f"You are the {persona.get('role', 'Specialist')}. Specialty: {persona.get('focus', '')}"
    )
    available = [tool["function"]["name"] for tool in tool_definitions()] if tools_enabled else []
    tool_line = (
        f"\nAvailable tools: {', '.join(available)}. Call them whenever external evidence or "
        "exact arithmetic would improve your answer."
        if available
        else ""
    )
    return (
        f"{base}{tool_line}\n{round_instructions}\n"
        "Respond strictly in JSON matching the requested schema. /no_think"
    )


# --------------------------------------------------------------------------- #
# Main debate loop
# --------------------------------------------------------------------------- #
def main() -> int:
    agent_id = os.environ["AGENT_ID"]
    task_prompt = os.environ["TASK_PROMPT"]
    run_id = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))
    all_agents = [
        name.strip()
        for name in os.environ.get("ALL_AGENTS", ",".join(DEFAULT_AGENTS)).split(",")
        if name.strip()
    ]

    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    with open(SYSTEM_PROMPTS_PATH, "r", encoding="utf-8") as handle:
        personas = json.load(handle)

    if agent_id not in personas:
        raise KeyError(f"Agent '{agent_id}' is not defined in {SYSTEM_PROMPTS_PATH}")
    persona = personas[agent_id]

    # Restrict the tool surface to this persona's allowlist (process-local).
    persona_tools = persona.get("tools")
    if isinstance(persona_tools, list) and persona_tools and not os.environ.get("TOOL_ALLOWLIST"):
        os.environ["TOOL_ALLOWLIST"] = ",".join(persona_tools)

    tools_enabled = bool(config.get("enable_tools", True)) and bool(tool_definitions())
    max_tool_iterations = int(config.get("max_tool_iterations", 3))
    total_rounds = int(os.environ.get("ROUNDS_OVERRIDE") or config.get("debate_rounds", 2))
    peer_timeout_sec = int(config.get("peer_timeout_sec", DEFAULT_PEER_TIMEOUT_SEC))
    model_alias = config.get("model_alias", "pareto-local")
    model_used = f"{config.get('hf_repo', 'unknown')}/{config.get('hf_file', 'unknown')}"

    rag_context = RAG_CONTEXT_PATH.read_text(encoding="utf-8") if RAG_CONTEXT_PATH.exists() else "{}"

    client = OpenAI(
        base_url=os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080/v1"),
        api_key=os.environ.get("LLAMA_API_KEY", "llama-cpp"),
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    prepare_agent_branch(agent_id)

    print(
        f"[{agent_id}] tools_enabled={tools_enabled} "
        f"tool_budget={max_tool_iterations} rounds={total_rounds}"
    )

    own_previous: dict | None = None
    peer_findings: list[dict] = []
    tool_trace: list[dict] = []

    for round_num in range(1, total_rounds + 1):
        print(f"\n=== [{agent_id}] Debate Round {round_num}/{total_rounds} ===")

        round_instructions = build_round_instructions(round_num, total_rounds, tools_enabled)
        debate_context = json.dumps(
            {
                "your_previous_round": own_previous,
                "peer_previous_round_submissions": peer_findings,
            },
            indent=2,
        )

        messages = [
            {"role": "system", "content": build_system_prompt(persona, round_instructions, tools_enabled)},
            {
                "role": "user",
                "content": (
                    f"### Task Objective\n{task_prompt}\n\n"
                    f"### sqlite-vec Retrieved Historical Context\n{rag_context}\n\n"
                    f"### Multi-Agent Debate State\n{debate_context}"
                ),
            },
        ]

        round_trace_start = len(tool_trace)
        if tools_enabled:
            messages = run_tool_loop(
                client,
                model_alias,
                messages,
                agent_id,
                max_iterations=max_tool_iterations,
                temperature=0.2,
                trace=tool_trace,
            )

        turn = structured_turn(client, model_alias, messages, temperature=0.25)

        round_trace = tool_trace[round_trace_start:]
        payload = {
            "agent_id": agent_id,
            "round_num": round_num,
            "run_id": run_id,
            **task_identity(task_prompt),
            "model_used": model_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": turn.summary,
            "key_findings": turn.key_findings,
            "peer_critiques_and_rebuttals": turn.peer_critiques_and_rebuttals,
            "remaining_risks": turn.remaining_risks,
            "confidence_score": float(turn.confidence_score),
            "tools_used": sorted({entry["call"] for entry in round_trace}),
            "tool_names": sorted({entry["tool"] for entry in round_trace}),
            "tool_call_count": len(round_trace),
            "tool_trace": round_trace,
        }

        round_file = OUTPUT_DIR / f"round_{round_num}.json"
        round_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (OUTPUT_DIR / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (OUTPUT_DIR / f"tool_trace_round_{round_num}.json").write_text(
            json.dumps(round_trace, indent=2), encoding="utf-8"
        )

        push_round_to_agent_branch(agent_id, round_num, run_id)
        own_previous = payload

        if round_num < total_rounds:
            peer_findings = wait_for_peer_rounds(
                agent_id, all_agents, round_num, run_id, peer_timeout_sec
            )

    print(f"\n[{agent_id}] Completed {total_rounds} debate round(s) with {len(tool_trace)} tool call(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
