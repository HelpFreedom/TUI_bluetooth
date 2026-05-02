"""BLE HID Remote — emulate a Bluetooth remote control via the GATT HID profile.

Registers a GATT application with HID, Battery, and Device Information
services on the system bus, plus an LE advertisement. Once a TV / set-top
box pairs and connects (it acts as the BLE central, we are the peripheral),
button presses are sent as HID input reports through notifications on the
Report characteristics.

Two HID reports are exposed:

* Consumer Control (Report ID 1) — 16-bit usage codes for Power, Vol±, Mute,
  Play/Pause, Next/Prev, Home, Back, Menu, Channel±, Search.
* Keyboard (Report ID 2) — 8-byte HID keyboard report for D-pad arrows,
  Enter (OK), Esc, Backspace, etc.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Awaitable, Callable, Optional

from dbus_next import DBusError, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method

log = logging.getLogger(__name__)

# Standard 16-bit UUIDs expanded to 128-bit form (BlueZ wants strings).
HID_SERVICE_UUID = "00001812-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
DEVICE_INFO_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"

HID_INFO_UUID = "00002a4a-0000-1000-8000-00805f9b34fb"
REPORT_MAP_UUID = "00002a4b-0000-1000-8000-00805f9b34fb"
HID_CONTROL_POINT_UUID = "00002a4c-0000-1000-8000-00805f9b34fb"
REPORT_UUID = "00002a4d-0000-1000-8000-00805f9b34fb"
PROTOCOL_MODE_UUID = "00002a4e-0000-1000-8000-00805f9b34fb"
PNP_ID_UUID = "00002a50-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
BOOT_KBD_INPUT_UUID = "00002a22-0000-1000-8000-00805f9b34fb"
BOOT_KBD_OUTPUT_UUID = "00002a32-0000-1000-8000-00805f9b34fb"

REPORT_REFERENCE_UUID = "00002908-0000-1000-8000-00805f9b34fb"

GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHARACTERISTIC_IFACE = "org.bluez.GattCharacteristic1"
GATT_DESCRIPTOR_IFACE = "org.bluez.GattDescriptor1"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"

APP_PATH = "/com/bluetui/remote"
ADV_PATH = "/com/bluetui/remote/advertisement"


# ---------------------------------------------------------------------------
# HID command codes
# ---------------------------------------------------------------------------


class CC:
    """Consumer Control codes (HID Usage Page 0x0C)."""

    POWER = 0x0030
    SLEEP = 0x0032
    PLAY_PAUSE = 0x00CD
    FAST_FORWARD = 0x00B3
    REWIND = 0x00B4
    NEXT_TRACK = 0x00B5
    PREV_TRACK = 0x00B6
    STOP = 0x00B7
    MUTE = 0x00E2
    VOLUME_UP = 0x00E9
    VOLUME_DOWN = 0x00EA
    MENU = 0x0040
    HOME = 0x0223
    BACK = 0x0224
    CHANNEL_UP = 0x009C
    CHANNEL_DOWN = 0x009D
    AC_SEARCH = 0x0221


class KB:
    """Keyboard scancodes (HID Usage Page 0x07)."""

    ENTER = 0x28
    ESC = 0x29
    BACKSPACE = 0x2A
    TAB = 0x2B
    SPACE = 0x2C
    RIGHT = 0x4F
    LEFT = 0x50
    DOWN = 0x51
    UP = 0x52


# ---------------------------------------------------------------------------
# HID Report Descriptor
# ---------------------------------------------------------------------------

HID_REPORT_MAP = bytes([
    # ---- Consumer Control (Report ID 1) ---------------------------------
    0x05, 0x0C,        # Usage Page (Consumer)
    0x09, 0x01,        # Usage (Consumer Control)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x19, 0x00,        #   Usage Minimum (0)
    0x2A, 0xFF, 0x03,  #   Usage Maximum (0x3FF)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x03,  #   Logical Maximum (0x3FF)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x00,        #   Input (Data,Array,Absolute)
    0xC0,              # End Collection

    # ---- Keyboard (Report ID 2) -----------------------------------------
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x02,        #   Report ID (2)

    # Modifier byte (8 bits)
    0x05, 0x07,        #   Usage Page (Key Codes)
    0x19, 0xE0,        #   Usage Min (Left Ctrl)
    0x29, 0xE7,        #   Usage Max (Right GUI)
    0x15, 0x00,        #   Logical Min (0)
    0x25, 0x01,        #   Logical Max (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data,Var,Abs)

    # Reserved byte
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x03,        #   Input (Const,Var,Abs)

    # Key array — 6 simultaneous keys
    0x95, 0x06,        #   Report Count (6)
    0x75, 0x08,        #   Report Size (8)
    0x15, 0x00,        #   Logical Min (0)
    0x26, 0xFF, 0x00,  #   Logical Max (255)
    0x05, 0x07,        #   Usage Page (Key Codes)
    0x19, 0x00,        #   Usage Min (0)
    0x2A, 0xFF, 0x00,  #   Usage Max (255)
    0x81, 0x00,        #   Input (Data,Array)

    0xC0,              # End Collection
])


# ---------------------------------------------------------------------------
# GATT object hierarchy (D-Bus exported)
# ---------------------------------------------------------------------------


class GattApplication(ServiceInterface):
    """Root ObjectManager that BlueZ introspects on RegisterApplication."""

    def __init__(self) -> None:
        super().__init__("org.freedesktop.DBus.ObjectManager")
        self.path = APP_PATH
        self.services: list[GattService] = []

    def add_service(self, svc: "GattService") -> None:
        self.services.append(svc)

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":  # noqa: N802
        result: dict = {}
        for svc in self.services:
            result[svc.path] = svc.props_dict()
            for ch in svc.characteristics:
                result[ch.path] = ch.props_dict()
                for d in ch.descriptors:
                    result[d.path] = d.props_dict()
        return result


class GattService(ServiceInterface):
    def __init__(self, path: str, uuid: str, primary: bool = True) -> None:
        super().__init__(GATT_SERVICE_IFACE)
        self.path = path
        self._uuid = uuid
        self._primary = primary
        self.characteristics: list[GattCharacteristic] = []

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":  # noqa: N802
        return self._primary

    def props_dict(self) -> dict:
        return {
            GATT_SERVICE_IFACE: {
                "UUID": Variant("s", self._uuid),
                "Primary": Variant("b", self._primary),
            }
        }


class GattCharacteristic(ServiceInterface):
    def __init__(
        self,
        path: str,
        service: GattService,
        uuid: str,
        flags: list[str],
        value: bytes = b"",
        on_write: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        super().__init__(GATT_CHARACTERISTIC_IFACE)
        self.path = path
        self._service = service
        self._uuid = uuid
        self._flags = flags
        self._value = bytes(value)
        self._on_write = on_write
        self.notifying = False
        self.descriptors: list[GattDescriptor] = []
        service.characteristics.append(self)

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":  # noqa: N802
        return self._service.path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":  # noqa: N802
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":  # noqa: N802
        return self._value

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":  # noqa: N802
        return self.notifying

    @method()
    def ReadValue(self, _options: "a{sv}") -> "ay":  # noqa: N802
        return self._value

    @method()
    def WriteValue(self, value: "ay", _options: "a{sv}"):  # noqa: N802
        if "write" not in self._flags and "write-without-response" not in self._flags:
            raise DBusError("org.bluez.Error.NotPermitted", "Read-only characteristic")
        self._value = bytes(value)
        if self._on_write is not None:
            try:
                self._on_write(self._value)
            except Exception:
                log.exception("on_write callback failed")

    @method()
    def StartNotify(self):  # noqa: N802
        if "notify" not in self._flags:
            raise DBusError("org.bluez.Error.NotSupported", "Not notifiable")
        if not self.notifying:
            self.notifying = True
            log.info("StartNotify on %s", self.path)
            try:
                self.emit_properties_changed({"Notifying": True})
            except Exception:
                pass

    @method()
    def StopNotify(self):  # noqa: N802
        if self.notifying:
            self.notifying = False
            log.info("StopNotify on %s", self.path)
            try:
                self.emit_properties_changed({"Notifying": False})
            except Exception:
                pass

    def update_value(self, new_value: bytes) -> None:
        """Set the value and, if subscribed, push a notification to the central."""
        self._value = bytes(new_value)
        if self.notifying:
            try:
                self.emit_properties_changed({"Value": self._value})
            except Exception:
                log.exception("failed to emit Value change for %s", self.path)

    def props_dict(self) -> dict:
        return {
            GATT_CHARACTERISTIC_IFACE: {
                "UUID": Variant("s", self._uuid),
                "Service": Variant("o", self._service.path),
                "Flags": Variant("as", self._flags),
            }
        }


class GattDescriptor(ServiceInterface):
    def __init__(
        self,
        path: str,
        characteristic: GattCharacteristic,
        uuid: str,
        flags: list[str],
        value: bytes = b"",
    ) -> None:
        super().__init__(GATT_DESCRIPTOR_IFACE)
        self.path = path
        self._char = characteristic
        self._uuid = uuid
        self._flags = flags
        self._value = bytes(value)
        characteristic.descriptors.append(self)

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Characteristic(self) -> "o":  # noqa: N802
        return self._char.path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":  # noqa: N802
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":  # noqa: N802
        return self._value

    @method()
    def ReadValue(self, _options: "a{sv}") -> "ay":  # noqa: N802
        return self._value

    @method()
    def WriteValue(self, value: "ay", _options: "a{sv}"):  # noqa: N802
        self._value = bytes(value)

    def props_dict(self) -> dict:
        return {
            GATT_DESCRIPTOR_IFACE: {
                "UUID": Variant("s", self._uuid),
                "Characteristic": Variant("o", self._char.path),
                "Flags": Variant("as", self._flags),
            }
        }


# ---------------------------------------------------------------------------
# LE Advertisement
# ---------------------------------------------------------------------------


class LEAdvertisement(ServiceInterface):
    def __init__(self, path: str, local_name: str = "Bluetui Remote") -> None:
        super().__init__(LE_ADVERTISEMENT_IFACE)
        self.path = path
        self._local_name = local_name

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":  # noqa: N802
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":  # noqa: N802
        return [HID_SERVICE_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":  # noqa: N802
        return self._local_name

    @dbus_property(access=PropertyAccess.READ)
    def Appearance(self) -> "q":  # noqa: N802
        # 0x03C1 = HID Keyboard. TVs commonly accept BT remotes that
        # advertise as keyboards. 0x0180 (Generic Remote Control) is also
        # valid but less widely recognised by older BlueZ peers.
        return 0x03C1

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "as":  # noqa: N802
        return ["tx-power"]

    @method()
    def Release(self):  # noqa: N802
        log.info("LEAdvertisement released by BlueZ")


# ---------------------------------------------------------------------------
# Top-level manager
# ---------------------------------------------------------------------------


class HidRemoteError(Exception):
    pass


class HidRemote:
    """Owns the GATT app + advertisement and exposes high-level send_* methods.

    Lifecycle:
        hr = HidRemote(bus, adapter_path)
        await hr.start()    # exports objects, registers with BlueZ
        hr.send_consumer(CC.VOLUME_UP)
        await hr.stop()     # unregisters and unexports
    """

    PRESS_DURATION = 0.04  # seconds between press and release reports

    def __init__(self, bus: MessageBus, adapter_path: str) -> None:
        self.bus = bus
        self.adapter_path = adapter_path
        self.app = GattApplication()
        self.advertisement = LEAdvertisement(ADV_PATH)
        self.cc_report: GattCharacteristic | None = None
        self.kb_report: GattCharacteristic | None = None
        self.battery_char: GattCharacteristic | None = None
        self._registered = False
        self._advertising = False
        self._exported_paths: list[str] = []
        self._build_services()

    # ------------------------------------------------------------ build

    def _build_services(self) -> None:
        # ===== HID Service =====
        hid = GattService(f"{self.app.path}/svc0", HID_SERVICE_UUID)
        self.app.add_service(hid)

        # HID Information: bcdHID=0x0111, country=0, flags=0x03 (RemoteWake | NormallyConnectable)
        GattCharacteristic(
            f"{hid.path}/char0", hid, HID_INFO_UUID,
            flags=["read"],
            value=bytes([0x11, 0x01, 0x00, 0x03]),
        )

        # Report Map — encryption mandated by HID-over-GATT spec.
        GattCharacteristic(
            f"{hid.path}/char1", hid, REPORT_MAP_UUID,
            flags=["encrypt-read"],
            value=HID_REPORT_MAP,
        )

        # HID Control Point
        GattCharacteristic(
            f"{hid.path}/char2", hid, HID_CONTROL_POINT_UUID,
            flags=["write-without-response"],
        )

        # Protocol Mode (1 = Report mode)
        GattCharacteristic(
            f"{hid.path}/char3", hid, PROTOCOL_MODE_UUID,
            flags=["read", "write-without-response"],
            value=bytes([0x01]),
        )

        # Report — Consumer Control input (Report ID 1)
        cc = GattCharacteristic(
            f"{hid.path}/char4", hid, REPORT_UUID,
            flags=["encrypt-read", "notify"],
            value=bytes(2),
        )
        GattDescriptor(
            f"{cc.path}/desc0", cc, REPORT_REFERENCE_UUID,
            flags=["read"],
            value=bytes([0x01, 0x01]),  # report id 1, type 1 = Input
        )
        self.cc_report = cc

        # Report — Keyboard input (Report ID 2)
        kb = GattCharacteristic(
            f"{hid.path}/char5", hid, REPORT_UUID,
            flags=["encrypt-read", "notify"],
            value=bytes(8),
        )
        GattDescriptor(
            f"{kb.path}/desc0", kb, REPORT_REFERENCE_UUID,
            flags=["read"],
            value=bytes([0x02, 0x01]),
        )
        self.kb_report = kb

        # Boot Keyboard Input Report — many HID hosts probe for this even
        # when Protocol Mode says Report mode. Provide it for compatibility.
        boot_kbd_in = GattCharacteristic(
            f"{hid.path}/char6", hid, BOOT_KBD_INPUT_UUID,
            flags=["encrypt-read", "notify"],
            value=bytes(8),
        )
        self.boot_kbd_in = boot_kbd_in

        # Boot Keyboard Output Report — used by host to set LED states
        # (Caps/Num/Scroll). Just a 1-byte stash.
        GattCharacteristic(
            f"{hid.path}/char7", hid, BOOT_KBD_OUTPUT_UUID,
            flags=["encrypt-read", "encrypt-write", "write-without-response"],
            value=bytes(1),
        )

        # ===== Battery Service =====
        batt = GattService(f"{self.app.path}/svc1", BATTERY_SERVICE_UUID)
        self.app.add_service(batt)
        self.battery_char = GattCharacteristic(
            f"{batt.path}/char0", batt, BATTERY_LEVEL_UUID,
            flags=["read", "notify"],
            value=bytes([100]),
        )

        # ===== Device Information Service =====
        di = GattService(f"{self.app.path}/svc2", DEVICE_INFO_SERVICE_UUID)
        self.app.add_service(di)
        # PnP ID: vendor source (1B), vendor (2B LE), product (2B LE), version (2B LE)
        # Source 0x02 = USB-IF. Vendor 0x046D (Logitech), product 0xB031, ver 0x0001.
        # This makes us look like a generic Logitech remote — many TVs trust this.
        pnp_value = struct.pack("<BHHH", 0x02, 0x046D, 0xB031, 0x0001)
        GattCharacteristic(
            f"{di.path}/char0", di, PNP_ID_UUID,
            flags=["read"],
            value=pnp_value,
        )

    # ----------------------------------------------------------- export

    def _all_paths(self) -> list[str]:
        """Every D-Bus path this HidRemote would export."""
        paths = [self.app.path]
        for svc in self.app.services:
            paths.append(svc.path)
            for ch in svc.characteristics:
                paths.append(ch.path)
                for d in ch.descriptors:
                    paths.append(d.path)
        paths.append(self.advertisement.path)
        return paths

    def _export_one(self, path: str, obj) -> None:
        """Export an object, transparently replacing any stale prior export.

        dbus-next raises ValueError if an interface is already exported at
        the same path. That can happen if a previous session was killed
        before unexport ran, or if two start() calls race. Unexport-then-
        export keeps us idempotent.
        """
        try:
            self.bus.export(path, obj)
        except ValueError:
            try:
                self.bus.unexport(path)
            except Exception:
                pass
            self.bus.export(path, obj)

    def _export_all(self) -> None:
        # Clear any stale state that may linger from a prior interrupted
        # registration before exporting fresh.
        for p in reversed(self._all_paths()):
            try:
                self.bus.unexport(p)
            except Exception:
                pass
        self._exported_paths.clear()

        self._export_one(self.app.path, self.app)
        self._exported_paths.append(self.app.path)
        for svc in self.app.services:
            self._export_one(svc.path, svc)
            self._exported_paths.append(svc.path)
            for ch in svc.characteristics:
                self._export_one(ch.path, ch)
                self._exported_paths.append(ch.path)
                for d in ch.descriptors:
                    self._export_one(d.path, d)
                    self._exported_paths.append(d.path)
        self._export_one(self.advertisement.path, self.advertisement)
        self._exported_paths.append(self.advertisement.path)

    def _unexport_all(self) -> None:
        # Unexport in reverse order to avoid dangling refs.
        for p in reversed(self._exported_paths):
            try:
                self.bus.unexport(p)
            except Exception:
                pass
        self._exported_paths.clear()

    # ------------------------------------------------------------ start / stop

    async def start(self) -> None:
        if self._registered:
            return
        self._export_all()
        intro = await self.bus.introspect("org.bluez", self.adapter_path)
        adapter_obj = self.bus.get_proxy_object("org.bluez", self.adapter_path, intro)

        # Register GATT app first so services exist before any connection.
        try:
            gatt_mgr = adapter_obj.get_interface(GATT_MANAGER_IFACE)
        except Exception as e:
            self._unexport_all()
            raise HidRemoteError(
                f"GattManager1 не найден на {self.adapter_path}: {e}"
            ) from e

        try:
            await gatt_mgr.call_register_application(self.app.path, {})
        except DBusError as e:
            self._unexport_all()
            raise HidRemoteError(
                f"BlueZ отверг регистрацию GATT: {e.text or e}"
            ) from e
        self._registered = True

        # Start advertising.
        try:
            adv_mgr = adapter_obj.get_interface(LE_ADVERTISING_MANAGER_IFACE)
            await adv_mgr.call_register_advertisement(self.advertisement.path, {})
            self._advertising = True
        except DBusError as e:
            # Try to clean up partial state.
            try:
                await gatt_mgr.call_unregister_application(self.app.path)
            except Exception:
                pass
            self._registered = False
            self._unexport_all()
            raise HidRemoteError(
                f"BlueZ отверг advertisement: {e.text or e}"
            ) from e

    async def stop(self) -> None:
        if not self._registered and not self._advertising:
            return
        try:
            intro = await self.bus.introspect("org.bluez", self.adapter_path)
            adapter_obj = self.bus.get_proxy_object(
                "org.bluez", self.adapter_path, intro
            )
            if self._advertising:
                try:
                    adv_mgr = adapter_obj.get_interface(LE_ADVERTISING_MANAGER_IFACE)
                    await adv_mgr.call_unregister_advertisement(self.advertisement.path)
                except Exception:
                    pass
                self._advertising = False
            if self._registered:
                try:
                    gatt_mgr = adapter_obj.get_interface(GATT_MANAGER_IFACE)
                    await gatt_mgr.call_unregister_application(self.app.path)
                except Exception:
                    pass
                self._registered = False
        finally:
            self._unexport_all()

    # ------------------------------------------------------------ status

    @property
    def is_active(self) -> bool:
        return self._registered and self._advertising

    @property
    def is_subscribed(self) -> bool:
        """True if at least one report characteristic has notifications enabled.

        BlueZ calls StartNotify on our report when the central (TV) subscribes,
        which it does shortly after a successful HID connection.
        """
        return bool(
            (self.cc_report and self.cc_report.notifying)
            or (self.kb_report and self.kb_report.notifying)
        )

    # ------------------------------------------------------------ send

    def send_consumer(self, code: int) -> bool:
        """Send a Consumer Control press+release.

        Returns False if no subscriber is currently listening.
        """
        if self.cc_report is None or not self.cc_report.notifying:
            return False
        press = struct.pack("<H", code)
        release = struct.pack("<H", 0)
        self.cc_report.update_value(press)

        async def _release() -> None:
            await asyncio.sleep(self.PRESS_DURATION)
            if self.cc_report is not None and self.cc_report.notifying:
                self.cc_report.update_value(release)

        asyncio.create_task(_release())
        return True

    def send_key(self, keycode: int, modifier: int = 0) -> bool:
        """Send a keyboard press+release.

        The 8-byte report is: modifier, reserved, key, 0, 0, 0, 0, 0.
        """
        if self.kb_report is None or not self.kb_report.notifying:
            return False
        press = bytes([modifier, 0, keycode, 0, 0, 0, 0, 0])
        release = bytes(8)
        self.kb_report.update_value(press)

        async def _release() -> None:
            await asyncio.sleep(self.PRESS_DURATION)
            if self.kb_report is not None and self.kb_report.notifying:
                self.kb_report.update_value(release)

        asyncio.create_task(_release())
        return True
