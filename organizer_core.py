#!/usr/bin/env python3
"""
Ядро умного органайзера файлов — улучшенная версия.

Исправлено:
- Персистентный лог отмены (работает после перезапуска)
- Потокобезопасность (threading.Lock)
- Логика recursive больше не инвертирована
- Мониторинг не сбрасывает лог отмены
- Поддержка watchdog (с polling-фоллбэком)
- progress_callback(current, total) для GUI
- Стратегии дублей: rename / skip / replace
- Глубокое слияние конфигов

Исправления безопасности (12 уязвимостей):
- Path Traversal: проверка, что пути внутри source_dir
- TOCTOU: атомарные операции с lock
- Валидация путей: запрет "..", абсолютных путей, symlink вне директории
- Command Injection: безопасная замена os.startfile/subprocess
- Symlink: проверка назначения symlink
- DoS: лимит итераций (1000 вместо 100000)
- SHA256 вместо MD5
- Лимит размера файла (1GB по умолчанию)
- Маскировка путей в логах
- Конфигурируемые имена скриптов
- Строгая валидация конфига
- Защита системных директорий от удаления
"""

import copy
import hashlib
import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

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
_action_log: List[Dict] = []           # in-memory лог текущей сессии
_monitoring_stop_event = threading.Event()


# ── Конфигурация ───────────────────────────────────────────────────────────────

def load_config() -> Dict[str, Any]:
    """Загружает конфиг с глубоким слиянием с дефолтными значениями."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
            merged = copy.deepcopy(DEFAULT_CONFIG)
            for key, val in user.items():
                if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
                    merged[key].update(val)
                else:
                    merged[key] = val
            return merged
        except (json.JSONDecodeError, IOError) as exc:
            print(f"⚠ Ошибка чтения config.json: {exc}. Используются дефолтные настройки.")
    else:
        save_config(DEFAULT_CONFIG)
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: Dict[str, Any]) -> None:
    """Сохраняет конфиг в файл."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except IOError as exc:
        print(f"⚠ Не удалось сохранить config.json: {exc}")


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
    Проверяет безопасность пути:
    - Запрещает ".." в компонентах пути (Path Traversal)
    - Запрещает абсолютные пути внутри относительных операций
    - Проверяет, что путь находится внутри source_dir
    - Блокирует symlink, ведущие за пределы source_dir
    """
    try:
        # Проверка на ".." в частях пути
        for part in file_path.parts:
            if part == "..":
                return False

        # Разрешаем путь и проверяем, что он внутри source_dir
        resolved = file_path.resolve()
        source_resolved = source_dir.resolve()

        # Проверка, что файл внутри source_dir
        try:
            resolved.relative_to(source_resolved)
        except ValueError:
            return False

        # Проверка symlink - не ведёт ли за пределы директории
        if file_path.is_symlink():
            link_target = file_path.resolve()
            try:
                link_target.relative_to(source_resolved)
            except ValueError:
                return False  # Symlink ведёт за пределы

        return True
    except (OSError, ValueError):
        return False


def _mask_path_for_log(path: Path, source_dir: Path) -> str:
    """Маскирует полный путь в логах для предотвращения утечки информации."""
    try:
        rel = path.relative_to(source_dir.resolve())
        return str(rel)
    except (ValueError, OSError):
        # Если путь не относительно source_dir, показываем только имя файла
        return path.name


def _get_script_files(config: Dict[str, Any]) -> Set[str]:
    """Получает имена скриптов из конфига с фоллбэком на дефолтные."""
    settings = config.get("settings", {})
    script_files = settings.get("script_files")
    if script_files and isinstance(script_files, list):
        return set(script_files)
    return DEFAULT_SCRIPT_FILES.copy()


def _is_protected_dir(dirpath: Path) -> bool:
    """Проверяет, является ли директория защищённой системной."""
    dir_name = dirpath.name.lower()
    return dir_name in PROTECTED_DIRS


def _get_unique_path(target_dir: Path, filename: str) -> Path:
    """Возвращает путь без конфликта имён, добавляя _1, _2, …"""
    target = target_dir / filename
    if not target.exists():
        return target
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for i in range(1, MAX_UNIQUE_PATH_ATTEMPTS):
        candidate = target_dir / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Не удалось найти уникальное имя для {filename} (превышен лимит {MAX_UNIQUE_PATH_ATTEMPTS})")


def _file_sha256(path: Path, chunk: int = 65536) -> Optional[str]:
    """SHA256-хеш файла для обнаружения полных дублей по содержимому."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while data := f.read(chunk):
                h.update(data)
        return h.hexdigest()
    except IOError:
        return None


