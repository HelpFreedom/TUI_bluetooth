"""Virtual Bluetooth remote screen — Classic BT HID Device flow.

Mirrors the behaviour of the Android BtRemote app: we register a HID
Device profile, then *initiate* a connection to the chosen TV (it acts
as the HID Host). Keystrokes are forwarded as HID input reports over
the L2CAP Interrupt channel (PSM 19).
"""
from __future__ import annotations

import asyncio

from dbus_next import DBusError
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static

from .classic_hid import CC, KB, ClassicHidError, ClassicHidRemote


class RemoteScreen(Screen):
    """Virtual remote that pairs with a TV and forwards keystrokes as HID."""

    CSS = """
    RemoteScreen { background: $background; }
    #status { height: 3; padding: 1 1; background: $boost; color: $text; }
    #layout { height: 1fr; padding: 1 2; }
    #help   { height: auto; dock: bottom; padding: 0 2; color: $text-muted; }
    """

    BINDINGS = [
        # ---- D-pad / OK / Back ------------------------------------------
        Binding("up", "key_up", "↑", show=False),
        Binding("down", "key_down", "↓", show=False),
        Binding("left", "key_left", "←", show=False),
        Binding("right", "key_right", "→", show=False),
        Binding("enter", "key_enter", "OK", show=False),
        Binding("backspace", "cc_back", "Back", show=False),
        # ---- Power / sleep ----------------------------------------------
        Binding("p", "cc_power", "Power", show=False),
        # ---- Volume / mute ----------------------------------------------
        Binding("plus", "cc_vol_up", "Vol+", show=False),
        Binding("equals_sign", "cc_vol_up", "Vol+", show=False),
        Binding("minus", "cc_vol_down", "Vol-", show=False),
        Binding("m", "cc_mute", "Mute", show=False),
        # ---- Playback ---------------------------------------------------
        Binding("space", "cc_play_pause", "Play/Pause", show=False),
        Binding("comma", "cc_prev", "Prev", show=False),
        Binding("period", "cc_next", "Next", show=False),
        # ---- Channel ----------------------------------------------------
        Binding("pageup", "cc_channel_up", "Ch+", show=False),
        Binding("pagedown", "cc_channel_down", "Ch-", show=False),
        # ---- Home / Menu / Search ---------------------------------------
        Binding("h", "cc_home", "Home", show=False),
        Binding("u", "cc_menu", "Menu", show=False),
        Binding("slash", "cc_search", "Search", show=False),
        # ---- Lifecycle --------------------------------------------------
        Binding("c", "reconnect", "Переподключиться", show=True),
        Binding("escape", "close", "Закрыть", show=True),
        Binding("q", "close", "Закрыть", show=True),
        Binding("question_mark", "show_help", "Справка", show=True),
    ]

    def __init__(self, hid: ClassicHidRemote, target_path: str,
                 target_address: str, target_name: str) -> None:
        super().__init__()
        self.hid = hid
        self._target_path = target_path
        self._target_address = target_address
        self._target_name = target_name
        self._last_action_text = ""

    # ------------------------------------------------------------ compose

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        yield Static(self._diagram(), id="layout")
        yield Static(self._help_text(), id="help")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = f"Bluetui Remote → {self._target_name}"
        self.sub_title = "Classic BT HID"
        self._render_status("подключаюсь к ТВ…", "yellow")
        # Run the connection in the background so the UI stays responsive
        # even if pairing takes several seconds.
        asyncio.create_task(self._do_connect())

    async def on_unmount(self) -> None:
        try:
            await self.hid.disconnect()
        except Exception:
            pass
        # Restore the regular pairing agent capability that the App
        # switched to NoInputNoOutput on remote-screen open.
        try:
            self.app.restore_agent_capability()
        except Exception:
            pass

    # ---------------------------------------------------------- connection

    async def _do_connect(self) -> None:
        try:
            await self.hid.start_profile()
        except ClassicHidError as e:
            self._render_status(f"ошибка профиля: {e}", "red")
            self.app.notify(str(e), severity="error", timeout=8)
            return
        try:
            await self.hid.connect(self._target_path, self._target_address)
        except ClassicHidError as e:
            msg = str(e)
            self._render_status(f"не удалось подключиться: {msg}", "red")
            self.app.notify(
                f"Подключение к {self._target_name} не удалось:\n{msg}\n\n"
                "Проверь что ТВ включён и в радиусе действия Bluetooth.",
                severity="error", timeout=10,
            )
            return
        self._render_status(f"подключено: жми клавиши", "green")
        self.app.notify(
            f"Подключено к {self._target_name}. Можно нажимать клавиши.",
            timeout=4,
        )

    async def action_reconnect(self) -> None:
        self._render_status("переподключение…", "yellow")
        try:
            await self.hid.disconnect()
        except Exception:
            pass
        await self._do_connect()

    async def action_close(self) -> None:
        try:
            await self.hid.disconnect()
        except Exception:
            pass
        self.app.pop_screen()

    # ----------------------------------------------------------- send

    def _send_consumer(self, code: int, label: str) -> None:
        if not self.hid.is_connected:
            self.app.notify(
                "Нет соединения с ТВ. Нажми 'c' чтобы переподключиться.",
                severity="warning", timeout=4,
            )
            return
        if not self.hid.send_consumer(code):
            self.app.notify("Не удалось отправить команду — соединение разорвано.",
                            severity="error", timeout=4)
            return
        self._show_action(f"▶ {label}")

    def _send_key(self, keycode: int, label: str) -> None:
        if not self.hid.is_connected:
            self.app.notify(
                "Нет соединения с ТВ. Нажми 'c' чтобы переподключиться.",
                severity="warning", timeout=4,
            )
            return
        if not self.hid.send_key(keycode):
            self.app.notify("Не удалось отправить — соединение разорвано.",
                            severity="error", timeout=4)
            return
        self._show_action(f"▶ {label}")

    # ---------------------------------------------------- bound actions

    def action_key_up(self):       self._send_key(KB.UP, "↑")
    def action_key_down(self):     self._send_key(KB.DOWN, "↓")
    def action_key_left(self):     self._send_key(KB.LEFT, "←")
    def action_key_right(self):    self._send_key(KB.RIGHT, "→")
    def action_key_enter(self):    self._send_key(KB.ENTER, "OK")

    def action_cc_back(self):         self._send_consumer(CC.BACK, "Back")
    def action_cc_power(self):        self._send_consumer(CC.POWER, "Power")
    def action_cc_vol_up(self):       self._send_consumer(CC.VOLUME_UP, "Vol+")
    def action_cc_vol_down(self):     self._send_consumer(CC.VOLUME_DOWN, "Vol-")
    def action_cc_mute(self):         self._send_consumer(CC.MUTE, "Mute")
    def action_cc_play_pause(self):   self._send_consumer(CC.PLAY_PAUSE, "Play/Pause")
    def action_cc_prev(self):         self._send_consumer(CC.PREV_TRACK, "Prev")
    def action_cc_next(self):         self._send_consumer(CC.NEXT_TRACK, "Next")
    def action_cc_channel_up(self):   self._send_consumer(CC.CHANNEL_UP, "Ch+")
    def action_cc_channel_down(self): self._send_consumer(CC.CHANNEL_DOWN, "Ch-")
    def action_cc_home(self):         self._send_consumer(CC.HOME, "Home")
    def action_cc_menu(self):         self._send_consumer(CC.MENU, "Menu")
    def action_cc_search(self):       self._send_consumer(CC.AC_SEARCH, "Search")

    def action_show_help(self):
        self.app.notify(
            "Стрелки=D-pad, Enter=OK, Backspace=Back, Space=Play/Pause, "
            "+/-=Vol, m=Mute, p=Power, h=Home, u=Menu, ,/.=Prev/Next, "
            "PgUp/PgDn=Ch±, /=Search, c=переподключиться, Esc/q=закрыть",
            timeout=10,
        )

    # ----------------------------------------------------------- rendering

    def _render_status(self, text: str, colour: str) -> None:
        line = Text()
        line.append("ТВ: ", style="bold")
        line.append(self._target_name, style="bold cyan")
        line.append(f" ({self._target_address})", style="dim")
        line.append("    ")
        line.append("Статус: ", style="bold")
        line.append(text, style=f"bold {colour}")
        if self._last_action_text:
            line.append(f"    {self._last_action_text}", style="cyan")
        try:
            self.query_one("#status", Static).update(line)
        except Exception:
            pass

    def _show_action(self, text: str) -> None:
        self._last_action_text = text
        self._render_status("подключено: жми клавиши", "green")

        async def _clear() -> None:
            await asyncio.sleep(1.2)
            if self._last_action_text == text:
                self._last_action_text = ""
                self._render_status("подключено: жми клавиши", "green")
        asyncio.create_task(_clear())

    def _diagram(self) -> Text:
        t = Text()
        t.append("Раскладка пульта\n\n", style="bold cyan")
        rows = [
            ("Power",        "p",            "питание ТВ"),
            ("",             "",             ""),
            ("D-pad ↑",      "↑",            "вверх"),
            ("D-pad ↓",      "↓",            "вниз"),
            ("D-pad ←",      "←",            "влево"),
            ("D-pad →",      "→",            "вправо"),
            ("OK",           "Enter",        "выбор"),
            ("Back",         "Backspace",    "назад"),
            ("Home",         "h",            "домой"),
            ("Menu",         "u",            "меню"),
            ("Search",       "/",            "поиск"),
            ("",             "",             ""),
            ("Vol+",         "+ или =",      "громче"),
            ("Vol-",         "-",            "тише"),
            ("Mute",         "m",            "выкл звук"),
            ("",             "",             ""),
            ("Play/Pause",   "Space",        "плей/пауза"),
            ("Prev",         ",",            "пред. трек"),
            ("Next",         ".",            "след. трек"),
            ("",             "",             ""),
            ("Channel +",    "PgUp",         "канал +"),
            ("Channel -",    "PgDn",         "канал -"),
        ]
        for button, keys, desc in rows:
            if not button:
                t.append("\n")
                continue
            t.append(f"  {button:<14}", style="bold")
            t.append(f"{keys:<14}", style="yellow")
            t.append(f"{desc}\n", style="dim")
        return t

    def _help_text(self) -> Text:
        t = Text()
        t.append("c", style="bold yellow")
        t.append(" — переподключиться   ")
        t.append("?", style="bold yellow")
        t.append(" — справка   ")
        t.append("Esc / q", style="bold yellow")
        t.append(" — закрыть пульт")
        return t
