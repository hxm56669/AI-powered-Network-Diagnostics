"""Portable schema for normalized Markdown front matter; body sections stay loader-validated."""
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from ..settings import StrictModel

ReviewStatus = Literal["approved", "code-reviewed", "seed", "draft", "revoked"]


class KnowledgeScope(StrictModel):
    organizations: list[str] = Field(min_length=1)


class KnowledgeCheck(StrictModel):
    check_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    command_template: str | None = None
    permission: str | None = None
    timeout_s: float | None = Field(default=None, gt=0)
    review_status: ReviewStatus = "draft"
    automatic_execution: Literal[False] = False
    parameters_schema: dict[str, Any] | None = None
    parameter_sources: dict[str, str] = Field(default_factory=dict)
    scope_kinds: list[Literal["host_netns", "interface", "interface_target", "flow_subset"]] = Field(default_factory=list)
    required_metrics: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def safe_parameters(self):
        from .parameters import RESERVED_CHECKS, validate_parameter_schema
        if self.check_id in RESERVED_CHECKS:
            raise ValueError("reserved_check_id")
        validate_parameter_schema(self.parameters_schema, self.parameter_sources)
        return self


class KnowledgeMetadata(StrictModel):
    doc_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
    title: str = Field(min_length=1)
    kind: Literal["runbook", "protocol", "incident", "environment"]
    language: str = Field(min_length=1)
    source_uri: str = Field(pattern=r"^(https?|internal)://")
    source_type: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    review_status: ReviewStatus
    reviewed_at: AwareDatetime | None = None
    reviewer: str | None = None
    license: str = Field(min_length=1)
    available_at: AwareDatetime
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None = None
    references: list[str] = Field(default_factory=list)
    applies_to: dict[str, list[str]]
    scope: KnowledgeScope
    symptoms: list[str] = Field(default_factory=list)
    cause_ids: list[str] = Field(default_factory=list)
    check_ids: list[str] = Field(default_factory=list)
    checks: list[KnowledgeCheck] = Field(default_factory=list)


def knowledge_schema():
    schema = KnowledgeMetadata.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:netdiag:knowledge:1"
    schema["description"] = "Normalized Markdown front matter. Section presence, unique checks and date ordering are also checked by the loader."
    schema["allOf"] = [
        {"if": {"properties": {"source_type": {"const": "seed"}}, "required": ["source_type"]},
         "then": {"properties": {"review_status": {"not": {"const": "approved"}},
                                    "references": {"minItems": 1}}, "required": ["references"]}},
        {"if": {"properties": {"review_status": {"const": "approved"}}, "required": ["review_status"]},
         "then": {"required": ["reviewed_at"], "properties": {"reviewed_at": {"type": "string", "format": "date-time"}}}}
    ]
    return schema
