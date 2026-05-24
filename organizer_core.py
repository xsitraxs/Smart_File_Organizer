#!/usr/bin/env python3
"""
Ядро умного органайзера файлов
Модуль содержит основную логику сортировки файлов по типам.
Импортируется в CLI и GUI версии.
Поддерживает конфигурацию через config.json, отмену действий и мониторинг.
"""

import os
import shutil
import json
import time
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable, Dict, Any

# Путь к конфигурации
CONFIG_FILE = Path(__file__).parent / "config.json"
DEFAULT_CONFIG = {
    "categories": {
        "images": ["jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "ico", "tiff", "raw"],
        "documents": ["pdf", "doc", "docx", "txt", "rtf", "odt", "xls", "xlsx", "ppt", "pptx", "md"],
        "videos": ["mp4", "avi", "mkv", "mov", "wmv", "flv", "webm", "m4v"],
        "audio": ["mp3", "wav", "flac", "aac", "ogg", "wma", "m4a"],
        "archives": ["zip", "rar", "7z", "tar", "gz", "bz2", "xz"],
        "torrents": ["torrent", "magnet"],
        "code": ["py", "js", "ts", "html", "css", "java", "cpp", "c", "h", "go", "rs", "rb", "php", "sql", "json", "xml", "yaml", "yml"],
        "installers": ["exe", "msi", "deb", "rpm", "pkg", "dmg", "appimage", "apk"],
        "fonts": ["ttf", "otf", "woff", "woff2", "eot"],
        "other": []
    },
    "settings": {
        "dry_run": False,
        "recursive": True,
        "clean_empty_dirs": True,
        "ignore_hidden": True,
        "log_file": "actions.log"
    }
}

# Глобальные переменные
FILE_CATEGORIES = {}
EXTENSION_TO_CATEGORY = {}
ACTION_LOG = []  # Список действий для отмены
MONITORING_STOP_EVENT = threading.Event()


def load_config() -> Dict[str, Any]:
    """Загружает конфигурацию из файла или использует значения по умолчанию."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # Объединяем с дефолтными значениями на случай отсутствия ключей
                for key in DEFAULT_CONFIG:
                    if key not in config:
                        config[key] = DEFAULT_CONFIG[key]
                return config
        except (json.JSONDecodeError, IOError) as e:
            print(f"⚠ Ошибка чтения config.json: {e}. Используются настройки по умолчанию.")
            return DEFAULT_CONFIG.copy()
    else:
        # Создаем файл конфигурации по умолчанию, если его нет
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()


def save_config(config: Dict[str, Any]):
    """Сохраняет конфигурацию в файл."""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def init_categories(config: Optional[Dict[str, Any]] = None):
    """Инициализирует маппинг расширений на основе конфигурации."""
    global FILE_CATEGORIES, EXTENSION_TO_CATEGORY

    if config is None:
        config = load_config()

    categories_data = config.get("categories", DEFAULT_CONFIG["categories"])

    FILE_CATEGORIES = {}
    EXTENSION_TO_CATEGORY = {}

    for category, extensions in categories_data.items():
        FILE_CATEGORIES[category] = [f".{ext.lower()}" for ext in extensions]
        for ext in extensions:
            EXTENSION_TO_CATEGORY[f".{ext.lower()}"] = category


def get_category(file_path: Path) -> str:
    """Определяет категорию файла по его расширению."""
    ext = file_path.suffix.lower()
    return EXTENSION_TO_CATEGORY.get(ext, 'other')


def get_unique_filename(target_dir: Path, filename: str) -> str:
    """Генерирует уникальное имя файла, если файл уже существует."""
    target_path = target_dir / filename
    if not target_path.exists():
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1

    while True:
        new_name = f"{stem}_{counter}{suffix}"
        if not (target_dir / new_name).exists():
            return new_name
        counter += 1


def log_action(action_type: str, source: str, destination: str, original_name: str = ""):
    """Записывает действие в лог для возможности отмены."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "action": action_type,
        "source": source,
        "destination": destination,
        "original_name": original_name
    }
    ACTION_LOG.append(entry)
    return entry


def save_action_log(log_file: str):
    """Сохраняет журнал действий в файл."""
    if not ACTION_LOG:
        return

    with open(log_file, 'a', encoding='utf-8') as f:
        for entry in ACTION_LOG:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    ACTION_LOG.clear()


