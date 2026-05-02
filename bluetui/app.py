"""Main Textual application."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from dbus_next import DBusError
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header

from .agent import PairingAgent, register_agent, unregister_agent
from .bluez import BluezError, BluezManager
from .modals import ConfirmModal, HelpModal, InfoModal, PinModal
from .widgets import AdapterBar, DeviceList

log = logging.getLogger(__name__)


class BluetuiApp(App):
    CSS_PATH = "style.tcss"
    TITLE = "Bluetooth TUI"
    SUB_TITLE = "BlueZ via D-Bus"

    BINDINGS = [
        Binding("enter", "primary", "Подключ./Отключ.", show=True),
        Binding("p", "pair", "Сопрячь", show=True),
        Binding("f", "forget", "Забыть", show=True),
        Binding("t", "trust", "Доверять", show=False),
        Binding("b", "block", "Блок.", show=False),
        Binding("i", "info", "Инфо", show=True),
        Binding("m", "media", "Медиа", show=True),
        Binding("r", "remote", "Пульт ТВ", show=True),
        Binding("s", "toggle_scan", "Скан", show=True),
        Binding("o", "toggle_power", "Питание", show=True),
        Binding("v", "toggle_visible", "Видимость", show=False),
        Binding("escape", "cancel", "Отмена", show=False),
        Binding("question_mark", "help", "Справка", show=True),
        Binding("q", "quit", "Выход", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    PAIR_TIMEOUT = 45.0  # seconds

    def __init__(self) -> None:
        # ansi_color=True activates Textual's `:ansi` mode where colors
        # are kept as ANSI escape codes (not converted to RGB). That's
        # what makes `ansi_default` literally emit "no background" so a
        # transparent terminal stays transparent under the app.
        super().__init__(ansi_color=True)
        self.bz = BluezManager()
        self._agent: PairingAgent | None = None
        self._refresh_scheduled = False
        self._pair_task: asyncio.Task | None = None
        self._pair_path: str | None = None
        self._pair_name: str = ""
        # Singleton Classic-BT HID profile manager (registered once, shared
        # between RemoteScreen opens).
        self._classic_hid = None

    # --------------------------------------------------------- compose / mount

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main"):
            yield AdapterBar(id="adapter-bar")
            yield DeviceList(id="device-list")
        yield Footer()

    async def on_mount(self) -> None:
        self.theme = "textual-dark"
        try:
            await self.bz.connect()
        except BluezError as e:
            self.notify(str(e), title="Ошибка Bluetooth", severity="error",
                        timeout=10)
            return
        except DBusError as e:
            self.notify(f"D-Bus: {e.text or e}", title="Ошибка",
                        severity="error", timeout=10)
            return
        except Exception as e:
            self.notify(f"{type(e).__name__}: {e}", title="Ошибка",
                        severity="error", timeout=10)
            return

        self.bz.on("adapter_changed", self._on_adapter_changed)
        self.bz.on("device_added", lambda *_: self._schedule_refresh())
        self.bz.on("device_removed", lambda *_: self._schedule_refresh())
        self.bz.on("device_changed", lambda *_: self._schedule_refresh())
        # Audio routing: any time an audio device transitions to Connected,
        # try to make it the default sink.
        self._connected_audio_seen: set[str] = set()
        self.bz.on("device_added", self._maybe_route_audio)
        self.bz.on("device_changed", self._maybe_route_audio)

        self._render_adapter()
        self._render_devices()
        self.query_one(DeviceList).focus()

        # Try to register a pairing agent. Failure is non-fatal.
        self._agent = PairingAgent(self._on_agent_request)
        ok, msg = await register_agent(self.bz.bus, self._agent,
                                       capability="KeyboardDisplay")
        if not ok:
            log.info("agent not registered: %s", msg)

    async def on_unmount(self) -> None:
        if self._classic_hid is not None:
            try:
                await self._classic_hid.stop_profile()
            except Exception:
                pass
            self._classic_hid = None
        try:
            if self.bz.bus is not None:
                await unregister_agent(self.bz.bus)
        except Exception:
            pass
        await self.bz.disconnect()

    # ----------------------------------------------------------- rendering

    def _on_adapter_changed(self, _props: dict) -> None:
        self._render_adapter()
        self._schedule_refresh()

    def _maybe_route_audio(self, path: str, p: dict) -> None:
        """Auto-route audio for any audio device that becomes connected.

        Fires on both device_added (covers devices already connected at
        startup) and device_changed (covers user-initiated connects and
        reconnects). De-duplicated per device path so each transition
        triggers at most one routing attempt.
        """
        from .audio import looks_like_audio_sink, route_audio_to
        connected = bool(p.get("Connected"))
        if not connected:
            self._connected_audio_seen.discard(path)
            return
        if path in self._connected_audio_seen:
            return
        if not looks_like_audio_sink(p):
            return
        self._connected_audio_seen.add(path)
        address = p.get("Address") or ""
        if not address:
            return
        name = self._dev_name(p)

        async def _reconnect() -> None:
            try:
                await self.bz.disconnect_device(path)
            except Exception:
                pass
            await asyncio.sleep(1.2)
            try:
                await self.bz.connect_device(path)
            except Exception:
                pass

        async def _route() -> None:
            sink = await route_audio_to(address, p, reconnect=_reconnect)
            if sink:
                self.notify(f"Звук → {name}", timeout=3)

        self._spawn(_route())

    def _render_adapter(self) -> None:
        try:
            self.query_one(AdapterBar).update_adapter(self.bz.adapter_props)
        except Exception:
            # Widget not mounted yet, or app shutting down.
            pass

    def _render_devices(self) -> None:
        try:
            self.query_one(DeviceList).render_devices(self.bz.devices)
        except Exception:
            pass

    def _schedule_refresh(self) -> None:
        """Coalesce many rapid events into a single render."""
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        self.set_timer(0.12, self._do_refresh)

    def _do_refresh(self) -> None:
        self._refresh_scheduled = False
        self._render_adapter()
        self._render_devices()

    # ------------------------------------------------------------- helpers

    def _selected(self) -> tuple[str | None, dict]:
        # query_one fails if DeviceList isn't on the active screen (e.g. the
        # Remote screen is open). App-level bindings can fire there too —
        # treat that as "no device selected" rather than crashing.
        try:
            path = self.query_one(DeviceList).get_selected_path()
        except Exception:
            return None, {}
        if path is None or path not in self.bz.devices:
            return None, {}
        return path, self.bz.devices[path]

    def _dev_name(self, props: dict) -> str:
        return props.get("Alias") or props.get("Name") or props.get("Address") or "device"

    async def _ask(self, screen):
        """Push a modal screen and await its dismissal value.

        Unlike ``push_screen_wait`` this does not require a worker context,
        so it can be called from regular action handlers and D-Bus
        callbacks.
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _on_dismiss(value):
            if not fut.done():
                fut.set_result(value)

        self.push_screen(screen, _on_dismiss)
        return await fut

    async def _run(self, coro, success: str | None = None,
                   failure_prefix: str = "Ошибка") -> bool:
        try:
            await coro
            if success:
                self.notify(success)
            return True
        except DBusError as e:
            text = e.text or str(e)
            self.notify(f"{failure_prefix}: {text}", severity="error")
        except Exception as e:
            self.notify(f"{failure_prefix}: {e}", severity="error")
        return False

    # ------------------------------------------------------ device actions

    def _spawn(self, coro) -> asyncio.Task:
        """Run a coroutine in the background. Exceptions are surfaced via _run."""
        return asyncio.create_task(coro)

    async def action_primary(self) -> None:
        path, p = self._selected()
        if path is None:
            return
        self._spawn(self._do_primary(path, p))

    async def _do_primary(self, path: str, p: dict) -> None:
        name = self._dev_name(p)
        if p.get("Connected"):
            self.notify(f"Отключаю {name}…")
            await self._run(
                self.bz.disconnect_device(path),
                success=f"Отключено: {name}",
                failure_prefix="Не удалось отключить",
            )
            return
        if not p.get("Paired"):
            ok = await self._pair_with_timeout(path, name)
            if not ok:
                return
            # auto-trust freshly paired devices for smoother reconnects
            await self._run(
                self.bz.set_trusted(path, True),
                failure_prefix="Не удалось пометить доверенным",
            )
        self.notify(f"Подключаю к {name}…")
        log.info("connect: name=%s path=%s icon=%s",
                 name, path, p.get("Icon"))
        ok = await self._run(
            self.bz.connect_device(path),
            success=f"Подключено: {name}",
            failure_prefix="Не удалось подключить",
        )
        log.info("connect: ok=%s connected=%s", ok,
                 self.bz.devices.get(path, {}).get("Connected"))
        if ok:
            # Auto-route audio to BT speakers/headphones once they're
            # connected. No-op for non-audio devices.
            from .audio import route_audio_to
            address = p.get("Address") or ""
            if address:
                # Use the freshest props (Icon may have appeared after Connect).
                fresh_props = dict(self.bz.devices.get(path, p))
                async def _route():
                    sink = await route_audio_to(address, fresh_props)
                    if sink:
                        self.notify(f"Звук → {name}", timeout=3)
                self._spawn(_route())

    async def action_pair(self) -> None:
        path, p = self._selected()
        if path is None:
            return
        name = self._dev_name(p)
        if p.get("Paired"):
            self.notify(f"{name} уже сопряжено")
            return
        self._spawn(self._pair_with_timeout(path, name))

    async def _pair_with_timeout(self, path: str, name: str) -> bool:
        """Pair with a hard timeout and Esc-cancel support.

        Returns True on success, False on timeout / failure / cancellation.
        """
        if self._pair_task and not self._pair_task.done():
            self.notify("Уже идёт сопряжение — нажмите Esc для отмены",
                        severity="warning")
            return False
        self._pair_path = path
        self._pair_name = name
        self.notify(f"Сопрягаю с {name}… (Esc — отмена, таймаут "
                    f"{int(self.PAIR_TIMEOUT)}с)")
        inner: asyncio.Task = asyncio.create_task(self.bz.pair_device(path))
        self._pair_task = inner
        try:
            await asyncio.wait_for(asyncio.shield(inner), self.PAIR_TIMEOUT)
            self.notify(f"Сопряжено: {name}")
            return True
        except asyncio.TimeoutError:
            self.notify(
                f"Сопряжение с {name}: таймаут — устройство не ответило",
                severity="warning", timeout=8,
            )
            await self._cancel_pair(path, inner)
            return False
        except asyncio.CancelledError:
            self.notify(f"Сопряжение с {name} отменено", severity="warning")
            await self._cancel_pair(path, inner)
            return False
        except DBusError as e:
            self.notify(f"Сопряжение не удалось: {e.text or e}",
                        severity="error", timeout=8)
            return False
        except Exception as e:
            self.notify(f"Сопряжение не удалось: {e}",
                        severity="error", timeout=8)
            return False
        finally:
            if self._pair_task is inner:
                self._pair_task = None
                self._pair_path = None
                self._pair_name = ""

    async def _cancel_pair(self, path: str, inner: asyncio.Task) -> None:
        if not inner.done():
            inner.cancel()
        try:
            await self.bz.cancel_pairing(path)
        except Exception:
            pass

    async def action_cancel(self) -> None:
        """Escape: cancel ongoing pairing if any."""
        if self._pair_task and not self._pair_task.done():
            self._pair_task.cancel()

    async def action_forget(self) -> None:
        path, p = self._selected()
        if path is None:
            return
        if not p.get("Paired"):
            self.notify("Это устройство не сопряжено")
            return
        name = self._dev_name(p)

        def on_confirm(ok: bool | None) -> None:
            if ok:
                asyncio.create_task(self._do_forget(path, name))

        self.push_screen(
            ConfirmModal(
                title="Удалить устройство?",
                message=f"Удалить «{name}» из списка сопряжённых?\n"
                        f"Если оно подключено, оно будет отключено.",
                yes_label="Удалить",
                no_label="Отмена",
                yes_variant="error",
            ),
            on_confirm,
        )

    async def _do_forget(self, path: str, name: str) -> None:
        await self._run(
            self.bz.remove_device(path),
            success=f"Удалено: {name}",
            failure_prefix="Не удалось удалить",
        )

    async def action_trust(self) -> None:
        path, p = self._selected()
        if path is None:
            return
        new_value = not bool(p.get("Trusted"))
        self._spawn(self._run(
            self.bz.set_trusted(path, new_value),
            success=f"Доверять: {'да' if new_value else 'нет'}",
            failure_prefix="Не удалось изменить",
        ))

    async def action_block(self) -> None:
        path, p = self._selected()
        if path is None:
            return
        new_value = not bool(p.get("Blocked"))
        self._spawn(self._run(
            self.bz.set_blocked(path, new_value),
            success=f"Заблокировано: {'да' if new_value else 'нет'}",
            failure_prefix="Не удалось изменить",
        ))

    async def action_info(self) -> None:
        path, p = self._selected()
        if path is None:
            return
        self.push_screen(InfoModal(p))

    async def action_remote(self) -> None:
        if self.bz.bus is None or self.bz.adapter_path is None:
            self.notify("Адаптер недоступен", severity="error")
            return
        from .classic_hid import ClassicHidRemote
        from .remote_screen import RemoteScreen
        if isinstance(self.screen, RemoteScreen):
            return  # already open
        # Need a target device — the user must navigate to the TV in the
        # main list first. (We don't auto-pick because connecting via HID
        # affects power state on the TV.)
        path, props = self._selected()
        if path is None:
            self.notify(
                "Выбери ТВ в списке (стрелками) и снова нажми R.",
                severity="warning", timeout=6,
            )
            return
        address = props.get("Address") or ""
        if not address:
            self.notify("Не удалось получить MAC-адрес устройства",
                        severity="error")
            return
        name = self._dev_name(props)
        if self._classic_hid is None:
            self._classic_hid = ClassicHidRemote(self.bz.bus)
        # JustWorks pairing while the remote screen is active — same as
        # commercial BT remotes.
        await self._switch_agent_capability("NoInputNoOutput")
        await self.push_screen(RemoteScreen(
            self._classic_hid, path, address, name,
        ))

    async def _switch_agent_capability(self, capability: str) -> None:
        if self.bz.bus is None:
            return
        try:
            await unregister_agent(self.bz.bus)
        except Exception:
            pass
        self._agent = PairingAgent(self._on_agent_request)
        ok, msg = await register_agent(self.bz.bus, self._agent,
                                       capability=capability)
        if not ok:
            log.info("agent re-register (%s) failed: %s", capability, msg)

    def restore_agent_capability(self) -> None:
        """Called by RemoteScreen.on_unmount to switch back to KeyboardDisplay.

        Sync wrapper that schedules the actual async restore — RemoteScreen
        unmount is invoked synchronously by Textual in some paths.
        """
        if self.bz.bus is None:
            return
        asyncio.create_task(self._switch_agent_capability("KeyboardDisplay"))

    async def action_media(self) -> None:
        path, p = self._selected()
        if path is None:
            return
        if not self.bz.has_media(path):
            self.notify(
                "Это устройство не предоставляет AVRCP.\n"
                "Подключитесь к нему и попробуйте снова.",
                severity="warning", timeout=6,
            )
            return
        # Lazy import to avoid circular module dependencies.
        from .modals import MediaControlModal
        self.push_screen(MediaControlModal(self, path, p))

    # ----------------------------------------------------- adapter actions

    async def action_toggle_scan(self) -> None:
        if self.bz.adapter_path is None:
            return
        if self.bz.adapter_props.get("Discovering"):
            self._spawn(self._run(self.bz.stop_discovery(),
                        success="Сканирование остановлено",
                        failure_prefix="Не удалось остановить"))
        else:
            async def start_scan():
                if not self.bz.adapter_props.get("Powered"):
                    await self._run(self.bz.set_powered(True),
                                    failure_prefix="Не удалось включить адаптер")
                await self._run(self.bz.start_discovery(),
                                success="Сканирование запущено",
                                failure_prefix="Не удалось начать скан")
            self._spawn(start_scan())

    async def action_toggle_power(self) -> None:
        if self.bz.adapter_path is None:
            return
        new_value = not bool(self.bz.adapter_props.get("Powered"))
        self._spawn(self._run(
            self.bz.set_powered(new_value),
            success=f"Адаптер: {'включён' if new_value else 'выключен'}",
            failure_prefix="Не удалось переключить питание",
        ))

    async def action_toggle_visible(self) -> None:
        if self.bz.adapter_path is None:
            return
        new_value = not bool(self.bz.adapter_props.get("Discoverable"))
        self._spawn(self._run(
            self.bz.set_discoverable(new_value),
            success=f"Видимость: {'да' if new_value else 'нет'}",
            failure_prefix="Не удалось переключить видимость",
        ))

    async def action_help(self) -> None:
        self.push_screen(HelpModal())

    # ---------------------------------------------------------- agent UI

    async def _on_agent_request(self, kind: str, device_path: str,
                                value: Any) -> Any:
        """Bridge from BlueZ agent to Textual modals."""
        props = self.bz.devices.get(device_path, {})
        name = self._dev_name(props)
        if kind == "pin":
            return await self._ask(
                PinModal(
                    title="Запрос PIN-кода",
                    message=f"Устройство «{name}» запрашивает PIN-код.",
                    numeric=False,
                )
            )
        if kind == "passkey":
            return await self._ask(
                PinModal(
                    title="Запрос passkey",
                    message=f"Устройство «{name}» запрашивает passkey (число).",
                    numeric=True,
                )
            )
        if kind == "confirm":
            return await self._ask(
                ConfirmModal(
                    title="Подтверждение паринга",
                    message=f"Устройство «{name}» предлагает passkey:\n\n"
                            f"        {value:06d}\n\n"
                            f"Совпадает ли он с тем, что показано на устройстве?",
                    yes_label="Да, совпадает",
                    no_label="Нет",
                    yes_variant="success",
                )
            )
        if kind == "authorize":
            return await self._ask(
                ConfirmModal(
                    title="Запрос подключения",
                    message=f"Разрешить «{name}» подключиться?",
                    yes_label="Разрешить",
                    no_label="Отклонить",
                    yes_variant="success",
                )
            )
        if kind == "authorize_service":
            # Auto-allow services for already-paired devices.
            return True
        if kind == "display_pin":
            self.notify(f"PIN: {value} (введите на устройстве)",
                        title=f"Паринг: {name}", timeout=15)
            return None
        if kind == "display_passkey":
            passkey, _entered = value
            self.notify(f"Passkey: {passkey:06d} (введите на устройстве)",
                        title=f"Паринг: {name}", timeout=15)
            return None
        return None


def main() -> int:
    log_path = Path.home() / ".cache" / "bluetui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        filename=log_path,
        filemode="w",  # truncate on each run for easier debugging
    )
    log.info("bluetui starting")
    try:
        BluetuiApp().run()
    except KeyboardInterrupt:
        return 130
    return 0
