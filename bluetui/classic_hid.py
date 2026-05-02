"""Classic Bluetooth HID Device profile.

Mirrors what the Android BtRemote app does (BluetoothHidDevice API):
register a HID Device SDP record on this adapter, then **initiate** an
outgoing connection to the TV (which is the HID Host). The TV does not
need to scan for us — we connect to it.

Implementation outline:
    * Build a complete SDP XML record describing us as a HID Device with
      a Report descriptor for Consumer Control + Keyboard (Report IDs
      matching BtRemote: 1=keyboard, 2=consumer).
    * Register an `org.bluez.Profile1` with HID UUID 0x1124, role=server,
      ServiceRecord=our XML. BlueZ adds it to the system SDP database.
    * To talk to a TV, call `Device1.ConnectProfile("00001124-…")` —
      BlueZ opens the L2CAP HID Control (PSM 17) and Interrupt (PSM 19)
      channels for us. Our `NewConnection` is invoked with a file
      descriptor for the control channel.
    * Open the interrupt channel ourselves via raw L2CAP (PSM 19) — that
      is where input reports are written.
    * Send HID input reports prefixed with the HIDP transaction header
      `0xA1` (DATA | INPUT) and the Report ID.
"""
from __future__ import annotations

import asyncio
import binascii
import logging
import os
import socket
import struct
import time
from typing import Awaitable, Callable, Optional

from dbus_next import DBusError, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

# Linux Bluetooth socket option constants (from <bluetooth/bluetooth.h>).
# Python's socket module doesn't always expose them as named constants.
SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_LOW = 1
BT_SECURITY_MEDIUM = 2
BT_SECURITY_HIGH = 3

# Audio / HFP profile UUIDs we want to drop after the HID link is up so the
# TV doesn't start streaming audio to our PC speakers.
AUDIO_PROFILE_UUIDS = (
    "0000110a-0000-1000-8000-00805f9b34fb",  # A2DP Source
    "0000110b-0000-1000-8000-00805f9b34fb",  # A2DP Sink
    "0000110c-0000-1000-8000-00805f9b34fb",  # AVRCP Target
    "0000110d-0000-1000-8000-00805f9b34fb",  # A2DP Distribution
    "0000110e-0000-1000-8000-00805f9b34fb",  # AVRCP Controller
    "00001108-0000-1000-8000-00805f9b34fb",  # Headset
    "0000111e-0000-1000-8000-00805f9b34fb",  # Hands-Free
    "00001112-0000-1000-8000-00805f9b34fb",  # Headset AG
)

log = logging.getLogger(__name__)

HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
HID_PROFILE_PATH = "/com/bluetui/hidprofile"
PSM_HID_CONTROL = 0x0011  # 17
PSM_HID_INTERRUPT = 0x0013  # 19

# HIDP transaction header for input reports going device → host:
#   high nibble 0xA = HIDP_TRANS_DATA, low nibble 0x1 = INPUT report
HIDP_INPUT = 0xA1


# ---------------------------------------------------------------------------
# Report IDs and HID Report Descriptor
# ---------------------------------------------------------------------------
# Layout matches BtRemote (Atharok/BtRemote on GitLab) so any TV that
# accepts that app should accept us.
KEYBOARD_REPORT_ID = 0x01
CONSUMER_REPORT_ID = 0x02
MOUSE_REPORT_ID = 0x03

HID_REPORT_DESCRIPTOR = bytes([
    # ---- Consumer Control (Report ID 2) -----------------------------
    0x05, 0x0C,        # Usage Page (Consumer)
    0x09, 0x01,        # Usage (Consumer Control)
    0xA1, 0x01,        # Collection (Application)
    0x85, CONSUMER_REPORT_ID,
    0x19, 0x00,
    0x2A, 0xFF, 0x03,
    0x75, 0x10,
    0x95, 0x01,
    0x15, 0x00,
    0x26, 0xFF, 0x03,
    0x81, 0x00,
    0xC0,

    # ---- Keyboard (Report ID 1) -------------------------------------
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,
    0x85, KEYBOARD_REPORT_ID,
    0x05, 0x07,
    0x19, 0xE0,
    0x29, 0xE7,
    0x15, 0x00,
    0x25, 0x01,
    0x75, 0x01,
    0x95, 0x08,
    0x81, 0x02,        # Modifier byte
    0x75, 0x08,
    0x95, 0x01,
    0x15, 0x00,
    0x26, 0xFF, 0x00,
    0x05, 0x07,
    0x19, 0x00,
    0x29, 0xFF,
    0x81, 0x00,        # Single-key array
    0xC0,
])

