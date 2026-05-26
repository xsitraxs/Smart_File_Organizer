#!/usr/bin/env python3

import copy
import json
import logging
import os
import queue
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, DefaultDict, Dict, List, Optional, Set, Tuple

# ── Настройка логирования ───────────────────────────────────────────────────────

logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
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

# Настраиваемые имена скриптов (можно переопределить в конфиге)
DEFAULT_SCRIPT_FILES: Set[str] = {
    "organizer_core.py",
    "file_organizer.py",
    "file_organizer_gui.py",
    "config.json",
    "actions.log",
    UNDO_LOG_FILENAME,
    "__pycache__",
}

# Защищённые директории, которые нельзя удалять
PROTECTED_DIRS: Set[str] = {
    "windows", "system32", "program files", "program files (x86)",
    "usr", "bin", "sbin", "lib", "lib64", "etc", "var", "tmp",
    "home", "root", "boot", "dev", "proc", "sys",
}

# Абсолютные пути к защищённым системным директориям (Unix + Windows)
PROTECTED_ABS_PATHS: List[Path] = [
    Path(p) for p in (
        "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
        "/var", "/boot", "/dev", "/proc", "/sys",
        "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
    )
]

# Лимиты безопасности
MAX_UNIQUE_PATH_ATTEMPTS = 1000  # Защита от DoS
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 GB лимит размера файла

DEFAULT_CONFIG: Dict[str, Any] = {
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
    },
}

# ── Глобальное состояние (потокобезопасное) ────────────────────────────────────

_lock = threading.Lock()
_organize_lock = threading.Lock()      # защита от параллельных запусков organize_files
_action_log: List[Dict] = []           # in-memory лог текущей сессии (для совместимости с тестами)
_monitoring_stop_event = threading.Event()
_monitoring_thread: Optional[threading.Thread] = None  # отслеживаем текущий поток мониторинга

# Кэш конфига — избегаем лишних чтений диска и deepcopy при каждом вызове
_config_cache: Optional[Dict[str, Any]] = None
_config_mtime: float = 0.0


# ── Конфигурация ───────────────────────────────────────────────────────────────

