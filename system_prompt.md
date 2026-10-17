<!--
================================================================================
  HOW TO USE THIS FILE
================================================================================

  This is the swarm's TASK. Every scheduled run (every 3 hours) and every manual
  "Run workflow" that leaves the task_prompt input empty reads this file and
  sends it to all three agents as the debate objective.

  To change what the swarm works on:
    1. Edit the "Mission" section below (and, optionally, Scope / Success
       criteria) directly on GitHub or locally.
    2. Commit to `main`.
    3. That's it. The next scheduled run picks it up.

  Anything inside an HTML comment like this one is STRIPPED before the agents
  see it, so notes to yourself never pollute the prompt. Only the visible
  Markdown is sent.

  Per-agent personas and their tool allowlists live in agents/system_prompts.json.
  Debate rounds, model, RAG depth and tool budget live in the Model_Config repo
  (model_config.json).

  Keep this file focused: one mission, explicit constraints, a definition of
  done. Vague missions produce vague debates.
================================================================================
-->

# Mission

Design and specify a production-grade, incrementally deployable architecture for
a **multi-region, edge-deployed API platform** that stays correct under partial
failures. The deliverable is a concrete blueprint the team can implement this
quarter — not a survey of options.

# Scope

In scope:

- Global request routing, failover, and health checking across at least three regions.
- Data placement and consistency for a write-heavy, per-tenant workload.
- Rate limiting, quota enforcement, and abuse containment when regions are partitioned.
- Observability: what must be measured to detect a degraded region before users do.
- Deployment: canary rollout, automatic rollback, and schema/version evolution.

Out of scope: marketing, pricing, team structure, anything requiring unknown
third-party vendors.

# Constraints

- 16 GB RAM, 4 vCPU runners; no GPU. Assume the same resource envelope for the
  service's edge tier.
- Must tolerate one full region loss and one degraded (high-latency) region
  simultaneously, with no data loss and bounded error budget burn.
- Prefer boring, widely deployed mechanisms over novel components.
- Every quantitative claim must be either computed exactly or grounded in a
  cited, current source.

# Success criteria

- The synthesis names a single recommended design with explicit trade-offs.
- Each remaining risk has a concrete mitigation and an owner-agnostic
  detection signal.
- Capacity, cost, and latency numbers are computed, not estimated.
- Open questions are listed explicitly rather than answered speculatively.
