# Control Layer services - Phase 12.3
# Provides container-to-backend communication and step management

from app.services.control_layer.auth import (
    generate_step_token,
    validate_step_token,
)

from app.services.control_layer.protocol import (
    StepConfig,
    ControlLayerClient,
    StepExecutor,
    StepTimeoutError,
)

from app.services.control_layer.environment import (
    get_step_environment,
)

from app.services.control_layer.workspace import (
    WorkspaceLayout,
    generate_step_config,
    write_step_config,
    initialize_workspace,
)

from app.services.control_layer.docker import (
    get_volume_mounts,
)

from app.services.control_layer.image import (
    IMAGE_NAMES,
    BaseImageContract,
    get_image_tag,
    generate_base_dockerfile,
    generate_claude_dockerfile,
    generate_gemini_dockerfile,
    get_control_layer_script,
)

__all__ = [
    # Auth
    "generate_step_token",
    "validate_step_token",
    # Protocol
    "StepConfig",
    "ControlLayerClient",
    "StepExecutor",
    "StepTimeoutError",
    # Environment
    "get_step_environment",
    # Workspace
    "WorkspaceLayout",
    "generate_step_config",
    "write_step_config",
    "initialize_workspace",
    # Docker
    "get_volume_mounts",
    # Image
    "IMAGE_NAMES",
    "BaseImageContract",
    "get_image_tag",
    "generate_base_dockerfile",
    "generate_claude_dockerfile",
    "generate_gemini_dockerfile",
    "get_control_layer_script",
]
