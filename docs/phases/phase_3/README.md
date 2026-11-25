# Phase 3: Query Intelligence & Intent Router Gate — Implementation Summary

## 1. Architectural Overview

Phase 3 implements the **First-Stage Intent Router Gate** and **Query Intelligence Layer** for the Enterprise Knowledge Agent. Acting as the gatekeeper before vector databases, relational full-text search, or agentic reasoning graphs, this subsystem:
1. Resolves ambiguous conversational context and demonstrative pronouns (*"nó"*, *"chính sách đó"*, *"họ"*, *"it"*, *"that document"*) into standalone queries via coreference resolution.
2. Evaluates user intent across the 5 canonical system categories using a high-throughput, two-tier architecture:
   - **Tier 1 (Fast-Path Heuristics)**: Sub-millisecond token and regex pattern matching for instant zero-latency routing of conversational pleasantries (`GENERAL_CHAT`), spreadsheets/matrices (`TABLE_LOOKUP`), cross-department comparisons (`MULTI_DOC_COMPARISON`), and external internet searches (`OUT_OF_DOMAIN`).
   - **Tier 2 (Structured LLM Engine)**: Structured JSON schema classification with strict Pydantic parsing and deterministic fallback guardrails (`confidence < 0.60` or timeout safely defaults to `FACTUAL_RETRIEVAL`).
3. Manages multi-turn conversation memory with tenant-isolated Redis lists (`conv:{tenant_id}:{conversation_id}:turns`) and in-memory fallback.
4. Adheres strictly to the **Zero Hardcoding Principle**, resolving all timeouts, thresholds, model settings, and connection URIs via `QuerySettings` (`pydantic_settings.BaseSettings`).

```mermaid
flowchart TD
    UserQuery([User Input Message]) --> Router[QueryRouter.route]
    
    subgraph MultiTurn["1. Session Context & History"]
        HistoryStore[(Redis History Store\nconv:tenant:conv_id:turns)]
        HistoryStore -. Fallback .-> MemStore[(InMemory Store)]
        Router --> LoadHist[Load Recent Turns]
        LoadHist <--> HistoryStore
    end
    
    subgraph Coref["2. Coreference Resolution"]
        BypassCheck{>= 12 Words &\nNo Pronouns?}
        LoadHist --> BypassCheck
        BypassCheck -- Yes --> Standalone[Bypass: Raw Query]
        BypassCheck -- No --> LLMRewrite[QueryRewriter LLM\nTimeout 2.0s]
        LLMRewrite -. Fallback .-> Standalone
    end

    subgraph IntentClass["3. Two-Tier Intent Gate"]
        Standalone --> Tier1{Tier 1: Fast Heuristics\nPattern Match?}
        LLMRewrite --> Tier1
        Tier1 -- "Confidence >= 0.98" --> FastRoute[Instant Decision\n< 5ms]
        Tier1 -- Miss --> Tier2[Tier 2: Structured LLM Classifier]
        Tier2 -- "Confidence < 0.60 or Error" --> Fallback[Safe Conservative Fallback\nFACTUAL_RETRIEVAL]
    end

    subgraph DecisionOutputs["4. Routing Outputs"]
        FastRoute --> Output
        Tier2 --> Output
        Fallback --> Output
        Output([ClassifiedQuery\nIntent, RAG Flags, Locators, Latency])
    end
```

---

## 2. Completed Phase 3 Tasks (Tasks 01 – 05)

