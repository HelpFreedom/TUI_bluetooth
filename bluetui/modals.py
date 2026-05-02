"""Modal dialogs: pairing prompts, device info, help, media control."""
from __future__ import annotations

import asyncio
from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from .icons import device_icon


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation."""

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    ConfirmModal Label {
        margin-bottom: 1;
    }
    ConfirmModal Grid {
        grid-size: 2;
        grid-gutter: 2;
        height: auto;
        margin-top: 1;
    }
    ConfirmModal Button {
        width: 100%;
    }
    """

    BINDINGS = [
        ("escape", "dismiss_no", "No"),
        ("y", "yes", "Yes"),
        ("n", "no", "No"),
    ]

    def __init__(self, title: str, message: str, yes_label: str = "Да",
                 no_label: str = "Нет", yes_variant: str = "primary"):
        super().__init__()
        self._title = title
        self._message = message
        self._yes_label = yes_label
        self._no_label = no_label
        self._yes_variant = yes_variant

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Text(self._title, style="bold"))
            yield Label(self._message)
            with Grid():
                yield Button(self._yes_label, id="yes", variant=self._yes_variant)
                yield Button(self._no_label, id="no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def action_dismiss_no(self) -> None:
        self.dismiss(False)


class PinModal(ModalScreen[Optional[str]]):
    """Prompt the user to enter a PIN or passkey."""

    DEFAULT_CSS = """
    PinModal {
        align: center middle;
    }
    PinModal > Vertical {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    PinModal Label {
        margin-bottom: 1;
    }
    PinModal Input {
        margin-bottom: 1;
    }
    PinModal Grid {
        grid-size: 2;
        grid-gutter: 2;
        height: auto;
    }
    PinModal Button {
        width: 100%;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, message: str, numeric: bool = False):
        super().__init__()
        self._title = title
        self._message = message
        self._numeric = numeric

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Text(self._title, style="bold"))
            yield Label(self._message)
            yield Input(placeholder="Введите код и нажмите Enter",
                        password=False, id="pin-input")
            with Grid():
                yield Button("OK", id="ok", variant="primary")
                yield Button("Отмена", id="cancel", variant="default")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit(self.query_one(Input).value)
        else:
            self.dismiss(None)

    def _submit(self, value: str) -> None:
        value = value.strip()
        if not value:
            self.dismiss(None)
            return
        if self._numeric and not value.isdigit():
            self.query_one(Input).value = ""
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class InfoModal(ModalScreen[None]):
    """Show device details."""

    DEFAULT_CSS = """
    InfoModal {
        align: center middle;
    }
    InfoModal > Vertical {
        width: 70;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    InfoModal Static {
        margin-bottom: 1;
    }
    InfoModal Button {
        width: 100%;
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, props: dict):
        super().__init__()
        self._props = props

    def compose(self) -> ComposeResult:
        p = self._props
        text = Text()
        text.append(f"{device_icon(p)}  ", style="bold")
        text.append(p.get("Alias") or p.get("Name") or "(unknown)",
                    style="bold cyan")
        text.append("\n\n")

        def row(label: str, value, style: str = "") -> None:
            text.append(f"  {label}: ", style="bold")
            text.append(f"{value}\n", style=style)

        row("Адрес", p.get("Address", "—"))
        row("Тип адреса", p.get("AddressType", "—"))
        row("Класс", p.get("Icon") or "—")
        row("Сопряжено", "да" if p.get("Paired") else "нет",
            "green" if p.get("Paired") else "")
        row("Подключено", "да" if p.get("Connected") else "нет",
            "green" if p.get("Connected") else "")
        row("Доверенное", "да" if p.get("Trusted") else "нет",
            "cyan" if p.get("Trusted") else "")
        row("Заблокировано", "да" if p.get("Blocked") else "нет",
            "red" if p.get("Blocked") else "")
        if "Battery" in p:
            row("Батарея", f"{p['Battery']}%")
        if p.get("RSSI") is not None:
            row("RSSI", f"{p['RSSI']} dBm")
        if p.get("TxPower") is not None:
            row("TxPower", f"{p['TxPower']} dBm")
        if p.get("Modalias"):
            row("Modalias", p["Modalias"])
        uuids = p.get("UUIDs") or []
        if uuids:
            text.append(f"\n  UUIDs ({len(uuids)}):\n", style="bold")
            for u in uuids[:10]:
                text.append(f"    {u}\n", style="dim")
            if len(uuids) > 10:
                text.append(f"    ... +{len(uuids) - 10} more\n", style="dim")

        with Vertical():
            yield Static(text)
            yield Button("Закрыть [Esc]", id="close", variant="primary")

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class HelpModal(ModalScreen[None]):
    """Reference card of key bindings."""

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }
    HelpModal > Vertical {
        width: 70;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    HelpModal Button {
        width: 100%;
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "dismiss", "Close"), ("question_mark", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        text = Text()
        text.append("Bluetooth TUI — горячие клавиши\n\n", style="bold cyan")

        sections = [
            ("Навигация", [
                ("↑ / ↓ / k / j", "переместить курсор по списку"),
                ("Tab / Shift+Tab", "переключить фокус"),
            ]),
            ("Действия с устройством", [
                ("Enter", "подключить / отключить (с авто-парингом если нужно)"),
                ("p", "сопрячь устройство (pair)"),
                ("f", "забыть устройство (forget / remove)"),
                ("t", "переключить «доверенное»"),
                ("b", "переключить «заблокировано»"),
                ("i", "показать информацию"),
                ("m", "медиа-управление (Play/Pause/Vol±)"),
                ("r", "пульт ТВ (BT HID — выдели ТВ и нажми R)"),
                ("Esc", "отменить идущее сопряжение"),
            ]),
            ("Адаптер", [
                ("s", "вкл/выкл сканирование"),
                ("o", "вкл/выкл питание адаптера"),
                ("v", "вкл/выкл видимость (discoverable)"),
            ]),
            ("Прочее", [
                ("?", "эта справка"),
                ("q / Ctrl+C", "выход"),
            ]),
        ]
        for title, rows in sections:
            text.append(f"{title}\n", style="bold yellow")
            for keys, desc in rows:
                text.append(f"  {keys:<22}", style="bold")
                text.append(f"{desc}\n")
            text.append("\n")

        with Vertical():
            yield Static(text)
            yield Button("Закрыть [Esc]", id="close", variant="primary")

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class MediaControlModal(ModalScreen[None]):
    """AVRCP media controls (Play / Pause / Stop / Next / Prev / Volume / FF / Rew).

    Sends commands via BlueZ MediaPlayer1 / MediaControl1. Whether the
    target device responds depends on its AVRCP implementation.
    """

    DEFAULT_CSS = """
    MediaControlModal {
        align: center middle;
    }
    MediaControlModal > Vertical {
        width: 64;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    MediaControlModal Label {
        margin-bottom: 1;
    }
    MediaControlModal #title {
        text-style: bold;
    }
    MediaControlModal #hint {
        color: $text-muted;
    }
    MediaControlModal Horizontal {
        height: auto;
        margin-bottom: 1;
    }
    MediaControlModal Button {
        width: 1fr;
        margin: 0 1 0 0;
    }
    MediaControlModal #close-row Button {
        width: 100%;
        margin: 0;
    }
    """

    BINDINGS = [
        ("space", "play_pause", "Play/Pause"),
        ("p", "play", "Play"),
        ("k", "play_pause", "Play/Pause"),
        ("o", "stop", "Stop"),
        ("n", "next", "Next"),
        ("comma", "previous", "Prev"),
        ("right", "fast_forward", "FF"),
        ("left", "rewind", "Rew"),
        ("plus", "vol_up", "Vol+"),
        ("equals_sign", "vol_up", "Vol+"),
        ("minus", "vol_down", "Vol-"),
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
    ]

    def __init__(self, app, device_path: str, props: dict):
        super().__init__()
        self._app_ref = app
        self._device_path = device_path
        self._props = props

    def compose(self) -> ComposeResult:
        from .icons import device_icon
        name = (self._props.get("Alias") or self._props.get("Name")
                or self._props.get("Address") or "device")
        with Vertical():
            yield Label(
                Text(f"{device_icon(self._props)}  {name}", style="bold cyan"),
                id="title",
            )
            yield Label(
                "AVRCP-команды через Bluetooth. Какие именно команды примет\n"
                "устройство — зависит от его прошивки.",
                id="hint",
            )
            with Horizontal():
                yield Button("⏮  Prev (,)", id="previous")
                yield Button("⏯  Play/Pause (Space)", id="play_pause",
                             variant="primary")
                yield Button("⏭  Next (n)", id="next")
            with Horizontal():
                yield Button("⏪  Rew (←)", id="rewind")
                yield Button("⏹  Stop (o)", id="stop")
                yield Button("⏩  FF (→)", id="fast_forward")
            with Horizontal():
                yield Button("♪- Vol- (-)", id="vol_down")
                yield Button("♪+ Vol+ (+)", id="vol_up")
            with Horizontal(id="close-row"):
                yield Button("Закрыть [Esc]", id="close", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "close":
            self.dismiss(None)
            return
        if bid == "play_pause":
            self.action_play_pause()
            return
        method = {
            "previous": "media_previous",
            "next": "media_next",
            "play": "media_play",
            "pause": "media_pause",
            "stop": "media_stop",
            "rewind": "media_rewind",
            "fast_forward": "media_fast_forward",
            "vol_up": "media_volume_up",
            "vol_down": "media_volume_down",
        }.get(bid)
        if method:
            self._send(method)

    def _send(self, method_name: str) -> None:
        bz = self._app_ref.bz

        async def runner():
            try:
                method = getattr(bz, method_name)
                await method(self._device_path)
            except Exception as e:
                self._app_ref.notify(f"AVRCP: {e}", severity="error", timeout=4)

        asyncio.create_task(runner())

    def action_play(self) -> None: self._send("media_play")
    def action_pause(self) -> None: self._send("media_pause")
    def action_stop(self) -> None: self._send("media_stop")
    def action_next(self) -> None: self._send("media_next")
    def action_previous(self) -> None: self._send("media_previous")
    def action_fast_forward(self) -> None: self._send("media_fast_forward")
    def action_rewind(self) -> None: self._send("media_rewind")
    def action_vol_up(self) -> None: self._send("media_volume_up")
    def action_vol_down(self) -> None: self._send("media_volume_down")

    def action_play_pause(self) -> None:
        # AVRCP "play" usually toggles play/pause on devices that report
        # state correctly. If yours doesn't, use Play / Stop / Pause buttons.
        self._send("media_play")

    def action_close(self) -> None:
        self.dismiss(None)
