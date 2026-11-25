"""User security and contextual boundaries for query routing and retrieval."""

from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict


class UserQueryContext(BaseModel):
    """Authenticated user query context enforcing zero-trust isolation and localization."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID | str = Field(
        ...,
        description="Tenant identifier for multi-tenant isolation",
    )
    user_id: UUID | str = Field(
        ...,
        description="Authenticated user identifier",
    )
    roles: list[str] = Field(
        default_factory=list,
        description="Assigned RBAC roles of the user (e.g. ['EMPLOYEE', 'MANAGER'])",
    )
    departments: list[str] = Field(
        default_factory=list,
        description="Assigned departments of the user (e.g. ['HR', 'ENGINEERING'])",
    )
    locale: str = Field(
        default="vi",
        description="User locale or language code (e.g. 'vi', 'en')",
    )