def undo_last_operation(source_dir: str, count: int = -1, verbose: bool = True, log_callback=None) -> dict:
    """
    Отменяет последние операции перемещения.

    Args:
        source_dir: Корневая директория (для контекста)
        count: Количество операций для отмены (-1 для всех доступных в текущей сессии)
        verbose: Выводить подробную информацию
        log_callback: Функция обратного вызова для логирования

    Returns:
        Статистика отмененных операций
    """
    stats = {'restored': 0, 'errors': 0}

    def log(message):
        if log_callback:
            log_callback(message)
        elif verbose:
            print(message)

    log(f"\n{'='*60}")
    log("ОТМЕНА ОПЕРАЦИЙ")
    log(f"{'='*60}")

    if not ACTION_LOG:
        log("Нет действий для отмены в текущей сессии.")
        log("Примечание: Отмена работает только для действий, выполненных в рамках текущего запуска программы.")
        return stats

    operations_to_undo = ACTION_LOG[:] if count == -1 else ACTION_LOG[-count:]

    # Обрабатываем в обратном порядке (последние сначала)
    for entry in reversed(operations_to_undo):
        if entry['action'] != 'move':
            continue

        dest_path = Path(entry['destination'])
        source_path = Path(entry['source'])

        if not dest_path.exists():
            log(f"⚠ Файл не найден (уже удален?): {dest_path}")
            stats['errors'] += 1
            continue

        try:
            # Убеждаемся, что исходная директория существует
            source_path.parent.mkdir(parents=True, exist_ok=True)

            shutil.move(str(dest_path), str(source_path))
            log(f"✓ Восстановлено: {entry['original_name']} ← {entry['destination']}")
            stats['restored'] += 1

            # Удаляем запись из лога после успешной отмены
            if entry in ACTION_LOG:
                ACTION_LOG.remove(entry)

        except Exception as e:
            log(f"✗ Ошибка при отмене: {e}")
            stats['errors'] += 1

    log(f"\nВосстановлено файлов: {stats['restored']}")
    log(f"Ошибок: {stats['errors']}")
    log(f"{'='*60}\n")

    return stats


def organize_files(source_dir: str, dry_run: bool = False, verbose: bool = True,
                   recursive: bool = True, log_callback: Optional[Callable] = None,
                   config: Optional[Dict[str, Any]] = None) -> dict:
    """
    Сортирует файлы в указанной директории по категориям.

    Args:
        source_dir: Путь к директории для организации
        dry_run: Если True, только показывает что будет сделано, без реальных действий
        verbose: Выводить подробную информацию
        recursive: Если True, обрабатывать файлы во всех подпапках
        log_callback: Функция обратного вызова для логирования (для GUI)
        config: Конфигурация (если None, загружается из config.json)

    Returns:
        Статистика выполненных операций
    """
    # Инициализация категорий
    init_categories(config)

    # Загрузка настроек
    settings = config.get("settings", DEFAULT_CONFIG["settings"]) if config else DEFAULT_CONFIG["settings"]
    if not dry_run: # Переопределяем dry_run из аргумента, если он явно передан
        dry_run = settings.get("dry_run", False)

    recursive = settings.get("recursive", True) if not recursive else recursive
    clean_empty = settings.get("clean_empty_dirs", True)
    log_file_name = settings.get("log_file", "actions.log")

    source_path = Path(source_dir).resolve()

    if not source_path.exists():
        raise FileNotFoundError(f"Директория не найдена: {source_path}")

    if not source_path.is_dir():
        raise NotADirectoryError(f"Это не директория: {source_path}")

    stats = {
        'moved': 0,
        'skipped': 0,
        'errors': 0,
        'by_category': {},
        'empty_dirs_removed': 0
    }

    # Очищаем лог действий перед новой операцией
    global ACTION_LOG
    ACTION_LOG = []

    def log(message):
        if log_callback:
            log_callback(message)
        elif verbose:
            print(message)

    log(f"\n{'='*60}")
    log("Умный органайзер файлов")
    log(f"{'='*60}")
    log(f"Целевая директория: {source_path}")
    if recursive:
        log("Режим: Рекурсивный (все подпапки)")
    else:
        log("Режим: Только корневая папка")
    if dry_run:
        log("РЕЖИМ ПРОСМОТРА (dry-run) - файлы не будут перемещены")
    log(f"{'='*60}\n")

    # Получаем список всех файлов
    if recursive:
        files = [f for f in source_path.rglob('*') if f.is_file()]
    else:
        files = [f for f in source_path.iterdir() if f.is_file()]

    if not files:
        log("Файлы не найдены в указанной директории.")
        return stats

    log(f"Найдено файлов: {len(files)}\n")

    # Имена файлов, которые нужно игнорировать
    ignore_names = {'organizer_core.py', 'file_organizer.py', 'file_organizer_gui.py', 'config.json', 'actions.log'}
    ignore_names.update(FILE_CATEGORIES.keys()) # Игнорируем папки категорий

    for file_path in files:
        # Пропускаем скрытые файлы
        if settings.get("ignore_hidden", True) and file_path.name.startswith('.'):
            stats['skipped'] += 1
            continue

        # Пропускаем служебные файлы
        if file_path.name in ignore_names or file_path.parent.name in FILE_CATEGORIES:
             # Дополнительная проверка: если файл лежит внутри папки категории, пропускаем
            if file_path.parent.name in FILE_CATEGORIES and file_path.parent.parent == source_path:
                 stats['skipped'] += 1
                 continue
            if file_path.name in ignore_names:
                 stats['skipped'] += 1
                 continue

        category = get_category(file_path)
        target_dir = source_path / category

        # Инициализируем счетчик категории
        if category not in stats['by_category']:
            stats['by_category'][category] = 0

        if dry_run:
            log(f"→ Будет перемещено: {file_path.name} → {category}/")
            stats['moved'] += 1
            stats['by_category'][category] += 1
        else:
            try:
                # Создаем папку категории если не существует
                target_dir.mkdir(exist_ok=True)

                # Обрабатываем дубликаты имен
                final_name = get_unique_filename(target_dir, file_path.name)
                target_path = target_dir / final_name

                renamed = final_name != file_path.name
                if renamed:
                    log(f"⚠ Файл {file_path.name} уже существует, переименован в {final_name}")

                # Перемещаем файл
                shutil.move(str(file_path), str(target_path))

                log(f"✓ Перемещено: {file_path.name} → {category}/{final_name}")

                # Логируем действие для отмены
                log_action('move', str(file_path), str(target_path), file_path.name)

                stats['moved'] += 1
                stats['by_category'][category] += 1

            except Exception as e:
                log(f"✗ Ошибка при перемещении {file_path.name}: {e}")
                stats['errors'] += 1

    # Сохраняем лог действий
    if not dry_run and stats['moved'] > 0:
        save_action_log(source_path / log_file_name)
        log(f"\n📝 Журнал действий сохранен в: {log_file_name}")

    # Удаление пустых папок
    if not dry_run and recursive and clean_empty:
        log("\nОчистка пустых папок...")
        for dirpath, dirnames, filenames in os.walk(str(source_path), topdown=False):
            dirpath = Path(dirpath)
            # Не удаляем корневую директорию и папки категорий
            if dirpath == source_path:
                continue
            if dirpath.parent == source_path and dirpath.name in FILE_CATEGORIES:
                continue

            try:
                if not any(dirpath.iterdir()):
                    dirpath.rmdir()
                    stats['empty_dirs_removed'] += 1
                    log(f"✓ Удалена пустая папка: {dirpath.relative_to(source_path)}")
            except Exception as e:
                log(f"⚠ Не удалось удалить папку {dirpath}: {e}")

    # Вывод статистики
    log(f"\n{'='*60}")
    log("СТАТИСТИКА")
    log(f"{'='*60}")
    log(f"Всего обработано: {stats['moved'] + stats['skipped']}")
    log(f"Перемещено: {stats['moved']}")
    log(f"Пропущено: {stats['skipped']}")
    log(f"Ошибок: {stats['errors']}")
    if recursive and not dry_run:
        log(f"Удалено пустых папок: {stats['empty_dirs_removed']}")

    if stats['by_category']:
        log("\nПо категориям:")
        for category, count in sorted(stats['by_category'].items()):
            log(f"  {category}: {count}")

    log(f"{'='*60}\n")

    return stats


