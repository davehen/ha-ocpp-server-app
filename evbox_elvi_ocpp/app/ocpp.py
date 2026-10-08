"""Minimal, frozen OCPP 1.6J WebSocket transport."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from websockets.legacy.server import WebSocketServerProtocol

LOGGER = logging.getLogger(__name__)
CallHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
AfterCallHandler = Callable[[str], Awaitable[None]]


class OcppCallError(RuntimeError):
    """A CALLERROR received from the charge point."""

    def __init__(self, code: str, description: str, details: dict[str, Any]) -> None:
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.details = details


class OcppNotSupportedError(RuntimeError):
    """An unsupported charge-point initiated action."""


class OcppConnection:
    """One OCPP-J 1.6 connection with request/response correlation."""

    CALL = 2
    CALL_RESULT = 3
    CALL_ERROR = 4

    def __init__(
        self,
        charge_point_id: str,
        websocket: WebSocketServerProtocol,
        call_handler: CallHandler,
        after_call_handler: AfterCallHandler,
        command_timeout: int,
    ) -> None:
        self.charge_point_id = charge_point_id
        self._websocket = websocket
        self._call_handler = call_handler
        self._after_call_handler = after_call_handler
        self._command_timeout = command_timeout
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._responses: OrderedDict[str, tuple[str, dict[str, Any], dict[str, Any]]] = (
            OrderedDict()
        )

    @property
    def closed(self) -> bool:
        """Return whether the underlying WebSocket is closed."""
        return self._websocket.closed

    async def run(self) -> None:
        """Process messages until the charge point disconnects."""
        from websockets.exceptions import ConnectionClosed

        try:
            async for raw_message in self._websocket:
                await self.handle_message(raw_message)
        except ConnectionClosed as error:
            LOGGER.info("OCPP connection closed for %s: %s", self.charge_point_id, error)
        finally:
            LOGGER.info(
                "OCPP connection ended for %s: code=%s reason=%s pending=%s",
                self.charge_point_id,
                getattr(self._websocket, "close_code", None),
                getattr(self._websocket, "close_reason", ""),
                len(self._pending),
            )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("OCPP connection closed"))
            self._pending.clear()

    async def handle_message(self, raw_message: str) -> None:
        """Handle one OCPP-J frame."""
        try:
            message = json.loads(raw_message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            LOGGER.warning("Ignoring malformed JSON from %s", self.charge_point_id)
            return
        if not isinstance(message, list) or not message:
            LOGGER.warning("Ignoring malformed OCPP frame from %s", self.charge_point_id)
            return
        message_type = message[0]
        if message_type == self.CALL and len(message) == 4:
            await self._handle_call(str(message[1]), str(message[2]), message[3])
        elif message_type == self.CALL_RESULT and len(message) == 3:
            self._resolve_result(str(message[1]), message[2])
        elif message_type == self.CALL_ERROR and len(message) == 5:
            self._resolve_error(str(message[1]), str(message[2]), str(message[3]), message[4])
        else:
            LOGGER.warning(
                "Ignoring unsupported OCPP frame from %s: %s",
                self.charge_point_id,
                message,
            )

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a CALL and await its correlated response."""
        async with self._call_lock:
            return await self._call(action, payload)

    async def _call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise ConnectionError("Charge point is not connected")
        unique_id = str(uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[unique_id] = future
        try:
            async with asyncio.timeout(self._command_timeout):
                await self._send([self.CALL, unique_id, action, payload])
                LOGGER.info("OCPP command sent: %s", action)
                return await future
        except TimeoutError:
            LOGGER.warning("OCPP command timed out: action=%s id=%s", action, unique_id)
            raise
        finally:
            self._pending.pop(unique_id, None)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the WebSocket."""
        await self._websocket.close(code=code, reason=reason)

    async def _handle_call(self, unique_id: str, action: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            await self._send_error(
                unique_id,
                "TypeConstraintViolation",
                "Payload must be an object",
            )
            return
        cached = self._responses.get(unique_id)
        if cached is not None:
            if cached[:2] != (action, payload):
                await self._send_error(
                    unique_id, "ProtocolError", "CALL id reused with different data"
                )
            else:
                await self._send([self.CALL_RESULT, unique_id, cached[2]])
            return
        LOGGER.info("OCPP message received: %s", action)
        if action in {"MeterValues", "StatusNotification"}:
            LOGGER.debug("OCPP %s payload: %s", action, json.dumps(payload))
        try:
            response = await self._call_handler(action, payload)
        except OcppNotSupportedError as error:
            await self._send_error(unique_id, "NotSupported", str(error))
            return
        except Exception as error:
            LOGGER.exception("Failed to handle OCPP action %s", action)
            await self._send_error(unique_id, "InternalError", str(error))
            return
        self._responses[unique_id] = (action, payload, response)
        if len(self._responses) > 32:
            self._responses.popitem(last=False)
        await self._send([self.CALL_RESULT, unique_id, response])
        await self._after_call_handler(action)

    def _resolve_result(self, unique_id: str, payload: Any) -> None:
        future = self._pending.get(unique_id)
        if future is None or future.done():
            LOGGER.warning("Received response for unknown OCPP call %s", unique_id)
            return
        if not isinstance(payload, dict):
            future.set_exception(TypeError("OCPP CALLRESULT payload must be an object"))
            return
        future.set_result(payload)

    def _resolve_error(self, unique_id: str, code: str, description: str, details: Any) -> None:
        future = self._pending.get(unique_id)
        if future is None or future.done():
            LOGGER.warning("Received error for unknown OCPP call %s", unique_id)
            return
        future.set_exception(
            OcppCallError(code, description, details if isinstance(details, dict) else {})
        )

    async def _send_error(self, unique_id: str, code: str, description: str) -> None:
        await self._send([self.CALL_ERROR, unique_id, code, description, {}])

    async def _send(self, message: list[Any]) -> None:
        encoded = json.dumps(message, separators=(",", ":"), allow_nan=False)
        if len(message) == 4 and message[2] in {"SetChargingProfile", "RemoteStartTransaction"}:
            LOGGER.debug("OCPP command frame: %s", encoded)
        async with self._send_lock:
            await self._websocket.send(encoded)
