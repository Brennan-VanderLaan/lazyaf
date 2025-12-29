# Custom assertion helpers

from .api import (
    assert_created_response,
    assert_deleted_response,
    assert_error_response,
    assert_git_head_response,
    assert_git_info_refs_response,
    assert_git_pack_response,
    assert_ingest_response,
    assert_json_contains,
    assert_json_list_contains,
    assert_json_list_length,
    assert_not_found,
    assert_status_code,
    assert_updated_response,
    assert_validation_error,
)
from .models import (
    assert_enum_value,
    assert_model_fields,
    assert_model_has_id,
    assert_model_has_timestamps,
    assert_schema_invalid,
    assert_schema_valid,
)

__all__ = [
    # API assertions
    "assert_status_code",
    "assert_json_contains",
    "assert_json_list_length",
    "assert_json_list_contains",
    "assert_error_response",
    "assert_created_response",
    "assert_updated_response",
    "assert_deleted_response",
    "assert_not_found",
    "assert_validation_error",
    # Git protocol assertions
    "assert_git_info_refs_response",
    "assert_git_head_response",
    "assert_git_pack_response",
    "assert_ingest_response",
    # Model assertions
    "assert_model_fields",
    "assert_model_has_id",
    "assert_model_has_timestamps",
    "assert_schema_valid",
    "assert_schema_invalid",
    "assert_enum_value",
]
