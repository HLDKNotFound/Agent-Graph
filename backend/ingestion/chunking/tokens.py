"""Token management, encoding, and deterministic coordinate utilities for document chunking."""

import hashlib
from functools import lru_cache
import uuid
import tiktoken

DEFAULT_ENCODING_NAME = "cl100k_base"
CHUNK_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # RFC 4122 DNS namespace


@lru_cache(maxsize=8)
def get_tokenizer(encoding_name: str = DEFAULT_ENCODING_NAME) -> tiktoken.Encoding:
    """Retrieve or initialize a cached tiktoken tokenizer encoding.

    Defaults to cl100k_base (OpenAI / standard LLM subword tokenizer) which runs
    entirely offline with zero network overhead.
    """
    try:
        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return tiktoken.get_encoding(DEFAULT_ENCODING_NAME)


def count_tokens(text: str | None, encoding_name: str = DEFAULT_ENCODING_NAME) -> int:
    """Accurately count the number of tokens in a string without throwing on special characters."""
    if not text:
        return 0
    enc = get_tokenizer(encoding_name)
    return len(enc.encode(text, disallowed_special=()))


def encode_tokens(text: str | None, encoding_name: str = DEFAULT_ENCODING_NAME) -> list[int]:
    """Encode a text string into an integer list of token IDs."""
    if not text:
        return []
    enc = get_tokenizer(encoding_name)
    return enc.encode(text, disallowed_special=())


def decode_tokens(tokens: list[int], encoding_name: str = DEFAULT_ENCODING_NAME) -> str:
    """Decode a sequence of integer token IDs back into a UTF-8 string."""
    if not tokens:
        return ""
    enc = get_tokenizer(encoding_name)
    return enc.decode(tokens)


def compute_chunk_hash(content: str) -> str:
    """Compute the SHA-256 hexadecimal hash string for chunk content deduplication."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def generate_chunk_id(
    tenant_id: str,
    document_id: str,
    chunk_index: int,
    content_hash: str | None = None,
) -> str:
    """Generate a deterministic UUID v5 identifier for a document chunk.

    Guarantees stable chunk identifiers across re-indexing runs while ensuring
    UUID format compliance for PostgreSQL and Qdrant persistence.
    """
    seed_parts = [tenant_id, document_id, str(chunk_index)]
    if content_hash:
        seed_parts.append(content_hash)
    seed = ":".join(seed_parts)
    return str(uuid.uuid5(CHUNK_ID_NAMESPACE, f"agentgraph.chunk.{seed}"))
