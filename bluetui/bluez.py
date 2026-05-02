"""Async D-Bus wrapper around BlueZ.

The BluezManager class connects to the system bus, locates an adapter,
loads existing devices, and emits callback events when state changes:

    bz.on("adapter_changed", cb)   # cb(props_dict)
    bz.on("device_added", cb)      # cb(path, props_dict)
    bz.on("device_removed", cb)    # cb(path)
    bz.on("device_changed", cb)    # cb(path, props_dict)

All public actions are coroutines and may raise dbus_next.DBusError.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ROOT = "/"
BLUEZ_ROOT = "/org/bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
BATTERY_IFACE = "org.bluez.Battery1"
MEDIA_CONTROL_IFACE = "org.bluez.MediaControl1"
MEDIA_PLAYER_IFACE = "org.bluez.MediaPlayer1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
AGENT_MGR_IFACE = "org.bluez.AgentManager1"


class BluezError(Exception):
    pass


def _unvariant(d: dict) -> dict:
    """Convert {key: Variant} to a plain dict of native Python values."""
    return {k: (v.value if isinstance(v, Variant) else v) for k, v in d.items()}


class BluezManager:
    def __init__(self) -> None:
        self.bus: MessageBus | None = None
        self.adapter_path: str | None = None
        self.adapter_props: dict[str, Any] = {}
        self._adapter_iface = None  # Adapter1 proxy
        self._adapter_props_iface = None  # Properties proxy on adapter
        self.devices: dict[str, dict[str, Any]] = {}
        self._device_ifaces: dict[str, Any] = {}  # path -> Device1 iface
        self._device_prop_ifaces: dict[str, Any] = {}  # path -> Properties iface
        self._media_control: dict[str, Any] = {}  # path -> MediaControl1 iface
        self._media_player: dict[str, Any] = {}   # player path -> MediaPlayer1 iface
        self._device_players: dict[str, set[str]] = {}  # device path -> player paths
        self._listeners: dict[str, list[Callable]] = {
            "adapter_changed": [],
            "device_added": [],
            "device_removed": [],
            "device_changed": [],
        }

    # ------------------------------------------------------------------ events

    def on(self, event: str, callback: Callable) -> None:
        self._listeners.setdefault(event, []).append(callback)

    def _emit(self, event: str, *args) -> None:
        for cb in self._listeners.get(event, []):
            try:
                cb(*args)
            except Exception:
                log.exception("listener for %s failed", event)

    # --------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        intro = await self.bus.introspect(BLUEZ, ROOT)
        root_obj = self.bus.get_proxy_object(BLUEZ, ROOT, intro)
        om = root_obj.get_interface(OM_IFACE)

        om.on_interfaces_added(self._on_interfaces_added)
        om.on_interfaces_removed(self._on_interfaces_removed)

        objects = await om.call_get_managed_objects()
        await self._load_initial(objects)

    async def disconnect(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None

    async def _load_initial(self, objects: dict) -> None:
        # First adapter wins.
        for path, ifaces in objects.items():
            if ADAPTER_IFACE in ifaces:
                await self._set_adapter(path, ifaces[ADAPTER_IFACE])
                break
        if self.adapter_path is None:
            raise BluezError(
                "Bluetooth adapter not found. Is the bluetooth service running?"
            )
        for path, ifaces in objects.items():
            if DEVICE_IFACE in ifaces and path.startswith(self.adapter_path + "/"):
                # Only direct device children, not GATT subpaths.
                if path.count("/") == self.adapter_path.count("/") + 1:
                    await self._add_device(
                        path,
                        ifaces[DEVICE_IFACE],
                        battery_props=ifaces.get(BATTERY_IFACE),
                    )

    async def _set_adapter(self, path: str, props_dict: dict) -> None:
        self.adapter_path = path
        self.adapter_props = _unvariant(props_dict)
        intro = await self.bus.introspect(BLUEZ, path)
        obj = self.bus.get_proxy_object(BLUEZ, path, intro)
        self._adapter_iface = obj.get_interface(ADAPTER_IFACE)
        self._adapter_props_iface = obj.get_interface(PROPS_IFACE)
        self._adapter_props_iface.on_properties_changed(self._on_adapter_props)

    def _on_adapter_props(self, iface: str, changed: dict, _invalidated) -> None:
        if iface != ADAPTER_IFACE:
            return
        for k, v in changed.items():
            self.adapter_props[k] = v.value if isinstance(v, Variant) else v
        self._emit("adapter_changed", self.adapter_props)

    # ---------------------------------------------------------------- devices

    async def _add_device(
        self,
        path: str,
        props_dict: dict | None,
        battery_props: dict | None = None,
    ) -> None:
        if path in self.devices:
            return
        props = _unvariant(props_dict or {})
        if battery_props:
            bp = _unvariant(battery_props)
            if "Percentage" in bp:
                props["Battery"] = bp["Percentage"]
        self.devices[path] = props

        try:
            intro = await self.bus.introspect(BLUEZ, path)
            obj = self.bus.get_proxy_object(BLUEZ, path, intro)
        except Exception:
            log.exception("failed to introspect %s", path)
            self._emit("device_added", path, props)
            return

        try:
            self._device_ifaces[path] = obj.get_interface(DEVICE_IFACE)
        except Exception:
            log.exception("no Device1 interface on %s", path)

        try:
            propsi = obj.get_interface(PROPS_IFACE)
            propsi.on_properties_changed(self._make_props_handler(path))
            self._device_prop_ifaces[path] = propsi
        except Exception:
            log.exception("failed to subscribe to props for %s", path)

        # Battery info if not already loaded.
        if "Battery" not in props:
            try:
                batt = obj.get_interface(BATTERY_IFACE)
                props["Battery"] = await batt.get_percentage()
            except Exception:
                pass

        # MediaControl1 (legacy AVRCP). Available on most audio devices.
        try:
            self._media_control[path] = obj.get_interface(MEDIA_CONTROL_IFACE)
            props["_HasMediaControl"] = True
        except Exception:
            pass

        self._emit("device_added", path, props)

    def _make_props_handler(self, path: str):
        """Return a 3-arg callback bound to a specific device path.

        dbus-next strictly validates that the handler's parameter count
        matches the signal's argument count, so we cannot use a lambda
        with default arguments here.
        """
        def handler(iface, changed, invalidated):
            self._on_device_props(path, iface, changed, invalidated)
        return handler

    def _on_device_props(self, path: str, iface: str, changed: dict, _inv) -> None:
        if path not in self.devices:
            return
        props = self.devices[path]
        if iface == DEVICE_IFACE:
            for k, v in changed.items():
                props[k] = v.value if isinstance(v, Variant) else v
        elif iface == BATTERY_IFACE and "Percentage" in changed:
            v = changed["Percentage"]
            props["Battery"] = v.value if isinstance(v, Variant) else v
        else:
            return
        self._emit("device_changed", path, props)

    def _on_interfaces_added(self, path: str, ifaces: dict) -> None:
        if not self.adapter_path:
            return
        # Direct device path: /org/bluez/hciN/dev_XX_XX_...
        if (
            DEVICE_IFACE in ifaces
            and path.startswith(self.adapter_path + "/")
            and path.count("/") == self.adapter_path.count("/") + 1
        ):
            asyncio.create_task(
                self._add_device(
                    path,
                    ifaces[DEVICE_IFACE],
                    battery_props=ifaces.get(BATTERY_IFACE),
                )
            )
            return
        if BATTERY_IFACE in ifaces and path in self.devices:
            bp = _unvariant(ifaces[BATTERY_IFACE])
            if "Percentage" in bp:
                self.devices[path]["Battery"] = bp["Percentage"]
                self._emit("device_changed", path, self.devices[path])
            return
        # MediaPlayer1 sits at /org/bluez/hciN/dev_XX_.../playerN
        if MEDIA_PLAYER_IFACE in ifaces:
            parent = path.rsplit("/", 1)[0]
            if parent in self.devices:
                asyncio.create_task(self._attach_player(parent, path))

    def _on_interfaces_removed(self, path: str, ifaces: list[str]) -> None:
        if DEVICE_IFACE in ifaces and path in self.devices:
            del self.devices[path]
            self._device_ifaces.pop(path, None)
            self._device_prop_ifaces.pop(path, None)
            self._media_control.pop(path, None)
            for player_path in self._device_players.pop(path, set()):
                self._media_player.pop(player_path, None)
            self._emit("device_removed", path)
            return
        if BATTERY_IFACE in ifaces and path in self.devices:
            self.devices[path].pop("Battery", None)
            self._emit("device_changed", path, self.devices[path])
            return
        if MEDIA_PLAYER_IFACE in ifaces:
            self._media_player.pop(path, None)
            for parent, players in self._device_players.items():
                if path in players:
                    players.discard(path)
                    if not players:
                        self.devices[parent].pop("_HasMediaPlayer", None)
                    self._emit("device_changed", parent, self.devices[parent])
                    break

    async def _attach_player(self, device_path: str, player_path: str) -> None:
        try:
            intro = await self.bus.introspect(BLUEZ, player_path)
            obj = self.bus.get_proxy_object(BLUEZ, player_path, intro)
            self._media_player[player_path] = obj.get_interface(MEDIA_PLAYER_IFACE)
            self._device_players.setdefault(device_path, set()).add(player_path)
            self.devices[device_path]["_HasMediaPlayer"] = True
            self._emit("device_changed", device_path, self.devices[device_path])
        except Exception:
            log.exception("failed to attach player %s", player_path)

    # ----------------------------------------------------------------- actions

    async def start_discovery(self) -> None:
        await self._adapter_iface.call_start_discovery()

    async def stop_discovery(self) -> None:
        await self._adapter_iface.call_stop_discovery()

    async def set_powered(self, on: bool) -> None:
        await self._adapter_props_iface.call_set(
            ADAPTER_IFACE, "Powered", Variant("b", on)
        )

    async def set_discoverable(self, on: bool) -> None:
        await self._adapter_props_iface.call_set(
            ADAPTER_IFACE, "Discoverable", Variant("b", on)
        )

    async def set_pairable(self, on: bool) -> None:
        await self._adapter_props_iface.call_set(
            ADAPTER_IFACE, "Pairable", Variant("b", on)
        )

    async def connect_device(self, path: str) -> None:
        await self._device_ifaces[path].call_connect()

    async def disconnect_device(self, path: str) -> None:
        await self._device_ifaces[path].call_disconnect()

    async def pair_device(self, path: str) -> None:
        await self._device_ifaces[path].call_pair()

    async def cancel_pairing(self, path: str) -> None:
        await self._device_ifaces[path].call_cancel_pairing()

    async def remove_device(self, path: str) -> None:
        await self._adapter_iface.call_remove_device(path)

    async def set_trusted(self, path: str, trusted: bool) -> None:
        propsi = self._device_prop_ifaces.get(path)
        if propsi is None:
            return
        await propsi.call_set(DEVICE_IFACE, "Trusted", Variant("b", trusted))

    async def set_blocked(self, path: str, blocked: bool) -> None:
        propsi = self._device_prop_ifaces.get(path)
        if propsi is None:
            return
        await propsi.call_set(DEVICE_IFACE, "Blocked", Variant("b", blocked))

    # ----------------------------------------------------------- AVRCP / media

    def has_media(self, path: str) -> bool:
        """True if the device exposes a MediaControl1 or MediaPlayer1 iface."""
        if path in self._media_control:
            return True
        return bool(self._device_players.get(path))

    def _player_iface_for(self, device_path: str):
        """Return the first MediaPlayer1 proxy for a device, or None."""
        players = self._device_players.get(device_path)
        if not players:
            return None
        # Pick any (usually only one)
        for pp in players:
            iface = self._media_player.get(pp)
            if iface is not None:
                return iface
        return None

    async def _media_call(self, device_path: str, player_method: str,
                           control_method: str | None = None) -> None:
        """Dispatch an AVRCP command via MediaPlayer1, falling back to MediaControl1."""
        iface = self._player_iface_for(device_path)
        if iface is not None:
            method = getattr(iface, f"call_{player_method}", None)
            if method is not None:
                await method()
                return
        # Fall back to legacy MediaControl1.
        ctrl = self._media_control.get(device_path)
        if ctrl is None:
            raise BluezError("устройство не поддерживает AVRCP")
        m_name = control_method or player_method
        method = getattr(ctrl, f"call_{m_name}", None)
        if method is None:
            raise BluezError(f"команда '{m_name}' недоступна")
        await method()

    async def media_play(self, path: str) -> None:
        await self._media_call(path, "play")

    async def media_pause(self, path: str) -> None:
        await self._media_call(path, "pause")

    async def media_stop(self, path: str) -> None:
        await self._media_call(path, "stop")

    async def media_next(self, path: str) -> None:
        await self._media_call(path, "next")

    async def media_previous(self, path: str) -> None:
        await self._media_call(path, "previous")

    async def media_fast_forward(self, path: str) -> None:
        await self._media_call(path, "fast_forward")

    async def media_rewind(self, path: str) -> None:
        await self._media_call(path, "rewind")

    async def media_volume_up(self, path: str) -> None:
        # Volume up/down are MediaControl1 only (legacy).
        ctrl = self._media_control.get(path)
        if ctrl is None:
            raise BluezError("устройство не поддерживает регулировку громкости через AVRCP")
        await ctrl.call_volume_up()

    async def media_volume_down(self, path: str) -> None:
        ctrl = self._media_control.get(path)
        if ctrl is None:
            raise BluezError("устройство не поддерживает регулировку громкости через AVRCP")
        await ctrl.call_volume_down()
