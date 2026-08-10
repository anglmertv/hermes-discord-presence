#!/usr/bin/env python3
"""
Hermes Agent -> Discord Rich Presence (v8: config-driven + reliable)

Что показывает в Discord-профиле (по шаблону из config.json):
  * details  -> "Model: <текущая модель>"  (живая, из agent.log)
  * state    -> рендерится по status_template, поля см. ниже
  * start    -> таймер от начала сессии
  * очистка  -> когда окно Hermes закрыто, presence пропадает за ~poll сек
  * мутекс   -> один экземпляр; второй пишет в .log и выходит
  * лог      -> ~/hermes_presence.log

Надёжность (v7):
  * пропавшие state.db/agent.log не роняют процесс: логируем причину
    (с дебаунсом) и продолжаем пробовать каждый цикл
  * reconnect к Discord: при потере связи Presence пересоздаётся с нуля,
    Discord закрылся -> ждём и подключаемся сам
  * защита от смены формата лога: нет совпадений -> "Model: unknown",
    БЕЗ показа устаревшей модели из state.db
  * stale-детект: и agent.log, и state.db не обновляются дольше stale_after
    -> статус "stale", а не фальшиво-"актуальный"

Конфигурация БЕЗ ПРАВКИ ИСХОДНИКОВ (Приоритет 2):
  Файл hermes_presence.json рядом со скриптом. Ключи необязательны, в путях
  раскрываются %VAR% из окружения (напр. %LOCALAPPDATA%). Полная схема:
      {
        "hermes_path": "%LOCALAPPDATA%\\hermes",   // или явные пути ниже
        "state_db":  "<явный путь к state.db>",     // опционально
        "agent_log": "<явный путь к agent.log>",    // опционально
        "discord_app_id": "<app id>",
        "status_template": "{action} • {tokens} tok • ${cost}",
        "poll_interval": 5,      // сек между циклами
        "stale_after": 900       // сек без обновления данных -> stale
      }
  Поля шаблона status_template:
      {action} {model} {msgs} {tokens} {cost}
  Приоритет значений:  env  >  config.json  >  встроенные дефолты.
  CLI: --app-id / --interval перекрывают config (но не env).

Тест: --dry-run (без Discord, без мутекса)
"""
import argparse
import ctypes
import json
import os
import re
import sqlite3
import sys
import time
from ctypes import wintypes

# Tray-иконка — опционально. Если pystray недоступен, работаем без трея.
try:
    import pystray
    from PIL import Image, ImageDraw
    PYSTRAY_AVAILABLE = True
except Exception:
    PYSTRAY_AVAILABLE = False

try:
    from pypresence import Presence
except ImportError:
    print("pypresence ne ustanovlen (pip install pypresence)", file=sys.stderr)
    sys.exit(1)

# --------------------------------------------------------------------------
# Конфигурация: env > config.json > default (без правки исходников)
# --------------------------------------------------------------------------
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_JSON = os.path.join(_SCRIPT_DIR, "hermes_presence.json")

# дефолты (используются, если нет ни конфига, ни env)
_DEF_POLL = 5
_DEF_STALE = 900
_DEF_TEMPLATE = "{action} • {msgs} msgs • {tokens} tok • ${cost}"


def _load_config() -> dict:
    """Читает конфиг из hermes_presence.json (рядом со скриптом или в ~)."""
    for path in (CONFIG_JSON, os.path.join(os.path.expanduser("~"), "hermes_presence.json")):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
                if isinstance(cfg, dict):
                    return cfg
            except Exception as exc:
                print(f"config parse error {path}: {exc}", file=sys.stderr)
    return {}


def _normalise(raw: dict) -> dict:
    """Приводит конфиг к плоскому виду (новый и старый формат совместимы)."""
    raw = raw if isinstance(raw, dict) else {}
    old_paths = raw.get("paths", {})
    if not isinstance(old_paths, dict):
        old_paths = {}
    return {
        "hermes_path": raw.get("hermes_path") or old_paths.get("hermes_home") or "",
        "app_id": raw.get("discord_app_id") or raw.get("app_id") or "",
        "status_template": raw.get("status_template") or "",
        "poll_interval": raw.get("poll_interval"),
        "stale_after": raw.get("stale_after"),
        "state_db": old_paths.get("state_db"),
        "agent_log": old_paths.get("agent_log"),
    }