# Consumer-Control codes (Usage Page 0x0C).
class CC:
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


# Keyboard scancodes (Usage Page 0x07).
class KB:
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
# SDP Service Record XML
# ---------------------------------------------------------------------------

def _build_sdp_record() -> str:
    hid_hex = binascii.hexlify(HID_REPORT_DESCRIPTOR).decode("ascii").upper()
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        '<record>\n'
        # ServiceClassIDList → Human Interface Device Service
        '  <attribute id="0x0001"><sequence>'
        '<uuid value="0x1124"/></sequence></attribute>\n'
        # ProtocolDescriptorList → L2CAP PSM 17 + HIDP
        '  <attribute id="0x0004"><sequence>'
        '<sequence><uuid value="0x0100"/><uint16 value="0x0011"/></sequence>'
        '<sequence><uuid value="0x0011"/></sequence>'
        '</sequence></attribute>\n'
        # BrowseGroupList → PublicBrowseGroup
        '  <attribute id="0x0005"><sequence>'
        '<uuid value="0x1002"/></sequence></attribute>\n'
        # LanguageBaseAttributeIDList
        '  <attribute id="0x0006"><sequence>'
        '<uint16 value="0x656e"/><uint16 value="0x006a"/><uint16 value="0x0100"/>'
        '</sequence></attribute>\n'
        # BluetoothProfileDescriptorList → HID profile v1.1
        '  <attribute id="0x0009"><sequence><sequence>'
        '<uuid value="0x1124"/><uint16 value="0x0101"/>'
        '</sequence></sequence></attribute>\n'
        # AdditionalProtocolDescriptorLists → L2CAP PSM 19 + HIDP
        '  <attribute id="0x000d"><sequence><sequence>'
        '<sequence><uuid value="0x0100"/><uint16 value="0x0013"/></sequence>'
        '<sequence><uuid value="0x0011"/></sequence>'
        '</sequence></sequence></attribute>\n'
        # ServiceName / Description / Provider
        '  <attribute id="0x0100"><text value="Bluetui Remote"/></attribute>\n'
        '  <attribute id="0x0101"><text value="Bluetooth HID Remote"/></attribute>\n'
        '  <attribute id="0x0102"><text value="bluetui"/></attribute>\n'
        # HID DeviceReleaseNumber (BCD)
        '  <attribute id="0x0200"><uint16 value="0x0100"/></attribute>\n'
        # HID ParserVersion
        '  <attribute id="0x0201"><uint16 value="0x0111"/></attribute>\n'
        # HID DeviceSubclass — combo keyboard / pointing
        '  <attribute id="0x0202"><uint8 value="0x40"/></attribute>\n'
        # HID CountryCode
        '  <attribute id="0x0203"><uint8 value="0x00"/></attribute>\n'
        # HID VirtualCable — disable so a single disconnect doesn't unpair
        '  <attribute id="0x0204"><boolean value="false"/></attribute>\n'
        # HID ReconnectInitiate — we initiate
        '  <attribute id="0x0205"><boolean value="true"/></attribute>\n'
        # HID DescriptorList — Report descriptor type 0x22 + bytes
        f'  <attribute id="0x0206"><sequence><sequence>'
        f'<uint8 value="0x22"/>'
        f'<text encoding="hex" value="{hid_hex}"/>'
        f'</sequence></sequence></attribute>\n'
        # HID LangIDBaseList
        '  <attribute id="0x0207"><sequence><sequence>'
        '<uint16 value="0x0409"/><uint16 value="0x0100"/>'
        '</sequence></sequence></attribute>\n'
        # HID Profile Version
        '  <attribute id="0x020b"><uint16 value="0x0100"/></attribute>\n'
        # HID Supervision Timeout
        '  <attribute id="0x020c"><uint16 value="0x0c80"/></attribute>\n'
        # HID NormallyConnectable
        '  <attribute id="0x020d"><boolean value="true"/></attribute>\n'
        # HID BootDevice
        '  <attribute id="0x020e"><boolean value="false"/></attribute>\n'
        '</record>\n'
    )


# ---------------------------------------------------------------------------
# Profile1 implementation
# ---------------------------------------------------------------------------


