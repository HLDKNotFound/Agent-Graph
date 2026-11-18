---
name: rag-evaluator
description: >-
  Executes, benchmarks, and analyzes retrieval and generation quality for the Enterprise Knowledge Agent
  against the 300-sample gold standard dataset. Use this skill when running ablation studies (E1 to E8),
  evaluating Recall@K, MRR@10, nDCG@10, Faithfulness, Citation Accuracy, or validating CI/CD evaluation gates.
---

# Enterprise RAG Evaluator & Benchmark Runner

This skill provides step-by-step procedures to run quantitative evaluation benchmarks, calculate retrieval and generation metrics, execute ablation experiments (E1 to E8), and verify performance against target SLAs without hardcoded values.

---

## 1. Evaluation Target Metrics & Production SLAs

| Metric | Target SLA | Description & Evaluation Method |
| :--- | :--- | :--- |
| **Recall@5** | $\ge 80.0\%$ | Fraction of ground-truth relevant chunks found in top 5 retrieved items. |
| **Recall@10** | $\ge 90.0\%$ | Fraction of ground-truth relevant chunks found in top 10 retrieved items. |
| **MRR@10** | $\ge 0.75$ | Mean Reciprocal Rank of the first relevant chunk in ranked candidates. |
| **nDCG@10** | $\ge 0.80$ | Normalized Discounted Cumulative Gain penalizing lower-ranked relevant chunks. |
| **Context Precision**| $\ge 0.80$ | Concentration of relevant chunks at the top of the context window. |
| **Faithfulness** | $\ge 0.90$ | Ratio of generated claims entailed by retrieved chunks (NLI Cross-Encoder). |
| **Answer Correctness**| $\ge 0.80$ | F1 token overlap + semantic embedding similarity against ground truth. |
| **Citation Accuracy**| $\ge 0.95$ | AST verification that inline marker `[x]` maps to text containing the claim. |
| **Abstention Accuracy**| $\ge 0.90$ | Correct abstention on unanswerable and unauthorized questions. |
| **Single-Hop Latency**| $P_{95} \le 1.5\text{s}$ | Latency of dense + sparse hybrid search + reranking over 500k chunks. |
| **End-to-End Latency**| $P_{95} \le 10.0\text{s}$ | Streaming time-to-first-token $\le 1.8\text{s}$, total response $\le 10\text{s}$. |

---

## 2. Benchmark Dataset Architecture (300 Gold Samples)

The benchmark suite is balanced across 7 failure-mode testing categories:
- **Simple Factual Retrieval (25% / 75 samples)**: Keyword and standard handbook lookup.
- **Semantic Retrieval (15% / 45 samples)**: High lexical divergence and multilingual queries.
- **Multi-Document Reasoning (20% / 60 samples)**: Synthesizing facts across multiple files/departments.
- **Multi-Hop Questions (15% / 45 samples)**: Dependent iterative search trajectories.
- **Numerical / Table QA (10% / 30 samples)**: Tabular calculations, spreadsheet coordinates.
- **Unanswerable Questions (10% / 30 samples)**: Tests strict refusal to hallucinate/speculate.
- **Permission-Sensitive (5% / 15 samples)**: Tests unauthorized access blockage.

Data Splits:
- `TRAIN` (20% / 60 samples): Development and parameter search.
- `DEV` (30% / 90 samples): PR smoke testing and validation.
- `TEST` (50% / 150 samples): Blind evaluation holdout set (never use for tuning).

---

## 3. Step-by-Step Benchmark Execution Workflow

### Step 1: Verify Dynamic Environment Configuration
Ensure all required environment variables are set. Never hardcode credentials, URLs, or model endpoints:
```bash
# Verify environment presence
test -n "$DATABASE_URL" || echo "WARNING: DATABASE_URL not set"
test -n "$QDRANT_HOST" || echo "WARNING: QDRANT_HOST not set"
test -n "$OPENAI_API_KEY" || test -n "$LOCAL_LLM_ENDPOINT" || echo "WARNING: No LLM endpoint configured"
```

### Step 2: Select Experiment Configuration
Select the target experiment from the Ablation Study Matrix (E1 to E8):
- `E1_DENSE_ONLY`: Dense semantic retrieval only (`BAAI/bge-m3` on Qdrant).
- `E2_BM25_ONLY`: Sparse lexical search only (PostgreSQL Full-Text Search).
- `E3_HYBRID_RRF`: Dense + BM25 combined via Reciprocal Rank Fusion ($k=60$).
- `E4_HYBRID_RERANK`: Hybrid + Cross-Encoder Reranking (`BAAI/bge-reranker-v2-m3`).
- `E5_HYBRID_REWRITE`: Hybrid + Query Rewriting & Expansion.
- `E6_HYBRID_RERANK_REWRITE`: Hybrid + Reranking + Query Rewriting.
- `E7_MULTI_HOP_AGENT`: Iterative LangGraph Agentic RAG.
- `E8_FULL_PIPELINE_RBAC`: Production baseline with pre/post permission filtering.

### Step 3: Run Evaluation Execution
Invoke the evaluation runner with dynamic CLI parameters:
```bash
# Run PR Smoke Test (30 dev samples)
python -m backend.eval.runner \
  --split DEV \
  --experiment E8_FULL_PIPELINE_RBAC \
  --sample-limit 30 \
  --output-dir "${BENCHMARK_OUTPUT_DIR:-./benchmark_runs}"

# Run Full Benchmark (300 samples across TEST split)
python -m backend.eval.runner \
  --split TEST \
  --experiment "${EXPERIMENT_TYPE:-E8_FULL_PIPELINE_RBAC}" \
  --output-dir "${BENCHMARK_OUTPUT_DIR:-./benchmark_runs}" \
  --save-to-db
```

### Step 4: Validate Results Against SLAs
Inspect the generated JSON summary report:
```bash
# Check if all SLA criteria passed
python -m backend.eval.sla_checker \
  --report-path "${BENCHMARK_OUTPUT_DIR:-./benchmark_runs}/latest_run.json"
```

If any metric (e.g. `Faithfulness < 0.90` or `MRR@10 < 0.75`) fails, inspect the individual failed questions in `benchmark_run_items` to diagnose retrieval misses or ungrounded assertions.
