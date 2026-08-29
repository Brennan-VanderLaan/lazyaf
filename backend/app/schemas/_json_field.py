"""
Shared pydantic before-validator for JSON-string columns.

Models store list/dict fields as JSON strings (the Card idiom); Read
schemas parse them back. Unlike the older inlined copies, a malformed
value here LOGS a warning before falling back to the default, so silent
data corruption cannot go dark.

Adopted by schemas/spec.py first; the remaining legacy inlined parsers
(schemas/pipeline.py, schemas/card.py, schemas/job.py, routers/cards.py,
routers/pipelines.py, services/pipeline_executor.py,
services/trigger_service.py) are a later sweep.
"""
import copy
import json
import logging

from pydantic import field_validator

logger = logging.getLogger(__name__)


def json_field_validator(field: str, default):
    """Build a pydantic mode='before' validator that parses `field` from a
    JSON string column, returning a copy of `default` (and logging a
    warning) when the stored value is malformed.

    Usage inside a BaseModel subclass:

        parse_repo_ids = json_field_validator("repo_ids", [])
    """

    def _parse(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Malformed JSON in %s field; falling back to %r "
                    "(stored value: %.200r)",
                    field,
                    default,
                    v,
                )
                return copy.deepcopy(default)
        return v

    return field_validator(field, mode="before")(classmethod(_parse))