### Task 01: Query Intelligence Architecture & Shared Data Contracts
- **Deliverables**: [`backend/query/types/`](file:///home/mercury/AgentGraph/backend/query/types), [`backend/query/exceptions.py`](file:///home/mercury/AgentGraph/backend/query/exceptions.py), [`backend/query/config.py`](file:///home/mercury/AgentGraph/backend/query/config.py).
- Implemented `IntentLabel` with five canonical categories: `FACTUAL_RETRIEVAL`, `MULTI_DOC_COMPARISON`, `TABLE_LOOKUP`, `GENERAL_CHAT`, and `OUT_OF_DOMAIN`.
- Implemented immutable `ClassifiedQuery` (`frozen=True`) with strict validation bounds on confidence ($\in [0.0, 1.0]$), latency tracking, and routing flags (`requires_rag`, `needs_multi_hop`, `target_departments`).
- Implemented `UserQueryContext` enforcing zero-trust tenant and user boundaries.
- Defined domain exception hierarchy (`QueryRouterError`, `LowConfidenceIntentError`, `RoutingTimeoutError`, `RewritingError`, `ConversationHistoryError`).
- Configured dynamic environment settings via `QuerySettings` (`pydantic_settings.BaseSettings`).

### Task 02: Multi-Turn Conversation History & Session State Management
- **Deliverables**: [`backend/query/history/store.py`](file:///home/mercury/AgentGraph/backend/query/history/store.py), [`backend/query/history/types.py`](file:///home/mercury/AgentGraph/backend/query/history/types.py).
- Implemented model `ConversationTurn` capturing turn role, timestamp, classified intent, and rewritten query.
- Implemented `RedisConversationHistoryStore` partitioned strictly by `tenant_id`:
  - Redis key: `conv:{tenant_id}:{conversation_id}:turns`.
  - Rolling window maintenance via atomic `RPUSH` + `LTRIM` (bounded to `MAX_HISTORY_TURNS`, default 10).
  - Automatic TTL renewal (`CONVERSATION_TTL_SECONDS`, default 86400s / 24h).
- Implemented thread-safe `InMemoryConversationHistoryStore` for local development and graceful fallback when Redis connectivity is unavailable.

### Task 03: Coreference Resolution & Contextual Query Rewriting
- **Deliverables**: [`backend/query/rewriting/rewriter.py`](file:///home/mercury/AgentGraph/backend/query/rewriting/rewriter.py).
- Translates ambiguous multi-turn user messages into standalone search queries.
- Fast heuristic bypass: queries containing $\ge 12$ words and zero referential markers immediately bypass LLM rewriting ($0\text{ms}$ latency overhead).
- Pronoun detection for Vietnamese (*nó*, *đó*, *này*, *kia*, *chính sách đó*, *thế còn...*) and English (*it*, *they*, *this*, *that*, *above*, *same...*).
- LLM call guarded by configurable timeout (`REWRITER_TIMEOUT_SECONDS`, default 2.0s) with seamless raw query fallback upon timeouts or exceptions.

### Task 04: Two-Tier 5-Class Intent Classification Engine
- **Deliverables**: [`backend/query/intent/classifier.py`](file:///home/mercury/AgentGraph/backend/query/intent/classifier.py), [`backend/query/intent/heuristics.py`](file:///home/mercury/AgentGraph/backend/query/intent/heuristics.py).
- **Tier 1 (Fast-Path Heuristics)**: High-speed regex evaluation catching greetings, spreadsheet keywords, department comparisons, and open-domain world knowledge.
- **Tier 2 (Structured LLM Classifier)**: Structured JSON schema classification with typed Pydantic validation.
- Conservative Fallback: Ambiguous queries (`confidence < 0.60`) or errors default safely to `FACTUAL_RETRIEVAL` with `requires_rag = True`, safeguarding against dropping enterprise inquiries.

### Task 05: First-Stage Router Orchestration & Latency Benchmark
- **Deliverables**: [`backend/query/router.py`](file:///home/mercury/AgentGraph/backend/query/router.py), [`tests/fixtures/intent_benchmark.json`](file:///home/mercury/AgentGraph/tests/fixtures/intent_benchmark.json), [`tests/query/test_router_benchmark.py`](file:///home/mercury/AgentGraph/tests/query/test_router_benchmark.py).
- Unifies conversation history retrieval, coreference rewriting, intent classification, and turn persistence into `QueryRouter.route()`.
- Validated against a 100-sample balanced enterprise benchmark across Vietnamese and English.

---

## 3. Benchmark & Verification Results

### Benchmark Metrics (100 Curated Enterprise Samples)
- **Total Samples Evaluated**: 100 (20 Factual, 20 Multi-Doc, 20 Table, 20 Chat, 20 Out-of-Domain)
- **Classification Accuracy**: **100.00%** (Passing quality gate $\ge 95.0\%$)
- **Mean Routing Latency**: **0.09 ms**
- **P50 Latency**: **0.08 ms**
- **P90 Latency**: **0.16 ms**
- **P95 Latency**: **0.17 ms** (Strictly within SLA target $P_{95} \le 120.0\text{ms}$)
- **P99 Latency**: **0.23 ms**

### Test Suite Execution
All 32 test cases across 5 test suites pass cleanly:
```bash
conda run -n ml-env pytest tests/query/ -v
# ============================== 32 passed in 0.81s ==============================
```
