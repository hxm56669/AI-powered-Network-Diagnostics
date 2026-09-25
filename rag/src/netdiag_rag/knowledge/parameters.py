"""Closed, non-recursive JSON Schema subset for diagnostic suggestion parameters.

No references, remote resolution, coercion, regex execution or arbitrary nested data.
Unknown schema keywords are rejected at knowledge ingestion.
"""
SOURCES = {"interface", "target", "hostname", "pid", "gateway", "external_target"}
RESERVED_CHECKS = {"no_check_needed", "insufficient_context"}


def validate_parameter_schema(schema, sources):
    if schema is None:
        if sources:
            raise ValueError("parameter_sources_require_schema")
        return
    if (set(schema) - {"type", "properties", "required", "additionalProperties"}
            or schema.get("type") != "object" or schema.get("additionalProperties") is not False):
        raise ValueError("parameters_require_closed_object_schema")
    properties, required = schema.get("properties", {}), schema.get("required", [])
    if (not isinstance(properties, dict) or len(properties) > 8 or not isinstance(required, list)
            or any(not isinstance(k, str) for k in required) or len(required) != len(set(required))
            or set(required) - properties.keys() or set(sources) != set(properties)
            or any(source not in SOURCES for source in sources.values())):
        raise ValueError("invalid_parameter_sources")
    for name, spec in properties.items():
        if (not isinstance(name, str) or not name.isidentifier() or len(name) > 64 or not isinstance(spec, dict)
                or set(spec) - {"type", "enum", "minLength", "maxLength", "minimum", "maximum"}
                or spec.get("type") not in ("string", "integer")):
            raise ValueError("unsupported_parameter_schema")
        if spec["type"] == "string":
            if set(spec) & {"minimum", "maximum"}:
                raise ValueError("unsupported_parameter_schema")
            low, high = spec.get("minLength", 0), spec.get("maxLength", 1024)
            if type(low) is not int or type(high) is not int or not 0 <= low <= high <= 1024:
                raise ValueError("invalid_parameter_bounds")
        else:
            if set(spec) & {"minLength", "maxLength"}:
                raise ValueError("unsupported_parameter_schema")
            low, high = spec.get("minimum", 0), spec.get("maximum", 2**31 - 1)
            if type(low) is not int or type(high) is not int or low > high:
                raise ValueError("invalid_parameter_bounds")
        if "enum" in spec:
            if not isinstance(spec["enum"], list) or not spec["enum"] or len(spec["enum"]) > 32:
                raise ValueError("invalid_parameter_enum")
            for value in spec["enum"]:
                validate_parameter(value, {k: v for k, v in spec.items() if k != "enum"})


def validate_parameter(value, spec):
    if spec["type"] == "string":
        valid = (isinstance(value, str) and spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", 1024)
                 and all(ord(c) >= 32 for c in value))
    else:
        valid = type(value) is int and spec.get("minimum", 0) <= value <= spec.get("maximum", 2**31 - 1)
    if not valid or ("enum" in spec and value not in spec["enum"]):
        raise ValueError("check_parameter_schema_mismatch")


def validate_parameters(parameters, schema):
    if schema is None:
        if parameters:
            raise ValueError("check_parameters_without_schema")
        return
    if set(parameters) - schema.get("properties", {}).keys() or set(schema.get("required", [])) - parameters.keys():
        raise ValueError("check_parameter_schema_mismatch")
    for key, value in parameters.items():
        validate_parameter(value, schema["properties"][key])
