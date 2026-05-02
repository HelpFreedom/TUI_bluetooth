"""Map BlueZ device icon strings to glyphs."""
from __future__ import annotations

ICON_MAP = {
    # Color emoji that render reliably across most terminal fonts.
    "audio-card": "🎧",
    "audio-headphones": "🎧",
    "audio-headset": "🎧",
    "input-keyboard": "⌨ ",
    "input-mouse": "🖱 ",

    # For categories where the colour-emoji glyph (📱 💻 🔊 etc.) isn't
    # in the default Debian font, use plain Misc Symbols characters that
    # are present in pretty much every monospace font.
    "audio-speakers": "♪ ",
    "audio-input-microphone": "♫ ",
    "phone": "☏ ",
    "computer": "▣ ",
    "computer-laptop": "▣ ",
    "input-tablet": "✎ ",
    "input-gaming": "◈ ",
    "camera-photo": "◉ ",
    "camera-video": "◉ ",
    "printer": "⎙ ",
    "scanner": "⎙ ",
    "multimedia-player": "♪ ",
    "network-wireless": "≋ ",
    "modem": "≋ ",
}

DEFAULT_ICON = "● "


_TV_HINTS = (
    "tv", "телевизор", "smart tv", "smarttv", "smart-tv",
    "телек", "samsung", "lg ", "tcl ", "sony bravia", "xiaomi tv",
    "hisense", "philips", "panasonic",
)


def device_icon(props: dict) -> str:
    """Return a glyph for a device based on its BlueZ Icon property.

    Falls back to a generic dot if Icon is missing or unrecognised.
    Many TVs report Class-of-Device as audio-card, so we also probe
    the device name for TV brand keywords and override.
    """
    name = (props.get("Alias") or props.get("Name") or "").lower()
    if any(hint in name for hint in _TV_HINTS):
        return "📺"
    return ICON_MAP.get(props.get("Icon", ""), DEFAULT_ICON)


def signal_bars(rssi: int | None) -> str:
    """Render an RSSI value as a 4-bar signal indicator."""
    if rssi is None:
        return "    "
    if rssi >= -55:
        return "▂▄▆█"
    if rssi >= -70:
        return "▂▄▆ "
    if rssi >= -85:
        return "▂▄  "
    return "▂   "