def _int_or(cfg_key: str, env: str, default: int) -> int:
    v = os.environ.get(env) or _cfg.get(cfg_key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_cfg = _normalise(_load_config())

def _resolve_home() -> str:
    if os.environ.get("HERMES_HOME"):
        return os.environ["HERMES_HOME"]
    if _cfg["hermes_path"]:
        return os.path.expandvars(_cfg["hermes_path"])  # %LOCALAPPDATA% и т.п.
    return os.path.join(LOCALAPPDATA, "hermes")


HERMES_HOME = _resolve_home()
STATE_DB = (os.environ.get("HERMES_STATE_DB")
            or _cfg["state_db"]
            or os.path.join(HERMES_HOME, "state.db"))
AGENT_LOG = (os.environ.get("HERMES_AGENT_LOG")
             or _cfg["agent_log"]
             or os.path.join(HERMES_HOME, "logs", "agent.log"))

# настраиваемые значения (config / env / default)
POLL_INTERVAL = _int_or("poll_interval", "HERMES_POLL_INTERVAL", _DEF_POLL)
STALE_AFTER = _int_or("stale_after", "HERMES_STALE_AFTER", _DEF_STALE)
STATUS_TEMPLATE = (os.environ.get("HERMES_STATUS_TEMPLATE")
                   or _cfg["status_template"]
                   or _DEF_TEMPLATE)
_APP_ID = (os.environ.get("HERMES_DISCORD_APP_ID")
           or _cfg["app_id"]
           or "")

LOG_FILE = os.path.join(os.path.expanduser("~"), "hermes_presence.log")
LARGE_IMAGE = os.environ.get("HERMES_LARGE_IMAGE", "hermes_logo")
MUTEX_NAME = "HermesDiscordPresence_SingleInstance"

# тайминги
TOOL_FRESH_SEC = 60          # последний инструмент считать "живым"
CHAT_FRESH_SEC = 600         # свежесть сообщения до "Idle"
_LOG_TAIL = 200 * 1024       # хвост agent.log для парсинга
_LOG_DEBOUNCE = 30           # не спамить лог одной ошибкой чаще 30 сек
LOG_MAX_BYTES = 512 * 1024   # ротация лога: больше -> в .old (перезапись)

# Красивые имена частых инструментов
TOOL_FORMAT = {
    "terminal": "terminal", "execute_code": "python", "web_search": "web search",
    "browser_navigate": "browser", "browser_snapshot": "browser",
    "read_file": "reading files", "write_file": "writing files",
    "search_files": "searching files", "patch": "editing files",
    "skill_view": "reading skills", "delegate_task": "delegating tasks",
    "vision_analyze": "analyzing image", "session_search": "searching memory",
}


def _maybe_rotate_log() -> None:
    """Простая ротация: если лог превысил LOG_MAX_BYTES — сдвинуть в .old."""
    try:
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".old")  # перезапишет старый .old
    except OSError:
        pass


def log(msg: str) -> None:
    """Пишет timestamped строку в лог (для молчаливого pythonw)."""
    try:
        _maybe_rotate_log()
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Single instance (Win32 named mutex)
# --------------------------------------------------------------------------
def acquire_mutex():
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            log("EXIT: другой экземпляр уже запущен")
            return None
        return handle
    except Exception:
        return ctypes.c_void_p(0)


# --------------------------------------------------------------------------
# Окно Hermes (WinAPI EnumWindows, без subprocess)
# --------------------------------------------------------------------------
# Импорт безопасен и на не-Windows (для pytest на CI): без WinAPI функции
# просто вернут False.
try:
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
except (AttributeError, OSError):
    user32 = None
    WNDENUMPROC = None