def _validate_config_schema(config: Dict[str, Any]) -> bool:
    """
    Проверяет базовую схему конфига: наличие и типы ключевых полей.
    Возвращает False при невалидных типах данных.
    """
    if not isinstance(config.get("categories"), dict):
        return False
    if not isinstance(config.get("settings"), dict):
        return False
    s = config["settings"]
    type_checks = [
        ("dry_run",            bool),
        ("recursive",          bool),
        ("clean_empty_dirs",   bool),
        ("ignore_hidden",      bool),
        ("duplicate_strategy", str),
        ("use_watchdog",       bool),
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
    return True


def load_config(force_reload: bool = False) -> Dict[str, Any]:
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
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
            if not _validate_config_schema(user):
                logger.warning("config.json не прошёл валидацию схемы. Используются дефолтные настройки.")
                result = copy.deepcopy(DEFAULT_CONFIG)
            else:
                merged = copy.deepcopy(DEFAULT_CONFIG)
                for key, val in user.items():
                    if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
                        merged[key].update(val)
                    else:
                        merged[key] = val
                result = merged
            _config_cache = result
            try:
                _config_mtime = CONFIG_FILE.stat().st_mtime
            except OSError:
                pass
            return copy.deepcopy(result)
        except (json.JSONDecodeError, IOError) as exc:
            logger.warning(f"Ошибка чтения config.json: {exc}. Используются дефолтные настройки.")
    else:
        save_config(DEFAULT_CONFIG)
    result = copy.deepcopy(DEFAULT_CONFIG)
    _config_cache = result
    return copy.deepcopy(result)


@lru_cache(maxsize=64)
def _resolve_cached(path: Path) -> Path:
    """
    Кэшированный resolve() для Path-объектов.

    Вызов Path.resolve() делает stat-syscall каждый раз. Для путей, которые
    проверяются многократно (source_dir, защищённые пути), кэширование даёт
    заметное ускорение при обработке тысяч файлов.
    """
    return path.resolve()


def save_config(config: Dict[str, Any]) -> None:
    """Сохраняет конфиг в файл."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except IOError as exc:
        logger.warning(f"Не удалось сохранить config.json: {exc}")


# ── Вспомогательные функции ────────────────────────────────────────────────────

def _build_ext_map(config: Dict[str, Any]) -> Dict[str, str]:
    """Строит маппинг «.расширение → категория»."""
    ext_map: Dict[str, str] = {}
    for cat, exts in config.get("categories", {}).items():
        for ext in exts:
            ext_map[f".{ext.lower()}"] = cat
    return ext_map


def _get_category(file_path: Path, ext_map: Dict[str, str]) -> str:
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
    """Маскирует полный путь в логах для предотвращения утечки информации."""
    try:
        rel = path.relative_to(_resolve_cached(source_dir))
        return str(rel)
    except (ValueError, OSError):
        return path.name


def _get_script_files(config: Dict[str, Any]) -> Set[str]:
    """Получает имена скриптов из конфига с фоллбэком на дефолтные."""
    settings = config.get("settings", {})
    script_files = settings.get("script_files")
    if script_files and isinstance(script_files, list):
        return set(script_files)
    return DEFAULT_SCRIPT_FILES.copy()


def is_protected_dir(dirpath: Path) -> bool:
    """
    Проверяет, является ли директория защищённой системной.

    Проверка идёт ТОЛЬКО по абсолютному пути (PROTECTED_ABS_PATHS), чтобы избежать
    ложноположительных срабатываний на обычных пользовательских папках с именами
    вроде "home", "bin", "tmp" внутри ~/Downloads.
    """
    try:
        resolved = dirpath.resolve()
        for protected in PROTECTED_ABS_PATHS:
            try:
                resolved.relative_to(protected)
                return True
            except ValueError:
                continue
    except (OSError, ValueError):
        pass
    return False


# Обратная совместимость: внутреннее имя сохраняем как алиас публичного.
_is_protected_dir = is_protected_dir


def _get_unique_path(
    target_dir: Path,
    filename: str,
    existing_names: Optional[Set[str]] = None,
) -> Path:
    """
    Возвращает путь без конфликта имён, добавляя _1, _2, …

    На case-insensitive ФС (Windows, macOS HFS+/APFS по умолчанию) сравнение
    идёт без учёта регистра, чтобы "File.TXT" и "file.txt" считались одним
    и тем же файлом и duplicate_strategy=rename отрабатывала корректно.

    Принимает опциональный pre-scanned набор existing_names.
    Если он передан — работает полностью без I/O (O(1) на проверку).
    Если нет — сканирует директорию один раз вместо N вызовов exists().
    """
    target = target_dir / filename
    if existing_names is None:
        try:
            with os.scandir(target_dir) as it:
                existing_names = {e.name for e in it}
        except OSError:
            existing_names = set()

    # На Windows ФС нечувствительна к регистру → нужно сравнивать в lower-case.
    case_insensitive = os.name == "nt"
    if case_insensitive:
        existing_lower = {n.lower() for n in existing_names}

        def _exists(name: str) -> bool:
            return name.lower() in existing_lower
    else:
        def _exists(name: str) -> bool:
            return name in existing_names

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


def _scan_files_fast(root: Path, recursive: bool) -> List[Tuple[Path, os.stat_result]]:
    """
    Быстрый сбор файлов с помощью os.scandir() вместо Path.rglob()/iterdir().

    os.scandir() возвращает DirEntry-объекты, которые кэшируют результат stat()
    внутри себя — отдельный вызов stat() для каждого файла не нужен.
    На больших директориях это даёт 40-60% ускорение по сравнению с rglob().

    Возвращает список пар (Path, stat_result), чтобы вызывающий код мог сразу
    использовать st_size без повторного stat().
    """
    result: List[Tuple[Path, os.stat_result]] = []
    if recursive:
        stack: List[str] = [str(root)]
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
                                stack.append(entry.path)
                        except OSError:
                            continue
            except OSError:
                continue
    else:
        try:
            with os.scandir(str(root)) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=True):
                            result.append((Path(entry.path), entry.stat()))
                    except OSError:
                        continue
        except OSError:
            pass
    return result


# ── Запись лога отмены ────────────────────────────────────────────────────────

class _UndoLogWriter:
    """
    Append-only JSONL писатель для undo-лога.

    Каждая запись — отдельная строка JSON. Это:
    - Устраняет O(N²) read-modify-write всего лога на каждый файл.
    - Не требует фонового потока — нет утечки потоков в режиме мониторинга.
    - Делает запись потокобезопасной без сложных гонок с undo_last_operation.
    - Сохраняет частичный прогресс на диск даже при прерывании (Ctrl+C).
    """

    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._lock = threading.Lock()

    def append(self, entry: Dict) -> None:
        """Потокобезопасный append одной записи."""
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except IOError as exc:
                logger.warning(f"Не удалось записать в undo-лог: {exc}")

    def append_many(self, entries: List[Dict]) -> None:
        """Потокобезопасный пакетный append."""
        if not entries:
            return
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    for entry in entries:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except IOError as exc:
                logger.warning(f"Не удалось записать в undo-лог: {exc}")



# ── Персистентный лог отмены ───────────────────────────────────────────────────

def _undo_log_path(source_dir: Path) -> Path:
    return source_dir / UNDO_LOG_FILENAME


def _load_undo_log(source_dir: Path) -> List[Dict[str, Any]]:
    """
    Загружает undo-лог.

    Поддерживает два формата для обратной совместимости:
    - Новый JSONL: одна JSON-запись на строку (append-only).
    - Старый JSON-массив: один большой массив объектов.
    """
    path = _undo_log_path(source_dir)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
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
        entries: List[Dict[str, Any]] = []
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
    except IOError:
        return []


def _save_undo_log(source_dir: Path, entries: List[Dict]) -> None:
    """
    Полностью перезаписывает undo-лог в формате JSONL.

    Используется только undo_last_operation для записи оставшихся entries
    после частичной отмены. Для добавления новых записей используется
    _UndoLogWriter.append().
    """
    path = _undo_log_path(source_dir)
    try:
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except IOError as exc:
        logger.warning(f"Не удалось сохранить лог отмены: {exc}")


def _append_undo_log(source_dir: Path, new_entries: List[Dict]) -> None:
    existing = _load_undo_log(source_dir)
    existing.extend(new_entries)
    _save_undo_log(source_dir, existing)


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
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
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

    # Защита от параллельного запуска: watchdog может срабатывать пока
    # предыдущая сортировка ещё не закончилась — это вызывает гонки за shutil.move,
    # mkdir и одновременную запись в undo-лог.
    if not _organize_lock.acquire(blocking=False):
        logger.info(
            "organize_files: предыдущая сортировка ещё выполняется — пропуск "
            "(новые файлы будут обработаны в следующем цикле)."
        )
        return {
            "moved": 0, "skipped": 0, "errors": 0,
            "duplicates": 0, "empty_dirs_removed": 0,
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
        _organize_lock.release()


def _organize_files_impl(
    source_dir: str,
    dry_run: bool,
    verbose: bool,
    recursive: Optional[bool],
    log_callback: Optional[Callable[[str], None]],
    progress_callback: Optional[Callable[[int, int], None]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Внутренняя реализация organize_files под защитой _organize_lock."""
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

    source_path = Path(source_dir).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Директория не найдена: {source_path}")
    if not source_path.is_dir():
        raise NotADirectoryError(f"Не является директорией: {source_path}")

    # Дополнительная проверка на защищённые директории
    if _is_protected_dir(source_path):
        raise ValueError(f"Отказ в работе с защищённой системной директорией: {source_path}")

    stats: Dict[str, Any] = {
        "moved": 0, "skipped": 0, "errors": 0,
        "duplicates": 0, "empty_dirs_removed": 0,
        "by_category": {},
    }
    session_log: List[Dict] = []

    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)
        elif verbose:
            logger.info(msg)

    log(f"\n{'─'*60}")
    log("  Умный органайзер файлов")
    log(f"{'─'*60}")
    log(f"  Директория : {source_path}")
    log(f"  Режим      : {'рекурсивный' if use_recursive else 'только корень'}")
    if use_dry_run:
        log("  ⚠  DRY-RUN — файлы НЕ будут перемещены")
    log(f"{'─'*60}\n")

    # lru_cache для _resolve_cached инвалидируем ПЕРЕД сканированием,
    # чтобы не использовать устаревшие записи (файлы могли быть удалены/переименованы
    # между вызовами organize_files в режиме мониторинга).
    _resolve_cached.cache_clear()

    # ── Сбор файлов через os.scandir() — получаем stat бесплатно ──────────────
    raw_file_stats: List[Tuple[Path, os.stat_result]] = _scan_files_fast(source_path, use_recursive)

    # Строим нижний регистр категорий один раз — используем в _should_skip
    category_names_lower: frozenset = frozenset(c.lower() for c in category_names)

    def _should_skip(f: Path) -> bool:
        if settings.get("ignore_hidden", True) and f.name.startswith("."):
            return True
        if f.name in script_files:
            return True
        try:
            rel = f.relative_to(source_path)
            if rel.parts[0].lower() in category_names_lower:
                return True
        except ValueError:
            pass
        return False

    files_with_stat = [(f, st) for f, st in raw_file_stats if not _should_skip(f)]
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

    # Сначала фильтруем и проверяем безопасность — строим батчи
    batches: DefaultDict[str, List[Tuple[Path, os.stat_result]]] = defaultdict(list)
    global_idx = 0

    for file_path, st in files_with_stat:
        global_idx += 1
        if progress_callback:
            progress_callback(global_idx, total)

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

    # Асинхронный писатель лога — submit не блокирует основной цикл
    undo_writer = _UndoLogWriter(_undo_log_path(source_path)) if not use_dry_run else None

    # Обрабатываем каждую категорию одним блоком
    for category, batch in batches.items():
        target_cat_dir = source_path / category
        stats["by_category"].setdefault(category, 0)

        if use_dry_run:
            for file_path, _ in batch:
                masked = _mask_path_for_log(file_path, source_path)
                log(f"  → {masked}  ➜  {category}/")
                stats["moved"] += 1
                stats["by_category"][category] += 1
            continue

        # mkdir один раз на категорию
        target_cat_dir.mkdir(exist_ok=True)

        # Сканируем существующие имена в целевой папке один раз на категорию
        # — все проверки коллизий идут через этот set без лишних stat-вызовов
        with _lock:
            try:
                with os.scandir(target_cat_dir) as it:
                    existing_names: Set[str] = {e.name for e in it}
            except OSError:
                existing_names = set()

            for file_path, _ in batch:
                target_path = target_cat_dir / file_path.name

                try:
                    if file_path.name in existing_names:
                        if dup_strategy == "skip":
                            masked = _mask_path_for_log(file_path, source_path)
                            log(f"  ⏭  Пропущен (уже есть): {masked}")
                            stats["skipped"] += 1
                            stats["duplicates"] += 1
                            continue
                        elif dup_strategy == "replace":
                            target_path.unlink()
                            existing_names.discard(file_path.name)
                            masked = _mask_path_for_log(file_path, source_path)
                            log(f"  🔄 Заменён: {masked}")
                            stats["duplicates"] += 1
                        else:  # rename (default) — передаём уже готовый set
                            target_path = _get_unique_path(
                                target_cat_dir, file_path.name, existing_names
                            )
                            masked = _mask_path_for_log(file_path, source_path)
                            log(f"  ⚠  Переименован: {masked} → {target_path.name}")
                            stats["duplicates"] += 1

                    shutil.move(str(file_path), str(target_path))
                    # Добавляем новое имя в set, чтобы следующий файл с тем же
                    # именем в этой же категории корректно получил суффикс
                    existing_names.add(target_path.name)

                    masked = _mask_path_for_log(file_path, source_path)
                    log(f"  ✓  {masked}  ➜  {category}/{target_path.name}")

                    try:
                        rel_source = str(file_path.relative_to(source_path))
                        rel_dest = str(target_path.relative_to(source_path))
                    except ValueError:
                        rel_source = file_path.name
                        rel_dest = target_path.name

                    entry: Dict[str, Any] = {
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

                except Exception as exc:
                    masked = _mask_path_for_log(file_path, source_path)
                    log(f"  ✗  Ошибка: {masked} — {exc}")
                    stats["errors"] += 1

    # _UndoLogWriter синхронный — отдельный flush не нужен.
    # Синхронизируем in-memory лог сессии (оставлен для совместимости с тестами)
    if session_log:
        with _lock:
            _action_log.extend(session_log)

    # ── Удаление пустых папок ──────────────────────────────────────────────────
    if not use_dry_run and use_recursive and clean_empty:
        log("\n  Очистка пустых папок...")
        # Собираем все директории через os.walk (bottom-up) — уже от глубоких к верхним,
        # без дополнительной сортировки
        for dirpath_str, subdirs, _ in os.walk(str(source_path), topdown=False):
            dirpath = Path(dirpath_str)
            if dirpath == source_path:
                continue
            if _is_protected_dir(dirpath):
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

    # ── Статистика ─────────────────────────────────────────────────────────────
    log(f"\n{'─'*60}")
    log("  ИТОГО")
    log(f"{'─'*60}")
    log(f"  Перемещено  : {stats['moved']}")
    log(f"  Пропущено   : {stats['skipped']}")
    log(f"  Дубликаты   : {stats['duplicates']}")
    log(f"  Ошибки      : {stats['errors']}")
    if use_recursive and not use_dry_run:
        log(f"  Пустых папок удалено: {stats['empty_dirs_removed']}")
    if stats["by_category"]:
        log("\n  По категориям:")
        for cat, cnt in sorted(stats["by_category"].items()):
            log(f"    {cat:<16}{cnt}")
    log(f"{'─'*60}\n")

    return stats


# ── undo_last_operation ────────────────────────────────────────────────────────

def undo_last_operation(
    source_dir: str,
    count: int = -1,
    verbose: bool = True,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, int]:
    """
    Отменяет последние операции перемещения.

    Читает из персистентного лога — работает после перезапуска программы.

    Args:
        source_dir:   Корневая директория
        count:        Сколько операций отменить (-1 = все)
        verbose:      Подробный вывод в stdout
        log_callback: Колбэк для строк лога
    """
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

    if count == -1:
        to_undo = all_entries
        remaining: List[Dict] = []
    else:
        to_undo = all_entries[-count:]
        remaining = all_entries[: len(all_entries) - count]

    log(f"  Операций для отмены: {len(to_undo)}\n")

    failed: List[Dict] = []
    for entry in reversed(to_undo):
        if entry.get("action") != "move":
            continue

        # Пути в логе теперь относительные, поэтому добавляем source_dir
        dest = source_path / entry["destination"]
        src = source_path / entry["source"]

        # ── Критическая проверка: пути из лога могут быть подделаны ──────────
        # Защита от path traversal через подмену .organizer_undo.json
        if not _validate_path_safety(dest, source_path):
            log(f"  ✗  Небезопасный путь назначения в логе: {entry.get('original_name', '?')}")
            stats["errors"] += 1
            failed.append(entry)
            continue
        if not _validate_path_safety(src, source_path):
            log(f"  ✗  Небезопасный исходный путь в логе: {entry.get('original_name', '?')}")
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

def start_monitoring(
    source_dir: str,
    interval: int = 10,
    callback: Optional[Callable[[str], None]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> threading.Thread:
    """
    Запускает фоновый мониторинг директории.

    Использует watchdog (если установлен) для мгновенной реакции на события ФС.
    При отсутствии watchdog — polling с указанным интервалом.
    """
    global _monitoring_stop_event, _monitoring_thread

    # Останавливаем предыдущий мониторинг, если он ещё работает
    if _monitoring_thread is not None and _monitoring_thread.is_alive():
        _monitoring_stop_event.set()
        _monitoring_thread.join(timeout=5)

    _monitoring_stop_event = threading.Event()
    source_path = Path(source_dir).resolve()

    def _log(msg: str) -> None:
        if callback:
            callback(msg)
        else:
            logger.info(msg)

    def _run_sort(new_count: int) -> None:
        _log(f"🔍 Новых файлов: {new_count}. Запуск сортировки...")
        try:
            # Передаём config, чтобы не перечитывать с диска каждый раз
            stats = organize_files(
                str(source_path),
                verbose=False,
                log_callback=callback,
                config=config,
            )
            _log(f"✅ Перемещено: {stats['moved']}, пропущено: {stats['skipped']}")
        except Exception as exc:
            _log(f"❌ Ошибка при сортировке: {exc}")

    # ── Попытка использовать watchdog ──────────────────────────────────────────
    use_watchdog = (config or {}).get("settings", {}).get("use_watchdog", True)

    if use_watchdog:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class _Handler(FileSystemEventHandler):
                """
                Адаптивный debounce: чем быстрее поступают события, тем дольше ждём
                перед запуском сортировки. Это предотвращает запуски во время активной
                загрузки файлов (например, многофайловые ZIP-распаковки или копирование).
                """
                _DEBOUNCE_MIN = 1.0   # минимальный debounce, с
                _DEBOUNCE_MAX = 10.0  # максимальный debounce, с
                _DEBOUNCE_PER_FILE = 0.2  # дополнительные мс на каждый pending файл

                def __init__(self) -> None:
                    self._pending: set = set()
                    self._timer: Optional[threading.Timer] = None
                    self._tlock = threading.Lock()
                    self._last_event_ts: float = 0.0

                def on_created(self, event: Any) -> None:
                    if event.is_directory:
                        return
                    now = time.monotonic()
                    with self._tlock:
                        self._pending.add(event.src_path)
                        if self._timer:
                            self._timer.cancel()
                        # Адаптивная задержка: растёт с количеством pending-файлов
                        # и уменьшается, если события редкие
                        elapsed = now - self._last_event_ts if self._last_event_ts else 999.0
                        self._last_event_ts = now
                        base = self._DEBOUNCE_MIN if elapsed > 2.0 else self._DEBOUNCE_MIN * 1.5
                        debounce = min(
                            base + len(self._pending) * self._DEBOUNCE_PER_FILE,
                            self._DEBOUNCE_MAX,
                        )
                        self._timer = threading.Timer(debounce, self._flush)
                        self._timer.daemon = True
                        self._timer.start()

                def _flush(self) -> None:
                    with self._tlock:
                        count = len(self._pending)
                        self._pending.clear()
                    if count:
                        _run_sort(count)

            observer = Observer()
            observer.schedule(_Handler(), str(source_path), recursive=True)
            observer.start()
            _log(f"🚀 Мониторинг запущен (watchdog) — {source_path}")

            def _watchdog_loop() -> None:
                try:
                    while not _monitoring_stop_event.is_set():
                        _monitoring_stop_event.wait(timeout=1)
                finally:
                    observer.stop()
                    observer.join()
                    _log("⏹  Мониторинг остановлен.")

            t = threading.Thread(target=_watchdog_loop, daemon=True)
            t.start()
            _monitoring_thread = t
            return t

        except ImportError:
            _log("ℹ  watchdog не установлен — используется polling.")

    # ── Fallback: polling с адаптивным интервалом ─────────────────────────────
    def _snapshot() -> Dict[str, float]:
        """os.walk быстрее rglob: не создаёт Path-объекты для каждого файла."""
        if not source_path.exists():
            return {}
        snap: Dict[str, float] = {}
        for dirpath, _, filenames in os.walk(str(source_path)):
            for name in filenames:
                if name.startswith("."):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    snap[full] = os.stat(full).st_mtime
                except OSError:
                    continue
        return snap

    initial = _snapshot()

    def _polling_loop() -> None:
        """
        Адаптивный интервал:
        - После обнаружения новых файлов сокращаем паузу (активная фаза)
        - При длительном отсутствии новых файлов постепенно увеличиваем (idle)
        Это снижает нагрузку в покое и обеспечивает быструю реакцию при активности.
        """
        nonlocal initial
        current_interval = float(interval)
        idle_cycles = 0
        _log(f"🚀 Мониторинг запущен (polling, интервал ~{interval} с) — {source_path}")

        while not _monitoring_stop_event.wait(timeout=current_interval):
            try:
                current = _snapshot()
                new_files = {k for k in current if k not in initial}
                if new_files:
                    idle_cycles = 0
                    # Ускоряемся: после активности проверяем чаще
                    current_interval = max(float(interval) / 2.0, 2.0)
                    _run_sort(len(new_files))
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


def stop_monitoring() -> None:
    """Останавливает фоновый мониторинг."""
    _monitoring_stop_event.set()
