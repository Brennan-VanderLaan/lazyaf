"""Auth headers for the endpoints that require a credential.

One source of truth (R3): the moment a route gains a credential, every test
that drives it legitimately needs the same header, and two copies of that
header drift the first time the scheme changes.
"""

from app.config import get_settings


def push_event_auth() -> dict[str, str]:
    """The header `POST /git/{id}.git/_internal/push-event` requires.

    That route used to take no credential at all, which meant an anonymous
    caller could FORGE a push - naming any branch and any sha - and make the
    platform materialize repo pipeline definitions and spawn containers
    without pushing anything, without a credential, and without changing a
    byte on disk. `_internal` was a name, not an enforcement.

    It takes the runner secret now. Tests that drive it are the legitimate
    caller and use this; the refusal itself is pinned by
    `tdd/integration/api/test_pipeline_sync_on_push.py::TestThePushEventEndpointIsAuthenticated`.
    """
    return {"Authorization": f"Bearer {get_settings().runner_auth_secret}"}
