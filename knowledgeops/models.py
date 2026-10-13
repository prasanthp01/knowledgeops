import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["staff", "engineering", "security", "finance"]
Collection = Literal["people", "operations", "security", "finance"]
Mode = Literal["SANDBOX_DETERMINISTIC", "REPLAY_RECORDED", "LIVE_MODEL_AUTHENTICATED"]
FIXTURES_VERSION = "synthetic-2026-10-13.2"
POLICY_VERSION = "demo-acl-1"
ROLES = {
    "staff": ["public", "staff"],
    "engineering": ["public", "staff", "engineering"],
    "security": ["public", "staff", "security"],
    "finance": ["public", "staff", "finance"],
}


def all_collections() -> list[Collection]:
    return ["people", "operations", "security", "finance"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchInput(StrictModel):
    question: str = Field(min_length=3, max_length=500)
    role: Role = "staff"
    corpus: Literal["atlas-synthetic"] = "atlas-synthetic"
    collections: list[Collection] = Field(
        default_factory=all_collections,
        min_length=1,
        max_length=4,
    )
    algorithm: Literal["bm25", "keyword"] = "bm25"
    top_k: int = Field(default=3, ge=1, le=5, strict=True)
    revision: Literal["current", "previous", "conflict"] = "current"
    keyword_filter: str = Field(default="", max_length=80)
    seed: int = Field(default=42, ge=0, le=4294967295, strict=True)

    @field_validator("question", "keyword_filter")
    @classmethod
    def safe_text(cls, value: str) -> str:
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise ValueError("Control characters are not allowed")
        return value

    @field_validator("collections")
    @classmethod
    def unique_collections(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Collections must be unique")
        return value


class SimulationStart(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    project_id: Literal["knowledgeops"] = "knowledgeops"
    mode: Literal["SANDBOX_DETERMINISTIC", "REPLAY_RECORDED"] = "SANDBOX_DETERMINISTIC"
    preset_id: str | None = Field(default=None, max_length=60)
    input: SearchInput
    client_correlation_id: str | None = Field(
        default=None, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    replay_run_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")


class PrivateInput(StrictModel):
    question: str = Field(min_length=3, max_length=500)
    collections: list[Collection] = Field(
        default_factory=all_collections,
        min_length=1,
        max_length=4,
    )
    operation_id: str = Field(min_length=16, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")


class Section(StrictModel):
    heading: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=12000)
    page: int = Field(default=1, ge=1, le=30)


class Document(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,59}$")
    version: int = Field(ge=1, le=10000)
    title: str = Field(min_length=1, max_length=150)
    collection: Collection
    groups: list[
        Annotated[str, Field(pattern=r"^(public|staff|engineering|security|finance)$")]
    ] = Field(min_length=1, max_length=5)
    created_at: str
    updated_at: str
    source_path: str = Field(pattern=r"^synthetic/[a-zA-Z0-9_./-]+\.(md|txt|pdf)$")
    classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
    retention_days: int = Field(default=365, ge=1, le=3650)
    revision: Literal["current", "previous", "conflict"] = "current"
    lifecycle: Literal["active", "revoked", "expired", "quarantined"] = "active"
    valid_until: str = "2030-01-01T00:00:00Z"
    policy_key: str
    sections: list[Section] = Field(min_length=1, max_length=30)
