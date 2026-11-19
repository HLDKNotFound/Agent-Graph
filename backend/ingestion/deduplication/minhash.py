"""MinHash signature generation, text normalization, and Jaccard similarity estimation.

Provides deterministic, vectorized MinHash signatures (default: 128 permutations)
over normalized k-shingles (default: 3-shingles) for document chunk near-deduplication.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Sequence

import numpy as np

from backend.ingestion.config import get_ingestion_settings
from backend.ingestion.deduplication.models import MinHashConfig

# Mersenne prime (2^31 - 1) for universal hashing without 64-bit integer overflow
MERSENNE_PRIME = 2147483647

# Optional accelerated xxhash support with sha256 fallback
try:
    import xxhash

    def _hash_token(token: str) -> int:
        return xxhash.xxh32_intdigest(token.encode("utf-8")) % MERSENNE_PRIME

except ImportError:  # pragma: no cover

    def _hash_token(token: str) -> int:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % MERSENNE_PRIME


def normalize_chunk_text(text: str) -> str:
    """Normalize text prior to shingle generation and MinHash signature computation.

    Pipeline:
    1. Unicode canonical composition and compatibility decomposition (NFKC) for international & Vietnamese diacritics.
    2. Case folding (lower casing).
    3. Punctuation and markdown symbol stripping (collapsing non-alphanumeric punctuation to spaces).
    4. Contiguous whitespace normalization (collapsing tabs, newlines, repeated spaces to a single space).

    Args:
        text: Raw textual body content.

    Returns:
        Clean, normalized lowercase string.
    """
    if not text:
        return ""

    # 1. Unicode normalization
    text = unicodedata.normalize("NFKC", text)

    # 2. Lowercase
    text = text.lower()

    # 3. Strip non-word, non-whitespace symbols and underscores (preserving international unicode letters and digits)
    text = re.sub(r"[^\w\s]|_", " ", text)

    # 4. Collapse contiguous whitespace and strip ends
    text = re.sub(r"\s+", " ", text).strip()
    return text


def generate_shingles(
    text: str,
    shingle_size: int = 3,
    ngram_type: str = "word",
    normalize: bool = True,
) -> list[str]:
    """Generate k-shingle n-grams from input text.

    Args:
        text: Source text.
        shingle_size: Shingle length k (default: 3).
        ngram_type: Segmentation type ('word' or 'character').
        normalize: Whether to apply normalize_chunk_text() first.

    Returns:
        List of generated shingles. If text is shorter than shingle_size, returns
        the entire normalized text as a single shingle to prevent empty signatures.
    """
    if normalize:
        text = normalize_chunk_text(text)

    if not text:
        return []

    if ngram_type == "character":
        if len(text) <= shingle_size:
            return [text]
        return [text[i : i + shingle_size] for i in range(len(text) - shingle_size + 1)]

    # Word-level n-grams (default)
    tokens = text.split()
    if not tokens:
        return []

    if len(tokens) <= shingle_size:
        return [" ".join(tokens)]

    return [" ".join(tokens[i : i + shingle_size]) for i in range(len(tokens) - shingle_size + 1)]


class MinHashSigner:
    """Computes deterministic, vectorized MinHash signatures using universal hash families.

    Given a set of shingles S, the MinHash signature of length N is computed as:
        h_i(x) = (a_i * x + b_i) mod p
        sig_i = min_{x in S} h_i(x)
    where p = 2^31 - 1, and (a_i, b_i) are deterministic parameters generated from a fixed seed.

    The expected fraction of matching signature components between two documents
    equals the Jaccard similarity of their shingle sets:
        E[ matching(sig_A, sig_B) / N ] = Jaccard(S_A, S_B)
    """

    def __init__(
        self,
        config: MinHashConfig | None = None,
        num_permutations: int | None = None,
        shingle_size: int | None = None,
        ngram_type: str | None = None,
        similarity_threshold: float | None = None,
        exact_threshold: float | None = None,
        seed: int | None = None,
    ) -> None:
        settings = get_ingestion_settings()
        self.config = config or MinHashConfig(
            num_permutations=num_permutations
            if num_permutations is not None
            else settings.minhash_num_permutations,
            shingle_size=shingle_size
            if shingle_size is not None
            else settings.minhash_shingle_size,
            ngram_type=ngram_type
            if ngram_type is not None
            else settings.minhash_ngram_type,
            similarity_threshold=similarity_threshold
            if similarity_threshold is not None
            else settings.minhash_similarity_threshold,
            exact_threshold=exact_threshold
            if exact_threshold is not None
            else settings.minhash_exact_threshold,
            seed=seed if seed is not None else settings.minhash_seed,
        )

        self.num_permutations = self.config.num_permutations
        self.shingle_size = self.config.shingle_size
        self.ngram_type = self.config.ngram_type
        self.similarity_threshold = self.config.similarity_threshold
        self.exact_threshold = self.config.exact_threshold
        self.seed = self.config.seed

        # Precompute deterministic universal hash permutation coefficients (a_i, b_i)
        rng = np.random.RandomState(self.seed)
        # Ensure a_i is non-zero and odd for maximal cycle length mod p
        raw_a = rng.randint(1, MERSENNE_PRIME, size=self.num_permutations, dtype=np.int64)
        self._a = np.where(raw_a % 2 == 0, raw_a + 1, raw_a).astype(np.int64)
        self._b = rng.randint(0, MERSENNE_PRIME, size=self.num_permutations, dtype=np.int64)

    def compute_signature(self, text: str) -> list[int]:
        """Compute the MinHash signature array for input chunk text.

        Args:
            text: Raw or normalized chunk text.

        Returns:
            List of N integers representing the MinHash signature.
        """
        shingles = generate_shingles(
            text=text,
            shingle_size=self.shingle_size,
            ngram_type=self.ngram_type,
            normalize=True,
        )
        return self.compute_signature_for_shingles(shingles)

    def compute_signature_for_shingles(self, shingles: Sequence[str]) -> list[int]:
        """Compute the MinHash signature array from pre-extracted shingles.

        Args:
            shingles: Sequence of shingle strings.

        Returns:
            List of N integers representing the MinHash signature.
        """
        if not shingles:
            # Return sentinel maximum integer vector for empty text
            return [MERSENNE_PRIME] * self.num_permutations

        unique_shingles = set(shingles)
        x_hashes = np.array(
            [_hash_token(s) for s in unique_shingles],
            dtype=np.int64,
        )

        # Vectorized universal hashing: (A * X + B) mod p
        # Shape: (num_permutations, len(unique_shingles))
        hash_matrix = (self._a[:, None] * x_hashes[None, :] + self._b[:, None]) % MERSENNE_PRIME

        # Take minimum hash value per permutation
        sig_vector = hash_matrix.min(axis=1)
        return sig_vector.tolist()

    def estimate_similarity(
        self,
        sig1: Sequence[int] | np.ndarray,
        sig2: Sequence[int] | np.ndarray,
    ) -> float:
        """Estimate the Jaccard similarity between two MinHash signatures.

        Args:
            sig1: First MinHash signature.
            sig2: Second MinHash signature.

        Returns:
            Estimated Jaccard similarity in range [0.0, 1.0].
        """
        if len(sig1) != len(sig2):
            raise ValueError(
                f"Signature length mismatch: {len(sig1)} vs {len(sig2)}"
            )

        if not sig1:
            return 0.0

        arr1 = np.asarray(sig1, dtype=np.int64)
        arr2 = np.asarray(sig2, dtype=np.int64)
        matches = np.count_nonzero(arr1 == arr2)
        return float(matches / len(arr1))

    def exact_jaccard_similarity(self, text1: str, text2: str) -> float:
        """Calculate the ground-truth set Jaccard similarity between two texts.

        Formula: |S1 intersection S2| / |S1 union S2|

        Args:
            text1: First document or chunk text.
            text2: Second document or chunk text.

        Returns:
            Exact Jaccard similarity in range [0.0, 1.0].
        """
        s1 = set(
            generate_shingles(
                text1,
                shingle_size=self.shingle_size,
                ngram_type=self.ngram_type,
                normalize=True,
            )
        )
        s2 = set(
            generate_shingles(
                text2,
                shingle_size=self.shingle_size,
                ngram_type=self.ngram_type,
                normalize=True,
            )
        )

        if not s1 and not s2:
            return 1.0 if normalize_chunk_text(text1) == normalize_chunk_text(text2) else 0.0
        if not s1 or not s2:
            return 0.0

        intersection = len(s1 & s2)
        union = len(s1 | s2)
        return float(intersection / union)

    def is_exact_match(
        self,
        text1: str,
        text2: str,
        content_hash1: str | None = None,
        content_hash2: str | None = None,
        sig1: Sequence[int] | None = None,
        sig2: Sequence[int] | None = None,
    ) -> bool:
        """Determine whether two chunks constitute an EXACT duplicate.

        Conditions for EXACT:
        1. Identical SHA-256 content hashes (if provided).
        2. Exact matching normalized text strings.
        3. Estimated or exact Jaccard similarity >= exact_threshold (default: 0.999).
        """
        if content_hash1 and content_hash2 and content_hash1 == content_hash2:
            return True

        norm1 = normalize_chunk_text(text1)
        norm2 = normalize_chunk_text(text2)
        if norm1 and norm1 == norm2:
            return True

        if sig1 is not None and sig2 is not None:
            sim = self.estimate_similarity(sig1, sig2)
            return sim >= self.exact_threshold

        return self.exact_jaccard_similarity(text1, text2) >= self.exact_threshold