def hermes_window_visible() -> bool:
    if user32 is None:
        return False  # не-Windows (тесты/CI)
    found = []

    def cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if "hermes" in buf.value.lower():
                    found.append(buf.value)
                    return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return bool(found)


# --------------------------------------------------------------------------
# Работа с файлами: graceful + логирование причины (с дебаунсом)
# --------------------------------------------------------------------------
class FileHealth:
    """Отслеживает доступность файла, чтобы не спамить лог."""
    def __init__(self, path: str, label: str):
        self.path = path
        self.label = label
        self.ok = None
        self.last_err = None
        self.first_fail: float | None = None
        self._last_log = 0.0
        self.last_mtime = None

    def check(self) -> bool:
        """Проверяет наличие и читаемость файла. Логирует причину с дебаунсом."""
        try:
            if not os.path.exists(self.path):
                raise FileNotFoundError(self.path)
            with open(self.path, "rb"):
                pass
            mtime = os.path.getmtime(self.path)
            if self.ok is False:
                log(f"OK {self.label} снова доступен: {self.path}")
            self.ok = True
            self.last_err = None
            self.first_fail = None
            self.last_mtime = mtime
            return True
        except Exception as exc:
            now = time.time()
            self.ok = False
            if self.first_fail is None:
                self.first_fail = now
            # логируем: первое появление ошибки или смена текста / дебаунс
            if (self.last_err != str(exc)) or (now - self._last_log > _LOG_DEBOUNCE):
                since = int(now - self.first_fail)
                log(f"WARN {self.label} недоступен {since}s: {exc}")
                self.last_err = str(exc)
                self._last_log = now
            return False


# источники
_state_health = FileHealth(STATE_DB, "state.db")
_log_health = FileHealth(AGENT_LOG, "agent.log")


# --------------------------------------------------------------------------
# Модель из agent.log (последняя строка с timestamp + model=...)
# --------------------------------------------------------------------------
_LINE_MODEL_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}).*?\bmodel=([A-Za-z0-9._/\-]+)"
)


