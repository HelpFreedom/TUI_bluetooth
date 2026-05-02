"""BlueZ pairing agent.

Implements org.bluez.Agent1 and forwards interactive prompts to a
user-supplied async callback. The callback receives:

    (kind, device_path, value) -> result

where kind is one of:
    'pin'           — return string PIN, or None to reject
    'passkey'       — return int passkey, or None to reject
    'display_pin'   — pincode (str) is shown to user; return ignored
    'display_passkey' — value is (passkey, entered); return ignored
    'confirm'       — return True to accept passkey, False to reject
    'authorize'     — return True to authorize incoming pairing
    'authorize_service' — value is service uuid; return True to allow
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from dbus_next import DBusError
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

log = logging.getLogger(__name__)

AGENT_PATH = "/com/bluetui/agent"


class _Rejected(DBusError):
    def __init__(self, msg: str = "rejected"):
        super().__init__("org.bluez.Error.Rejected", msg)


class PairingAgent(ServiceInterface):
    """Bluetooth pairing agent that delegates to an async callback."""

    def __init__(self, on_request: Callable[..., Awaitable]):
        super().__init__("org.bluez.Agent1")
        self._on_request = on_request

    @method()
    def Release(self):  # noqa: N802 (D-Bus method name)
        log.debug("agent released")

    @method()
    async def RequestPinCode(self, device: "o") -> "s":  # noqa: N802
        result = await self._on_request("pin", device, None)
        if result is None:
            raise _Rejected()
        return str(result)

    @method()
    async def RequestPasskey(self, device: "o") -> "u":  # noqa: N802
        result = await self._on_request("passkey", device, None)
        if result is None:
            raise _Rejected()
        return int(result)

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802
        # Fire-and-forget UI update; we cannot await in a sync method.
        import asyncio
        asyncio.create_task(self._on_request("display_pin", device, pincode))

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802
        import asyncio
        asyncio.create_task(
            self._on_request("display_passkey", device, (passkey, entered))
        )

    @method()
    async def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802
        ok = await self._on_request("confirm", device, passkey)
        if not ok:
            raise _Rejected()

    @method()
    async def RequestAuthorization(self, device: "o"):  # noqa: N802
        ok = await self._on_request("authorize", device, None)
        if not ok:
            raise _Rejected()

    @method()
    async def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802
        ok = await self._on_request("authorize_service", device, uuid)
        if not ok:
            raise _Rejected()

    @method()
    def Cancel(self):  # noqa: N802
        log.debug("agent cancelled")


async def register_agent(
    bus: MessageBus,
    agent: PairingAgent,
    capability: str = "KeyboardDisplay",
) -> tuple[bool, str]:
    """Register the agent with BlueZ. Returns (success, message)."""
    try:
        bus.export(AGENT_PATH, agent)
    except Exception as e:
        return False, f"could not export agent: {e}"
    try:
        intro = await bus.introspect("org.bluez", "/org/bluez")
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", intro)
        mgr = obj.get_interface("org.bluez.AgentManager1")
        await mgr.call_register_agent(AGENT_PATH, capability)
        try:
            await mgr.call_request_default_agent(AGENT_PATH)
        except Exception:
            # Not fatal — another agent may stay default.
            pass
        return True, "registered"
    except DBusError as e:
        return False, e.text or str(e)
    except Exception as e:
        return False, str(e)


async def unregister_agent(bus: MessageBus) -> None:
    try:
        intro = await bus.introspect("org.bluez", "/org/bluez")
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", intro)
        mgr = obj.get_interface("org.bluez.AgentManager1")
        await mgr.call_unregister_agent(AGENT_PATH)
    except Exception:
        pass
    try:
        bus.unexport(AGENT_PATH)
    except Exception:
        pass
