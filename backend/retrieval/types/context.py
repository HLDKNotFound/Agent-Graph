"""Zero-trust security context and authorization boundaries for hybrid retrieval."""

from __future__ import annotations

import uuid
from typing import Sequence
from pydantic import BaseModel, ConfigDict, Field


class RetrievalContext(BaseModel):
    """Zero-trust multi-tenant security boundary applied across Stage 1, Stage 2, and Stage 3.

    Enforces:
    - Strict tenant isolation (tenant_id)
    - User identity & audit tracking (user_id)
    - Role-based access control (roles)
    - Departmental boundary containment (departments)
    - Classification clearance levels (classifications e.g. PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED)
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID = Field(
        ...,
        description="Mandatory owning tenant UUID",
    )
    user_id: uuid.UUID = Field(
        ...,
        description="Authenticated acting user UUID",
    )
    roles: list[str] = Field(
        default_factory=list,
        description="Active assigned RBAC role identifiers (e.g. ['EMPLOYEE', 'HR_MANAGER'])",
    )
    departments: list[str] = Field(
        default_factory=list,
        description="Authorized department IDs or department names the user belongs to",
    )
    classifications: list[str] = Field(
        default_factory=lambda: ["PUBLIC", "INTERNAL"],
        description="Maximum permitted document classification tiers",
    )
    locale: str = Field(
        default="vi",
        description="User locale preference ('vi', 'en')",
    )
    correlation_id: str | None = Field(
        default=None,
        description="Distributed request tracing correlation ID",
    )

    def can_access_classification(self, classification: str) -> bool:
        """Verify whether the context has clearance for a given classification level."""
        normalized = classification.strip().upper()
        # PUBLIC is always universally accessible
        if normalized == "PUBLIC":
            return True
        allowed = [c.strip().upper() for c in self.classifications]
        return normalized in allowed

    def has_role(self, role: str) -> bool:
        """Check if user possesses a specific role."""
        return role.strip().upper() in [r.strip().upper() for r in self.roles]

    def belongs_to_department(self, department: str) -> bool:
        """Check if user belongs to a specific department."""
        return department.strip() in [d.strip() for d in self.departments]
