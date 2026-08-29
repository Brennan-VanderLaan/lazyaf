# Control Layer services - Phase 12.3
#
# What lives here after the wave2-123-wiring retirement:
# - auth.py:      step token generation/validation (the /api/steps/* router
#                 dependency; secret settings-driven via main.py startup)
# - workspace.py: generate_step_config - the SINGLE producer of the step
#                 config file contract consumed by the in-container runtime
#                 at images/base/control/ (R3)
#
# Retired (see upcoming/wave2-123-wiring.md section 3): protocol.py /
# docker.py / environment.py (superseded by the real images/ runtime,
# LocalExecutor MountSpec machinery, and ENV baked into the base image) and
# image.py's Dockerfile string generators (superseded by the images/ tree).
# Also retired (12.3 dead-code sweep): workspace.py's WorkspaceLayout /
# initialize_workspace / get_workspace_paths / write_step_config -
# entrypoint.sh is the single HOME-skeleton owner.

from app.services.control_layer.auth import (
    generate_step_token,
    validate_step_token,
)

from app.services.control_layer.workspace import generate_step_config

__all__ = [
    # Auth
    "generate_step_token",
    "validate_step_token",
    # Workspace
    "generate_step_config",
]
