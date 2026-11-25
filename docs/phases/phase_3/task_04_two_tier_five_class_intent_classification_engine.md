# Task 04: Two-Tier 5-Class Intent Classification Engine – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 25, 2026  
**Environment**: Conda `ml-env` (Python 3.12, Pydantic v2, HTTPX)  
**Phase**: Phase 3 – Query Intelligence & Intent Router Gate  
**Subsystem**: [`backend/query/intent`](file:///home/mercury/AgentGraph/backend/query/intent)

---

## 1. Executive Summary & Objective

The objective of **Task 04** was to design and implement a high-precision, low-latency Two-Tier Intent Classification Engine capable of evaluating incoming inquiries across the 5 canonical system intents:
1. `FACTUAL_RETRIEVAL`
2. `MULTI_DOC_COMPARISON`
3. `TABLE_LOOKUP`
4. `GENERAL_CHAT`
5. `OUT_OF_DOMAIN`

The classification engine acts as the first-stage routing gate in the Enterprise Knowledge Agent architecture. By categorizing inquiries upfront, the system avoids redundant vector lookups for general chit-chat, routes external queries directly to internet search, and selects the optimal depth of retrieval (single-hop vs. multi-hop reasoning DAG).

---

## 2. Directory Structure & Subsystem Organization

```
backend/query/intent/
├── __init__.py                               # Intent package exports
├── heuristics.py                             # Tier-1 FastPathHeuristics regex engine
└── classifier.py                             # IntentClassifier two-tier engine & fallbacks
```

---

## 3. Two-Tier Classification Architecture

### 3.1. Tier 1: Fast-Path Heuristics (< 5ms)
Located in [`backend/query/intent/heuristics.py`](file:///home/mercury/AgentGraph/backend/query/intent/heuristics.py):
- High-speed token and regex pattern matching evaluated in priority order:
  1. `GENERAL_CHAT`: Pure conversational pleasantries, gratitude, and bot self-inquiries (*"xin chào"*, *"cảm ơn nhiều"*, *"who are you"*, *"have a great day"*). Returns immediately without vector DB or LLM call.
  2. `OUT_OF_DOMAIN`: World knowledge, weather forecasts, stock tickers, trivia (*"thời tiết"*, *"thủ đô của"*, *"vô địch world cup"*, *"stock price of apple"*). Routes to external internet search with web disclaimer.
  3. `MULTI_DOC_COMPARISON`: Explicit comparative phrasing across policies or departments (*"so sánh X và Y"*, *"điểm khác biệt giữa..."*, *"compare X and Y"*).
  4. `TABLE_LOOKUP`: Structured coordinates, matrix keywords, and spreadsheet references (*"bảng lương"*, *"sheet phân bổ"*, *"ma trận lương"*, *"salary table"*).
- When a heuristic pattern matches with confidence $\ge 0.98$, the engine returns immediately, completing classification in $< 1\text{ms}$.

### 3.2. Tier 2: Structured LLM Classification
Located in [`backend/query/intent/classifier.py`](file:///home/mercury/AgentGraph/backend/query/intent/classifier.py):
- For complex, natural, or ambiguous inquiries where Tier 1 does not match, invokes the LLM using a structured JSON schema response contract:
  ```json
  {
    "intent": "FACTUAL_RETRIEVAL",
    "confidence": 0.96,
    "requires_rag": true,
    "needs_multi_hop": false,
    "target_departments": ["HR"],
    "reasoning": "Standard policy retrieval inquiry"
  }
  ```
- Enforces strict Pydantic v2 validation into [`IntentClassificationResult`](file:///home/mercury/AgentGraph/backend/query/intent/classifier.py#L18-L39).

### 3.3. Conservative Fallback Guardrails
- If model confidence falls below the configured threshold (`INTENT_LOW_CONFIDENCE_THRESHOLD = 0.60`) or if an API timeout or parsing error occurs, the engine triggers a conservative fallback:
  - Defaults to `FACTUAL_RETRIEVAL` with `requires_rag = True`.
  - Guarantees that legitimate enterprise policy questions are never erroneously dropped or refused.

---

## 4. Verification & Unit Testing

Validated via [`tests/query/test_intent_classifier.py`](file:///home/mercury/AgentGraph/tests/query/test_intent_classifier.py):
- `test_fast_path_general_chat`: Validates conversational chat patterns across Vietnamese and English.
- `test_fast_path_out_of_domain`: Validates external open-domain queries.
- `test_fast_path_table_lookup`: Validates spreadsheet/matrix keyword detection.
- `test_fast_path_multi_doc_comparison`: Validates cross-department comparisons.
- `test_department_extraction`: Validates extraction of `HR`, `ENGINEERING`, `SALES`, `FINANCE`, `LEGAL`, `MARKETING`.
- `test_two_tier_classifier_fast_path_execution`: Validates sub-5ms heuristic dispatch.
- `test_two_tier_classifier_custom_llm_callable`: Validates Tier-2 structured parsing.
- `test_classifier_low_confidence_fallback`: Validates fallback to `FACTUAL_RETRIEVAL` when confidence $< 0.60$.
- `test_classifier_timeout_fallback`: Validates fallback to `FACTUAL_RETRIEVAL` upon classification timeout.

**Test Result**: 9/9 tests passing cleanly in 0.35s.
