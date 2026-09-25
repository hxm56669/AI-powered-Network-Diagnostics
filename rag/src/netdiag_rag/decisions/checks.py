"""Build and revalidate registered checks using only the current trusted scope."""
from __future__ import annotations

from ..contracts import NextCheck
from ..knowledge.parameters import validate_parameters
from ..knowledge.schema import KnowledgeCheck
from ..util import canonical


def definitions(candidate):
    checks = candidate.get("metadata", {}).get("checks", [])
    return [{"check_id": key, **value} for key, value in checks.items()] if isinstance(checks, dict) else checks


def _available_parameters(request, bundle):
    evidence = bundle.evidence if bundle else []
    interfaces = set(request.interfaces) if request else set()
    observed = {e.scope["interface"] for e in evidence if e.scope.get("interface")}
    interfaces = interfaces & observed if interfaces else observed
    if bundle and "ambiguous_interface" in bundle.missing:
        interfaces = set()
    context = request.check_context if request else None
    if context and context.interface in interfaces:
        interfaces = {context.interface}
    selected_interface = next(iter(interfaces)) if len(interfaces) == 1 else None
    # Targets must come from the same selected interface; never guess a gateway role.
    targets = {e.scope["target"] for e in evidence if selected_interface
               and e.scope.get("interface") == selected_interface and e.scope.get("target")}
    available = {"interface": selected_interface, "target": next(iter(targets)) if len(targets) == 1 else None}
    if context and context.interface == selected_interface:
        available.update(context.model_dump(exclude_none=True, exclude={"interface"}))
    return available


def bound_parameters(definition, available):
    schema = definition.parameters_schema
    if schema is None:
        return {}, []
    parameters = {name: available[source] for name, source in definition.parameter_sources.items()
                  if available.get(source) is not None}
    missing = sorted(set(schema.get("required", [])) - parameters.keys())
    if not missing:
        validate_parameters(parameters, schema)
    return parameters, missing


def build_checks(candidates, request=None, bundle=None, allow_seed=False):
    entries, conflicts, rejected = {}, set(), []
    allowed = {"approved"} | ({"seed"} if allow_seed else set())
    for candidate in candidates:
        metadata = candidate.get("metadata", {})
        for raw in definitions(candidate):
            try:
                definition = KnowledgeCheck.model_validate(raw)
            except ValueError:
                rejected.append({"check_id": raw.get("check_id", "unknown"), "reason": "invalid_check_definition"})
                continue
            identity = definition.check_id
            signature = canonical(definition.model_dump(mode="json"))
            if metadata.get("review_status") not in allowed or definition.review_status not in allowed:
                conflicts.add(identity)
                rejected.append({"check_id": identity, "reason": "check_not_reviewed"})
            if identity in entries:
                if entries[identity][0] != signature:
                    conflicts.add(identity)
                else:
                    entries[identity][2].append(candidate["chunk_id"])
                continue
            entries[identity] = [signature, definition, [candidate["chunk_id"]]]
    result = []
    evidence = bundle.evidence if bundle else []
    metrics = {e.metric for e in evidence}
    scopes = {e.scope.get("kind") for e in evidence}
    available = _available_parameters(request, bundle)
    for identity, (_, definition, citations) in entries.items():
        if identity in conflicts:
            rejected.append({"check_id": identity, "reason": "conflicting_or_unreviewed_check"})
            continue
        if (set(definition.required_metrics) - metrics
                or definition.scope_kinds and not scopes.intersection(definition.scope_kinds)):
            rejected.append({"check_id": identity, "reason": "check_scope_or_capability_unavailable"})
            continue
        try:
            parameters, missing = bound_parameters(definition, available)
        except ValueError:
            rejected.append({"check_id": identity, "reason": "check_parameter_schema_mismatch"})
            continue
        if missing:
            rejected.append({"check_id": identity, "reason": "check_parameters_missing", "missing": missing})
            continue
        result.append(NextCheck(check_id=identity, purpose=definition.purpose,
                      knowledge_ids=list(dict.fromkeys(citations)), parameters=parameters,
                      target=parameters.get("target")))
    return result, rejected


def validate_checks(checks, *, available_checks):
    by_id = {check.check_id: check for check in available_checks}
    for check in checks:
        registered = by_id.get(check.check_id)
        if registered is None:
            raise ValueError("unregistered_check_or_invalid_scope")
        if not check.knowledge_ids or set(check.knowledge_ids) - set(registered.knowledge_ids):
            raise ValueError("unknown_check_citation")
        # Missing parameters in older/model output can only be populated from trusted bindings.
        if check.parameters and check.parameters != registered.parameters:
            raise ValueError("check_parameters_outside_scope")
        if check.target is not None and check.target != registered.target:
            raise ValueError("check_target_conflict")
        check.parameters = dict(registered.parameters)
        check.target = registered.target
        check.purpose = registered.purpose
    return checks