def tail_model():
    """-> (model, last_ts) из последней строки agent.log с timestamp и model=.

    Возвращает (None, None), если файла нет или формат изменился (ни одного
    совпадения). НЕ падает на sess[0] из state.db — это было бы ложью.
    """
    if not _log_health.check():
        return None, None
    try:
        size = os.path.getsize(AGENT_LOG)
        with open(AGENT_LOG, "rb") as fh:
            fh.seek(max(0, size - _LOG_TAIL))
            data = fh.read().decode("utf-8", errors="ignore")
    except OSError as exc:
        log(f"WARN agent.log чтение: {exc}")
        return None, None

    model, ts = None, None
    for line in data.splitlines():
        m = _LINE_MODEL_RE.search(line)
        if m:
            ts = m.group(1)
            model = m.group(2)
    if model is None:
        return None, None  # формат лога изменился / нет API-вызовов в хвосте

    try:
        last_ts = time.mktime(time.strptime(ts.replace("T", " "), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        last_ts = time.time()
    return model, last_ts


def fmt_tokens(n) -> str:
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


# --------------------------------------------------------------------------
# Сбор данных о сессии (state.db, read-only)
# --------------------------------------------------------------------------
def read_session():
    """-> dict с message_count, tokens, cost, last_msg_ts; None если БД недоступна.

    При недоступности пишет причину в лог (через FileHealth) и возвращает None.
    """
    if not _state_health.check():
        return None
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
        try:
            sess = con.execute(
                """SELECT started_at, message_count,
                          input_tokens, output_tokens, estimated_cost_usd
                   FROM sessions ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
            last_tool = con.execute(
                """SELECT tool_name, timestamp FROM messages
                   WHERE tool_name IS NOT NULL AND tool_name != ''
                   ORDER BY timestamp DESC LIMIT 1"""
            ).fetchone()
            last_msg_ts = con.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as exc:
        log(f"WARN state.db query: {exc}")  # редко: файл есть, но битый/заблокирован
        return None

    now = time.time()
    return {
        "started_at": sess[0] if sess else now,
        "msgs": sess[1] if sess else 0,
        "tokens": ((sess[2] or 0) + (sess[3] or 0)) if sess else 0,
        "cost": sess[4] if sess and isinstance(sess[4], (int, float)) else 0.0,
        "last_tool": last_tool,
        "last_msg_ts": last_msg_ts or 0,
    }


def build_presence(sess, model, model_ts):
    """Собирает (details, state, timer) из данных по шаблону из конфига."""
    now = time.time()

    # источники свежей активности
    last_msg = sess["last_msg_ts"]

    # stale: и лог, и БД не обновляются дольше порога (STALE_AFTER из конфига)
    stale = (model_ts is None or now - model_ts > STALE_AFTER) and \
            (last_msg == 0 or now - last_msg > STALE_AFTER)

    # модель
    if model is not None:
        details = f"Model: {model}"
        if now - model_ts > STALE_AFTER:
            details = f"Model: {model} (stale)"
    else:
        details = "Model: unknown"

    # действие
    action = "Idle in Hermes"
    if stale:
        action = "stale"
    else:
        if sess["last_tool"] and sess["last_tool"][1] and \
                now - sess["last_tool"][1] < TOOL_FRESH_SEC:
            name = TOOL_FORMAT.get(sess["last_tool"][0],
                                   sess["last_tool"][0].replace("_", " "))
            action = f"Running: {name}"
        elif last_msg and now - last_msg < CHAT_FRESH_SEC:
            action = "Chatting with Hermes"

    # статистика: поля для шаблона
    msgs = sess["msgs"]
    tokens = sess["tokens"]
    cost = sess["cost"] if isinstance(sess["cost"], (int, float)) else 0.0
    if msgs == 0 and not sess["last_msg_ts"]:
        action = "loading"

    fields = {
        "action": action,
        "model": model if model is not None else "unknown",
        "msgs": msgs,
        "tokens": fmt_tokens(tokens),
        "cost": f"{cost:.2f}",
    }

    # рендер по шаблону (невалидный ключ -> фолбэк на дефолтный шаблон)
    try:
        state = STATUS_TEMPLATE.format(**fields)
    except (KeyError, ValueError, IndexError) as exc:
        log(f"status_template error: {exc}; использую дефолтный шаблон")
        state = _DEF_TEMPLATE.format(**fields)

    # таймер
    timer = now
    started = sess["started_at"]
    if now - started < 3600:
        timer = started
    elif last_msg and now - last_msg < 3600:
        timer = max(started, now - 3600)

    return details, state, int(timer)


# --------------------------------------------------------------------------
# Discord RPC с чистым reconnect (пересоздание Presence)
# --------------------------------------------------------------------------
class Rpc:
    """Обёртка над pypresence: автосоздание/пересоздание при сбоях."""
    def __init__(self, app_id: str):
        self.app_id = app_id
        self.rpc = None
        self.connected = False

    def _new(self):
        return Presence(self.app_id)

    def ensure(self) -> bool:
        if self.connected:
            return True
        try:
            if self.rpc is None:
                self.rpc = self._new()
            self.rpc.connect()
            self.connected = True
            log("RPC connected")
            return True
        except Exception as exc:
            # вероятно Discord не запущен. очищаем экземпляр для чистой попытки
            self._reset()
            log(f"RPC connect error: {exc}")
            return False

    def _reset(self):
        try:
            if self.rpc is not None:
                self.rpc.close()
        except Exception:
            pass
        self.rpc = None
        self.connected = False

    def update(self, details, state, start_ts) -> bool:
        if not self.ensure():
            return False
        try:
            self.rpc.update(details=details, state=state, start=start_ts,
                            large_image=LARGE_IMAGE, large_text="Hermes Agent")
            return True
        except Exception as exc:
            log(f"RPC update error (discord перезапущен?): {exc}")
            self._reset()  # пересоздадим Presence на следующем цикле
            return False

    def clear(self):
        self.connected = False
        if self.rpc is not None:
            try:
                self.rpc.clear()
                log("CLEAR")
            except Exception:
                log("CLEAR error")
        self._reset()


# --------------------------------------------------------------------------
# Tray-иконка (pystray): управление без Task Manager
# --------------------------------------------------------------------------
class Tray:
    """Иконка в системном трее: статус, пауза/возобновление, выход.

    Состояния -> цвет иконки:
      connected: зелёный   (Hermes открыт, RPC подключён)
      idle:      зелёный   (Hermes открыт, юзер молчит — это норм)
      waiting:   жёлтый    (окно Hermes закрыто, ждём запуска)
      paused:    серый     (пауза)
      error:     красный
    """
    COLORS = {
        "connected": (46, 204, 113),
        "idle": (46, 204, 113),
        "waiting": (241, 196, 15),
        "paused": (149, 165, 166),
        "error": (231, 76, 60),
    }

    def __init__(self):
        self.paused = False
        self.exit_requested = False
        self.state = "starting"
        self._icon = None
        self._last_note = ""

    # -- иконка (рисуем программно, без внешних файлов) --
    @staticmethod
    def _image(color, size=64):
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((6, 6, size - 6, size - 6), fill=color)
        return img

    def start(self):
        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: f"Hermes Presence — {self.state}",
                None,
                enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Пауза presence",
                             self._toggle_pause,
                             checked=lambda item: self.paused),
            pystray.MenuItem("Открыть конфиг", self._open_config),
            pystray.MenuItem("Открыть лог", self._open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self._exit),
        )
        self._icon = pystray.Icon(
            "HermesPresence",
            self._image(self.COLORS["idle"]),
            "Hermes Presence",
            menu,
        )
        self._icon.run_detached()  # свой поток, не блокирует main
        log("tray: started")

    # -- меню --
    def _toggle_pause(self, icon, item):
        self.paused = not self.paused
        self.state = "paused" if self.paused else "idle"
        self._refresh_icon()
        icon.update_menu()
        self._notify("Presence paused" if self.paused else "Presence resumed")

    def _open_config(self, icon, item):
        _open_file(CONFIG_JSON)

    def _open_log(self, icon, item):
        _open_file(LOG_FILE)

    def _exit(self, icon, item):
        self.exit_requested = True
        log("tray: exit requested")

    # -- статус (вызывается из main-цикла) --
    def set_state(self, state: str):
        if state == self.state:
            return
        # Потокобезопасность pystray (Windows): меню/notify НЕ трогаем из
        # фонового цикла (это вешало message loop -> клик по меню не работал).
        # Тут только смена картинки иконки (безопасно) + флаг для строки меню.
        self.state = state
        self._refresh_icon()

    def _refresh_icon(self):
        if self._icon is None:
            return
        try:
            color = self.COLORS.get(self.state, self.COLORS["idle"])
            self._icon.icon = self._image(color)
            self._icon.title = f"Hermes Presence — {self.state}"
            # НЕ вызываем update_menu() здесь — оно пересоздаёт win32-меню из
            # чужого потока и вешает трей. Пункт-статус обновляется сам (лямбда).
        except Exception as exc:
            log(f"tray: refresh icon error: {exc}")

    def _notify(self, msg: str):
        if msg == self._last_note:
            return
        self._last_note = msg
        try:
            self._icon.notify(msg, "Hermes Presence")
        except Exception as exc:
            log(f"tray: notify error ({msg}): {exc}")

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
        log("tray: stopped")


def _open_file(path: str):
    """Открывает файл/папку в дефолтной программе (Windows)."""
    try:
        os.startfile(path)
    except Exception as exc:
        log(f"open file error ({path}): {exc}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Hermes Agent -> Discord Rich Presence")
    ap.add_argument("--app-id", default="", help="Discord application ID")
    ap.add_argument("--interval", type=int, default=None,
                    help="check cycle in seconds (default: из config.json poll_interval)")
    ap.add_argument("--idle-threshold", type=int, default=600)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    app_id = args.app_id or _APP_ID
    if not app_id:
        print("ERROR: укажи --app-id, env HERMES_DISCORD_APP_ID или discord_app_id в config.json",
              file=sys.stderr)
        sys.exit(2)

    interval = args.interval or POLL_INTERVAL

    if not args.dry_run:
        mutex = acquire_mutex()
        if mutex is None:
            sys.exit(3)

    rpc = None
    if not args.dry_run:
        rpc = Rpc(app_id)

    tray = None
    if PYSTRAY_AVAILABLE and not args.dry_run:
        tray = Tray()
        tray.start()

    presence_on = False
    timer = None
    last_sent = None
    last_push_ts = 0.0

    log(f"=== START (v9) app_id={app_id} dry={args.dry_run} "
        f"state_db={STATE_DB} agent_log={AGENT_LOG} "
        f"poll={interval}s stale={STALE_AFTER}s tray={bool(tray)} ===")
    print(f"v9: state_db={STATE_DB}")
    print(f"    agent_log={AGENT_LOG}")
    print(f"    window=Hermes* visible | model=agent.log (без fallback на sess) | "
          f"stale>={STALE_AFTER}s | template='{STATUS_TEMPLATE}' | log={LOG_FILE}")
    print(f"    watch: every {interval}s (idle after {args.idle_threshold}s) | "
          f"tray={'on' if tray else 'off'}")

    while True:
        try:
            if tray and tray.exit_requested:
                log("tray: выход из цикла")
                break
            if tray and tray.paused:
                tray.set_state("paused")
                time.sleep(interval)
                continue

            if not hermes_window_visible():
                if tray:
                    tray.set_state("waiting")
                if presence_on:
                    presence_on = False
                    last_sent = None
                    timer = None
                    if rpc:
                        rpc.clear()
                    else:
                        print("[dry-run] presence CLEARED", file=sys.stderr)
            else:
                sess = read_session()
                model, model_ts = tail_model()

                if sess is None and model is None:
                    # оба источника недоступны — нечем обновлять; держим старый
                    # presence если он был, иначе пропускаем. Данные появятся сами.
                    if tray:
                        tray.set_state("waiting")
                    print("[debug] both sources unavailable", file=sys.stderr)
                    time.sleep(interval)
                    continue

                # если БД недоступна, но лог живой — показываем модель и action
                if sess is None:
                    sess = {"started_at": time.time(), "msgs": 0, "tokens": 0,
                            "cost": 0.0, "last_tool": None, "last_msg_ts": 0}

                details, state, suggested_timer = build_presence(sess, model, model_ts)

                now = time.time()
                # таймер стабилен после первого выбора: не дрейфует каждый цикл
                if timer is None:
                    timer = suggested_timer
                if now - timer > 7200:
                    timer = int(now)  # ребейз раз в 2 часа

                payload = (details, state, timer)

                if args.dry_run:
                    if payload != last_sent:
                        print(f"[dry-run] '{details}' | '{state}' | start={timer}")
                        last_sent = payload
                else:
                    # пушим при изменении ИЛИ keepalive раз в 2 мин (Discord
                    # отваливает presence без активности)
                    if rpc and (payload != last_sent or now - last_push_ts > 120):
                        if rpc.update(details, state, timer):
                            worked = "UPD" if payload == last_sent else "PUSH"
                            log(f"{worked} '{details}' | '{state}'")
                            last_sent = payload
                            last_push_ts = now
                            presence_on = True
                # индикатор: presence живой -> connected
                if tray and rpc and rpc.connected:
                    tray.set_state("connected")
        except Exception as exc:
            log(f"LOOP ERROR: {exc}")
            if tray:
                tray.set_state("error")
        time.sleep(interval)

    # --- выход: убрать presence и закрыть трей ---
    if rpc:
        rpc.clear()
    if tray:
        tray.stop()
    log("=== завершено ===")


if __name__ == "__main__":
    main()
