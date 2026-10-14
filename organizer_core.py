#!/usr/bin/env python3

import contextlib
import copy
import fnmatch
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

# Допустимые имена категорий: строчные латинские буквы, цифры, '_' и '-'.
# Запрещают '/', '\\', '..', абсолютные пути и любые небезопасные символы.
_CATEGORY_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

# Зарезервированные имена Windows: каталог с таким именем (даже с расширением)
# ведёт себя как устройство, поэтому создание папки категории на Windows
# ломается непредсказуемо. Запрещаем на ВСЕХ платформах, чтобы один и тот же
# config.json вёл себя одинаково везде.
WINDOWS_RESERVED_NAMES: frozenset = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# ── Настройка логирования ───────────────────────────────────────────────────────

class _EncodingSafeStream:
    """
    Обёртка потока, которая не падает на символах, отсутствующих в кодировке.

    На Windows консоль по умолчанию работает в cp1252, где нет ни кириллицы,
    ни рамок «─», ни эмодзи из наших логов: обычная запись падает с
    UnicodeEncodeError (в logging это превращается в «--- Logging error ---»).
    Здесь такие символы заменяются на '?', а логи остаются читаемыми.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        try:
            written = self._stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self._stream, "encoding", None) or "utf-8"
            sanitized = text.encode(encoding, "replace").decode(encoding, "replace")
            written = self._stream.write(sanitized)
        return int(written)

    def flush(self) -> None:
        with contextlib.suppress(Exception):
            self._stream.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def __getattr__(self, item: str) -> Any:  # прозрачно отдаём остальные атрибуты
        return getattr(self._stream, item)


def _make_log_handler() -> logging.StreamHandler:
    """StreamHandler, устойчивый к узким консольным кодировкам (Windows cp1252)."""
    return logging.StreamHandler(_EncodingSafeStream(sys.stderr))


def _harden_console_encoding() -> None:
    """
    Делает stdout/stderr пригодными для печати кириллицы и рамок.

    Проблема Windows: когда вывод перенаправлен (`python file_organizer.py
    > log.txt`) или читается пайпом, Python берёт локальную кодировку
    (cp1252/cp866), и обычный print() русских строк падает с
    UnicodeEncodeError — в лучшем случае в файле оказываются \\uXXXX-escape.
    Интерактивная консоль Windows работает через Unicode API, поэтому её
    кодировку мы не меняем: там добавляем только страховку errors='replace'.

    Для перенаправленного вывода включаем UTF-8 (как делают git и ripgrep):
    в файле/пайпе читаемая кириллица важнее «совместимости» с cp1252.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # не TextIOWrapper (StringIO и т.п.) — пропускаем
            continue
        try:
            is_tty = bool(getattr(stream, "isatty", lambda: False)())
            if is_tty:
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError, LookupError):
            logger.debug("Не удалось перенастроить кодировку потока вывода", exc_info=True)


logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = _make_log_handler()
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ── Константы ──────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config.json"
UNDO_LOG_FILENAME = ".organizer_undo.json"

# Пользовательский список исключений (формат как у .gitignore). Читается из
# корня сортируемой папки, сам никогда не сортируется.
ORGANIZER_IGNORE_FILENAME = ".organizerignore"

# Настраиваемые имена скриптов (можно переопределить в конфиге)
DEFAULT_SCRIPT_FILES: set[str] = {
    "organizer_core.py",
    "file_organizer.py",
    "file_organizer_gui.py",
    "config.json",
    "actions.log",
    UNDO_LOG_FILENAME,
    "__pycache__",
}

# Абсолютные пути к защищённым системным директориям (Unix + Windows)
# Защищаются ТОЛЬКО системные директории, НЕ пользовательские (/tmp, /home/user)
_PROTECTED_EXACT_PATHS = (
    "/", "/root", "/home",
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/var", "/boot", "/dev", "/proc", "/sys",
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
)

# Каталоги, у которых защищаются и все подпапки. Корня "/" и пользовательских
# /root, /home здесь нет намеренно: иначе защищённым стало бы вообще всё
# (/home/пользователь/Загрузки тоже должен сортироваться).
_PROTECTED_TREE_PATHS = tuple(
    p for p in _PROTECTED_EXACT_PATHS if p not in ("/", "/root", "/home")
)


def _is_within(path: Path, parent: Path) -> bool:
    """True, если path лежит внутри parent (сам parent не считается)."""
    if path == parent:
        return False
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _expand_with_resolved(paths: "tuple[str, ...] | set[Path]") -> set[Path]:
    """
    Литеральные пути + их resolve()-формы.

    Нужно для macOS: /etc, /var, /bin и /tmp — симлинки в /private/..., так что
    проверка только по литеральному пути пропускала бы organize_files("/etc").
    Обратная ситуация (аргумент — симлинк на системный путь) ловится тем же.
    """
    expanded: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        expanded.add(path)
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            expanded.add(path.resolve())
    return expanded


_PROTECTED_EXACT: set[Path] = _expand_with_resolved(_PROTECTED_EXACT_PATHS)
_PROTECTED_TREES: set[Path] = _expand_with_resolved(_PROTECTED_TREE_PATHS)
# Домашний каталог пользователя — только точным совпадением (подпапки сортируются)
with contextlib.suppress(OSError, RuntimeError, ValueError):
    _PROTECTED_EXACT.add(Path.home())
    _PROTECTED_EXACT.add(Path.home().resolve())

# macOS: пользовательский temp лежит внутри защищённого /var — /var/folders/....
# Без этого исключения tempfile.mkdtemp() на macOS попадал бы под защиту /var
# и ни один скрипт не смог бы рассортировать временную папку.
_USER_TEMP_TREES: set[Path] = _expand_with_resolved(
    ("/var/folders", "/private/var/folders")
)

# Публичный список для обратной совместимости
PROTECTED_ABS_PATHS: list[Path] = [Path(p) for p in _PROTECTED_EXACT_PATHS]

# Лимиты безопасности
MAX_UNIQUE_PATH_ATTEMPTS = 1000  # Защита от DoS
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 GB лимит размера файла

# Журнал отмены. Без ограничения он растёт бесконечно: каждая сортировка
# дописывает строки. По умолчанию храним последние DEFAULT_MAX_UNDO_ENTRIES
# записей (0 = без ограничения).
DEFAULT_MAX_UNDO_ENTRIES = 1000
# fsync делаем не на каждую запись (это дорого), а пачками: данные переживают
# падение процесса сразу (flush), потерю питания — по мере fsync.
JOURNAL_FSYNC_INTERVAL = 50
# Оценка размера одной JSON-записи — чтобы замечать разросшийся журнал, не
# читая его на каждой записи.
JOURNAL_BYTES_PER_ENTRY_HINT = 256

# Сколько дней хранить бэкапы заменённых файлов (0 = хранить всегда).
DEFAULT_BACKUP_RETENTION_DAYS = 0
# Чистим только строго узнаваемые имена: <оригинал>.bak-ГГГГММДД_ЧЧММСС
_BACKUP_NAME_RE = re.compile(r"\.bak-(\d{8}_\d{6})$")


# Дубликаты по содержимому (опция settings.content_duplicates).
# Сравнивать файлы целиком дорого, поэтому сначала сверяем размер и хэш первых
# PARTIAL_HASH_BYTES байт, а полный SHA-256 считаем только при совпадении
# префикса (коллизия первых 64 КБ возможна, но редка).
PARTIAL_HASH_BYTES = 64 * 1024
HASH_CHUNK_BYTES = 1024 * 1024

