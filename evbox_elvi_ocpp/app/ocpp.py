"""Thin, pinned python-ocpp 1.6 adapter; no handwritten JSON protocol engine."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from functools import partial
from typing import Any
from uuid import uuid4

from ocpp.charge_point import camel_to_snake_case, remove_nones, snake_to_camel_case
from ocpp.exceptions import OCPPError, ProtocolError
from ocpp.messages import MessageType, unpack
from ocpp.v16 import ChargePoint, call, call_result
from websockets.exceptions import ConnectionClosed

LOGGER = logging.getLogger(__name__)
WIRE_LOGGER = logging.getLogger("app.ocpp.wire")
# python-ocpp logs raw frames at INFO. Keep them opt-in through DEBUG, including
# complete MeterValues/StatusNotification frames needed for Elvi diagnosis.
WIRE_LOGGER.addFilter(
    lambda record: record.levelno >= logging.WARNING or LOGGER.isEnabledFor(logging.DEBUG)
)
CallHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
AfterCallHandler = Callable[[str], Awaitable[None]]
INCOMING_ACTIONS = (
    "Authorize",
    "BootNotification",
    "DataTransfer",
    "DiagnosticsStatusNotification",
    "FirmwareStatusNotification",
    "Heartbeat",
    "MeterValues",
    "StartTransaction",
    "StatusNotification",
    "StopTransaction",
)


class OcppCallError(RuntimeError):
    def __init__(self, code: str, description: str, details: dict[str, Any]) -> None:
        super().__init__(f"{code}: {description}")
        self.code, self.description, self.details = code, description, details


class OcppNotSupportedError(RuntimeError):
    """An unsupported bridge action."""


class OcppConnection(ChargePoint):
    """Library-owned schemas/routing/serialization/correlation, scoped lifecycle.

    The adapter adds only bounded send waits, disconnect cancellation, bounded
    replay of duplicate incoming CALLs, and filtering of late/duplicate replies
    before they enter the library response queue.
    """

    def __init__(
        self, charge_point_id, websocket, call_handler, after_call_handler, command_timeout
    ):
        super().__init__(
            charge_point_id, websocket, response_timeout=command_timeout, logger=WIRE_LOGGER
        )
        self.charge_point_id = charge_point_id
        self._websocket = websocket
        self._call_handler, self._after_call_handler = call_handler, after_call_handler
        self._command_timeout = command_timeout
        self._outbound_lock = asyncio.Lock()
        self._expected_id: str | None = None
        self._response_seen = False
        self._ended = False
        self._call_tasks: set[asyncio.Task[Any]] = set()
        self._incoming: Any = None
        self._responses: OrderedDict[str, tuple[str, dict[str, Any], str]] = OrderedDict()
        # A small explicit route table uses the same handlers/schemas as @on.
        self.route_map = {
            action: {"_on_action": partial(self._dispatch, action=action)}
            for action in INCOMING_ACTIONS
        }

    @property
    def closed(self) -> bool:
        return self._ended or self._websocket.closed

    async def _dispatch(self, *, action: str, **payload):
        response = await self._call_handler(action, snake_to_camel_case(payload))
        return getattr(call_result, action)(**camel_to_snake_case(response))

    async def run(self) -> None:
        try:
            await super().start()
        except ConnectionClosed as error:
            LOGGER.info("OCPP connection closed for %s: %s", self.id, error)
        finally:
            self._end()
            LOGGER.info(
                "OCPP connection ended for %s: code=%s reason=%s",
                self.id,
                getattr(self._websocket, "close_code", None),
                getattr(self._websocket, "close_reason", ""),
            )

    def _end(self) -> None:
        self._ended = True
        for task in tuple(self._call_tasks):
            task.cancel()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._end()
        await self._websocket.close(code=code, reason=reason)

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._outbound_lock:
            if self.closed:
                raise ConnectionError("Charge point is disconnected")
            unique_id = str(uuid4())
            self._expected_id, self._response_seen = unique_id, False
            task = asyncio.current_task()
            self._call_tasks.add(task)
            try:
                request = getattr(call, action)(**camel_to_snake_case(payload))
                LOGGER.info("OCPP command sent: %s id=%s", action, unique_id)
                async with asyncio.timeout(self._command_timeout):
                    response = await super().call(request, suppress=False, unique_id=unique_id)
                return snake_to_camel_case(remove_nones(asdict(response)))
            except OCPPError as error:
                raise OcppCallError(error.code, error.description, error.details) from None
            except asyncio.CancelledError:
                if self.closed:
                    raise ConnectionError("OCPP connection closed during command") from None
                raise
            except TimeoutError:
                LOGGER.warning(
                    "OCPP command timed out: action=%s id=%s; outcome unknown", action, unique_id
                )
                raise
            finally:
                self._expected_id = None
                self._call_tasks.discard(task)

    async def handle_message(self, raw_message) -> None:
        await self.route_message(raw_message)

    async def route_message(self, raw_msg) -> None:
        try:
            message = unpack(raw_msg)
        except (OCPPError, ValueError, TypeError, UnicodeDecodeError):
            LOGGER.warning("Ignoring malformed OCPP frame from %s", self.id)
            return
        if not isinstance(message.unique_id, str) or not 1 <= len(message.unique_id) <= 36:
            LOGGER.warning("Ignoring invalid OCPP message ID from %s", self.id)
            return
        if message.message_type_id in {MessageType.CallResult, MessageType.CallError}:
            if message.unique_id != self._expected_id or self._response_seen:
                LOGGER.warning("Ignoring late/duplicate OCPP response id=%s", message.unique_id)
                return
            self._response_seen = True
            await super().route_message(raw_msg)
            return
        if not isinstance(message.action, str):
            LOGGER.warning("Ignoring invalid OCPP action from %s", self.id)
            return
        cached = self._responses.get(message.unique_id)
        if cached is not None:
            if cached[:2] == (message.action, message.payload):
                await super()._send(cached[2])
            else:
                await super()._send(
                    message.create_call_error(
                        ProtocolError(description="CALL ID reused with different payload")
                    ).to_json()
                )
            return
        LOGGER.info("OCPP message received: %s", message.action)
        self._incoming = message
        try:
            await super().route_message(raw_msg)
        finally:
            self._incoming = None
        cached = self._responses.get(message.unique_id)
        if cached is not None and unpack(cached[2]).message_type_id == MessageType.CallResult:
            await self._after_call_handler(message.action)

    async def _send(self, raw_message: str) -> None:
        # Keep replay bookkeeping independent of JSON parsing/schema handling.
        message = unpack(raw_message)
        incoming = self._incoming
        await super()._send(raw_message)
        if (
            incoming is not None
            and message.message_type_id in {MessageType.CallResult, MessageType.CallError}
            and message.unique_id == incoming.unique_id
        ):
            self._responses[incoming.unique_id] = (incoming.action, incoming.payload, raw_message)
            if len(self._responses) > 32:
                self._responses.popitem(last=False)
