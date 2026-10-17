# Multi-Agent Debate Swarm on GitHub Actions

A swarm of specialized LLM agents debates a problem across **multiple rounds**,
remembers past runs through a **persistent vector knowledge base**, and writes
everything back into Git — all on a stock 16 GB `ubuntu-latest` runner running a
quantized GGUF model on CPU.

Two repositories, deliberately separated:

| Repository | Role | Contents |
| --- | --- | --- |
| **`Rishi794/Model_Config`** (this is the *control plane*) | Model configuration | `model_config.json` — model, quantization, `llama-server` CPU flags, debate rounds, RAG depth |
| **`Rishi794/Github_Agent_Swarm`** (this repo, *execution plane*) | Code + state | Workflows, agents, and the Git branches that carry agent state |

Because the model config lives outside the code, you can swap a model, quant, or
tune a flag by editing one JSON file — no code change, no redeploy.

---

## 1. Architecture

```
                        ┌──────────────────────────────┐
                        │  Rishi794/Model_Config       │
                        │  model_config.json           │   <- hot-swappable model/flags/rounds
                        └───────────────┬──────────────┘
                                        │ actions/checkout (path: external_config/)
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ JOB 1  debate-agents  (matrix: researcher | security_auditor | synthesizer)    │
│                                                                               │
│  rag_query.py  ──►  /tmp/rag_context.json   (embeddings, then process exits)   │
│        │                                                                      │
│        ▼                                                                      │
│  llama-server  (loaded ONCE, -np 1, q8_0 KV, -lm mmap)                        │
│        │                                                                      │
│        ▼                                                                      │
│  debate_runner.py:  round 1 ──► push ──► barrier ──► round 2 ──► push ──► …    │
│        │                    │                        │                        │
│        └────────────────────┴────────────────────────┘                        │
│                     git push → agent-state/<agent_id>  (one writer per ref)    │
└───────────────────────────────────┬───────────────────────────────────────────┘
                                    │ needs:
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ JOB 2  consolidate-sqlite-storage   (single writer, concurrency-guarded)       │
│                                                                               │
│  fetch all agent-state/* branches → embed findings → knowledge_base.db        │
│      (SQLite + sqlite-vec vec0 table) → WAL checkpoint → VACUUM → commit       │
│                                                                               │
│  push → central-storage  (orphan branch: knowledge_base.db + README.md)        │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Branches in this repository

| Branch | Writer | Contents |
| --- | --- | --- |
| `main` | you | Workflows and Python orchestration only. Never receives automated data commits. |
| `agent-state/researcher` | researcher runner only | `output/round_<r>.json`, `output/latest.json` |
| `agent-state/security_auditor` | security auditor runner only | same |
| `agent-state/synthesizer` | synthesizer runner only | same |
| `central-storage` | consolidator job only | `knowledge_base.db` (sqlite-vec), `README.md` dashboard |

One writer per ref means parallel runners can never produce `non-fast-forward`
push collisions. Peers read each other with `git fetch` + `git show`, and every
payload carries a `run_id` so stale state from a previous run is ignored.

Read the live state from anywhere:

```
https://raw.githubusercontent.com/Rishi794/Github_Agent_Swarm/central-storage/README.md
```

---

## 2. Workflows

### `multi_agent_debate.yml` — multi-round debate with vector memory (primary)

| Stage | What happens |
| --- | --- |
| Swap + disk cleanup | Adds a 4 GB swapfile and deletes preinstalled SDKs to guarantee headroom. |
| RAG | `agents/rag_query.py` embeds the task with `fastembed` and KNN-searches `knowledge_base.db`. The process **exits before** `llama-server` starts, so embedding and LLM weights never share RAM. |
| Model | Downloads a prebuilt `llama-server` (Ubuntu x64 CPU build) plus the GGUF from the config repo, cached by `actions/cache`. |
| Debate | One `llama-server` per agent serves **all** rounds. Each round: generate → push to `agent-state/<agent>` → wait for peers → next round. |
| Fan-in | `agents/consolidate_sqlite.py` embeds every round, appends to `knowledge_base.db`, checkpoints WAL, VACUUMs, commits to `central-storage`. |

### `multi_agent_swarm_ollama.yml` — single-pass swarm (lightweight alternative)

Same branch topology, one inference per agent against a locally served Ollama
model, results merged into `knowledge_base.json` + `SWARM_REPORT.md` on
`central-storage`. Useful for quick runs; the debate workflow is the one that
produces cross-critiqued, vector-indexed knowledge.

---

## 3. Running it

1. Edit `model_config.json` in `Rishi794/Model_Config` if you want a different model or tune flags.
2. **Actions → Multi-Agent Debate Swarm (llama.cpp + sqlite-vec) → Run workflow**
   - `task_prompt` — the problem statement.
   - `config_repo` — defaults to `Rishi794/Model_Config`.
   - `config_ref` — branch/tag of the config repo (defaults to `main`).
   - `rounds_override` — optional; overrides `debate_rounds` for a single run.
3. Watch `agent-state/*` branches fill up round by round, then inspect
   `central-storage` for the database and the Markdown transcript.

### Secrets

`Rishi794/Model_Config` is public, so **no secret is required** — the workflow uses
the default `GITHUB_TOKEN`.

If you fork the config repo as **private**, create a fine-grained PAT with read-only
`Contents` permission and store it here as `CONFIG_REPO_TOKEN`; the workflow already
prefers it when present (`${{ secrets.CONFIG_REPO_TOKEN || github.token }}`).

`permissions: contents: write` is required so the matrix jobs can push their state branches.

---

## 4. Staying under 16 GB (five layers)

| Layer | Implementation |
| --- | --- |
| 1. Strict process isolation | Retrieval runs as a short-lived process that exits before the LLM server boots. |
| 2. Single slot | `-np 1` — removes the ~4x KV-cache multiplication of the default multi-slot server. |
| 3. 8-bit KV cache | `-ctk q8_0 -ctv q8_0` — halves cache memory vs `f16`. |
| 4. mmap, never mlock | `-lm mmap` — the kernel can evict weight pages instead of pinning them. |
| 5. One model load per run | Rounds loop inside the job behind a Git barrier instead of spawning a job per round. |

If a run still dies with exit code 137, shrink `ctx_size`, drop to a smaller quant
(`Q4_K_M` → `Q3_K_M`) or a smaller model in `model_config.json`.

---

## 5. Repository layout

```
.
├── .github/workflows/
│   ├── multi_agent_debate.yml         # fan-out debate + sqlite-vec fan-in  (primary)
│   └── multi_agent_swarm_ollama.yml   # single-pass Ollama swarm            (alternative)
├── agents/
│   ├── prompts.json                   # agent personas (keys = matrix agent ids)
│   ├── rag_query.py                   # pre-LLM sqlite-vec KNN retrieval
│   ├── debate_runner.py               # multi-round loop + Git barriers
│   ├── consolidate_sqlite.py          # fan-in: embed rounds into knowledge_base.db
│   ├── run_agent_ollama.py            # single-pass inference (Ollama workflow)
│   └── consolidate_json.py            # fan-in: append to knowledge_base.json
└── requirements.txt
```

### Adding an agent

Add a key to `agents/prompts.json` **and** to the `matrix.agent_id` list in both
workflows. Everything else (branch names, collection, consolidation) derives from
that list at runtime.

---

## 6. Local dry run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TASK_PROMPT="Architect a Byzantine-fault-tolerant distributed rate limiter."
export AGENT_ID=researcher
export GITHUB_RUN_ID=local-1

# 1) retrieval (safe no-op without a knowledge base)
CONFIG_PATH=external_config/model_config.json python agents/rag_query.py

# 2) debate (needs llama-server on 127.0.0.1:8080)
python agents/debate_runner.py

# 3) consolidation
CONFIG_PATH=external_config/model_config.json \
INCOMING_DIR=incoming_findings \
KB_DB_PATH=knowledge_base.db \
REPORT_PATH=README.md \
python agents/consolidate_sqlite.py
```

Every script reads its paths from environment variables, so the same code runs
unchanged in CI and locally. Inspect the resulting vector store with:

```sql
SELECT m.agent_id, m.round_num, m.summary, v.distance
FROM vec_findings v JOIN findings_metadata m ON m.id = v.finding_id
WHERE v.embedding MATCH :query_vector AND k = 4
ORDER BY v.distance ASC;
```

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Exit code 137 | Runner RAM exhausted | Lower `ctx_size`, use a smaller quant/model, keep `-np 1`. |
| `No ... asset found` during model download | Pinned `llama_cpp_release` has no Linux CPU build | Set `llama_cpp_release` back to `"auto"`. |
| `Timed out waiting for peers` | A peer agent failed or is very slow | Raise `peer_timeout_sec`; check the failing matrix leg. |
| `llama-server did not become healthy` | Bad flag combination in `llama_server_flags` | Inspect the printed server log tail; verify the flag exists in your pinned build. |
| Vector search returns nothing | No `knowledge_base.db` yet, or `embedding_dim` changed | Run the debate once to populate it; keep `embedding_dim` in sync with `embedding_model`. |
| Cache restore is slow / misses | Cache key includes the GGUF filename and `llama_cpp_release` | Expected: a new model or pinned build produces a new key. |
