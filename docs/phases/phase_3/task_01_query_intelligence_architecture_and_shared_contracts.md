# Task 01: Query Intelligence Architecture & Shared Data Contracts – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 25, 2026  
**Environment**: Conda `ml-env` (Python 3.12, Pydantic v2, PostgreSQL 16+, Qdrant v1.8+)  
**Phase**: Phase 3 – Query Intelligence & Intent Router Gate  
**Subsystem**: [`backend/query`](file:///home/mercury/AgentGraph/backend/query)

---

## 1. Executive Summary & Objective

The objective of **Task 01** was to establish the architectural foundation, domain exceptions, dynamic configuration, and shared data contracts for the **Query Intelligence & Intent Routing Subsystem**. This gateway serves as the very first entry point in the Enterprise Knowledge Agent pipeline, inspecting incoming questions before any relational database queries, vector search, or reasoning graph execution takes place.

### Strict Scope Boundaries Adhered To
1. **Zero Premature Business Logic**: Decoupled type contracts from heavy concrete rewriters and classifiers, reserving runtime components for subsequent tasks.
2. **Zero Hardcoding Principle**: Implemented `QuerySettings` (`pydantic_settings.BaseSettings`) to resolve all operational timeouts, rolling windows, and provider credentials dynamically from `.env` with sensible fallbacks.
3. **Immutability Invariants**: Enforced strict immutability (`frozen=True`) on core data structures (`ClassifiedQuery`, `UserQueryContext`) and validated that confidence scores are strictly bounded within $[0.0, 1.0]$.
4. **Three-Tier Security Integration**: Established `UserQueryContext` carrying tenant isolation markers, user identifiers, and RBAC roles to enforce security across retrieval layers.

---

## 2. Directory Structure & Subsystem Organization

```
backend/query/
├── __init__.py                               # Package facade exporting types, exceptions, and router
├── config.py                                 # QuerySettings dynamic Pydantic configuration
├── exceptions.py                             # Typed domain exception hierarchy
├── types/                                    # Pydantic v2 schemas and taxonomy models
│   ├── __init__.py                           # Export index for shared contracts
│   ├── intent.py                             # IntentLabel enum (5 canonical intents)
│   ├── context.py                            # UserQueryContext zero-trust boundary
│   └── classification.py                     # ClassifiedQuery immutable output model
```

---

## 3. Core Models & Architectural Contracts

### 3.1. Five-Class Intent Taxonomy (`IntentLabel`)
Located in [`backend/query/types/intent.py`](file:///home/mercury/AgentGraph/backend/query/types/intent.py):
- `FACTUAL_RETRIEVAL`: Queries seeking internal policies, regulations, or employee guidelines (`requires_rag=True`, `needs_multi_hop=False`).
- `MULTI_DOC_COMPARISON`: Comparative queries spanning multiple departments or policies (`requires_rag=True`, `needs_multi_hop=True`).
- `TABLE_LOOKUP`: Lookup targeting spreadsheet matrices, salary scales, or tabular coordinates (`requires_rag=True`, `needs_multi_hop=False`).
- `GENERAL_CHAT`: Polite conversational greetings, small talk, or system identity inquiries (`requires_rag=False`, `needs_multi_hop=False`).
- `OUT_OF_DOMAIN`: Queries outside enterprise domain routed to external web search (`requires_rag=False`, `triggers_web_search=True`).

### 3.2. Immutable Output Model (`ClassifiedQuery`)
Located in [`backend/query/types/classification.py`](file:///home/mercury/AgentGraph/backend/query/types/classification.py):
```python
class ClassifiedQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw_query: str
    rewritten_query: str
    intent: IntentLabel
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str | None = None
    conversation_id: str | None = None
    turn_id: str | None = None
    latency_ms: float = 0.0
    requires_rag: bool = False
    needs_multi_hop: bool = False
    target_departments: list[str] = Field(default_factory=list)
```

### 3.3. Zero-Trust User Context (`UserQueryContext`)
Located in [`backend/query/types/context.py`](file:///home/mercury/AgentGraph/backend/query/types/context.py):
Carries `tenant_id`, `user_id`, `roles`, `departments`, and `locale` to ensure zero cross-tenant contamination.

### 3.4. Dynamic Configuration (`QuerySettings`)
Located in [`backend/query/config.py`](file:///home/mercury/AgentGraph/backend/query/config.py):
- `REDIS_URL`: Multi-turn session memory connection string.
- `CONVERSATION_TTL_SECONDS`: Session TTL (default: 86400s / 24h).
- `MAX_HISTORY_TURNS`: Rolling window retention (default: 10).
- `REWRITER_TIMEOUT_SECONDS`: Query rewriting latency budget (default: 2.0s).
- `INTENT_FAST_PATH_CONFIDENCE_THRESHOLD`: Heuristic threshold (default: 0.98).
- `INTENT_LOW_CONFIDENCE_THRESHOLD`: Fallback trigger threshold (default: 0.60).
- `ROUTER_TIMEOUT_SECONDS`: End-to-end routing latency budget (default: 2.0s).

---

## 4. Verification & Unit Testing

Validated via [`tests/query/test_query_architecture_contracts.py`](file:///home/mercury/AgentGraph/tests/query/test_query_architecture_contracts.py):
- `test_intent_label_attributes`: Verifies RAG, multi-hop, and web search flags across all 5 classes.
- `test_user_query_context`: Verifies zero-trust boundary fields and immutability invariants.
- `test_classified_query_validation_and_immutability`: Asserts confidence bounds ($\in [0.0, 1.0]$) and rejects mutations.
- `test_typed_exceptions`: Verifies structured error attributes and formatting.
- `test_query_settings_dynamic_defaults`: Verifies settings resolution from environment variables.

**Test Result**: 5/5 tests passing cleanly in 0.09s.
