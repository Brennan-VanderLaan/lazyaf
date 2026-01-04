"""
WebSocket endpoint for remote runners (Phase 12.6).

Handles WebSocket connections from runner agents, allowing them to:
- Register with the backend
- Receive job assignments immediately (push, not poll)
- Send heartbeats, logs, and completion status
"""

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.execution.remote_executor import get_remote_executor
from app.services.execution.runner_protocol import (
    parse_runner_message,
    validate_runner_message,
    REGISTRATION_TIMEOUT,
    HEARTBEAT_INTERVAL,
    RegisterMessage,
    AckMessage,
    HeartbeatMessage,
    LogMessage,
    StepCompleteMessage,
)
from app.services.websocket import manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/runner")
async def runner_websocket(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    """
    WebSocket endpoint for runner connections.

    Protocol:
    1. Runner connects
    2. Runner sends 'register' message within 10s
    3. Backend validates and sends 'registered'
    4. Runner is now idle, may receive 'execute_step' at any time
    5. Runner must send heartbeat every 10s
    6. Backend may push jobs immediately when runner is idle

    Message format: JSON with 'type' field
    """
    await websocket.accept()
    remote_executor = get_remote_executor()
    runner_id = None

    try:
        # ====================================================================
        # Phase 1: Wait for registration
        # ====================================================================
        try:
            data = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=REGISTRATION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Runner registration timeout")
            await websocket.send_json({
                "type": "error",
                "message": "Registration timeout",
            })
            await websocket.close(code=4000, reason="Registration timeout")
            return

        # Validate registration message
        errors = validate_runner_message(data)
        if errors:
            logger.warning(f"Invalid registration: {errors}")
            await websocket.send_json({
                "type": "error",
                "message": f"Invalid registration: {', '.join(errors)}",
            })
            await websocket.close(code=4001, reason="Invalid registration")
            return

        if data.get("type") != "register":
            logger.warning(f"Expected register, got: {data.get('type')}")
            await websocket.send_json({
                "type": "error",
                "message": "Expected register message",
            })
            await websocket.close(code=4001, reason="Expected register message")
            return

        # Parse registration message
        msg = parse_runner_message(data)
        if not isinstance(msg, RegisterMessage):
            await websocket.close(code=4001, reason="Invalid register message")
            return

        # Use provided runner_id or generate one
        runner_id = msg.runner_id or str(uuid4())

        # Register runner
        runner = await remote_executor.register_runner(
            db=db,
            websocket=websocket,
            runner_id=runner_id,
            name=msg.name,
            runner_type=msg.runner_type,
            labels=msg.labels,
        )

        # Send registered confirmation
        await websocket.send_json({
            "type": "registered",
            "runner_id": runner.id,
        })

        logger.info(f"Runner {runner_id} registered: {msg.name}")

        # Broadcast runner status
        await manager.send_runner_status({
            "id": runner.id,
            "name": runner.name,
            "status": runner.status,
            "runner_type": runner.runner_type,
        })

        # ====================================================================
        # Phase 2: Main message loop
        # ====================================================================
        while True:
            try:
                # Wait for message with heartbeat timeout
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=HEARTBEAT_INTERVAL * 2,  # Allow some slack
                )
            except asyncio.TimeoutError:
                # Send ping to keep alive
                try:
                    await websocket.send_json({"type": "ping"})
                    continue
                except Exception:
                    break
            except WebSocketDisconnect:
                break

            # Process message
            await handle_runner_message(db, runner_id, data, websocket)

    except WebSocketDisconnect:
        logger.info(f"Runner {runner_id} disconnected")
    except Exception as e:
        logger.error(f"Runner WebSocket error: {e}")
    finally:
        # Clean up on disconnect
        if runner_id:
            await remote_executor.handle_disconnect(db, runner_id)

            # Broadcast runner status
            await manager.send_runner_status({
                "id": runner_id,
                "status": "disconnected",
            })


async def handle_runner_message(
    db: AsyncSession,
    runner_id: str,
    data: dict,
    websocket: WebSocket,
) -> None:
    """
    Handle a message from a connected runner.

    Args:
        db: Database session
        runner_id: Runner ID
        data: JSON message data
        websocket: WebSocket connection
    """
    remote_executor = get_remote_executor()

    # Validate message
    errors = validate_runner_message(data)
    if errors:
        await websocket.send_json({
            "type": "error",
            "message": f"Invalid message: {', '.join(errors)}",
        })
        return

    msg_type = data.get("type")

    try:
        msg = parse_runner_message(data)
    except ValueError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
        })
        return

    if isinstance(msg, AckMessage):
        await remote_executor.handle_ack(runner_id, msg.step_id)
        logger.debug(f"Runner {runner_id} ACKed step {msg.step_id}")

    elif isinstance(msg, HeartbeatMessage):
        await remote_executor.handle_heartbeat(db, runner_id)
        await websocket.send_json({"type": "pong"})

    elif isinstance(msg, LogMessage):
        # Broadcast logs
        await manager.broadcast("step_logs", {
            "runner_id": runner_id,
            "step_id": msg.step_id,
            "lines": msg.lines,
        })

    elif isinstance(msg, StepCompleteMessage):
        await remote_executor.handle_step_complete(
            db=db,
            runner_id=runner_id,
            step_id=msg.step_id,
            exit_code=msg.exit_code,
            error=msg.error,
        )

        # Broadcast step completion
        await manager.broadcast("step_status", {
            "runner_id": runner_id,
            "step_id": msg.step_id,
            "status": "completed" if msg.exit_code == 0 else "failed",
            "exit_code": msg.exit_code,
            "error": msg.error,
        })

    else:
        logger.warning(f"Unhandled message type from {runner_id}: {msg_type}")
