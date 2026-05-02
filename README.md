# bluetui
![Превью](https://raw.githubusercontent.com/HelpFreedom/TUI_bluetooth/refs/heads/main/ChatGPT%20Image%20May%202%2C%202026%2C%2009_52_01%20AM.png)
Терминальный TUI-менеджер Bluetooth для Linux. Работает поверх **BlueZ** через системный **D-Bus**, без `sudo`.

```
┌─ Адаптер: hci0   ⏻ ON   ⌕ Скан OFF   ◉ Видимость OFF ─┐
│                                                         │
│ ● Подключено (1)                                        │
│  ●  🎧  Sony WH-1000XM4              AC:80:0A:…  bat 80%│
│                                                         │
│ ★ Сопряжено (2)                                         │
│  ○  📺  Smart TV (Зал)               9C:8E:CD:…       ★ │
│  ○  ⌨   Keychron K3                  DC:2C:26:…         │
│                                                         │
│ ⌕ Поблизости (3)                                        │
│  ·  ♪   JBL Flip 5                   ▂▄▆█  -54dBm       │
│  ·  ☏   Pixel 7                      ▂▄    -78dBm       │
└─────────────────────────────────────────────────────────┘
 Подключ./Отключ.  Сопрячь  Забыть  Инфо  Медиа  Пульт ТВ
```

## Возможности

- **Список устройств** с группировкой по статусу (подключено / сопряжено / поблизости), уровнем сигнала (RSSI) и зарядом батареи
- **Управление адаптером**: питание, видимость, запуск / остановка сканирования
- **Сопряжение и подключение** с диалогами PIN / passkey / подтверждения paskey (агент `org.bluez.Agent1` с `KeyboardDisplay`)
- **AVRCP-плеер** для подключённых аудиоустройств: play / pause / stop / next / prev / volume±
- **Авто-роутинг звука** на BT-наушники/колонки через `pactl` (работает и на PulseAudio, и на PipeWire)
- **Bluetooth HID-пульт для Smart TV** — приложение регистрирует Profile1 с HID-дескриптором (Consumer Control + Keyboard), открывает L2CAP Control + Interrupt и шлёт HID-репорты. Проверено на TCL Android TV; должно работать на Samsung/LG/Sony, поддерживающих BT HID.
- **Без sudo** — все операции идут через системный D-Bus, права даёт PolicyKit

## Скриншоты горячих клавиш

| Клавиша | Действие |
|--------|----------|
| `↑` / `↓` или `j` / `k` | Перемещение по списку |
| `Enter` | Подключить / отключить выделенное устройство |
| `p` | Сопрячь |
| `f` | Забыть (удалить сопряжение) |
| `i` | Информация об устройстве |
| `m` | AVRCP-медиаконтрол |
| `r` | Открыть экран Bluetooth-пульта (для ТВ) |
| `s` | Включить / выключить сканирование |
| `o` | Питание адаптера on/off |
| `t` | Доверять / не доверять |
| `b` | Заблокировать |
| `?` | Справка |
| `q` | Выход |

## Требования

- Linux с BlueZ ≥ 5.50 (Debian 11+, Ubuntu 20.04+, Arch — должно работать «из коробки»)
- Python ≥ 3.9
- Системный сервис `bluetoothd` запущен: `systemctl status bluetooth`
- Для авто-роутинга звука: `pulseaudio` или `pipewire-pulse` + `pactl`
- Для эмодзи-иконок: моноширинный шрифт с поддержкой `📺 🎧 ⌨` (например **JetBrains Mono**, **Fira Code Nerd**, **DejaVu Sans Mono**)

Python-зависимости (`requirements.txt`):

```
textual>=2.0
dbus-next>=0.2.3
```

## Установка

```bash
git clone https://github.com/HelpFreedom/bluetui.git
cd bluetui
pip3 install --user -r requirements.txt
./bluetui.sh
```

Опционально — короткий алиас в `PATH`:

```bash
ln -s "$(realpath ./bluetui.sh)" ~/.local/bin/bt
# теперь:
bt
```

(Убедись что `~/.local/bin` есть в `$PATH`.)

## Bluetooth-пульт для Smart TV

В отличие от ИК-пультов или Android TV Remote v2 (по WiFi), bluetui реализует **настоящий Bluetooth-пульт**: компьютер представляется ТВ как HID-устройство (как фирменный пульт от того же ТВ).

**Как пользоваться:**

1. На ТВ включи режим обнаружения Bluetooth (обычно: *Настройки → Пульты → Добавить устройство*).
2. В bluetui нажми `s` для сканирования. Когда ТВ появится в списке «Поблизости» — выдели его и нажми `r`.
3. Откроется экран пульта. JustWorks-сопряжение пройдёт автоматически (агент переключается на `NoInputNoOutput`).
4. Используй стрелки, Enter, Esc, цифры, кнопки громкости — всё транслируется в HID-репорты.

**Технические детали:** регистрируется `org.bluez.Profile1` с UUID `0x1124` (HID), разворачивается SDP-запись с дескриптором (Consumer Control Report ID 1 + Keyboard Report ID 2), открываются raw L2CAP-сокеты на PSM 17 (Control) и PSM 19 (Interrupt) через `socket.AF_BLUETOOTH` + `BTPROTO_L2CAP`. После подключения профили A2DP/AVRCP/HSP принудительно отключаются, чтобы звук с ТВ не уходил в колонки ПК.

## Архитектура

```
bluetui/
├── __main__.py          # python -m bluetui → main()
├── app.py               # BluetuiApp — основное Textual-приложение
├── bluez.py             # BluezManager — обёртка над D-Bus / BlueZ ObjectManager
├── agent.py             # org.bluez.Agent1 для PIN/passkey-диалогов
├── widgets.py           # AdapterBar, DeviceList
├── modals.py            # ConfirmModal, PinModal, InfoModal, HelpModal, MediaControlModal
├── icons.py             # Маппинг BlueZ Icon → Unicode-глиф
├── audio.py             # Авто-роутинг звука через pactl
├── classic_hid.py       # Classic-BT HID Device profile (пульт для ТВ)
├── hid_remote.py        # BLE HID over GATT (legacy, сохранён для тестов)
├── remote_screen.py     # UI-экран виртуального пульта
└── style.tcss           # Глобальные стили (Textual CSS)
```

Главные сторонние зависимости:
- **[Textual](https://textual.textualize.io/)** — TUI-фреймворк
- **[dbus-next](https://github.com/altdesktop/python-dbus-next)** — асинхронный D-Bus

## Известные ограничения

- **HID-пульт работает только с ТВ, поддерживающими BT HID-host профиль.** Большинство Android TV / Tizen / WebOS поддерживают, но не все.
- **Авто-роутинг звука** иногда требует двух попыток подключения — это известная гонка между BlueZ и PulseAudio. Приложение делает retry автоматически.
- **Громкость на наушниках** регулируется не через AVRCP-абсолютную громкость, а через системный микшер. Это удобнее в большинстве случаев.

## Лог

Логи пишутся в `~/.cache/bluetui.log` (truncate при каждом запуске). Туда уходят все ошибки D-Bus, события подключения, диагностика звукового роутинга и HID-канала.

## Лицензия

GNU General Public License v3.0 — см. [`LICENSE`](LICENSE).
