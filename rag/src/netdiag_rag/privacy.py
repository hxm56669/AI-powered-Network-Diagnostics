"""One task-local redaction map shared by query, decision and generation inputs."""
import re


class Redactor:
    def __init__(self, request=None, bundle=None):
        self.values = {}
        if request and request.host_id:
            self.replace(request.host_id, "HOST")
        if request and request.check_context:
            for key in ("hostname", "gateway", "external_target"):
                value = getattr(request.check_context, key)
                if value:
                    self.replace(value, "ADDRESS")
        if bundle:
            for evidence in bundle.evidence:
                for key in ("target", "hostname", "ssid", "bssid", "mac"):
                    value = evidence.scope.get(key)
                    if isinstance(value, str):
                        self.replace(value, "ADDRESS" if key in ("target", "hostname") else key.upper())

    def replace(self, value, category):
        if value not in self.values:
            self.values[value] = f"[{category}_{len(self.values) + 1}]"
        return self.values[value]

    def text(self, value):
        # Do not serialize this reversible map in diagnostics or provider receipts.
        for original in sorted(self.values, key=len, reverse=True):
            value = re.sub(r"(?<![\w])" + re.escape(original) + r"(?![\w])",
                           lambda m: self.values[original], value)
        patterns = [(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", "MAC"),
                    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "ADDRESS"),
                    (r"(?<!\w)(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:]+(?:%[\w.-]+)?", "ADDRESS"),
                    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "EMAIL"),
                    (r"(?i)\b(?:sk-[a-z0-9_-]{12,}|bearer\s+[a-z0-9._-]+)", "SECRET")]
        for pattern, category in patterns:
            value = re.sub(pattern, lambda m: self.replace(m[0], category), value)
        return value

    def __call__(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        return value
