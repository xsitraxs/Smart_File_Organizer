#!/usr/bin/env python3
"""
Ядро умного органайзера файлов
Модуль содержит основную логику сортировки файлов по типам.
Импортируется в CLI и GUI версии.
"""

import os
import shutil
from pathlib import Path
from datetime import datetime

# Конфигурация: расширение -> папка назначения
FILE_CATEGORIES = {
    # Изображения
    'images': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp', '.ico', '.tiff', '.raw'],
    # Документы
    'documents': ['.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.xls', '.xlsx', '.ppt', '.pptx', '.md'],
    # Видео
    'videos': ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v'],
    # Аудио
    'audio': ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a'],
    # Архивы
    'archives': ['.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz'],
    # Торренты
    'torrents': ['.torrent', '.magnet'],
    # Код
    'code': ['.py', '.js', '.ts', '.html', '.css', '.java', '.cpp', '.c', '.h', '.go', '.rs', '.rb', '.php', '.sql', '.json', '.xml', '.yaml', '.yml'],
    # Установщики
    'installers': ['.exe', '.msi', '.deb', '.rpm', '.pkg', '.dmg', '.appimage', '.apk'],
    # Шрифты
    'fonts': ['.ttf', '.otf', '.woff', '.woff2', '.eot'],
}

# Обратный маппинг: расширение -> категория
EXTENSION_TO_CATEGORY = {}
for category, extensions in FILE_CATEGORIES.items():
    for ext in extensions:
        EXTENSION_TO_CATEGORY[ext.lower()] = category


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


def organize_files(source_dir: str, dry_run: bool = False, verbose: bool = True, recursive: bool = True, log_callback=None) -> dict:
    """
    Сортирует файлы в указанной директории по категориям.

    Args:
        source_dir: Путь к директории для организации
        dry_run: Если True, только показывает что будет сделано, без реальных действий
        verbose: Выводить подробную информацию
        recursive: Если True, обрабатывать файлы во всех подпапках
        log_callback: Функция обратного вызова для логирования (для GUI)

    Returns:
        Статистика выполненных операций
    """
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

    # Имена файлов, которые нужно игнорировать (скрипты органайзера)
    ignore_names = {'organizer_core.py', 'file_organizer.py', 'file_organizer_gui.py'}

    for file_path in files:
        # Пропускаем сам скрипт и скрытые файлы
        if file_path.name.startswith('.') or file_path.name in ignore_names:
            if verbose:
                log(f"⊘ Пропущено: {file_path.name}")
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

                if final_name != file_path.name:
                    log(f"⚠ Файл {file_path.name} уже существует, переименован в {final_name}")

                # Перемещаем файл
                shutil.move(str(file_path), str(target_path))

                log(f"✓ Перемещено: {file_path.name} → {category}/{final_name}")

                stats['moved'] += 1
                stats['by_category'][category] += 1

            except Exception as e:
                log(f"✗ Ошибка при перемещении {file_path.name}: {e}")
                stats['errors'] += 1

    # Удаление пустых папок (только если не dry-run и рекурсивный режим)
    if not dry_run and recursive:
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