DEFAULT_CONFIG: dict[str, Any] = {
    "categories": {
        "images":     ["jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "ico", "tiff", "raw", "heic", "heif"],
        "documents":  ["pdf", "doc", "docx", "txt", "rtf", "odt", "xls", "xlsx", "ppt", "pptx", "md", "csv", "epub"],
        "videos":     ["mp4", "avi", "mkv", "mov", "wmv", "flv", "webm", "m4v", "3gp"],
        "audio":      ["mp3", "wav", "flac", "aac", "ogg", "wma", "m4a", "opus"],
        "archives":   ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst"],
        "code":       ["py", "js", "ts", "html", "css", "java", "cpp", "c", "h", "go",
                       "rs", "rb", "php", "sql", "json", "xml", "yaml", "yml", "sh",
                       "bat", "ps1", "kt", "swift", "dart"],
        "installers": ["exe", "msi", "deb", "rpm", "pkg", "dmg", "appimage", "apk"],
        "torrents":   ["torrent"],
        "fonts":      ["ttf", "otf", "woff", "woff2", "eot"],
        "other":      [],
    },
    "settings": {
        "dry_run":            False,
        "recursive":          True,
        "clean_empty_dirs":   True,
        "ignore_hidden":      True,
        "log_file":           "actions.log",
        "duplicate_strategy": "rename",   # rename | skip | replace
        "use_watchdog":       True,
        "script_files":       list(DEFAULT_SCRIPT_FILES),  # Настраиваемые имена
        "max_file_size":      MAX_FILE_SIZE,  # Лимит размера файла
        "content_duplicates": False,  # Не переносить файлы с уже имеющимся содержимым
        "max_undo_entries":   DEFAULT_MAX_UNDO_ENTRIES,  # 0 = журнал без ограничения
        "backup_retention_days": DEFAULT_BACKUP_RETENTION_DAYS,  # 0 = хранить бэкапы всегда
    },
}

# ── Глобальное состояние (потокобезопасное) ────────────────────────────────────

_lock = threading.Lock()

# M4 fix: блокировка ПО ПАПКЕ, а не глобальная. Раньше сортировка одной папки
# молча блокировала сортировку любой другой папки в том же процессе — теперь
# параллельная работа с разными папками разрешена, а повторный вход в ту же
# папку по-прежнему пропускается (защита от гонок watchdog/monitoring).
class _DirLock:
    """Лок каталога и число потоков, которые держат на него ссылку."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


_dir_locks: dict[str, _DirLock] = {}
_dir_locks_guard = threading.Lock()


def _get_dir_lock(key: str) -> _DirLock:
    """Возвращает (создавая при необходимости) лок для конкретной директории."""
    with _dir_locks_guard:
        entry = _dir_locks.get(key)
        if entry is None:
            entry = _DirLock()
            _dir_locks[key] = entry
        entry.users += 1
        return entry


def _release_dir_lock(key: str, entry: _DirLock) -> None:
    """
    Отпускает ссылку на лок каталога и удаляет запись, когда она не нужна.

    Раньше словарь _dir_locks рос бесконечно: каждое уникальное имя папки —
    а при мониторинге папки загрузок это ещё и временные имена файлов —
    навсегда оставляло в нём лок. Счётчик ссылок гарантирует, что запись
    удаляется только тогда, когда её никто не держит и не ждёт: иначе два
    потока могли бы получить разные лок-объекты для одной папки и сортировать
    её одновременно.
    """
    with _dir_locks_guard:
        entry.users -= 1
        if entry.users <= 0 and _dir_locks.get(key) is entry:
            del _dir_locks[key]


# DEPRECATED: in-memory лог сессии оставлен только для обратной совместимости.
# M5 fix: персистентный JSONL-журнал (UNDO_LOG_FILENAME) полностью заменяет его,
# поэтому рост списка ограничен — длительный процесс мониторинга не копит память.
_action_log: list[dict] = []
MAX_IN_MEMORY_LOG_ENTRIES = 5000

_monitoring_stop_event = threading.Event()
_monitoring_thread: Optional[threading.Thread] = None  # отслеживаем текущий поток мониторинга

# Кэш конфига — избегаем лишних чтений диска и deepcopy при каждом вызове
_config_cache: Optional[dict[str, Any]] = None
_config_mtime: float = 0.0


# ── Конфигурация ───────────────────────────────────────────────────────────────

def _validate_category_name(name: str) -> bool:
    """
    Валидация имени категории:
    - Только строчные буквы, цифры, подчёркивание, дефис
    - Нет слешей, обратных слешей, '..', абсолютных путей
    - Не пустая строка
    """
    if not name or not isinstance(name, str):
        return False
    # Запрещаем путь, абсолютные пути, '..' и пустые компоненты
    if any(c in name for c in ("/", "\\")):
        return False
    if name.startswith(".") or name == "..":
        return False
    if os.path.isabs(name):
        return False
    # Зарезервированные имена Windows (con, nul, com1, lpt1, …)
    if name.lower().split(".")[0] in WINDOWS_RESERVED_NAMES:
        logger.warning(f"config.json: имя категории зарезервировано в Windows: {name!r}")
        return False
    # Разрешаем только безопасные символы
    return bool(_CATEGORY_NAME_RE.match(name))


def _validate_config_schema(config: Any) -> bool:
    """
    Проверяет базовую схему конфига: наличие и типы ключевых полей.
    Возвращает False при невалидных типах данных.

    H4 fix: конфиг может быть произвольным JSON (массив, строка, число) —
    первым делом проверяем, что это словарь, иначе ранний AttributeError.
    """
    if not isinstance(config, dict):
        logger.warning(
            f"config.json: корневой элемент должен быть объектом, "
            f"получено {type(config).__name__}"
        )
        return False
    if not isinstance(config.get("categories"), dict):
        return False
    if not isinstance(config.get("settings"), dict):
        return False

    # Валидация имён категорий
    for cat_name, exts in config["categories"].items():
        if not _validate_category_name(cat_name):
            logger.warning(f"config.json: недопустимое имя категории: {cat_name!r}")
            return False
        if not isinstance(exts, list):
            logger.warning(f"config.json: расширения для категории {cat_name!r} должны быть списком")
            return False
        for ext in exts:
            if not isinstance(ext, str):
                logger.warning(f"config.json: расширение в категории {cat_name!r} должно быть строкой")
                return False

    s = config["settings"]
    type_checks = [
        ("dry_run",            bool),
        ("recursive",          bool),
        ("clean_empty_dirs",   bool),
        ("ignore_hidden",      bool),
        ("duplicate_strategy", str),
        ("use_watchdog",       bool),
        ("content_duplicates", bool),
    ]
    for key, expected_type in type_checks:
        if key in s and not isinstance(s[key], expected_type):
            logger.warning(f"config.json: неверный тип поля settings.{key} (ожидается {expected_type.__name__})")
            return False
    # Валидация значения duplicate_strategy
    valid_strategies = {"rename", "skip", "replace"}
    strategy = s.get("duplicate_strategy")
    if strategy is not None and strategy not in valid_strategies:
        logger.warning(
            f"config.json: неверное значение duplicate_strategy={strategy!r} "
            f"(допустимо: {sorted(valid_strategies)})"
        )
        return False
    # Валидация max_file_size
    max_size = s.get("max_file_size")
    if max_size is not None and (not isinstance(max_size, int) or max_size <= 0):
        logger.warning("config.json: max_file_size должно быть положительным целым числом")
        return False
    # Валидация размера журнала и срока хранения бэкапов (0 — «без ограничения»)
    for key in ("max_undo_entries", "backup_retention_days"):
        value = s.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            logger.warning(f"config.json: {key} должно быть целым числом >= 0")
            return False
    return True


def _merge_with_defaults(user: Any) -> dict[str, Any]:
    """
    Сливает пользовательский конфиг с дефолтным.

    H4 fix: принимает любой JSON-тип. Если корневой элемент не dict или схема
    невалидна — возвращает дефолты с предупреждением (вместо AttributeError).
    """
    if not isinstance(user, dict):
        logger.warning(
            "config: корневой элемент должен быть объектом (dict). "
            "Используются дефолтные настройки."
        )
        return copy.deepcopy(DEFAULT_CONFIG)
    if not _validate_config_schema(user):
        logger.warning(
            "config не прошёл валидацию схемы. Используются дефолтные настройки."
        )
        return copy.deepcopy(DEFAULT_CONFIG)

    merged = copy.deepcopy(DEFAULT_CONFIG)
    for key, val in user.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key].update(val)
        else:
            merged[key] = val
    return merged


def load_config_from_path(config_path: Path) -> dict[str, Any]:
    """
    Загружает конфиг из произвольного файла (для `--config`).

    В отличие от load_config(): не кэшируется и не создаёт файл при отсутствии —
    явно указанный пользователем путь обязан существовать.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Файл конфигурации не найден: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать конфигурацию {path}: {exc}") from exc
    return _merge_with_defaults(user)


def load_config(force_reload: bool = False) -> dict[str, Any]:
    """
    Загружает конфиг с глубоким слиянием с дефолтными значениями.

    Результат кэшируется в памяти и инвалидируется только при изменении
    файла (по mtime). Повторные вызовы без изменения файла не делают I/O.
    Возвращает deepcopy, чтобы вызывающий код не мог испортить кэш.
    """
    global _config_cache, _config_mtime
    if not force_reload and _config_cache is not None:
        try:
            current_mtime = CONFIG_FILE.stat().st_mtime
            if current_mtime == _config_mtime:
                return copy.deepcopy(_config_cache)
        except OSError:
            return copy.deepcopy(_config_cache)  # файл исчез — отдаём кэш

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                user = json.load(f)
            result = _merge_with_defaults(user)
            _config_cache = result
            with contextlib.suppress(OSError):
                _config_mtime = CONFIG_FILE.stat().st_mtime
            return copy.deepcopy(result)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Ошибка чтения config.json: {exc}. Используются дефолтные настройки.")
    else:
        save_config(DEFAULT_CONFIG)
    result = copy.deepcopy(DEFAULT_CONFIG)
    _config_cache = result
    return copy.deepcopy(result)


@lru_cache(maxsize=4096)
def _resolve_cached(path: Path) -> Path:
    """
    Кэшированный resolve() для Path-объектов.

    Вызов Path.resolve() делает stat-syscall каждый раз. Для путей, которые
    проверяются многократно (source_dir, защищённые пути), кэширование даёт
    заметное ускорение при обработке тысяч файлов.
    """
    return path.resolve()


def save_config(config: dict[str, Any]) -> None:
    """Сохраняет конфиг в файл."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.warning(f"Не удалось сохранить config.json: {exc}")


# ── Вспомогательные функции ────────────────────────────────────────────────────

def _build_ext_map(config: dict[str, Any]) -> dict[str, str]:
    """Строит маппинг «.расширение → категория»."""
    ext_map: dict[str, str] = {}
    for cat, exts in config.get("categories", {}).items():
        for ext in exts:
            ext_map[f".{ext.lower()}"] = cat
    return ext_map


def _get_category(file_path: Path, ext_map: dict[str, str]) -> str:
    return ext_map.get(file_path.suffix.lower(), "other")


def _validate_path_safety(file_path: Path, source_dir: Path) -> bool:
    """
    Проверяет безопасность пути.

    Использует _resolve_cached() для source_dir, чтобы не делать stat-syscall
    при каждом из тысяч вызовов в одном прогоне organize_files.
    """
    try:
        for part in file_path.parts:
            if part == "..":
                return False

        resolved = _resolve_cached(file_path) if file_path.is_absolute() else file_path.resolve()
        source_resolved = _resolve_cached(source_dir)

        try:
            resolved.relative_to(source_resolved)
        except ValueError:
            return False

        if file_path.is_symlink():
            link_target = file_path.resolve()
            try:
                link_target.relative_to(source_resolved)
            except ValueError:
                return False

        return True
    except (OSError, ValueError):
        return False


def _mask_path_for_log(path: Path, source_dir: Path) -> str:
    """
    Маскирует полный путь в логах для предотвращения утечки информации.

    Возвращает путь относительно рабочей директории в POSIX-виде (одинаково
    читается в логах на любой ОС). Сравнение идёт по resolve()-формам: на macOS
    /var — симлинк в /private/var, а Windows отдаёт в TMP короткое имя 8.3,
    поэтому неразрешённый путь не совпадал с разрешённой рабочей папкой и
    маскировка вырождалась в одно имя файла.
    """
    try:
        source_resolved = _resolve_cached(source_dir)
        try:
            rel = path.relative_to(source_resolved)
        except ValueError:
            rel = path.resolve().relative_to(source_resolved)
        return rel.as_posix()
    except (ValueError, OSError):
        return path.name


def _mask_dir_for_log(path: Path) -> str:
    """
    Маскирует путь директории для лога (M11).

    Полный абсолютный путь — это данные пользователя, которые не должны
    утекать в логи/трейсбэки. Показываем путь относительно домашней папки
    (`~/Downloads`), а вне неё — только имя последнего каталога.
    """
    try:
        resolved = _resolve_cached(path)
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            return f"~/{resolved.relative_to(Path.home())}"
        return resolved.name or "/"
    except (OSError, ValueError, RuntimeError):
        return path.name or str(path)


def _get_script_files(config: dict[str, Any]) -> set[str]:
    """Получает имена скриптов из конфига с фоллбэком на дефолтные."""
    settings = config.get("settings", {})
    script_files = settings.get("script_files")
    if script_files and isinstance(script_files, list):
        return set(script_files)
    return DEFAULT_SCRIPT_FILES.copy()


def is_protected_dir(dirpath: Path) -> bool:
    """
    Проверяет, является ли директория защищённой системной.

    Защищаются:
    - Точные совпадения с критическими путями: /, /root, /home, домашний каталог
    - Системные директории и их подпапки (/etc, /etc/..., /usr, /usr/..., ...)

    НЕ защищаются (пользовательские данные):
    - /tmp, /var/folders (macOS temp) и их подпапки
    - /home/user/... и /root/... (но /home и /root как таковые защищаются)

    Пути сравниваются и в resolve()-форме: на macOS /etc и /var — симлинки
    в /private/..., и без этого проверка organize_files("/etc") проходила бы.
    """
    try:
        resolved = dirpath.resolve()
    except (OSError, RuntimeError, ValueError):
        return False

    if resolved in _PROTECTED_EXACT:
        return True

    # Пользовательский temp не защищаем, даже если формально он лежит внутри
    # системного каталога (macOS: /var/folders/... внутри /private/var).
    for temp_root in _USER_TEMP_TREES:
        if _is_within(resolved, temp_root):
            return False

    return any(_is_within(resolved, protected) for protected in _PROTECTED_TREES)


# ── Особенности файловой системы: регистр и Unicode ────────────────────────────

# Кэш результатов пробника: регистр может отличаться от каталога к каталогу
# (APFS умеет быть case-sensitive; Windows — per-directory через fsutil).
_case_sensitivity_cache: dict[str, bool] = {}
_case_sensitivity_lock = threading.Lock()

# В имени пробного файла обязан быть символ в верхнем регистре, иначе
# сравнение с lower-вариантом не имеет смысла.
CASE_PROBE_PREFIX = ".organizer_CaseProbe_"


def _platform_case_guess() -> bool:
    """
    Платформенная эвристика: ФС «по умолчанию» нечувствительна к регистру.

    Используется ТОЛЬКО как fallback, когда реальный пробник невозможен
    (каталог только для чтения и т.п.). Основной путь —
    _fs_is_case_insensitive(directory) с реальной проверкой ФС.
    """
    return os.name == "nt" or sys.platform == "darwin"


def _probe_case_sensitivity(directory: Path) -> Optional[bool]:
    """
    Реально проверяет ФС на чувствительность к регистру.

    Создаёт временный файл со смешанным регистром и смотрит, виден ли он под
    именем в нижнем регистре. Возвращает None, если проверить нельзя (нет прав,
    каталог недоступен) — тогда вызывающий использует платформенную эвристику.

    Точнее догадки по платформе: APFS можно отформатировать как case-sensitive,
    а Windows умеет включать регистрозависимость для отдельного каталога —
    в обоих случаях платформенная эвристика даёт неверный ответ.
    """
    try:
        if not Path(directory).is_dir():
            return None
        fd, probe_str = tempfile.mkstemp(prefix=CASE_PROBE_PREFIX, dir=str(directory))
    except OSError:
        return None

    probe = Path(probe_str)
    try:
        os.close(fd)
        lowered = probe.with_name(probe.name.lower())
        if lowered.name == probe.name:  # pragma: no cover - префикс содержит верхний регистр
            return None
        return lowered.exists()
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()


def _fs_is_case_insensitive(directory: Optional[Path] = None) -> bool:
    """
    Нечувствительна ли ФС к регистру?

    Args:
        directory: каталог, для которого нужен ответ. Если задан — выполняется
            реальный пробник ФС с кэшированием по каталогу. Если не задан (или
            пробник невозможен) — платформенная эвристика как fallback.
    """
    if directory is None:
        return _platform_case_guess()

    try:
        key = str(Path(directory).resolve())
    except (OSError, ValueError):
        key = str(directory)

    with _case_sensitivity_lock:
        cached = _case_sensitivity_cache.get(key)
    if cached is not None:
        return cached

    probed = _probe_case_sensitivity(Path(directory))
    result = _platform_case_guess() if probed is None else probed

    with _case_sensitivity_lock:
        _case_sensitivity_cache[key] = result
    return result


def _normalize_name(name: str) -> str:
    """
    Unicode-нормализация имени файла (NFC).

    macOS (APFS/HFS+) хранит имена в NFD: «й» = «и» + U+0306, «é» = «e» + U+0301.
    Имя, введённое пользователем в NFC, и то же имя, прочитанное с диска в NFD, —
    это ОДНА запись ФС. Без нормализации мы считали бы их разными файлами
    и могли молча перезаписать существующий файл при перемещении.
    """
    try:
        return unicodedata.normalize("NFC", name)
    except (TypeError, ValueError):  # pragma: no cover - защита от не-строк
        return name


def _name_key(name: str, case_insensitive: bool) -> str:
    """
    Ключ сравнения имён файлов для конкретной ФС.

    Учитывает обе особенности одновременно: Unicode-нормализацию (NFC) и
    регистр (если ФС регистронезависима).
    """
    key = _normalize_name(name)
    return key.lower() if case_insensitive else key


def _max_path_length() -> Optional[int]:
    """Лимит длины пути для текущей ОС (None — если лимита нет)."""
    if os.name == "nt":
        # Windows MAX_PATH (260) минус завершающий NUL
        return 260 - 1
    if sys.platform == "darwin":
        # POSIX PATH_MAX на macOS — 1024 байта
        return 1024
    return None


def _path_too_long(path: Path) -> bool:
    """
    Превышает ли путь лимит ОС?

    Windows с выключенной поддержкой длинных путей (по умолчанию) падает на
    путях длиннее 259 символов с невнятным OSError уже в момент перемещения.
    Лучше явно посчитать это ошибкой конкретного файла и сообщить пользователю.
    """
    limit = _max_path_length()
    if limit is None:
        return False
    try:
        return len(str(path)) > limit
    except (TypeError, ValueError):  # pragma: no cover
        return False


def _get_unique_path(
    target_dir: Path,
    filename: str,
    existing_names: Optional[set[str]] = None,
    existing_keys: Optional[set[str]] = None,
) -> Path:
    """
    Возвращает путь без конфликта имён, добавляя _1, _2, …

    Конфликт определяется так, как его видит сама ФС:
    - на регистронезависимой ФС «File.TXT» и «file.txt» — одно имя;
    - имена сравниваются в Unicode-нормализации NFC, поэтому «café» (NFC) и
      «café» (NFD, как хранит macOS) тоже считаются одним именем.

    Чувствительность ФС определяется реальным пробником этого каталога
    (_fs_is_case_insensitive), а не догадкой по платформе.

    Принимает опциональные pre-scanned наборы: existing_names (имена как есть)
    и existing_keys (их ключи сравнения). Если переданы — работает без I/O.
    Если нет — сканирует директорию один раз вместо N вызовов exists().
    """
    target = target_dir / filename
    if existing_names is None:
        try:
            with os.scandir(target_dir) as it:
                existing_names = {e.name for e in it}
        except OSError:
            existing_names = set()

    case_insensitive = _fs_is_case_insensitive(target_dir)
    if existing_keys is None:
        existing_keys = {_name_key(n, case_insensitive) for n in existing_names}

    def _exists(name: str) -> bool:
        return _name_key(name, case_insensitive) in existing_keys

    if not _exists(filename):
        return target

    stem, suffix = Path(filename).stem, Path(filename).suffix
    for i in range(1, MAX_UNIQUE_PATH_ATTEMPTS):
        candidate_name = f"{stem}_{i}{suffix}"
        if not _exists(candidate_name):
            return target_dir / candidate_name
    raise FileExistsError(
        f"Не удалось найти уникальное имя для {filename} (превышен лимит {MAX_UNIQUE_PATH_ATTEMPTS})"
    )


def _name_in_set(
    name: str, names: set[str], keys: set[str], case_insensitive: bool
) -> bool:
    """
    Есть ли имя в наборе (с учётом регистра ФС и Unicode-нормализации)?

    `keys` — предвычисленные ключи сравнения существующих имён
    (см. _name_key), построенные с тем же значением case_insensitive.
    """
    return _name_key(name, case_insensitive) in keys


def _add_name(
    name: str, names: set[str], keys: set[str], case_insensitive: bool
) -> None:
    """Регистрирует созданное имя в наборе имён и в наборе ключей сравнения."""
    names.add(name)
    keys.add(_name_key(name, case_insensitive))


# ── .organizerignore ──────────────────────────────────────────────────────────

def _parse_ignore_patterns(text: str) -> list[tuple[str, bool]]:
    """
    Разбирает .organizerignore в список пар (шаблон, «отрицание»).

    Формат как у .gitignore: строка — шаблон, «#» — комментарий, пустые строки
    пропускаются, «!шаблон» возвращает файл обратно в сортировку.
    """
    patterns: list[tuple[str, bool]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:].strip()
        if not line:
            continue
        patterns.append((line, negate))
    return patterns


def _load_ignore_patterns(source_dir: Path) -> list[tuple[str, bool]]:
    """Читает .organizerignore из корня сортируемой папки (нет файла — нет правил)."""
    path = source_dir / ORGANIZER_IGNORE_FILENAME
    try:
        # utf-8-sig: файл, сохранённый «Блокнотом», начинается с BOM.
        # errors="replace": битые байты не должны мешать сортировке.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        return []
    except OSError as exc:
        # Например, на месте файла оказался каталог — сортировка продолжается.
        logger.warning(f"{ORGANIZER_IGNORE_FILENAME}: не удалось прочитать: {exc}")
        return []
    return _parse_ignore_patterns(text)


def _ignore_ancestor_names(rel_posix: str, is_dir: bool) -> list[str]:
    """Имена каталогов на пути — для шаблонов вида «node_modules/»."""
    parts = rel_posix.split("/")
    if not is_dir:
        parts = parts[:-1]
    return parts


def _ignore_pattern_matches(pattern: str, rel_posix: str, is_dir: bool) -> bool:
    """
    Один шаблон .organizerignore против относительного пути (разделитель «/»).

    Семантика как у .gitignore:
    - «*.tmp» (без «/») — совпадение по имени на любом уровне;
    - «docs/*.pdf» (с «/») — от корня сортируемой папки;
    - «node_modules/» — каталог и всё внутри, на любом уровне;
    - «build/cache/» — каталог и всё внутри, от корня.
    """
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if not pattern:
        return False
    name = rel_posix.rsplit("/", 1)[-1]
    if dir_only:
        if "/" in pattern:
            names = _ignore_ancestor_names(rel_posix, is_dir)
            return any(
                fnmatch.fnmatchcase("/".join(names[: i + 1]), pattern)
                for i in range(len(names))
            )
        return any(
            fnmatch.fnmatchcase(part, pattern)
            for part in _ignore_ancestor_names(rel_posix, is_dir)
        )
    if "/" in pattern:
        return fnmatch.fnmatchcase(rel_posix, pattern)
    if fnmatch.fnmatchcase(name, pattern):
        return True
    # Без «/» шаблон действует и на каталоги: «*.bak» исключает папку «old.bak» целиком
    return any(
        fnmatch.fnmatchcase(part, pattern)
        for part in _ignore_ancestor_names(rel_posix, is_dir)
    )


def _matches_ignore(
    rel_posix: str, patterns: list[tuple[str, bool]], is_dir: bool
) -> bool:
    """Применяет шаблоны по порядку: решает последнее совпадение (как в .gitignore)."""
    ignored = False
    for pattern, negate in patterns:
        if _ignore_pattern_matches(pattern, rel_posix, is_dir):
            ignored = not negate
    return ignored


def _scan_files_fast(
    root: Path,
    recursive: bool,
    ignore_dir: Optional[Callable[[Path], bool]] = None,
) -> tuple[list[tuple[Path, os.stat_result]], list[str]]:
    """
    Быстрый сбор файлов с помощью os.scandir() вместо Path.rglob()/iterdir().

    os.scandir() возвращает DirEntry-объекты, которые кэшируют результат stat()
    внутри себя — отдельный вызов stat() для каждого файла не нужен.
    На больших директориях это даёт 40-60% ускорение по сравнению с rglob().

    Возвращает (список пар (Path, stat_result), список недоступных путей).
    M8 fix: ошибки сканирования больше не проглатываются молча — недоступные
    подпапки возвращаются вызывающему коду, который учитывает их в статистике,
    вместо ложного «Нечего делать».

    ignore_dir: необязательный предикат «каталог исключён». Исключённые каталоги
    не обходятся вовсе — на больших папках (node_modules, .venv) это экономит
    весь обход, а не только перенос файлов.
    """
    result: list[tuple[Path, os.stat_result]] = []
    scan_errors: list[str] = []
    if recursive:
        stack: list[str] = [str(root)]
        while stack:
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        try:
                            # is_file() с follow_symlinks=True — поведение как у Path.is_file()
                            if entry.is_file(follow_symlinks=True):
                                result.append((Path(entry.path), entry.stat()))
                            elif entry.is_dir(follow_symlinks=False):
                                if ignore_dir is not None and ignore_dir(Path(entry.path)):
                                    continue
                                stack.append(entry.path)
                            # Прочие типы (FIFO, сокеты, битые симлинки) не сортируются
                        except OSError:
                            scan_errors.append(entry.path)
            except OSError:
                scan_errors.append(current_dir)
    else:
        try:
            with os.scandir(str(root)) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=True):
                            result.append((Path(entry.path), entry.stat()))
                    except OSError:
                        scan_errors.append(entry.path)
        except OSError:
            scan_errors.append(str(root))
    return result, scan_errors


# ── Дубликаты по содержимому ───────────────────────────────────────────────────

def _hash_prefix(path: Path, size: int, limit: int = PARTIAL_HASH_BYTES) -> Optional[str]:
    """SHA-256 первых `limit` байт файла (или всего файла, если он короче)."""
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            remaining = min(size, limit)
            while remaining > 0:
                chunk = fh.read(min(HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                hasher.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return None
    return hasher.hexdigest()


def _hash_full(path: Path) -> Optional[str]:
    """SHA-256 всего файла — вызывается только при совпадении размера и префикса."""
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(HASH_CHUNK_BYTES), b""):
                hasher.update(chunk)
    except OSError:
        return None
    return hasher.hexdigest()


class _ContentIndex:
    """
    Индекс содержимого файлов каталога: (размер, хэш префикса) → файлы.

    Индексация ленивая: файлы каталога читаются только при первой проверке
    (первый файл, который вообще понадобилось сравнить). Полный SHA-256
    считается тоже лениво — для кандидатов, совпавших по размеру и префиксу.
    Файлы, перемещённые в течение текущего прогона, добавляются в индекс, поэтому
    дубликат ловится и внутри одной партии, а не только среди старых файлов.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._by_prefix: dict[tuple[int, str], list[Path]] = {}
        self._full: dict[Path, Optional[str]] = {}
        self._built = False
        self._unavailable = False

    def _build(self) -> None:
        self._built = True
        try:
            with os.scandir(self._dir) as it:
                entries = [
                    (Path(e.path), e.stat().st_size)
                    for e in it
                    if e.is_file(follow_symlinks=True)
                ]
        except OSError:
            self._unavailable = True
            return
        for path, size in entries:
            self._index(path, size)

    def _index(self, path: Path, size: int) -> None:
        prefix = _hash_prefix(path, size)
        if prefix is not None:
            self._by_prefix.setdefault((size, prefix), []).append(path)

    def add(self, path: Path) -> None:
        """Регистрирует файл (после перемещения) для последующих сравнений."""
        if not self._built:
            self._build()
        if self._unavailable:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        self._index(path, size)

    def find_identical(self, path: Path, size: int) -> Optional[Path]:
        """Возвращает известный файл с тем же содержимым, иначе None."""
        if not self._built:
            self._build()
        if self._unavailable:
            return None
        prefix = _hash_prefix(path, size)
        if prefix is None:
            return None
        candidates = self._by_prefix.get((size, prefix))
        if not candidates:
            return None
        digest = _hash_full(path)
        if digest is None:
            return None
        for candidate in candidates:
            if candidate == path:
                continue
            try:
                if candidate.samefile(path):
                    continue
            except OSError:
                pass
            if self._full_digest(candidate) == digest:
                return candidate
        return None

    def _full_digest(self, path: Path) -> Optional[str]:
        if path not in self._full:
            self._full[path] = _hash_full(path)
        return self._full[path]


# ── Запись лога отмены ────────────────────────────────────────────────────────

class _UndoLogWriter:
    """
    Append-only JSONL писатель для undo-лога.

    Каждая запись — отдельная строка JSON. Это:
    - Устраняет O(N²) read-modify-write всего лога на каждый файл.
    - Не требует фонового потока — нет утечки потоков в режиме мониторинга.
    - Делает запись потокобезопасной без сложных гонок с undo_last_operation.
    - Сохраняет частичный прогресс на диск даже при прерывании (Ctrl+C).

    Долговечность и размер:
    - запись идёт в постоянный дескриптор (одно открытие на прогон);
    - flush после каждой записи — данные уходят в ОС и переживают падение
      процесса, а не только чистый выход;
    - fsync пачками (JOURNAL_FSYNC_INTERVAL) и при close() — журнал переживает
      потерю питания, при этом мы не платим fsync за каждый файл;
    - при превышении лимита записей журнал компактизируется: остаются самые
      свежие (см. settings.max_undo_entries).
    """

    def __init__(self, log_path: Path, max_entries: int = 0) -> None:
        self._path = log_path
        self._max_entries = max(0, int(max_entries))
        self._lock = threading.Lock()
        self._handle: Optional[io.BufferedWriter] = None
        self._appends = 0
        self._since_fsync = 0
        self.trimmed = 0  # сколько старых записей выбросила компактизация

    # ── внутреннее ────────────────────────────────────────────────────────────
    def _open(self) -> io.BufferedWriter:
        # Дескриптор держим намеренно: закрывается через close() (в конце
        # сортировки), а не сразу — иначе flush/fsync на каждую запись требовал
        # бы открывать файл заново.
        if self._handle is None:
            self._handle = open(self._path, "ab")  # noqa: SIM115
        return self._handle

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()

    def _maybe_compact(self) -> None:
        """Держит журнал в пределах max_undo_entries (0 = без ограничения)."""
        if self._max_entries <= 0:
            return
        threshold = max(1, self._max_entries // 2)
        needed = self._appends >= threshold
        if not needed:
            # Файл мог быть большим ещё до нас (лимит уменьшили в конфиге):
            # размер проверяем дешевле, чем число записей.
            try:
                needed = self._path.stat().st_size > (
                    self._max_entries * JOURNAL_BYTES_PER_ENTRY_HINT
                )
            except OSError:
                return
        if not needed:
            return

        self._close_handle()
        entries = _read_undo_log_file(self._path)
        self._appends = 0
        if len(entries) <= self._max_entries:
            return
        dropped = len(entries) - self._max_entries
        _write_undo_log_file(self._path, entries[-self._max_entries :])
        self.trimmed += dropped
        logger.info(
            f"undo-журнал: удалены самые старые записи ({dropped}) — "
            f"храним последние {self._max_entries}"
        )

    # ── публичное ─────────────────────────────────────────────────────────────
    def append(self, entry: dict) -> None:
        """Потокобезопасный append одной записи."""
        with self._lock:
            try:
                data = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
                handle = self._open()
                handle.write(data)
                handle.flush()
            except OSError as exc:
                logger.warning(f"Не удалось записать в undo-лог: {exc}")
                self._close_handle()
                return

            self._appends += 1
            self._since_fsync += 1
            if self._since_fsync >= JOURNAL_FSYNC_INTERVAL:
                self._fsync_locked()
            self._maybe_compact()

    def _fsync_locked(self) -> None:
        """
        fsync без захвата лока — вызывает тот, кто уже держит self._lock.

        Отдельный метод, а не flush_to_disk(): лок в append нереентерабельный,
        и вызов публичного метода изнутри него вставал бы навсегда (это уже
        случалось — на 50-й записи, когда срабатывает пачка fsync).
        """
        self._since_fsync = 0
        handle = self._handle
        if handle is None:
            return
        with contextlib.suppress(OSError, ValueError):
            handle.flush()
            os.fsync(handle.fileno())

    def flush_to_disk(self) -> None:
        """Сбрасывает журнал на диск (fsync): записи переживают потерю питания."""
        with self._lock:
            self._fsync_locked()

    def close(self) -> None:
        """Сбрасывает и закрывает журнал (вызывается в конце сортировки)."""
        self.flush_to_disk()
        with self._lock:
            self._close_handle()


# ── Персистентный лог отмены ───────────────────────────────────────────────────

def _undo_log_path(source_dir: Path) -> Path:
    return source_dir / UNDO_LOG_FILENAME


def _load_undo_log(source_dir: Path) -> list[dict[str, Any]]:
    """Загружает undo-лог каталога (см. _read_undo_log_file)."""
    return _read_undo_log_file(_undo_log_path(source_dir))


def _read_undo_log_file(path: Path) -> list[dict[str, Any]]:
    """
    Читает undo-лог по пути.

    Поддерживает два формата для обратной совместимости:
    - Новый JSONL: одна JSON-запись на строку (append-only).
    - Старый JSON-массив: один большой массив объектов.

    Оборванная запись (Ctrl+C прямо во время записи) пропускается — остальные
    записи журнала остаются читаемыми, поэтому отдельная «двухфазная» запись
    для каждой операции не нужна.
    """
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return []
        # Старый формат: единый JSON-массив (начинается с '[')
        stripped = content.lstrip()
        if stripped.startswith("["):
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    return data
                return []
            except json.JSONDecodeError:
                return []
        # Новый формат: JSONL
        entries: list[dict[str, Any]] = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    entries.append(obj)
            except json.JSONDecodeError:
                continue  # пропускаем битую строку
        return entries
    except OSError:
        return []


def _save_undo_log(source_dir: Path, entries: list[dict]) -> None:
    """Полностью перезаписывает undo-лог каталога (см. _write_undo_log_file)."""
    _write_undo_log_file(_undo_log_path(source_dir), entries)


def _write_undo_log_file(path: Path, entries: list[dict]) -> None:
    """
    Полностью перезаписывает undo-лог в формате JSONL АТОМАРНО.

    M2 fix: запись идёт во временный файл с fsync, затем os.replace —
    атомарная подмена. Прерывание (Ctrl+C, сбой) не оставит половинчатый
    журнал, а параллельный писатель не может затереть записи частично.

    Используется undo_last_operation (запись оставшихся записей после
    частичной отмены), компактизацией журнала и очисткой старых бэкапов.
    Для добавления новых записей служит _UndoLogWriter.append().
    """
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning(f"Не удалось сохранить лог отмены: {exc}")
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def get_undo_count(source_dir: str) -> int:
    """Возвращает количество операций, доступных для отмены."""
    return len(_load_undo_log(Path(source_dir).resolve()))


# ── organize_files ─────────────────────────────────────────────────────────────

def organize_files(
    source_dir: str,
    dry_run: bool = False,
    verbose: bool = True,
    recursive: Optional[bool] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Сортирует файлы по категориям.

    Args:
        source_dir:         Путь к директории
        dry_run:            Только план, без действий (переопределяет конфиг)
        verbose:            Подробный вывод в stdout
        recursive:          None = брать из конфига, True/False = переопределить
        log_callback:       Колбэк для строк лога (GUI / мониторинг)
        progress_callback:  Колбэк (current: int, total: int) для прогресс-бара
        config:             Конфигурация; None = загрузить из config.json

    Returns:
        Словарь статистики
    """
    if config is None:
        config = load_config()

    # Защита от параллельного запуска в одной и той же папке: watchdog может
    # срабатывать, пока предыдущая сортировка ещё не закончилась — это вызывает
    # гонки за shutil.move, mkdir и одновременную запись в undo-лог.
    try:
        lock_key = str(Path(source_dir).resolve())
    except (OSError, ValueError):
        lock_key = str(source_dir)
    dir_lock = _get_dir_lock(lock_key)

    if not dir_lock.lock.acquire(blocking=False):
        logger.info(
            "organize_files: сортировка этой папки уже выполняется — пропуск "
            "(новые файлы будут обработаны в следующем цикле)."
        )
        _release_dir_lock(lock_key, dir_lock)
        return {
            "moved": 0, "skipped": 0, "errors": 0,
            "duplicates": 0, "content_duplicates": 0, "empty_dirs_removed": 0,
            "journal_trimmed": 0, "backups_removed": 0, "ignored": 0,
            "by_category": {}, "skipped_concurrent": True,
        }

    try:
        return _organize_files_impl(
            source_dir=source_dir,
            dry_run=dry_run,
            verbose=verbose,
            recursive=recursive,
            log_callback=log_callback,
            progress_callback=progress_callback,
            config=config,
        )
    finally:
        dir_lock.lock.release()
        _release_dir_lock(lock_key, dir_lock)


def _organize_files_impl(
    source_dir: str,
    dry_run: bool,
    verbose: bool,
    recursive: Optional[bool],
    log_callback: Optional[Callable[[str], None]],
    progress_callback: Optional[Callable[[int, int], None]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Внутренняя реализация organize_files под блокировкой своей папки."""
    # H4 fix: validate config even when passed directly (bypassing load_config)
    if not _validate_config_schema(config):
        raise ValueError("Переданная конфигурация не прошла валидацию схемы")

    settings = config.get("settings", DEFAULT_CONFIG["settings"])
    ext_map = _build_ext_map(config)
    category_names: frozenset = frozenset(config.get("categories", {}).keys())
    script_files = _get_script_files(config)  # Получаем из конфига
    max_file_size = settings.get("max_file_size", MAX_FILE_SIZE)

    # Параметр recursive: явный аргумент имеет приоритет над конфигом
    use_recursive: bool = settings.get("recursive", True) if recursive is None else recursive
    # dry_run: если передан True — переопределяем; иначе из конфига
    use_dry_run: bool = settings.get("dry_run", False) or dry_run
    clean_empty: bool = settings.get("clean_empty_dirs", True)
    dup_strategy: str = settings.get("duplicate_strategy", "rename")  # rename | skip | replace
    # Срок хранения бэкапов заменённых файлов (0 = хранить всегда).
    # Тип и знак проверены схемой конфига (см. _validate_config_schema).
    backup_retention_days: int = settings.get(
        "backup_retention_days", DEFAULT_BACKUP_RETENTION_DAYS
    )
    # Журнал отмены не должен расти бесконечно (0 = без ограничения)
    max_undo_entries: int = settings.get("max_undo_entries", DEFAULT_MAX_UNDO_ENTRIES)
    # Дубликаты по содержимому — опция: чтение файлов стоит времени, по умолчанию выключено
    detect_content_dups: bool = bool(settings.get("content_duplicates", False))

    source_path = Path(source_dir).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Директория не найдена: {source_path}")
    if not source_path.is_dir():
        raise NotADirectoryError(f"Не является директорией: {source_path}")

    # Дополнительная проверка на защищённые директории
    if is_protected_dir(source_path):
        raise ValueError(f"Отказ в работе с защищённой системной директорией: {source_path}")

    stats: dict[str, Any] = {
        "moved": 0, "skipped": 0, "errors": 0,
        "duplicates": 0, "content_duplicates": 0, "empty_dirs_removed": 0,
        "journal_trimmed": 0, "backups_removed": 0, "ignored": 0,
        "by_category": {},
    }
    session_log: list[dict] = []

    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)
        elif verbose:
            logger.info(msg)

    log(f"\n{'─'*60}")
    log("  Умный органайзер файлов")
    log(f"{'─'*60}")
    log(f"  Директория : {_mask_dir_for_log(source_path)}")
    log(f"  Режим      : {'рекурсивный' if use_recursive else 'только корень'}")
    if use_dry_run:
        log("  ⚠  DRY-RUN — файлы НЕ будут перемещены")
    if detect_content_dups:
        log("  ♻  Дубликаты по содержимому: файлы с уже имеющимся содержимым не переносятся")
    if backup_retention_days > 0:
        log(f"  🧹 Бэкапы старше {backup_retention_days} дн. удаляются")
    log(f"{'─'*60}\n")

    # lru_cache для _resolve_cached инвалидируем ПЕРЕД сканированием,
    # чтобы не использовать устаревшие записи (файлы могли быть удалены/переименованы
    # между вызовами organize_files в режиме мониторинга).
    _resolve_cached.cache_clear()

    # Очистка старых бэкапов — до сортировки: тогда она выполняется и когда
    # сортировать уже нечего (иначе уборка никогда бы не запускалась).
    if backup_retention_days > 0:
        stats["backups_removed"] = _cleanup_old_backups(
            source_path, category_names, backup_retention_days, use_dry_run, log
        )

    # ── .organizerignore: пользовательские исключения ──────────────────────────
    ignore_patterns = _load_ignore_patterns(source_path)
    ignored_files = 0
    ignored_dirs = 0

    def _dir_is_ignored(dir_path: Path) -> bool:
        """True — каталог исключён шаблонами .organizerignore (сам или родителем)."""
        if not ignore_patterns:
            return False
        try:
            rel = dir_path.relative_to(source_path).as_posix()
        except ValueError:
            return False
        return _matches_ignore(rel, ignore_patterns, is_dir=True)

    def _ignored_dir(dir_path: Path) -> bool:
        """Предикат для сканера: исключённый каталог внутрь не пускаем и считаем."""
        nonlocal ignored_dirs
        if _dir_is_ignored(dir_path):
            ignored_dirs += 1
            return True
        return False

    # ── Сбор файлов через os.scandir() — получаем stat бесплатно ──────────────
    raw_file_stats, scan_errors = _scan_files_fast(
        source_path, use_recursive, _ignored_dir if ignore_patterns else None
    )
    # M8 fix: недоступные подпапки/файлы видны в статистике, а не молчат
    if scan_errors:
        log(f"  ⚠  Не удалось прочитать объектов: {len(scan_errors)}")
        for err_path in scan_errors[:5]:
            log(f"     • {_mask_path_for_log(Path(err_path), source_path)}")
        if len(scan_errors) > 5:
            log(f"     • … и ещё {len(scan_errors) - 5}")
        stats["errors"] += len(scan_errors)

    # Строим нижний регистр категорий один раз — используем в _should_skip
    category_names_lower: frozenset = frozenset(c.lower() for c in category_names)

    def _should_skip(f: Path) -> bool:
        nonlocal ignored_files
        if settings.get("ignore_hidden", True) and f.name.startswith("."):
            return True
        # Свой пробный файл (проверка регистра ФС) не сортируем даже при
        # ignore_hidden=false: он живёт миллисекунды и не является данными.
        if f.name.startswith(CASE_PROBE_PREFIX):
            return True
        # Бэкап заменённого файла (<оригинал>.bak-<время>) — служебный: он должен
        # лежать рядом с тем файлом, который заменили. Иначе расширение «bak-…»
        # считалось бы неизвестным и бэкап уезжал в other/, а undo, который ищет
        # его по пути из журнала, не мог бы вернуть заменённый файл.
        if _backup_timestamp(f.name) is not None:
            return True
        try:
            rel = f.relative_to(source_path)
            # Шаблоны .organizerignore: файл просто не сортируем (см. README).
            if ignore_patterns and _matches_ignore(
                rel.as_posix(), ignore_patterns, is_dir=False
            ):
                ignored_files += 1
                return True
            # M12 fix: служебные файлы самого органайзера пропускаем только в КОРНЕ
            # сортируемой папки. Раньше сравнение шло по basename для любого уровня —
            # пользовательский config.json или organizer_core.py в подпапке
            # молча игнорировался. Сам .organizerignore тоже не сортируем: иначе
            # при ignore_hidden=false он уезжал бы в other/ и правила «терялись».
            if len(rel.parts) == 1 and (
                f.name in script_files or f.name == ORGANIZER_IGNORE_FILENAME
            ):
                return True
            first_part_lower = rel.parts[0].lower()
            if first_part_lower in category_names_lower:
                # M1 fix: файл внутри папки-категории пропускается ТОЛЬКО если
                # он уже лежит в СВОЕЙ категории. Файл videos/photo.jpg (картинка
                # в неправильной категории) должен быть пересортирован в images/.
                actual_category = _get_category(f, ext_map)
                if actual_category.lower() == first_part_lower:
                    return True
        except ValueError:
            pass
        return False

    files_with_stat = [(f, st) for f, st in raw_file_stats if not _should_skip(f)]
    # ignored_files — файлы, пропущенные поштучно; файлы внутри исключённых
    # каталогов даже не сканируются, поэтому их число не считаем (видно по папкам)
    stats["ignored"] = ignored_files
    if ignore_patterns:
        log(
            f"  🙈 {ORGANIZER_IGNORE_FILENAME}: файлов пропущено: {ignored_files}, "
            f"папок: {ignored_dirs}"
        )
    total = len(files_with_stat)
    log(f"  Файлов для обработки: {total}\n")

    if total == 0:
        log("  Нечего делать.\n")
        return stats

    # ── Пакетная обработка: группируем по категориям ───────────────────────────
    # Это позволяет:
    # 1. Создать целевую папку ровно один раз на категорию
    # 2. Сканировать содержимое целевой папки один раз на категорию
    #    и использовать in-memory set для всех проверок коллизий
    # 3. Значительно сократить число mkdir/exists системных вызовов

    # Сначала фильтруем и проверяем безопасность — строим батчи.
    # M6 fix: фильтрация (быстрая фаза) занимает первую половину прогресса,
    # перемещение файлов (реальная работа) — вторую. Раньше прогресс доходил
    # до 100% ещё до первого перемещения.
    batches: defaultdict[str, list[tuple[Path, os.stat_result]]] = defaultdict(list)
    progress_total = total * 2
    global_idx = 0

    for file_path, st in files_with_stat:
        global_idx += 1
        if progress_callback:
            progress_callback(min(global_idx, total), progress_total)

        if not _validate_path_safety(file_path, source_path):
            log(f"  ✗  Пропущен небезопасный путь: {file_path.name}")
            stats["errors"] += 1
            continue

        if st.st_size > max_file_size:
            log(f"  ✗  Файл слишком большой (> {max_file_size // (1024*1024)} MB): {file_path.name}")
            stats["errors"] += 1
            continue

        category = _get_category(file_path, ext_map)
        batches[category].append((file_path, st))

    # Писатель undo-журнала: append-only JSONL, потокобезопасен и не делает
    # read-modify-write всего файла на каждую запись.
    undo_writer = (
        _UndoLogWriter(_undo_log_path(source_path), max_entries=max_undo_entries)
        if not use_dry_run
        else None
    )

    # M6 fix: вторая половина прогресса — по факту обработки файлов
    handled = 0

    def _report_move_progress() -> None:
        nonlocal handled
        handled += 1
        if progress_callback:
            progress_callback(min(total + handled, progress_total), progress_total)

    # Обрабатываем каждую категорию одним блоком
    for category, batch in batches.items():
        target_cat_dir = source_path / category
        stats["by_category"].setdefault(category, 0)

        # os.scandir отдаёт файлы в порядке файловой системы, а от порядка зависит
        # результат: какой из одинаковых файлов останется на месте (дубликаты по
        # содержимому) и кто получит суффикс _1 при коллизии имён. Сортируем,
        # чтобы поведение было предсказуемым и одинаковым между запусками.
        batch.sort(key=lambda item: str(item[0]))

        if use_dry_run:
            for file_path, _ in batch:
                masked = _mask_path_for_log(file_path, source_path)
                log(f"  → {masked}  ➜  {category}/")
                stats["moved"] += 1
                stats["by_category"][category] += 1
                _report_move_progress()
            continue

        # mkdir один раз на категорию
        try:
            target_cat_dir.mkdir(exist_ok=True)
        except Exception as exc:
            log(f"  ✗  Ошибка создания папки категории '{category}': {exc}")
            stats["errors"] += len(batch)
            continue

        # H1 fix: проверяем, что целевая папка категории не уводит нас за пределы source_path
        # (защита от симлинков на категории и небезопасных имён категорий из конфига)
        try:
            resolved_target = target_cat_dir.resolve()
            resolved_target.relative_to(source_path)
        except (OSError, ValueError):
            log(f"  ✗  Целевая папка категории '{category}' выходит за пределы рабочей директории — пропуск")
            stats["errors"] += len(batch)
            continue

        # Сканируем существующие имена в целевой папке один раз на категорию
        # — все проверки коллизий идут через этот set без лишних stat-вызовов
        with _lock:
            try:
                with os.scandir(target_cat_dir) as it:
                    existing_names: set[str] = {e.name for e in it}
            except OSError:
                existing_names = set()

            # Коллизии считаем так, как их видит сама ФС: реальный пробник
            # каталога (регистр) + Unicode-нормализация NFC (macOS хранит NFD).
            case_insensitive = _fs_is_case_insensitive(target_cat_dir)
            existing_keys: set[str] = {
                _name_key(n, case_insensitive) for n in existing_names
            }
            # Индекс содержимого строится лениво — только если опция включена
            # и есть с чем сравнивать (первый же файл запускает индексацию).
            content_index: Optional[_ContentIndex] = (
                _ContentIndex(target_cat_dir) if detect_content_dups else None
            )

            for file_path, file_stat in batch:
                target_path = target_cat_dir / file_path.name

                # Дубликаты по содержимому: если точно такой же файл (по SHA-256)
                # уже лежит в категории, не тащим второй экземпляр. Проверяем до
                # коллизии имён: одинаковое содержимое важнее одинакового имени.
                if content_index is not None:
                    twin = content_index.find_identical(file_path, file_stat.st_size)
                    if twin is not None:
                        stats["content_duplicates"] += 1
                        stats["skipped"] += 1
                        log(
                            f"  ♻  Пропущен (то же содержимое, что у "
                            f"{_mask_path_for_log(twin, source_path)}): "
                            f"{_mask_path_for_log(file_path, source_path)}"
                        )
                        _report_move_progress()
                        continue

                try:
                    if _name_in_set(
                        file_path.name, existing_names, existing_keys, case_insensitive
                    ):
                        if dup_strategy == "skip":
                            masked = _mask_path_for_log(file_path, source_path)
                            log(f"  ⏭  Пропущен (уже есть): {masked}")
                            stats["skipped"] += 1
                            stats["duplicates"] += 1
                            _report_move_progress()
                            continue
                        elif dup_strategy == "replace":
                            # H2 fix: заменяемый файл переносится в бэкап (rename-aside),
                            # а не удаляется — замена становится обратимой.
                            # Имя бэкапа делаем уникальным: два файла с одинаковым
                            # именем в течение одной секунды не должны затереть
                            # бэкап друг друга.
                            backup_name = (
                                f"{target_path.name}.bak-"
                                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                            )
                            backup_path = _get_unique_path(
                                target_cat_dir,
                                backup_name,
                                existing_names,
                                existing_keys,
                            )
                            if _path_too_long(backup_path):
                                log(
                                    f"  ✗  Путь слишком длинный для этой ОС "
                                    f"(лимит {_max_path_length()} символов): "
                                    f"{_mask_path_for_log(file_path, source_path)}"
                                )
                                stats["errors"] += 1
                                _report_move_progress()
                                continue

                            try:
                                shutil.move(str(target_path), str(backup_path))
                                existing_names.discard(file_path.name)
                                existing_keys.discard(
                                    _name_key(file_path.name, case_insensitive)
                                )
                                _add_name(
                                    backup_path.name,
                                    existing_names,
                                    existing_keys,
                                    case_insensitive,
                                )
                                masked = _mask_path_for_log(file_path, source_path)
                                log(f"  🔄 Заменён (бэкап: {backup_path.name}): {masked}")
                                stats["duplicates"] += 1

                                # Запись о бэкапе в undo-журнал: полный undo вернёт
                                # заменённый файл на место
                                if undo_writer:
                                    backup_entry: dict[str, Any] = {
                                        "timestamp": datetime.now().isoformat(),
                                        "action": "backup",
                                        "source": backup_path.relative_to(source_path).as_posix(),
                                        "destination": target_path.relative_to(source_path).as_posix(),
                                        "original_name": file_path.name,
                                    }
                                    undo_writer.append(backup_entry)
                                    session_log.append(backup_entry)
                            except Exception as exc:
                                log(f"  ✗  Ошибка создания бэкапа для замены: {exc}")
                                stats["errors"] += 1
                                _report_move_progress()
                                continue
                        else:  # rename (default) — передаём уже готовый set
                            target_path = _get_unique_path(
                                target_cat_dir,
                                file_path.name,
                                existing_names,
                                existing_keys,
                            )
                            masked = _mask_path_for_log(file_path, source_path)
                            log(f"  ⚠  Переименован: {masked} → {target_path.name}")
                            stats["duplicates"] += 1

                    if _path_too_long(target_path):
                        log(
                            f"  ✗  Путь слишком длинный для этой ОС "
                            f"(лимит {_max_path_length()} символов): "
                            f"{_mask_path_for_log(file_path, source_path)}"
                        )
                        stats["errors"] += 1
                        _report_move_progress()
                        continue

                    shutil.move(str(file_path), str(target_path))
                    # Добавляем новое имя в set, чтобы следующий файл с тем же
                    # именем в этой же категории корректно получил суффикс
                    _add_name(
                        target_path.name,
                        existing_names,
                        existing_keys,
                        case_insensitive,
                    )
                    if content_index is not None:
                        # Дубликаты внутри одной партии: второй одинаковый файл
                        # должен найти первый, уже перемещённый.
                        content_index.add(target_path)

                    masked = _mask_path_for_log(file_path, source_path)
                    log(f"  ✓  {masked}  ➜  {category}/{target_path.name}")

                    # Журнал хранит пути в POSIX-виде: записанный на Windows
                    # журнал должен читаться на Linux/macOS и наоборот.
                    try:
                        rel_source = file_path.relative_to(source_path).as_posix()
                        rel_dest = target_path.relative_to(source_path).as_posix()
                    except ValueError:
                        rel_source = file_path.name
                        rel_dest = target_path.name

                    entry: dict[str, Any] = {
                        "timestamp":     datetime.now().isoformat(),
                        "action":        "move",
                        "source":        rel_source,
                        "destination":   rel_dest,
                        "original_name": file_path.name,
                    }
                    session_log.append(entry)
                    # Append в JSONL — потокобезопасно, без read-modify-write
                    if undo_writer:
                        undo_writer.append(entry)

                    stats["moved"] += 1
                    stats["by_category"][category] += 1
                    _report_move_progress()

                except Exception as exc:
                    masked = _mask_path_for_log(file_path, source_path)
                    log(f"  ✗  Ошибка: {masked} — {exc}")
                    stats["errors"] += 1
                    _report_move_progress()

    # _UndoLogWriter синхронный — отдельный flush не нужен.
    # Синхронизируем in-memory лог сессии (оставлен для совместимости с тестами).
    # M5 fix: держим не больше MAX_IN_MEMORY_LOG_ENTRIES записей.
    if session_log:
        with _lock:
            _action_log.extend(session_log)
            if len(_action_log) > MAX_IN_MEMORY_LOG_ENTRIES:
                del _action_log[: len(_action_log) - MAX_IN_MEMORY_LOG_ENTRIES]

    # ── Удаление пустых папок ──────────────────────────────────────────────────
    if not use_dry_run and use_recursive and clean_empty:
        log("\n  Очистка пустых папок...")
        # Собираем все директории через os.walk (bottom-up) — уже от глубоких к верхним,
        # без дополнительной сортировки
        for dirpath_str, _subdirs, _ in os.walk(str(source_path), topdown=False):
            dirpath = Path(dirpath_str)
            if dirpath == source_path:
                continue
            # Исключённые каталоги — не наша территория: пользователь просил их
            # не трогать, поэтому и пустыми не удаляем (это касается и подпапок
            # внутри исключённого каталога).
            if _dir_is_ignored(dirpath):
                continue
            if is_protected_dir(dirpath):
                log(f"  ⚠  Пропущена защищённая директория: {dirpath.name}")
                continue
            try:
                rel = dirpath.relative_to(source_path)
            except ValueError:
                continue
            if rel.parts[0].lower() in category_names_lower:
                continue
            try:
                # os.scandir дешевле any(iterdir()) — не создаёт Path-объекты
                with os.scandir(dirpath) as it:
                    is_empty = next(it, None) is None
                if is_empty:
                    dirpath.rmdir()
                    stats["empty_dirs_removed"] += 1
                    masked_rel = _mask_path_for_log(dirpath, source_path)
                    log(f"  ✓  Удалена пустая папка: {masked_rel}")
            except Exception:
                pass

    # Журнал сбрасываем на диск и закрываем до итога: записи переживают
    # потерю питания, а число выброшенных компактизацией записей попадает
    # в статистику.
    if undo_writer is not None:
        stats["journal_trimmed"] = undo_writer.trimmed
        undo_writer.close()

    # ── Статистика ─────────────────────────────────────────────────────────────
    log(f"\n{'─'*60}")
    log("  ИТОГО")
    log(f"{'─'*60}")
    log(f"  Перемещено  : {stats['moved']}")
    log(f"  Пропущено   : {stats['skipped']}")
    log(f"  Дубликаты   : {stats['duplicates']}")
    if detect_content_dups or stats["content_duplicates"]:
        log(f"  ♻ По содержимому: {stats['content_duplicates']}")
    if stats["journal_trimmed"]:
        log(f"  🗜 Журнал отмены: удалено старых записей: {stats['journal_trimmed']}")
    if stats["backups_removed"]:
        action = "Будет удалено" if use_dry_run else "Удалено"
        log(f"  🧹 {action} старых бэкапов: {stats['backups_removed']}")
    log(f"  Ошибки      : {stats['errors']}")
    if use_recursive and not use_dry_run:
        log(f"  Пустых папок удалено: {stats['empty_dirs_removed']}")
    if stats["by_category"]:
        log("\n  По категориям:")
        for cat, cnt in sorted(stats["by_category"].items()):
            log(f"    {cat:<16}{cnt}")
    log(f"{'─'*60}\n")

    return stats


# ── Очистка старых бэкапов ─────────────────────────────────────────────────────

def _backup_timestamp(filename: str) -> Optional[datetime]:
    """
    Время создания бэкапа из его имени: <оригинал>.bak-ГГГГММДД_ЧЧММСС.

    None — если имя не подходит под формат. Чужие файлы, лишь похожие на
    бэкапы, не удаляются никогда.
    """
    match = _BACKUP_NAME_RE.search(filename)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _drop_backup_journal_entries(
    source_path: Path, removed_rel: list[str], log: Callable[[str], None]
) -> None:
    """Убирает из журнала записи об удалённых бэкапах: восстанавливать нечего."""
    entries = _load_undo_log(source_path)
    if not entries:
        return
    gone = set(removed_rel)
    kept = [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and entry.get("action") == "backup"
            and entry.get("source") in gone
        )
    ]
    if len(kept) != len(entries):
        _save_undo_log(source_path, kept)
        log(f"  🗑  Журнал отмены: убрано записей об удалённых бэкапах: {len(entries) - len(kept)}")


def _cleanup_old_backups(
    source_path: Path,
    category_names: "frozenset[str] | set[str]",
    retention_days: int,
    dry_run: bool,
    log: Callable[[str], None],
) -> int:
    """
    Удаляет устаревшие бэкапы заменённых файлов (`<оригинал>.bak-ГГГГММДД_ЧЧММСС`).

    Бэкапы создаёт стратегия replace, и без очистки они копятся в папках
    категорий вечно.

    Удаляем только строго узнаваемые имена и только старше retention_days.
    Возвращает число удалённых файлов; в dry-run файлы не трогаются, но счётчик
    показывает, сколько было бы удалено (как stats["moved"] для переносов).
    """
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed_rel: list[str] = []
    removed = 0

    for category in sorted(category_names):
        cat_dir = source_path / category
        if not cat_dir.is_dir():
            continue
        try:
            with os.scandir(cat_dir) as it:
                entries = [e for e in it if e.is_file(follow_symlinks=False)]
        except OSError:
            continue

        for entry in entries:
            stamp = _backup_timestamp(entry.name)
            if stamp is None or stamp > cutoff:
                continue
            backup_path = Path(entry.path)
            masked = _mask_path_for_log(backup_path, source_path)
            if dry_run:
                removed += 1
                log(f"  🧹 Старый бэкап будет удалён: {masked}")
                continue
            try:
                backup_path.unlink()
            except OSError as exc:
                log(f"  ✗  Не удалось удалить бэкап {masked}: {exc}")
                continue
            removed += 1
            try:
                removed_rel.append(backup_path.relative_to(source_path).as_posix())
            except ValueError:
                removed_rel.append(backup_path.name)
            log(f"  🧹 Удалён старый бэкап: {masked}")

    if removed_rel:
        _drop_backup_journal_entries(source_path, removed_rel, log)
    return removed


# ── undo_last_operation ────────────────────────────────────────────────────────

def undo_last_operation(
    source_dir: str,
    count: int = -1,
    verbose: bool = True,
    log_callback: Optional[Callable[[str], None]] = None,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, int]:
    """
    Отменяет последние операции перемещения.

    Читает из персистентного лога — работает после перезапуска программы.

    Args:
        source_dir:   Корневая директория
        count:        Сколько операций отменить (-1 = все, 0 = no-op)
        verbose:      Подробный вывод в stdout
        log_callback: Колбэк для строк лога
        config:       Конфигурация (влияет на duplicate_strategy при конфликте);
                      None = загрузить из config.json
    """
    # M2 fix: undo и organize не должны выполняться одновременно —
    # иначе undo может читать журнал до того, как organize дописал записи,
    # и перезаписать их своим _save_undo_log.
    try:
        lock_key = str(Path(source_dir).resolve())
    except (OSError, ValueError):
        lock_key = str(source_dir)
    dir_lock = _get_dir_lock(lock_key)
    try:
        with dir_lock.lock:
            return _undo_last_operation_impl(
                source_dir=source_dir,
                count=count,
                verbose=verbose,
                log_callback=log_callback,
                config=config,
            )
    finally:
        _release_dir_lock(lock_key, dir_lock)


def _undo_last_operation_impl(
    source_dir: str,
    count: int = -1,
    verbose: bool = True,
    log_callback: Optional[Callable[[str], None]] = None,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, int]:
    """Внутренняя реализация undo_last_operation под блокировкой своей папки."""
    stats = {"restored": 0, "errors": 0}
    source_path = Path(source_dir).resolve()

    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)
        elif verbose:
            logger.info(msg)

    log(f"\n{'─'*60}")
    log("  ОТМЕНА ОПЕРАЦИЙ")
    log(f"{'─'*60}")

    all_entries = _load_undo_log(source_path)
    if not all_entries:
        log("  Нет операций для отмены.")
        log(f"{'─'*60}\n")
        return stats

    # Валидация count
    if count == 0:
        log("  count=0: нечего отменять.")
        log(f"{'─'*60}\n")
        return stats
    if count < -1:
        log(f"  Некорректное значение count={count} (допустимо: -1 или >0). Ничего не делаем.")
        log(f"{'─'*60}\n")
        return stats

    if count == -1:
        to_undo = all_entries
        remaining: list[dict] = []
    else:
        if count > len(all_entries):
            log(f"  count={count} превышает доступных операций ({len(all_entries)}). Отменяем все.")
            to_undo = all_entries
            remaining = []
        else:
            to_undo = all_entries[-count:]
            remaining = all_entries[: len(all_entries) - count]

    log(f"  Операций для отмены: {len(to_undo)}\n")

    failed: list[dict] = []
    for entry in reversed(to_undo):
        # H4 fix: gracefully handle malformed entries
        if not isinstance(entry, dict):
            stats["errors"] += 1
            failed.append(entry if isinstance(entry, dict) else {"action": "invalid"})
            continue

        action = entry.get("action")

        # H2 fix: запись о бэкапе (replace) отменяется обратным перемещением
        # бэкапа на место заменённого файла. Обход идёт в обратном порядке,
        # поэтому «move» уже вернул новый файл в исходную папку и имя свободно.
        if action == "backup":
            backup_rel = entry.get("source")
            dest_rel = entry.get("destination")
            if not backup_rel or not dest_rel:
                log(f"  ✗  Неполная запись бэкапа в логе: {entry.get('original_name', '?')}")
                stats["errors"] += 1
                failed.append(entry)
                continue

            backup_path = source_path / backup_rel
            restore_path = source_path / dest_rel

            if not _validate_path_safety(backup_path, source_path) or not _validate_path_safety(
                restore_path, source_path
            ):
                log(f"  ✗  Небезопасный путь бэкапа в логе: {entry.get('original_name', '?')}")
                stats["errors"] += 1
                failed.append(entry)
                continue

            if not backup_path.exists():
                log(f"  ⚠  Бэкап не найден: {backup_path.name}")
                stats["errors"] += 1
                failed.append(entry)
                continue

            if restore_path.exists():
                # На месте заменённого файла уже что-то есть — не затираем молча
                log(
                    f"  ⏭  Пропущено восстановление бэкапа "
                    f"(файл уже существует): {restore_path.name}"
                )
                stats["errors"] += 1
                failed.append(entry)
                continue

            try:
                shutil.move(str(backup_path), str(restore_path))
                log(f"  ✓  Возвращён из бэкапа: {restore_path.name}")
                stats["restored"] += 1
            except OSError as exc:
                log(f"  ✗  Ошибка восстановления бэкапа: {exc}")
                stats["errors"] += 1
                failed.append(entry)
            continue

        if action != "move":
            # Прочие служебные записи сохраняем, но не отменяем
            remaining.append(entry)
            continue

        # H4 fix: use .get() with validation for required keys
        dest_rel = entry.get("destination")
        src_rel = entry.get("source")
        orig_name = entry.get("original_name", "?")

        if not dest_rel or not src_rel:
            log(f"  ✗  Неполная запись в логе (нет destination/source): {orig_name}")
            stats["errors"] += 1
            failed.append(entry)
            continue

        # Пути в логе теперь относительные, поэтому добавляем source_dir
        dest = source_path / dest_rel
        src = source_path / src_rel

        # ── Критическая проверка: пути из лога могут быть подделаны ──────────
        # Защита от path traversal через подмену .organizer_undo.json
        if not _validate_path_safety(dest, source_path):
            log(f"  ✗  Небезопасный путь назначения в логе: {orig_name}")
            stats["errors"] += 1
            failed.append(entry)
            continue
        if not _validate_path_safety(src, source_path):
            log(f"  ✗  Небезопасный исходный путь в логе: {orig_name}")
            stats["errors"] += 1
            failed.append(entry)
            continue

        if not dest.exists():
            log(f"  ⚠  Файл не найден: {dest.name}")
            stats["errors"] += 1
            failed.append(entry)
            continue

        try:
            src.parent.mkdir(parents=True, exist_ok=True)

            # Проверяем, не появился ли новый файл на исходном месте
            if src.exists():
                # Применяем стратегию duplicate_strategy (по умолчанию rename)
                # для избежания неявной перезаписи пользовательских данных
                _cfg = config if config is not None else load_config()
                settings = _cfg.get("settings", DEFAULT_CONFIG["settings"])
                dup_strategy = settings.get("duplicate_strategy", "rename")

                if dup_strategy == "skip":
                    log(f"  ⏭  Пропущен (файл уже существует): {entry['original_name']}")
                    stats["errors"] += 1
                    failed.append(entry)
                    continue
                elif dup_strategy == "replace":
                    # replace — перезаписываем (как раньше), но с предупреждением
                    log(f"  ⚠  Заменён существующий файл: {entry['original_name']}")
                    src.unlink()
                else:  # rename
                    # Используем _get_unique_path для поиска свободного имени
                    with _lock:
                        try:
                            with os.scandir(src.parent) as it:
                                existing_names = {e.name for e in it}
                        except OSError:
                            existing_names = set()
                    src = _get_unique_path(src.parent, src.name, existing_names)
                    log(f"  ⚠  Переименован при восстановлении: {entry['original_name']} → {src.name}")

            shutil.move(str(dest), str(src))
            log(f"  ✓  Восстановлен: {entry['original_name']}")
            stats["restored"] += 1
        except Exception as exc:
            log(f"  ✗  Ошибка: {exc}")
            stats["errors"] += 1
            failed.append(entry)

    _save_undo_log(source_path, remaining + failed)

    with _lock:
        _action_log.clear()

    log(f"\n  Восстановлено : {stats['restored']}")
    log(f"  Ошибок        : {stats['errors']}")
    log(f"{'─'*60}\n")
    return stats


# ── Мониторинг ─────────────────────────────────────────────────────────────────

# Реестр активных watchdog-обработчиков, чтобы stop_monitoring() мог
# отменить запланированный threading.Timer (иначе отложенная сортировка
# запускается уже после остановки мониторинга).
_active_handlers: list[Any] = []
_active_handlers_lock = threading.Lock()

# Минимальный интервал, в течение которого размер/mtime файла должны
# оставаться неизменными, прежде чем файл считается «дозаписанным» и
# готовым к сортировке. Защищает от перемещения ещё скачивающихся файлов.
_FILE_STABLE_SECONDS = 2.0


def _file_is_stable(path_str: str, stable_seconds: float = _FILE_STABLE_SECONDS) -> bool:
    """
    Проверяет, что файл «устоялся»: размер и mtime не меняются.

    Делает два замера с паузой stable_seconds. Используется перед сортировкой
    в режиме мониторинга, чтобы не перемещать недокачанные файлы целиком.
    """
    try:
        st1 = os.stat(path_str)
    except OSError:
        return False
    time.sleep(stable_seconds)
    try:
        st2 = os.stat(path_str)
    except OSError:
        return False
    return st1.st_size == st2.st_size and st1.st_mtime == st2.st_mtime


# Расширения-«маркеры» незавершённой загрузки. События по таким файлам
# игнорируются — файл обрабатывается после переименования в финальное имя.
INCOMPLETE_SUFFIXES: tuple[str, ...] = (
    ".crdownload", ".part", ".partial", ".download",
    ".filepart", ".opdownload",
)


def _is_incomplete_path(path_str: str) -> bool:
    """True, если файл — маркер незавершённой загрузки."""
    lowered = path_str.lower()
    return any(lowered.endswith(sfx) for sfx in INCOMPLETE_SUFFIXES)


def _path_from_event(event: Any) -> str:
    """
    Извлекает путь из события watchdog.

    Для on_moved это dest_path (итоговое имя: file.crdownload → file.zip),
    для остальных — src_path. H6 fix: ранее обрабатывался только src_path
    события on_created, из-за чего переименование/перемещение готового файла
    в папку не запускало сортировку.
    """
    dest = getattr(event, "dest_path", None)
    if dest:
        return str(dest)
    return str(getattr(event, "src_path", ""))


class _DebounceScheduler:
    """
    Адаптивный debounce для событий файловой системы.

    Чем быстрее поступают события, тем дольше ждём перед запуском сортировки.
    Это предотвращает запуски во время активной загрузки файлов
    (многофайловые ZIP-распаковки, параллельное копирование).

    Вынесен в отдельный класс (а не вложен в обработчик watchdog), чтобы
    логику можно было тестировать без установленного watchdog.
    """

    DEBOUNCE_MIN = 1.0        # минимальный debounce, с
    DEBOUNCE_MAX = 10.0       # максимальный debounce, с
    DEBOUNCE_PER_FILE = 0.2   # дополнительные с на каждый pending файл

    def __init__(
        self,
        on_flush: Callable[[int], None],
        is_stopped: Optional[Callable[[], bool]] = None,
        ignore_hidden: bool = True,
    ) -> None:
        self._on_flush = on_flush
        self._is_stopped = is_stopped or (lambda: False)
        self._ignore_hidden = ignore_hidden
        self._pending: set[str] = set()
        self._timer: Optional[threading.Timer] = None
        self._tlock = threading.Lock()
        self._last_event_ts: float = 0.0
        self._cancelled = False

    @property
    def pending_count(self) -> int:
        with self._tlock:
            return len(self._pending)

    def schedule(self, path_str: str) -> None:
        """Регистрирует путь и (пере)запланирует отложенный flush."""
        if self._cancelled or self._is_stopped() or not path_str:
            return
        if _is_incomplete_path(path_str):
            return
        # Скрытые файлы и служебные файлы органайзера всё равно не сортируются
        # (см. _should_skip) — не будим мониторинг впустую.
        name = Path(path_str).name
        if self._ignore_hidden and name.startswith("."):
            return
        # Пробный файл проверки регистра ФС не должен будить сортировку,
        # даже если пользователь отключил игнорирование скрытых файлов.
        if name.startswith(CASE_PROBE_PREFIX):
            return
        now = time.monotonic()
        with self._tlock:
            self._pending.add(path_str)
            if self._timer:
                self._timer.cancel()
            # Адаптивная задержка: растёт с количеством pending-файлов
            # и уменьшается, если события редкие
            elapsed = now - self._last_event_ts if self._last_event_ts else 999.0
            self._last_event_ts = now
            base = self.DEBOUNCE_MIN if elapsed > 2.0 else self.DEBOUNCE_MIN * 1.5
            debounce = min(
                base + len(self._pending) * self.DEBOUNCE_PER_FILE,
                self.DEBOUNCE_MAX,
            )
            self._timer = threading.Timer(debounce, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        if self._cancelled or self._is_stopped():
            return
        with self._tlock:
            count = len(self._pending)
            self._pending.clear()
            self._timer = None
        if count:
            self._on_flush(count)

    def cancel(self) -> None:
        """Отменяет запланированный таймер и запрещает новые задачи."""
        with self._tlock:
            self._cancelled = True
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._pending.clear()


def start_monitoring(
    source_dir: str,
    interval: int = 10,
    callback: Optional[Callable[[str], None]] = None,
    config: Optional[dict[str, Any]] = None,
) -> threading.Thread:
    """
    Запускает фоновый мониторинг директории.

    Использует watchdog (если установлен) для мгновенной реакции на события ФС.
    При отсутствии watchdog — polling с указанным интервалом.

    Реагирует на создание, перемещение в папку и дозапись существующих файлов.
    """
    global _monitoring_stop_event, _monitoring_thread

    # Останавливаем предыдущий мониторинг, если он ещё работает
    if _monitoring_thread is not None and _monitoring_thread.is_alive():
        stop_monitoring()

    _monitoring_stop_event = threading.Event()
    source_path = Path(source_dir).resolve()

    # H6 fix: если config не передан — берём из config.json, а не считаем use_watchdog=True
    if config is None:
        config = load_config()

    def _log(msg: str) -> None:
        if callback:
            callback(msg)
        else:
            logger.info(msg)

    def _run_sort(new_count: int) -> None:
        if _monitoring_stop_event.is_set():
            # Мониторинг остановлен, пока отложенная сортировка ждала своего часа
            return
        _log(f"🔍 Обнаружены изменения (путей: {new_count}). Запуск сортировки...")
        try:
            # Передаём config, чтобы не перечитывать с диска каждый раз
            stats = organize_files(
                str(source_path),
                verbose=False,
                log_callback=callback,
                config=config,
            )
            if stats.get("skipped_concurrent"):
                _log("⏭  Сортировка уже выполняется — пропуск цикла.")
            else:
                _log(
                    f"✅ Перемещено: {stats['moved']}, пропущено: {stats['skipped']}, "
                    f"ошибок: {stats['errors']}"
                )
        except Exception as exc:
            _log(f"❌ Ошибка при сортировке: {exc}")

    # ── Попытка использовать watchdog ──────────────────────────────────────────
    use_watchdog = config.get("settings", {}).get("use_watchdog", True)

    if use_watchdog:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            class _WatchdogHandler(FileSystemEventHandler):
                """
                Обработчик событий watchdog с адаптивным debounce.

                H6 fix: обрабатываются не только on_created, но и on_moved
                (переименование/перемещение готового файла в папку, например
                file.crdownload → file.zip) и on_modified (дозапись в существующий
                файл). Логика debounce вынесена в _DebounceScheduler.
                """

                def __init__(self, scheduler: "_DebounceScheduler") -> None:
                    super().__init__()
                    self._scheduler = scheduler

                def on_created(self, event: Any) -> None:
                    if event.is_directory:
                        return
                    self._scheduler.schedule(_path_from_event(event))

                def on_moved(self, event: Any) -> None:
                    # event.dest_path — итоговое имя (например, file.zip после
                    # переименования из file.crdownload), его и обрабатываем.
                    if event.is_directory:
                        return
                    self._scheduler.schedule(_path_from_event(event))

                def on_modified(self, event: Any) -> None:
                    # Дозапись в существующий файл (например, докачка .zip).
                    if event.is_directory:
                        return
                    self._scheduler.schedule(_path_from_event(event))

                def cancel_timer(self) -> None:
                    """Отменяет запланированный таймер и запрещает новые события."""
                    self._scheduler.cancel()

            scheduler = _DebounceScheduler(
                on_flush=_run_sort,
                is_stopped=lambda: _monitoring_stop_event.is_set(),
                ignore_hidden=bool(config.get("settings", {}).get("ignore_hidden", True)),
            )
            handler = _WatchdogHandler(scheduler)
            observer = Observer()
            observer.schedule(handler, str(source_path), recursive=True)
            observer.start()
            with _active_handlers_lock:
                _active_handlers.append({"handler": handler, "observer": observer})
            _log(f"🚀 Мониторинг запущен (watchdog) — {_mask_dir_for_log(source_path)}")

            def _watchdog_loop() -> None:
                try:
                    while not _monitoring_stop_event.is_set():
                        _monitoring_stop_event.wait(timeout=1)
                finally:
                    # H6 fix: сначала отменяем отложенный таймер, чтобы _flush
                    # не запустил сортировку после остановки мониторинга
                    handler.cancel_timer()
                    observer.stop()
                    observer.join()
                    with _active_handlers_lock, contextlib.suppress(ValueError):
                        _active_handlers.remove(
                            {"handler": handler, "observer": observer}
                        )
                    _log("⏹  Мониторинг остановлен.")

            t = threading.Thread(target=_watchdog_loop, daemon=True)
            t.start()
            _monitoring_thread = t
            return t

        except ImportError:
            _log("ℹ  watchdog не установлен — используется polling.")

    # ── Fallback: polling с адаптивным интервалом ─────────────────────────────
    def _snapshot() -> dict[str, float]:
        """
        os.walk быстрее rglob: не создаёт Path-объекты для каждого файла.

        Возвращает путь → (mtime, size). H6 fix: size позволяет замечать
        дозапись в существующий файл (ранее сравнивался только mtime/имя).
        """
        if not source_path.exists():
            return {}
        snap: dict[str, float] = {}
        for dirpath, _, filenames in os.walk(str(source_path)):
            for name in filenames:
                if name.startswith("."):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                    snap[full] = st.st_mtime + st.st_size / 1e12
                except OSError:
                    continue
        return snap

    initial = _snapshot()

    def _polling_loop() -> None:
        """
        Адаптивный интервал:
        - После обнаружения новых/изменённых файлов сокращаем паузу (активная фаза)
        - При длительном отсутствии новых файлов постепенно увеличиваем (idle)
        Это снижает нагрузку в покое и обеспечивает быструю реакцию при активности.
        """
        nonlocal initial
        current_interval = float(max(interval, 1))
        idle_cycles = 0
        _log(
            f"🚀 Мониторинг запущен (polling, интервал ~{interval} с) — "
            f"{_mask_dir_for_log(source_path)}"
        )

        while not _monitoring_stop_event.wait(timeout=current_interval):
            try:
                current = _snapshot()
                # Новые пути ИЛИ изменившиеся файлы (дозапись)
                changed = {k for k, v in current.items() if initial.get(k) != v}
                if changed:
                    idle_cycles = 0
                    # Ускоряемся: после активности проверяем чаще
                    current_interval = max(float(interval) / 2.0, 2.0)
                    # Проверяем стабильность: если файлы ещё пишутся — ждём
                    # следующий цикл и не запускаем сортировку
                    unstable = [p for p in changed if not _file_is_stable(p)]
                    if unstable:
                        _log(
                            f"⏳ Файлы ещё изменяются ({len(unstable)}) — "
                            f"сортировка отложена до следующего цикла."
                        )
                    else:
                        _run_sort(len(changed))
                    initial = _snapshot()
                else:
                    idle_cycles += 1
                    # Замедляемся: каждые 5 тихих циклов добавляем один базовый интервал,
                    # но не более чем в 3× от базового
                    current_interval = min(
                        float(interval) * (1.0 + idle_cycles // 5),
                        float(interval) * 3.0,
                    )
                    initial = current
            except Exception as exc:
                _log(f"⚠  Ошибка мониторинга: {exc}")

        _log("⏹  Мониторинг остановлен.")

    t = threading.Thread(target=_polling_loop, daemon=True)
    t.start()
    _monitoring_thread = t
    return t


def stop_monitoring(timeout: float = 5.0) -> None:
    """
    Останавливает фоновый мониторинг и ДОЖИДАЕТСЯ завершения потока.

    H6 fix: ранее только выставлялся Event — поток мог продолжать работать
    и запускать отложенные сортировки уже после «остановки». Теперь:
    - отменяются запланированные threading.Timer всех watchdog-обработчиков;
    - поток джойнится с таймаутом;
    - глобальные ссылки сбрасываются.
    """
    global _monitoring_thread
    _monitoring_stop_event.set()

    with _active_handlers_lock:
        for item in list(_active_handlers):
            with contextlib.suppress(Exception):
                item["handler"].cancel_timer()

    thread = _monitoring_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    _monitoring_thread = None