class HidProfile(ServiceInterface):
    """Profile1 callback target. BlueZ calls NewConnection when an L2CAP
    channel for our HID UUID has been established.
    """

    def __init__(self, on_new_connection: Callable[[str, int], None],
                 on_disconnect: Callable[[str], None]) -> None:
        super().__init__("org.bluez.Profile1")
        self._on_new = on_new_connection
        self._on_disc = on_disconnect

    @method()
    def Release(self):  # noqa: N802
        log.info("HID Profile1 released by BlueZ")

    @method()
    def NewConnection(self, device: "o", fd: "h", fd_props: "a{sv}"):  # noqa: N802
        log.info("HID Profile1 NewConnection device=%s fd=%s props=%s",
                 device, fd, fd_props)
        self._on_new(device, int(fd))

    @method()
    def RequestDisconnection(self, device: "o"):  # noqa: N802
        log.info("HID Profile1 RequestDisconnection %s", device)
        self._on_disc(device)


# ---------------------------------------------------------------------------
# High-level manager
# ---------------------------------------------------------------------------


class ClassicHidError(Exception):
    pass


class ClassicHidRemote:
    """Acts as a Bluetooth HID Device. Pairs with a TV / Android TV box and
    sends HID input reports.

    Lifecycle:
        hr = ClassicHidRemote(bus)
        await hr.start_profile()             # register Profile1 + SDP
        await hr.connect("AA:BB:CC:DD:EE:FF") # pair if needed, ConnectProfile
        hr.send_consumer(CC.VOLUME_UP)
        await hr.disconnect()
        await hr.stop_profile()
    """

    PRESS_DURATION = 0.04
    # Drop repeated presses of the same key arriving faster than this.
    # Terminal autorepeat is ~30 Hz; this caps us at ~6 Hz per key, which
    # matches comfortable navigation speed on a TV.
    MIN_REPEAT_INTERVAL = 0.16

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self._profile: HidProfile | None = None
        self._registered = False
        # Track currently connected TV.
        self.target_path: str | None = None  # /org/bluez/hciN/dev_XX_…
        self.target_address: str | None = None
        # L2CAP sockets we opened ourselves (skip BlueZ ConnectProfile).
        self._control_socket: socket.socket | None = None
        self._interrupt_socket: socket.socket | None = None
        # File descriptors that BlueZ may pass us via Profile1.NewConnection
        # (in case the host happens to initiate the connection back to us).
        self._control_fds: dict[str, int] = {}
        # Status callbacks
        self.on_state: Callable[[str], None] | None = None  # "connected"|"disconnected"
        # Per-key throttle state.
        self._last_sent: dict[tuple, float] = {}

    # -------------------------------------------------------------- profile

    async def start_profile(self) -> None:
        if self._registered:
            return
        self._profile = HidProfile(
            on_new_connection=self._on_new_connection,
            on_disconnect=self._on_disconnect_request,
        )
        try:
            self.bus.unexport(HID_PROFILE_PATH)
        except Exception:
            pass
        self.bus.export(HID_PROFILE_PATH, self._profile)
        intro = await self.bus.introspect("org.bluez", "/org/bluez")
        obj = self.bus.get_proxy_object("org.bluez", "/org/bluez", intro)
        mgr = obj.get_interface("org.bluez.ProfileManager1")
        try:
            await mgr.call_register_profile(
                HID_PROFILE_PATH,
                HID_UUID,
                {
                    "Name": Variant("s", "Bluetui Remote"),
                    "Role": Variant("s", "server"),
                    "RequireAuthentication": Variant("b", True),
                    "RequireAuthorization": Variant("b", False),
                    "AutoConnect": Variant("b", False),
                    "ServiceRecord": Variant("s", _build_sdp_record()),
                },
            )
        except DBusError as e:
            raise ClassicHidError(
                f"BlueZ отверг регистрацию HID-профиля: {e.text or e}"
            ) from e
        self._registered = True
        log.info("HID Profile1 registered with BlueZ")

    async def stop_profile(self) -> None:
        await self.disconnect()
        if not self._registered:
            return
        try:
            intro = await self.bus.introspect("org.bluez", "/org/bluez")
            obj = self.bus.get_proxy_object("org.bluez", "/org/bluez", intro)
            mgr = obj.get_interface("org.bluez.ProfileManager1")
            await mgr.call_unregister_profile(HID_PROFILE_PATH)
        except Exception:
            log.exception("UnregisterProfile failed")
        try:
            self.bus.unexport(HID_PROFILE_PATH)
        except Exception:
            pass
        self._registered = False
        log.info("HID Profile1 unregistered")

    # ----------------------------------------------------------- callbacks

    def _on_new_connection(self, device_path: str, fd: int) -> None:
        # Save the control fd. We don't talk on it directly, but we keep
        # it so the kernel doesn't tear down the L2CAP control channel.
        self._control_fds[device_path] = fd
        if self.on_state is not None:
            self.on_state("control_connected")

    def _on_disconnect_request(self, device_path: str) -> None:
        fd = self._control_fds.pop(device_path, None)
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if self.target_path == device_path:
            self._close_socket("_interrupt_socket")
            self._close_socket("_control_socket")
            self.target_path = None
            self.target_address = None
            if self.on_state is not None:
                self.on_state("disconnected")

    # ------------------------------------------------------------- connect

    async def connect(self, device_path: str, address: str) -> None:
        """Initiate a HID connection to the given device.

        We don't use BlueZ ConnectProfile because the TV doesn't advertise
        HID UUID 0x1124 (it's a HID Host, not a HID Device — TVs consume
        HID input rather than provide it). Instead we:

        1. Pair via Device1.Pair() if needed (uses the JustWorks agent
           already registered by the App).
        2. Mark the device as Trusted so reconnects don't need approval.
        3. Open outgoing L2CAP sockets directly to PSMs 17 (Control) and
           19 (Interrupt) — same path the Android BluetoothHidDevice API
           takes under the hood.
        """
        if not self._registered:
            await self.start_profile()

        intro = await self.bus.introspect("org.bluez", device_path)
        obj = self.bus.get_proxy_object("org.bluez", device_path, intro)
        dev = obj.get_interface("org.bluez.Device1")
        propsi = obj.get_interface("org.freedesktop.DBus.Properties")

        # Step 1: ensure paired (BlueZ auto-bonds via the JustWorks agent).
        try:
            paired_var = await propsi.call_get("org.bluez.Device1", "Paired")
            paired = bool(paired_var.value)
        except Exception:
            paired = False
        if not paired:
            log.info("Pairing with %s before HID connect", address)
            try:
                await dev.call_pair()
            except DBusError as e:
                # AlreadyExists is fine.
                if "AlreadyExists" not in (e.text or ""):
                    raise ClassicHidError(
                        f"Pairing failed: {e.text or e}"
                    ) from e

        # Step 2: trust the device so future reconnects are silent.
        try:
            await propsi.call_set(
                "org.bluez.Device1", "Trusted", Variant("b", True)
            )
        except Exception:
            log.exception("set Trusted failed")

        # Step 3: open both L2CAP channels.
        try:
            self._control_socket = await self._open_l2cap(address, PSM_HID_CONTROL)
        except Exception as e:
            raise ClassicHidError(
                f"Не удалось открыть L2CAP Control (PSM 17): {e}"
            ) from e
        try:
            self._interrupt_socket = await self._open_l2cap(address, PSM_HID_INTERRUPT)
        except Exception as e:
            self._close_socket("_control_socket")
            raise ClassicHidError(
                f"Не удалось открыть L2CAP Interrupt (PSM 19): {e}"
            ) from e

        self.target_path = device_path
        self.target_address = address
        if self.on_state is not None:
            self.on_state("connected")
        log.info("ClassicHidRemote connected to %s (%s)", device_path, address)

        # Spawn a reader on the control channel so the kernel keeps it alive
        # and we can log any host-issued SET_REPORT / GET_REPORT requests.
        asyncio.create_task(self._control_reader())

        # Drop any audio / AVRCP profile connections BlueZ silently brought
        # up alongside ours. Otherwise the TV decides we're a Bluetooth
        # speaker and starts streaming audio to the PC.
        asyncio.create_task(self._disconnect_audio_profiles(dev))

    async def _disconnect_audio_profiles(self, dev) -> None:
        for uuid in AUDIO_PROFILE_UUIDS:
            try:
                await dev.call_disconnect_profile(uuid)
                log.info("disconnected audio profile %s", uuid)
            except DBusError as e:
                # NotConnected / NotAvailable means the profile wasn't up
                # in the first place — that's fine.
                txt = (e.text or "").lower()
                if "notconnected" in txt or "not connected" in txt \
                        or "notavailable" in txt or "doesnotexist" in txt:
                    continue
                log.debug("disconnect %s: %s", uuid, e.text or e)
            except Exception:
                pass

    async def _open_l2cap(self, address: str, psm: int) -> socket.socket:
        """Open one outgoing L2CAP SOCK_SEQPACKET socket to (address, psm)
        with encryption required (BT_SECURITY_MEDIUM).
        """
        sock = socket.socket(
            socket.AF_BLUETOOTH,
            socket.SOCK_SEQPACKET,
            socket.BTPROTO_L2CAP,
        )
        # Require encrypted link before connect — most TVs reject HID
        # input on unencrypted L2CAP channels.
        try:
            sock.setsockopt(
                SOL_BLUETOOTH, BT_SECURITY,
                struct.pack("BB", BT_SECURITY_MEDIUM, 0),
            )
        except OSError:
            log.warning("setsockopt(BT_SECURITY) failed; proceeding without")
        # asyncio.sock_connect runs getaddrinfo() which doesn't know
        # AF_BLUETOOTH. We connect in an executor (it's a one-shot
        # blocking call, no big deal).
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, sock.connect, (address, psm))
        except Exception:
            sock.close()
            raise
        sock.setblocking(False)
        log.info("L2CAP connected: %s PSM=%d fd=%d", address, psm, sock.fileno())
        return sock

    def _close_socket(self, attr: str) -> None:
        sock = getattr(self, attr, None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
            setattr(self, attr, None)

    async def _control_reader(self) -> None:
        """Read frames from control channel (best-effort — we don't really
        respond, but reading prevents the kernel from buffering forever
        and lets us notice disconnect)."""
        sock = self._control_socket
        if sock is None:
            return
        loop = asyncio.get_running_loop()
        try:
            while sock is self._control_socket:
                data = await loop.sock_recv(sock, 256)
                if not data:
                    log.info("control channel EOF")
                    break
                log.debug("control rx: %s", data.hex())
        except Exception as e:
            log.debug("control reader stopped: %s", e)

    async def disconnect(self) -> None:
        self.target_path = None
        self.target_address = None
        self._close_socket("_interrupt_socket")
        self._close_socket("_control_socket")
        # Close any fds that BlueZ may have given us via NewConnection.
        for fd in list(self._control_fds.values()):
            try:
                os.close(fd)
            except Exception:
                pass
        self._control_fds.clear()
        if self.on_state is not None:
            self.on_state("disconnected")

    # --------------------------------------------------------------- send

    @property
    def is_connected(self) -> bool:
        return self._interrupt_socket is not None

    def _send_raw(self, report_id: int, payload: bytes) -> bool:
        """Write a HID input report on the interrupt channel.

        Frame: [0xA1, REPORT_ID, *payload]
        Returns False if not connected or the write failed.
        """
        sock = self._interrupt_socket
        if sock is None:
            return False
        msg = bytes([HIDP_INPUT, report_id]) + payload
        try:
            sock.send(msg)
            return True
        except OSError as e:
            log.warning("send_raw failed: %s", e)
            return False

    def _throttled(self, kind: str, code: int) -> bool:
        """Drop terminal-autorepeat bursts so we don't flood the TV with
        events that take it minutes to chew through.
        """
        now = time.monotonic()
        key = (kind, code)
        last = self._last_sent.get(key, 0.0)
        if now - last < self.MIN_REPEAT_INTERVAL:
            return True
        self._last_sent[key] = now
        return False

    def send_consumer(self, code: int) -> bool:
        if not self.is_connected:
            return False
        if self._throttled("cc", code):
            return True  # silently coalesce
        ok = self._send_raw(CONSUMER_REPORT_ID, struct.pack("<H", code))
        if not ok:
            return False

        async def _release() -> None:
            await asyncio.sleep(self.PRESS_DURATION)
            self._send_raw(CONSUMER_REPORT_ID, struct.pack("<H", 0))

        asyncio.create_task(_release())
        return True

    def send_key(self, keycode: int, modifier: int = 0) -> bool:
        """Send a single keyboard key press + release.

        Our HID descriptor declares the keyboard report as exactly 2 bytes:
        [modifier, keycode]. Sending an 8-byte (full BootKB) report would
        be ignored beyond the first 2 bytes — that was the bug behind
        arrow keys / Enter not working on TVs.
        """
        if not self.is_connected:
            return False
        if self._throttled("kb", keycode):
            return True
        press = bytes([modifier, keycode])
        ok = self._send_raw(KEYBOARD_REPORT_ID, press)
        if not ok:
            return False

        async def _release() -> None:
            await asyncio.sleep(self.PRESS_DURATION)
            self._send_raw(KEYBOARD_REPORT_ID, bytes(2))

        asyncio.create_task(_release())
        return True