def start_monitoring(source_dir: str, interval: int = 10, callback: Optional[Callable] = None):
    """
    Запускает фоновый мониторинг директории и автоматическую сортировку новых файлов.

    Args:
        source_dir: Директория для мониторинга
        interval: Интервал проверки в секундах
        callback: Функция обратного вызова при обнаружении изменений
    """
    global MONITORING_STOP_EVENT
    MONITORING_STOP_EVENT = threading.Event()

    source_path = Path(source_dir).resolve()
    initial_files = set()

    # Собираем начальный список файлов
    if source_path.exists():
        initial_files = {str(f.relative_to(source_path)) for f in source_path.rglob('*') if f.is_file()}

    def monitor_loop():
        nonlocal initial_files
        while not MONITORING_STOP_EVENT.is_set():
            time.sleep(interval)

            if not source_path.exists():
                continue

            current_files = {str(f.relative_to(source_path)) for f in source_path.rglob('*') if f.is_file()}
            new_files = current_files - initial_files

            if new_files:
                msg = f"🔍 Обнаружено новых файлов: {len(new_files)}. Запуск сортировки..."
                if callback:
                    callback(msg)
                else:
                    print(msg)

                # Запускаем сортировку только для новых файлов (упрощенно - полный прогон)
                # В реальной реализации можно оптимизировать передачу списка файлов
                try:
                    stats = organize_files(str(source_path), verbose=False, log_callback=callback)
                    if stats['moved'] > 0:
                        msg = f"✅ Сортировка завершена. Перемещено: {stats['moved']}"
                        if callback:
                            callback(msg)
                        else:
                            print(msg)
                except Exception as e:
                    msg = f"❌ Ошибка при сортировке: {e}"
                    if callback:
                        callback(msg)
                    else:
                        print(msg)

                # Обновляем список известных файлов
                initial_files = current_files

    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()
    return thread


def stop_monitoring():
    """Останавливает фоновый мониторинг."""
    MONITORING_STOP_EVENT.set()
