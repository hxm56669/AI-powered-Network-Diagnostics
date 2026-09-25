"""Strict generation DTOs and bounded, content-free validation receipts."""
import json
import re

from pydantic import ValidationError, create_model

from ..contracts import DiagnosisContent, IncidentReasoning
from ..settings import StrictModel
from ..util import canonical, unique_object

REPAIR_MESSAGE_BYTES = 512


_PROGRAM_CHECK_MODELS = {model: create_model(model.__name__ + "ProgramChecks", __base__=StrictModel,
    **{name: (field.annotation, field) for name, field in model.model_fields.items() if name != "next_checks"})
    for model in (IncidentReasoning, DiagnosisContent)}


def output_model(bundle, program_checks=False):
    model = IncidentReasoning if bundle is not None else DiagnosisContent
    return _PROGRAM_CHECK_MODELS[model] if program_checks else model


def output_schema(bundle, program_checks=False):
    schema = output_model(bundle, program_checks).model_json_schema()
    if bundle is not None:
        missing = schema["$defs"]["Hypothesis"]["properties"]["missing_evidence_ids"]
        if bundle.missing:
            missing["items"]["enum"] = list(bundle.missing)
        else:
            missing["maxItems"] = 0
    else:
        body = schema["properties"]["observations"]
        body.pop("default", None)
        body["minItems"] = 1
        schema["required"].append("observations")
        claim = schema["$defs"]["Claim"]
        claim["properties"]["knowledge_ids"]["minItems"] = 1
        claim["required"].append("knowledge_ids")
    def without_titles(value):
        if isinstance(value, dict):
            return {key: without_titles(item) for key, item in value.items() if key != "title"}
        if isinstance(value, list):
            return [without_titles(item) for item in value]
        return value
    return without_titles(schema)


def repair_message(failure):
    """Keep feedback within the space reserved before the first model call."""
    feedback = {"code": failure["code"]}
    corrections = {
        "knowledge_answer_requires_body": "在observations中提供非空回答正文，每段knowledge_ids引用所依据知识的完整chunk_id。",
        "unknown_missing_evidence": "将所有missing_evidence_ids改为[]；完整缺失项由报告data_quality展示，不要自行改写ID。",
        "incident_narrative_contains_numbers": "删除原因、反证和告警中的数值，只作定性说明；观测数值由程序填写，保持引用ID不变。",
    }
    if failure["code"] in corrections:
        feedback["correction"] = corrections[failure["code"]]
    if failure.get("issues"):
        issue = failure["issues"][0]
        feedback["issue"] = {"field": issue["field"][:80], "type": issue["type"][:64]}
    prefix = "校验失败。根据原始证据和schema重新输出JSON，修正："
    content = prefix + canonical(feedback)
    if len(content.encode()) > REPAIR_MESSAGE_BYTES:
        content = prefix + canonical({"code": failure["code"]})
    return {"role": "user", "content": content}


def parse_output(text, bundle, program_checks=False):
    value = json.loads(text, object_pairs_hook=unique_object)
    parsed = output_model(bundle, program_checks).model_validate(value)
    return DiagnosisContent.model_validate(parsed.model_dump())


def validation_receipt(error):
    if isinstance(error, ValidationError):
        known = {"outcome", "observations", "hypotheses", "next_checks", "warnings", "text",
                 "evidence_ids", "knowledge_ids", "cause_id", "certainty", "counterevidence",
                 "missing_evidence_ids", "check_id", "purpose", "target"}
        issues = error.errors(include_input=False, include_context=False, include_url=False)[:8]
        return {"code": "output_schema_invalid", "issues": [
            {"type": issue["type"], "field": ".".join(str(part) if isinstance(part, int) or part in known
                                                     else "unknown_field" for part in issue["loc"])}
            for issue in issues]}
    if isinstance(error, json.JSONDecodeError):
        return {"code": "invalid_output_json"}
    message = str(error)
    return {"code": message if re.fullmatch(r"[a-z0-9_]{1,80}", message) else "output_validation_failed"}
