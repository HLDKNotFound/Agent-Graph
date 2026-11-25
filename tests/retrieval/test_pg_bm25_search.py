"""Unit tests for Task 03: PostgreSQL GIN BM25 Sparse Lexical Search."""

import asyncio
import uuid
import pytest

from backend.retrieval.config import RetrievalSettings
from backend.retrieval.exceptions import (
    RetrievalSecurityViolationError,
    SparseRetrievalError,
)
from backend.retrieval.sparse.pg_bm25_search import PostgresBM25Retriever
from backend.retrieval.types import (
    RetrievalContext,
    RetrievedChunkSource,
)


class MockRecord(dict):
    """Mock asyncpg Record supporting dict access."""
    def __getitem__(self, key):
        return super().__getitem__(key)


class MockConnection:
    """Mock asyncpg connection for isolated test execution."""

    def __init__(self, records: list[dict] | None = None, should_fail_websearch: bool = False, should_fail_all: bool = False) -> None:
        self.records = records or []
        self.should_fail_websearch = should_fail_websearch
        self.should_fail_all = should_fail_all
        self.executed_queries: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.executed_queries.append((query, args))
        if self.should_fail_all:
            raise ConnectionError("PostgreSQL pool terminated")
        if self.should_fail_websearch and "websearch_to_tsquery" in query:
            raise RuntimeError("syntax error in websearch query")
        return [MockRecord(r) for r in self.records]


class MockPool:
    """Mock asyncpg connection pool."""

    def __init__(self, connection: MockConnection) -> None:
        self.connection = connection

    def acquire(self):
        class _ContextManager:
            def __init__(self, conn):
                self.conn = conn

            async def __aenter__(self):
                return self.conn

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        return _ContextManager(self.connection)


class TestPostgresBM25QueryBuilder:
    """Validate parameterized SQL construction and security checks."""

    def test_sql_query_structure(self) -> None:
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        ctx = RetrievalContext(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=["ENGINEERING_LEAD"],
            departments=["ENGINEERING"],
            classifications=["PUBLIC", "INTERNAL"],
        )

        retriever = PostgresBM25Retriever()
        sql, params = retriever.build_sql_query(ctx, "chính sách nghỉ phép", 40)

        assert "websearch_to_tsquery('simple', $1)" in sql
        assert "to_tsvector('simple', c.content)" in sql
        assert "ts_rank_cd" in sql
        assert "32" in sql  # Normalization flag 32
        assert "WHERE c.tenant_id = $2::uuid" in sql
        assert "ORDER BY relevance_score DESC" in sql

        # Parameter checks
        assert params[0] == "chính sách nghỉ phép"
        assert params[1] == str(tenant_id)
        assert params[2] == ["PUBLIC", "INTERNAL"]
        assert params[3] == ["ENGINEERING_LEAD"]
        assert params[4] == ["ENGINEERING"]
        assert params[5] == 40


class TestPostgresBM25Execution:
    """Validate search execution, ranking, and coordinate mapping."""

    def test_search_success_mapping(self) -> None:
        async def _run() -> None:
            tenant_id = uuid.uuid4()
            user_id = uuid.uuid4()
            chunk_id = uuid.uuid4()
            doc_id = uuid.uuid4()

            mock_row = {
                "chunk_id": chunk_id,
                "tenant_id": tenant_id,
                "document_id": doc_id,
                "document_version_id": None,
                "chunk_index": 2,
                "content": "Nhân viên có 12 ngày phép hàng năm.",
                "token_count": 10,
                "page_number": 4,
                "section_heading": "Chính sách nghỉ phép",
                "sheet_name": None,
                "row_range": None,
                "char_start": 300,
                "char_end": 450,
                "chunk_metadata": {"allowed_roles": ["EMPLOYEE"]},
                "original_filename": "Quy_dinh_nhan_su.pdf",
                "classification": "INTERNAL",
                "department_id": None,
                "relevance_score": 0.452,
            }

            conn = MockConnection(records=[mock_row])
            pool = MockPool(conn)
            retriever = PostgresBM25Retriever(pool=pool)

            ctx = RetrievalContext(
                tenant_id=tenant_id,
                user_id=user_id,
                roles=["EMPLOYEE"],
            )

            results = await retriever.search("nghỉ phép", ctx, top_k=10)
            assert len(results) == 1
            res = results[0]

            assert res.chunk_id == chunk_id
            assert res.document_id == doc_id
            assert res.content == "Nhân viên có 12 ngày phép hàng năm."
            assert res.sparse_score == 0.452
            assert res.sparse_rank == 1
            assert res.source == RetrievedChunkSource.SPARSE
            assert res.locator.page_number == 4
            assert res.locator.section_heading == "Chính sách nghỉ phép"
            assert res.locator.char_start == 300
            assert res.document_title == "Quy_dinh_nhan_su.pdf"

        asyncio.run(_run())

    def test_empty_query_returns_empty_list(self) -> None:
        async def _run() -> None:
            conn = MockConnection(records=[])
            pool = MockPool(conn)
            retriever = PostgresBM25Retriever(pool=pool)
            ctx = RetrievalContext(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())

            assert await retriever.search("", ctx) == []
            assert await retriever.search("   ", ctx) == []
            assert len(conn.executed_queries) == 0

        asyncio.run(_run())

    def test_fallback_on_websearch_syntax_error(self) -> None:
        async def _run() -> None:
            chunk_id = uuid.uuid4()
            mock_row = {
                "chunk_id": chunk_id,
                "tenant_id": uuid.uuid4(),
                "document_id": uuid.uuid4(),
                "document_version_id": None,
                "chunk_index": 0,
                "content": "Fallback content",
                "token_count": 5,
                "page_number": 1,
                "section_heading": None,
                "sheet_name": None,
                "row_range": None,
                "char_start": None,
                "char_end": None,
                "chunk_metadata": {},
                "original_filename": "doc.pdf",
                "classification": "PUBLIC",
                "department_id": None,
                "relevance_score": 0.2,
            }

            conn = MockConnection(records=[mock_row], should_fail_websearch=True)
            pool = MockPool(conn)
            retriever = PostgresBM25Retriever(pool=pool)
            ctx = RetrievalContext(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())

            results = await retriever.search("query with complex:syntax!", ctx)
            assert len(results) == 1
            # Check fallback was attempted
            assert len(conn.executed_queries) == 2
            assert "plainto_tsquery" in conn.executed_queries[1][0]

        asyncio.run(_run())

    def test_database_error_handling(self) -> None:
        async def _run() -> None:
            conn = MockConnection(should_fail_all=True)
            pool = MockPool(conn)
            retriever = PostgresBM25Retriever(pool=pool)
            ctx = RetrievalContext(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())

            with pytest.raises(SparseRetrievalError) as exc_info:
                await retriever.search("test query", ctx)
            assert "PostgreSQL sparse lexical search failed" in str(exc_info.value)

        asyncio.run(_run())
