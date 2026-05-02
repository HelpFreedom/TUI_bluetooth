"""Custom Textual widgets for bluetui."""
from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .icons import device_icon, signal_bars


class AdapterBar(Static):
    """One-line summary of adapter state and quick toggles."""

    DEFAULT_CSS = """
    AdapterBar {
        height: 1;
        padding: 0 1;
        background: ansi_default;
        color: $text;
    }
    """

    def update_adapter(self, props: dict) -> None:
        name = props.get("Alias") or props.get("Name") or "(unknown adapter)"
        addr = props.get("Address", "")
        powered = bool(props.get("Powered"))
        discovering = bool(props.get("Discovering"))
        discoverable = bool(props.get("Discoverable"))

        line = Text()
        line.append("Адаптер ", style="bold")
        line.append(f"{name}", style="bold cyan")
        if addr:
            line.append(f" {addr}", style="dim")
        line.append("    ")
        line.append("⏻ ", style="bold")
        line.append("ON" if powered else "OFF",
                    style="bold green" if powered else "bold red")
        line.append("    ")
        line.append("⌕ Скан ", style="bold")
        line.append("ON" if discovering else "OFF",
                    style="bold yellow" if discovering else "dim")
        line.append("    ")
        line.append("◉ Видимость ", style="bold")
        line.append("ON" if discoverable else "OFF",
                    style="bold yellow" if discoverable else "dim")
        self.update(line)


def _device_name(p: dict) -> str:
    return p.get("Alias") or p.get("Name") or p.get("Address") or "(unknown)"


def _device_sort_key_paired(item):
    _, p = item
    return _device_name(p).lower()


def _device_sort_key_available(item):
    _, p = item
    rssi = p.get("RSSI")
    # higher RSSI (less negative) first; None last.
    rssi_key = -rssi if rssi is not None else 1_000_000
    return (rssi_key, _device_name(p).lower())


class DeviceList(OptionList):
    """Single OptionList rendering devices grouped by status."""

    DEFAULT_CSS = """
    DeviceList {
        height: 1fr;
        padding: 0 1;
        border: round $primary 30%;
        background: ansi_default;
    }
    DeviceList:focus {
        border: round $accent;
    }
    """

    def __init__(self, **kw):
        super().__init__(**kw)

    def get_selected_path(self) -> str | None:
        if self.highlighted is None:
            return None
        try:
            opt = self.get_option_at_index(self.highlighted)
        except Exception:
            return None
        return opt.id

    def render_devices(self, devices: dict[str, dict]) -> None:
        connected, paired, available = [], [], []
        for path, p in devices.items():
            if p.get("Connected"):
                connected.append((path, p))
            elif p.get("Paired"):
                paired.append((path, p))
            else:
                available.append((path, p))

        connected.sort(key=_device_sort_key_paired)
        paired.sort(key=_device_sort_key_paired)
        available.sort(key=_device_sort_key_available)

        # Remember current highlighted device to restore after rebuild.
        prev_path = self.get_selected_path()

        new_options: list[Option] = []

        if connected:
            new_options.append(self._header(f"● Подключено ({len(connected)})", "green"))
            for path, p in connected:
                new_options.append(self._device_option(path, p))
        else:
            new_options.append(self._header("● Подключено (нет)", "green dim"))

        new_options.append(self._spacer())

        if paired:
            new_options.append(self._header(f"★ Сопряжено ({len(paired)})", "cyan"))
            for path, p in paired:
                new_options.append(self._device_option(path, p))

        new_options.append(self._spacer())

        if available:
            new_options.append(self._header(f"⌕ Поблизости ({len(available)})", "yellow"))
            for path, p in available:
                new_options.append(self._device_option(path, p))
        else:
            new_options.append(self._header(
                "⌕ Поблизости (нажмите 's' для сканирования)", "yellow dim"
            ))

        self.clear_options()
        self.add_options(new_options)

        # Restore highlight to the same device path if still present.
        if prev_path:
            for i in range(self.option_count):
                opt = self.get_option_at_index(i)
                if opt.id == prev_path:
                    self.highlighted = i
                    return
        # Otherwise, highlight first selectable item.
        for i in range(self.option_count):
            opt = self.get_option_at_index(i)
            if not opt.disabled:
                self.highlighted = i
                return

    # ------------------------------------------------------------- internals

    def _header(self, text: str, style: str) -> Option:
        return Option(Text(text, style=f"bold {style}"), disabled=True)

    def _spacer(self) -> Option:
        return Option(Text(""), disabled=True)

    def _device_option(self, path: str, p: dict) -> Option:
        icon = device_icon(p)
        name = _device_name(p)
        addr = p.get("Address", "")
        bat = p.get("Battery")
        rssi = p.get("RSSI")
        connected = p.get("Connected")
        paired = p.get("Paired")
        trusted = p.get("Trusted")

        line = Text()
        # leading status mark
        if connected:
            line.append("●  ", style="bold green")
        elif paired:
            line.append("○  ", style="cyan")
        else:
            line.append("·  ", style="dim")

        line.append(f"{icon}  ")
        line.append(name, style="bold" if connected else "")

        # Right-side metadata, padded to align roughly.
        meta = Text()
        meta.append(f"  {addr}", style="dim")
        if bat is not None:
            colour = "green" if bat > 30 else ("yellow" if bat > 15 else "red")
            meta.append(f"  bat {bat}%", style=colour)
        if rssi is not None and not connected:
            meta.append(f"  {signal_bars(rssi)}", style="yellow")
            meta.append(f" {rssi}dBm", style="dim yellow")
        if trusted and not connected:
            meta.append("  ★", style="cyan")

        line.append_text(meta)
        return Option(line, id=path)