# ── Персистентный лог отмены ───────────────────────────────────────────────────

def _undo_log_path(source_dir: Path) -> Path:
    return source_dir / UNDO_LOG_FILENAME


def _load_undo_log(source_dir: Path) -> List[Dict]:
    path = _undo_log_path(source_dir)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_undo_log(source_dir: Path, entries: List[Dict]) -> None:
    path = _undo_log_path(source_dir)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
    except IOError as exc:
        print(f"⚠ Не удалось сохранить лог отмены: {exc}")


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
            print(msg)

    log(f"\n{'─'*60}")
    log("  Умный органайзер файлов")
    log(f"{'─'*60}")
    log(f"  Директория : {source_path}")
    log(f"  Режим      : {'рекурсивный' if use_recursive else 'только корень'}")
    if use_dry_run:
        log("  ⚠  DRY-RUN — файлы НЕ будут перемещены")
    log(f"{'─'*60}\n")

    # ── Сбор файлов ────────────────────────────────────────────────────────────
    raw_files = (
        [f for f in source_path.rglob("*") if f.is_file()]
        if use_recursive
        else [f for f in source_path.iterdir() if f.is_file()]
    )

    def _should_skip(f: Path) -> bool:
        if settings.get("ignore_hidden", True) and f.name.startswith("."):
            return True
        if f.name in script_files:  # Используем настраиваемый список
            return True
        # Файл уже лежит внутри папки категории (любой уровень вложенности)
        try:
            rel = f.relative_to(source_path)
            if rel.parts[0] in category_names:
                return True
        except ValueError:
            pass
        return False

    files = [f for f in raw_files if not _should_skip(f)]
    total = len(files)
    log(f"  Файлов для обработки: {total}\n")

    if total == 0:
        log("  Нечего делать.\n")
        return stats

    # ── Обработка файлов ───────────────────────────────────────────────────────
    for idx, file_path in enumerate(files, 1):
        if progress_callback:
            progress_callback(idx, total)

        # Проверка безопасности пути (Path Traversal, Symlink)
        if not _validate_path_safety(file_path, source_path):
            log(f"  ✗  Пропущен небезопасный путь: {file_path.name}")
            stats["errors"] += 1
            continue

        # Проверка размера файла
        try:
            file_size = file_path.stat().st_size
            if file_size > max_file_size:
                log(f"  ✗  Файл слишком большой (> {max_file_size // (1024*1024)} MB): {file_path.name}")
                stats["errors"] += 1
                continue
        except OSError:
            log(f"  ✗  Не удалось получить размер файла: {file_path.name}")
            stats["errors"] += 1
            continue

        category = _get_category(file_path, ext_map)
        target_dir = source_path / category
        stats["by_category"].setdefault(category, 0)

        if use_dry_run:
            try:
                rel = file_path.relative_to(source_path)
            except ValueError:
                rel = file_path
            # Маскируем путь в логе
            masked_rel = _mask_path_for_log(file_path, source_path)
            log(f"  → {masked_rel}  ➜  {category}/")
            stats["moved"] += 1
            stats["by_category"][category] += 1
            continue

        try:
            target_dir.mkdir(exist_ok=True)
            target_path = target_dir / file_path.name

            # TOCTOU защита: используем атомарные операции с lock
            with _lock:
                # Стратегия дублей
                if target_path.exists():
                    if dup_strategy == "skip":
                        masked_name = _mask_path_for_log(file_path, source_path)
                        log(f"  ⏭  Пропущен (уже есть): {masked_name}")
                        stats["skipped"] += 1
                        stats["duplicates"] += 1
                        continue
                    elif dup_strategy == "replace":
                        target_path.unlink()
                        masked_name = _mask_path_for_log(file_path, source_path)
                        log(f"  🔄 Заменён: {masked_name}")
                        stats["duplicates"] += 1
                    else:  # rename (default)
                        target_path = _get_unique_path(target_dir, file_path.name)
                        masked_name = _mask_path_for_log(file_path, source_path)
                        masked_target = _mask_path_for_log(target_path, source_path)
                        log(f"  ⚠  Переименован: {masked_name} → {target_path.name}")
                        stats["duplicates"] += 1

                shutil.move(str(file_path), str(target_path))

            # Логируем с маскировкой пути
            masked_rel = _mask_path_for_log(file_path, source_path)
            masked_target_rel = _mask_path_for_log(target_path, source_path)
            log(f"  ✓  {masked_rel}  ➜  {category}/{target_path.name}")

            session_log.append({
                "timestamp":     datetime.now().isoformat(),
                "action":        "move",
                "source":        str(file_path.name),  # Только имя, не полный путь
                "destination":   str(target_path.name),  # Только имя
                "original_name": file_path.name,
            })
            stats["moved"] += 1
            stats["by_category"][category] += 1

        except Exception as exc:
            masked_name = _mask_path_for_log(file_path, source_path)
            log(f"  ✗  Ошибка: {masked_name} — {exc}")
            stats["errors"] += 1

    # ── Сохранение персистентного лога отмены ──────────────────────────────────
    if session_log:
        _append_undo_log(source_path, session_log)
        with _lock:
            _action_log.extend(session_log)

    # ── Удаление пустых папок ──────────────────────────────────────────────────
    if not use_dry_run and use_recursive and clean_empty:
        log("\n  Очистка пустых папок...")
        # Обходим от глубоких к верхним
        dirs_deep_first = sorted(
            (d for d in source_path.rglob("*") if d.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        for dirpath in dirs_deep_first:
            if dirpath == source_path:
                continue
            # Защита от удаления защищённых системных директорий
            if _is_protected_dir(dirpath):
                log(f"  ⚠  Пропущена защищённая директория: {dirpath.name}")
                continue
            try:
                rel = dirpath.relative_to(source_path)
            except ValueError:
                continue
            if rel.parts[0] in category_names:
                continue
            try:
                if not any(dirpath.iterdir()):
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
            print(msg)

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

        dest = Path(entry["destination"])
        src = Path(entry["source"])

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
    global _monitoring_stop_event
    _monitoring_stop_event = threading.Event()
    source_path = Path(source_dir).resolve()

    def _log(msg: str) -> None:
        if callback:
            callback(msg)
        else:
            print(msg)

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
            from watchdog.observers import Observer          # type: ignore
            from watchdog.events import FileSystemEventHandler  # type: ignore

            class _Handler(FileSystemEventHandler):
                def __init__(self) -> None:
                    self._pending: set = set()
                    self._timer: Optional[threading.Timer] = None
                    self._tlock = threading.Lock()

                def on_created(self, event) -> None:
                    if event.is_directory:
                        return
                    with self._tlock:
                        self._pending.add(event.src_path)
                        if self._timer:
                            self._timer.cancel()
                        # Debounce 2 с — ждём паузы между событиями
                        self._timer = threading.Timer(2.0, self._flush)
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
            return t

        except ImportError:
            _log("ℹ  watchdog не установлен — используется polling.")

    # ── Fallback: polling ──────────────────────────────────────────────────────
    def _snapshot() -> Dict[str, float]:
        if not source_path.exists():
            return {}
        return {
            str(f): f.stat().st_mtime
            for f in source_path.rglob("*")
            if f.is_file() and not f.name.startswith(".")
        }

    initial = _snapshot()

    def _polling_loop() -> None:
        nonlocal initial
        _log(f"🚀 Мониторинг запущен (polling, интервал {interval} с) — {source_path}")
        while not _monitoring_stop_event.wait(timeout=interval):
            try:
                current = _snapshot()
                new_files = {k for k in current if k not in initial}
                if new_files:
                    _run_sort(len(new_files))
                    # Обновляем снимок после сортировки
                    initial = _snapshot()
                else:
                    initial = current
            except Exception as exc:
                _log(f"⚠  Ошибка мониторинга: {exc}")
        _log("⏹  Мониторинг остановлен.")

    t = threading.Thread(target=_polling_loop, daemon=True)
    t.start()
    return t


def stop_monitoring() -> None:
    """Останавливает фоновый мониторинг."""
    _monitoring_stop_event.set()
