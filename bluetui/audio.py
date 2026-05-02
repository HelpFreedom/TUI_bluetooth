"""PulseAudio / PipeWire helpers — auto-route audio to a freshly connected
Bluetooth audio device.

We use the `pactl` CLI rather than the libpulse D-Bus interface: it works
on both PulseAudio and PipeWire (via the pipewire-pulse shim), is always
available, and the surface area we need is tiny.

Public entry point: ``await route_audio_to(address, props)``.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

AUDIO_ICONS = {
    "audio-card",
    "audio-headphones",
    "audio-headset",
    "audio-speakers",
}

A2DP_SINK_UUID_PREFIX = "0000110b-"  # Advanced Audio Distribution — Sink role


def looks_like_audio_sink(props: dict) -> bool:
    """Heuristic: is this device a BT speaker / headset / headphones?"""
    if props.get("Icon", "") in AUDIO_ICONS:
        return True
    uuids = props.get("UUIDs") or []
    return any(u.lower().startswith(A2DP_SINK_UUID_PREFIX) for u in uuids)


async def _run(*argv: str, capture: bool = False) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return -1, ""
    out, _ = await proc.communicate()
    text = out.decode("utf-8", errors="replace") if out is not None else ""
    return proc.returncode or 0, text


async def _list_sinks() -> list[str]:
    rc, out = await _run("pactl", "list", "short", "sinks", capture=True)
    if rc != 0:
        return []
    sinks = []
    for line in out.splitlines():
        # pactl short format: <id>\t<name>\t<module>\t<sample_spec>\t<state>
        parts = line.split("\t")
        if len(parts) >= 2:
            sinks.append(parts[1])
    return sinks


async def _list_sink_inputs() -> list[str]:
    rc, out = await _run("pactl", "list", "short", "sink-inputs", capture=True)
    if rc != 0:
        return []
    ids = []
    for line in out.splitlines():
        parts = line.split("\t")
        if parts and parts[0]:
            ids.append(parts[0])
    return ids


async def _wait_for_bt_sink(address: str, timeout: float = 6.0) -> str | None:
    """Poll until a sink whose name contains the device address appears.

    Returns the sink name (e.g. ``bluez_sink.AC_80_0A_…_a2dp_sink`` on
    PulseAudio or ``bluez_output.AC_80_0A_…`` on PipeWire) or None on
    timeout.
    """
    target = address.replace(":", "_").upper()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    interval = 0.25
    while loop.time() < deadline:
        for sink in await _list_sinks():
            up = sink.upper()
            if target in up and "BLUEZ" in up:
                return sink
        await asyncio.sleep(interval)
    return None


async def _list_cards() -> list[tuple[str, str]]:
    """Return [(card_id, card_name)] from pactl."""
    rc, out = await _run("pactl", "list", "short", "cards", capture=True)
    if rc != 0:
        return []
    cards = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            cards.append((parts[0], parts[1]))
    return cards


async def _bluez5_discover_module_id() -> str | None:
    rc, out = await _run("pactl", "list", "short", "modules", capture=True)
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and "bluez5-discover" in parts[1]:
            return parts[0]
    return None


async def _ensure_bluez5_discover() -> bool:
    """Make sure ``module-bluez5-discover`` is loaded — without it
    PulseAudio cannot see any BT audio device. Idempotent.
    """
    if await _bluez5_discover_module_id() is not None:
        return True
    log.info("module-bluez5-discover not loaded; loading it now")
    rc, _ = await _run("pactl", "load-module", "module-bluez5-discover")
    if rc != 0:
        log.info("load-module module-bluez5-discover failed rc=%s", rc)
        return False
    return True


async def _reload_bluez_discover() -> bool:
    """PulseAudio occasionally drops the BT-device-added signal (race vs.
    BlueZ). Unload + load ``module-bluez5-discover`` to force a full
    re-scan of already-connected devices. Loads it from scratch if it's
    not running.
    """
    module_id = await _bluez5_discover_module_id()
    if module_id is None:
        return await _ensure_bluez5_discover()
    log.info("reloading module-bluez5-discover (id=%s)", module_id)
    await _run("pactl", "unload-module", module_id)
    await asyncio.sleep(0.3)
    rc, _ = await _run("pactl", "load-module", "module-bluez5-discover")
    if rc != 0:
        log.info("load-module module-bluez5-discover failed rc=%s", rc)
        return False
    return True


async def _ensure_a2dp_profile(address: str) -> bool:
    """If a bluez_card for the device exists but A2DP isn't selected,
    switch its profile so a sink shows up."""
    target = address.replace(":", "_").upper()
    for _id, name in await _list_cards():
        if "BLUEZ" in name.upper() and target in name.upper():
            log.info("found bluez card: %s — trying A2DP profile", name)
            for prof in ("a2dp_sink", "a2dp-sink", "a2dp_sink_aac"):
                rc, _ = await _run("pactl", "set-card-profile", name, prof)
                if rc == 0:
                    log.info("set-card-profile %s → %s OK", name, prof)
                    return True
            log.info("none of the A2DP profile names worked for %s", name)
            return False
    return False


async def route_audio_to(
    address: str,
    props: dict,
    reconnect: Optional[Callable[[], Awaitable[None]]] = None,
    move_existing_streams: bool = True,
) -> str | None:
    """Make the BT sink for ``address`` the default and (optionally) move
    any currently-playing streams to it.

    Returns the sink name on success, None if nothing was done. Failures
    are logged but never raised — call sites can fire-and-forget.
    """
    log.info("route_audio_to start: address=%s icon=%s uuids_count=%s",
             address, props.get("Icon"),
             len(props.get("UUIDs") or []))
    if not looks_like_audio_sink(props):
        log.info("route_audio_to: device is not an audio sink, skipping")
        return None
    if shutil.which("pactl") is None:
        log.info("pactl not on PATH — cannot auto-route audio")
        return None

    # Step 0: PulseAudio's bluez5-discover module must be loaded for any
    # BT card / sink to ever appear. If a previous run unloaded it (or
    # the user's session never loaded it), bring it back.
    await _ensure_bluez5_discover()

    sink_name = await _wait_for_bt_sink(address, timeout=4.0)

    if sink_name is None:
        # No sink yet. Inspect what PulseAudio sees right now.
        sinks_now = await _list_sinks()
        cards_now = await _list_cards()
        log.info("no sink yet for %s | sinks=%s | cards=%s",
                 address, sinks_now, [c[1] for c in cards_now])

        # Case A: there's a bluez_card for this device but the wrong
        # profile is selected → switch to A2DP.
        switched = await _ensure_a2dp_profile(address)
        if switched:
            sink_name = await _wait_for_bt_sink(address, timeout=4.0)

    if sink_name is None:
        # Case B: PulseAudio doesn't even know the device exists. This
        # is a well-known PulseAudio race — module-bluez5-discover misses
        # the device-added signal. Reload it to force a full re-scan.
        if await _reload_bluez_discover():
            sink_name = await _wait_for_bt_sink(address, timeout=5.0)
            if sink_name is None:
                # After reload we may still need to nudge the profile.
                if await _ensure_a2dp_profile(address):
                    sink_name = await _wait_for_bt_sink(address, timeout=4.0)

    if sink_name is None and reconnect is not None:
        # Case C: PulseAudio still doesn't see it. Cycle the BT
        # connection so BlueZ emits a fresh device-connected event that
        # PulseAudio can latch onto.
        log.info("forcing BT disconnect+reconnect to wake PulseAudio")
        try:
            await reconnect()
        except Exception:
            log.exception("reconnect callback raised")
        sink_name = await _wait_for_bt_sink(address, timeout=8.0)
        if sink_name is None and await _ensure_a2dp_profile(address):
            sink_name = await _wait_for_bt_sink(address, timeout=4.0)

    if sink_name is None:
        log.info("no BT sink appeared for %s after all fallbacks", address)
        return None

    rc, _ = await _run("pactl", "set-default-sink", sink_name)
    if rc != 0:
        log.info("set-default-sink %s rc=%s", sink_name, rc)
        return None
    log.info("audio: default sink → %s", sink_name)

    if move_existing_streams:
        for input_id in await _list_sink_inputs():
            await _run("pactl", "move-sink-input", input_id, sink_name)

    return sink_name
