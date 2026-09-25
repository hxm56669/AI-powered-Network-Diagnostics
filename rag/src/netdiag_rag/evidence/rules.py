"""Declarative anomaly hints; cause confirmation remains a separate fact gate."""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from ..settings import StrictModel
from ..util import digest


class Condition(StrictModel):
    field: str = Field(pattern=r"^(summary\.[A-Za-z0-9_.]+|coverage|warnings)$")
    op: Literal["eq", "gt", "ge", "lt", "contains"]
    value: str | float | bool

    @model_validator(mode="after")
    def comparable(self):
        if self.op in {"gt", "ge", "lt"} and (not isinstance(self.value, (int, float)) or isinstance(self.value, bool)):
            raise ValueError("numeric_rule_threshold_required")
        return self

    def matches(self, evidence):
        root, *parts = self.field.split(".")
        current = getattr(evidence, root)
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return False
            current = current[part]
        if self.op == "eq":
            return current == self.value
        if self.op == "contains":
            return isinstance(current, list) and self.value in current
        if not isinstance(current, (int, float)) or isinstance(current, bool):
            return False
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            return False
        return {"gt": current > self.value, "ge": current >= self.value, "lt": current < self.value}[self.op]


class Rule(StrictModel):
    rule_id: str = Field(pattern=r"^[a-z0-9_.-]+$")
    version: int = Field(ge=1)
    metrics: list[str] = Field(min_length=1)
    scope_kinds: list[Literal["host_netns", "interface", "interface_target", "flow_subset"]] = Field(min_length=1)
    conditions: list[Condition] = Field(min_length=1)
    symptom: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    counterevidence: list[str] = Field(default_factory=list)


class RuleSet(StrictModel):
    version: int = Field(ge=1)
    rules: list[Rule] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self):
        if len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError("duplicate_rule_id")
        return self


class RuleEngine:
    def __init__(self, path=None):
        path = Path(path) if path else Path(__file__).with_name("rules.yaml")
        self.definition = RuleSet.model_validate(yaml.load(path.read_text(), Loader=UniqueRuleLoader))
        self.fingerprint = digest(self.definition.model_dump(mode="json"))

    def evaluate(self, evidence):
        hits = []
        for rule in self.definition.rules:
            matched = [e.evidence_id for e in evidence if e.metric in rule.metrics
                       and e.scope.get("kind") in rule.scope_kinds
                       and all(c.matches(e) for c in rule.conditions)]
            if matched:
                hits.append({**rule.model_dump(mode="json"), "evidence_ids": matched})
        return hits


class UniqueRuleLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate_or_invalid_rule_key")
        result[key] = loader.construct_object(value_node)
    return result


UniqueRuleLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
