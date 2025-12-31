"""
Custom assertion helpers for API testing.

These helpers provide cleaner, more expressive assertions for common
patterns in API tests.
"""
from typing import Any

from httpx import Response


def assert_status_code(response: Response, expected: int) -> None:
    """Assert response has expected status code with helpful error message."""
    assert response.status_code == expected, (
        f"Expected status {expected}, got {response.status_code}. "
        f"Response body: {response.text}"
    )


def assert_json_contains(response: Response, expected: dict[str, Any] = None, **kwargs) -> None:
    """Assert response JSON contains all expected key-value pairs.

    This allows for partial matching - the response can contain additional
    fields not specified in expected.

    Can be called as:
        assert_json_contains(response, {"name": "value"})
        assert_json_contains(response, name="value")
    """
    if expected is None:
        expected = kwargs
    else:
        expected = {**expected, **kwargs}

    actual = response.json()
    for key, value in expected.items():
        assert key in actual, f"Expected key '{key}' not found in response: {actual}"
        assert actual[key] == value, (
            f"Expected {key}={value!r}, got {key}={actual[key]!r}"
        )


def assert_json_list_length(response: Response, expected_length: int) -> None:
    """Assert response JSON is a list of expected length."""
    actual = response.json()
    assert isinstance(actual, list), f"Expected list, got {type(actual)}"
    assert len(actual) == expected_length, (
        f"Expected {expected_length} items, got {len(actual)}"
    )


def assert_json_list_contains(
    response: Response,
    expected: dict[str, Any],
    key: str = "id",
) -> dict[str, Any]:
    """Assert response JSON list contains an item matching expected.

    Args:
        response: HTTP response
        expected: Dict of key-value pairs to match
        key: Field to use for identifying the match (default: "id")

    Returns:
        The matching item from the list
    """
    actual = response.json()
    assert isinstance(actual, list), f"Expected list, got {type(actual)}"

    for item in actual:
        if all(item.get(k) == v for k, v in expected.items()):
            return item

    raise AssertionError(
        f"No item matching {expected} found in response list. "
        f"Items: {actual}"
    )


def assert_error_response(response: Response, status_code: int, detail: str) -> None:
    """Assert response is an error with expected status and detail message."""
    assert_status_code(response, status_code)
    actual = response.json()
    assert "detail" in actual, f"Expected 'detail' in error response: {actual}"
    assert actual["detail"] == detail, (
        f"Expected detail '{detail}', got '{actual['detail']}'"
    )


def assert_created_response(response: Response, expected: dict[str, Any] = None, **kwargs) -> dict[str, Any]:
    """Assert response is a successful creation (201) with expected fields.

    Returns the full response JSON for further assertions.

    Can be called as:
        assert_created_response(response, {"name": "value"})
        assert_created_response(response, name="value")
        assert_created_response(response)  # Just check 201 and has id
    """
    assert_status_code(response, 201)
    if expected is not None or kwargs:
        assert_json_contains(response, expected, **kwargs)
    actual = response.json()
    assert "id" in actual, "Created response should include 'id'"
    return actual


def assert_updated_response(response: Response, expected: dict[str, Any] = None, **kwargs) -> dict[str, Any]:
    """Assert response is a successful update (200) with expected fields.

    Returns the full response JSON for further assertions.

    Can be called as:
        assert_updated_response(response, {"name": "value"})
        assert_updated_response(response, name="value")
        assert_updated_response(response)  # Just check 200
    """
    assert_status_code(response, 200)
    if expected is not None or kwargs:
        assert_json_contains(response, expected, **kwargs)
    return response.json()


def assert_deleted_response(response: Response) -> None:
    """Assert response is a successful deletion (204)."""
    assert_status_code(response, 204)


def assert_not_found(response: Response, resource_type: str = None) -> None:
    """Assert response is a 404 Not Found error.

    If resource_type is provided, checks for "{resource_type} not found".
    Otherwise, just checks for 404 status and any "not found" message.
    """
    assert_status_code(response, 404)
    actual = response.json()
    assert "detail" in actual, f"Expected 'detail' in error response: {actual}"

    if resource_type:
        expected_detail = f"{resource_type} not found"
        assert actual["detail"] == expected_detail, (
            f"Expected detail '{expected_detail}', got '{actual['detail']}'"
        )
    else:
        assert "not found" in actual["detail"].lower(), (
            f"Expected 'not found' in detail, got '{actual['detail']}'"
        )


def assert_validation_error(response: Response) -> dict[str, Any]:
    """Assert response is a validation error (422).

    Returns the error detail for further inspection.
    """
    assert_status_code(response, 422)
    return response.json()


# -----------------------------------------------------------------------------
# Git Protocol Assertions
# -----------------------------------------------------------------------------

def assert_git_info_refs_response(
    response: Response,
    service: str,
) -> bytes:
    """Assert response is a valid git info/refs response.

    Args:
        response: HTTP response
        service: Expected service (git-upload-pack or git-receive-pack)

    Returns:
        The response content for further inspection
    """
    assert_status_code(response, 200)

    expected_content_type = f"application/x-{service}-advertisement"
    assert response.headers["content-type"] == expected_content_type, (
        f"Expected content-type '{expected_content_type}', "
        f"got '{response.headers['content-type']}'"
    )

    content = response.content
    service_line = f"# service={service}\n".encode()
    assert service_line in content, f"Missing service announcement: {service_line}"
    assert content.endswith(b"0000"), "Response should end with flush packet"

    return content


def assert_git_head_response(response: Response) -> str:
    """Assert response is a valid git HEAD response.

    Returns:
        The HEAD reference (e.g., 'ref: refs/heads/main')
    """
    assert_status_code(response, 200)
    assert "text/plain" in response.headers["content-type"]

    content = response.text
    assert content.startswith("ref: refs/heads/"), (
        f"HEAD should be symbolic ref, got: {content}"
    )
    assert content.endswith("\n"), "HEAD response should end with newline"

    return content.strip()


def assert_git_pack_response(
    response: Response,
    service: str,
) -> bytes:
    """Assert response is a valid git pack response.

    Args:
        response: HTTP response
        service: git-upload-pack or git-receive-pack

    Returns:
        The response content
    """
    assert_status_code(response, 200)

    expected_content_type = f"application/x-{service}-result"
    assert response.headers["content-type"] == expected_content_type, (
        f"Expected content-type '{expected_content_type}', "
        f"got '{response.headers['content-type']}'"
    )

    return response.content


def assert_ingest_response(response: Response, expected_name: str) -> dict[str, Any]:
    """Assert response is a valid ingest response.

    Args:
        response: HTTP response
        expected_name: Expected repo name

    Returns:
        The full response JSON
    """
    assert_status_code(response, 201)

    result = response.json()
    assert "id" in result, "Ingest response should include 'id'"
    assert result["name"] == expected_name, (
        f"Expected name '{expected_name}', got '{result['name']}'"
    )
    assert "internal_git_url" in result, "Ingest response should include 'internal_git_url'"
    assert "clone_url" in result, "Ingest response should include 'clone_url'"

    # Validate URL formats
    assert result["internal_git_url"].startswith("/git/"), (
        f"internal_git_url should start with /git/, got: {result['internal_git_url']}"
    )
    assert result["internal_git_url"].endswith(".git"), (
        f"internal_git_url should end with .git, got: {result['internal_git_url']}"
    )

    return result
