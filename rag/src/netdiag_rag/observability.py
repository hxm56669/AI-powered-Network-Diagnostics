"""JSON application events, deliberately excluding payloads and credentials."""
import logging

from .util import canonical, iso, utcnow


class EventFormatter(logging.Formatter):
    def format(self, record):
        value = {"timestamp": iso(utcnow()), "level": record.levelname,
                 "logger": record.name, "event": record.getMessage()}
        for key in ("trace_id", "job_id", "report_id", "generation_id", "stage", "input_count", "output_count",
                    "cache_hit", "model", "degradations", "error_type", "error_code", "outcome"):
            if hasattr(record, key):
                value[key] = getattr(record, key)
        if record.exc_info:
            value["error_type"] = record.exc_info[0].__name__
        return canonical(value)


def configure_logging():
    logger = logging.getLogger("netdiag_rag")
    handler = logging.StreamHandler()
    handler.setFormatter(EventFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
