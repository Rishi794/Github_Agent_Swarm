"""Multi-round debate engine.

Each agent (GitHub Actions matrix leg) runs this script. It talks to a **single**
locally hosted ``llama-server`` instance for the whole debate, and between rounds
it synchronizes with peers through isolated Git branches (``agent-state/<agent>``)
so that no two runners ever push to the same ref.

Round r protocol
----------------
1. Build the prompt (task + sqlite-vec memory + own previous turn + peer turns).
2. Generate one structured JSON turn from the local model.
3. Commit ``output/round_r.json`` to ``agent-state/<agent_id>`` and force-push.
4. Poll ``origin/agent-state/<peer>`` until every peer has published the same
   ``run_id`` for round r (Git-based barrier), then continue to round r+1.

Environment variables
---------------------
AGENT_ID           Agent role, must be a key of agents/prompts.json (required)
TASK_PROMPT        Debate topic (required)
GITHUB_RUN_ID      Run identifier used to reject stale peer state (required)
ALL_AGENTS         Comma separated roster (default: researcher,security_auditor,synthesizer)
CONFIG_PATH        Path to model_config.json (default: external_config/model_config.json)
PROMPTS_PATH       Path to prompts.json (default: agents/prompts.json)
RAG_CONTEXT_PATH   Retrieved vector memory written by rag_query.py (default: /tmp/rag_context.json)
LLAMA_BASE_URL     OpenAI-compatible endpoint of the local server (default: http://127.0.0.1:8080/v1)
LLAMA_API_KEY      Dummy API key for llama-server (default: llama-cpp)
ROUNDS_OVERRIDE    Optional integer overriding debate_rounds from the config
GIT_REMOTE         Git remote name (default: origin)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel, ConfigDict

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "external_config/model_config.json"))
PROMPTS_PATH = Path(os.environ.get("PROMPTS_PATH", "agents/prompts.json"))
RAG_CONTEXT_PATH = Path(os.environ.get("RAG_CONTEXT_PATH", "/tmp/rag_context.json"))
OUTPUT_DIR = Path("output")

DEFAULT_AGENTS = ["researcher", "security_auditor", "synthesizer"]
DEFAULT_PEER_TIMEOUT_SEC = 1800
POLL_INTERVAL_SEC = 6


class DebateTurnOutput(BaseModel):
    """Schema the local model is constrained to produce each round."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    key_findings: list[str] = []
    peer_critiques_and_rebuttals: list[str] = []
    remaining_risks: list[str] = []
    confidence_score: float = 0.0


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
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Best-effort extraction of a JSON object from free-form model output."""
    fenced = _FENCE_RE.search(text)
    if fenced:
        return fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def structured_turn(
    client: OpenAI,
    model_alias: str,
    messages: list[dict],
    temperature: float,
) -> DebateTurnOutput:
    """Request a schema-constrained turn.

    Prefers OpenAI-style ``response_format`` json_schema parsing, and transparently
    falls back to plain JSON-object mode for llama.cpp builds that do not accept
    the strict structured-output extension.
    """
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
        print(f"[debate] Structured parse unavailable ({type(exc).__name__}: {exc}); using JSON mode.")

    response = client.chat.completions.create(
        model=model_alias,
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    return DebateTurnOutput.model_validate_json(_extract_json(content))


# --------------------------------------------------------------------------- #
# Main debate loop
# --------------------------------------------------------------------------- #
def build_round_prompt(
    round_num: int,
    total_rounds: int,
    own_previous: dict | None,
    peer_findings: list[dict],
) -> str:
    if round_num == 1:
        return (
            "This is Round 1 (Initial Thesis). Formulate your independent analysis based on "
            "the Task Objective and the Retrieved Vector Memory. "
            "Leave peer_critiques_and_rebuttals empty."
        )

    return (
        f"This is Debate Round {round_num} of {total_rounds}. Critically examine your peers' "
        f"Round {round_num - 1} findings, which are provided below.\n"
        "1. Point out specific flaws, blind spots, or contradictions in peer proposals.\n"
        "2. Defend or revise your own prior position where peers raised valid counterarguments.\n"
        "3. Converge toward a battle-tested technical consensus."
    )


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
    with open(PROMPTS_PATH, "r", encoding="utf-8") as handle:
        personas = json.load(handle)

    if agent_id not in personas:
        raise KeyError(f"Agent '{agent_id}' is not defined in {PROMPTS_PATH}")
    persona = personas[agent_id]

    total_rounds = int(
        os.environ.get("ROUNDS_OVERRIDE") or config.get("debate_rounds", 2)
    )
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

    own_previous: dict | None = None
    peer_findings: list[dict] = []

    for round_num in range(1, total_rounds + 1):
        print(f"\n=== [{agent_id}] Debate Round {round_num}/{total_rounds} ===")

        round_instructions = build_round_prompt(round_num, total_rounds, own_previous, peer_findings)
        debate_context = json.dumps(
            {
                "your_previous_round": own_previous,
                "peer_previous_round_submissions": peer_findings,
            },
            indent=2,
        )

        system_msg = (
            f"You are the {persona['role']}. Specialty: {persona['focus']}\n"
            f"{round_instructions}\n"
            "Respond strictly in JSON matching the requested schema. /no_think"
        )
        user_msg = (
            f"### Task Objective\n{task_prompt}\n\n"
            f"### sqlite-vec Retrieved Historical Context\n{rag_context}\n\n"
            f"### Multi-Agent Debate State\n{debate_context}"
        )

        turn = structured_turn(
            client,
            model_alias,
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.25,
        )

        payload = {
            "agent_id": agent_id,
            "round_num": round_num,
            "run_id": run_id,
            "task": task_prompt,
            "model_used": model_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": turn.summary,
            "key_findings": turn.key_findings,
            "peer_critiques_and_rebuttals": turn.peer_critiques_and_rebuttals,
            "remaining_risks": turn.remaining_risks,
            "confidence_score": float(turn.confidence_score),
        }

        round_file = OUTPUT_DIR / f"round_{round_num}.json"
        round_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (OUTPUT_DIR / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

        push_round_to_agent_branch(agent_id, round_num, run_id)
        own_previous = payload

        if round_num < total_rounds:
            peer_findings = wait_for_peer_rounds(
                agent_id, all_agents, round_num, run_id, peer_timeout_sec
            )

    print(f"\n[{agent_id}] Completed {total_rounds} debate round(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
