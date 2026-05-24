#!/usr/bin/env python3
"""
Умный органайзер файлов
Автоматически сортирует файлы в папке по типам (изображения, документы, видео и т.д.)
"""

import os
import shutil
import argparse
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
    'installers': ['.exe', '.msi', '.deb', '.rpm', '.pkg', '.dmg', '.appimage'],
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


def organize_files(source_dir: str, dry_run: bool = False, verbose: bool = True) -> dict:
    """
    Сортирует файлы в указанной директории по категориям.

    Args:
        source_dir: Путь к директории для организации
        dry_run: Если True, только показывает что будет сделано, без реальных действий
        verbose: Выводить подробную информацию

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
        'by_category': {}
    }

    print(f"\n{'='*60}")
    print(f"Умный органайзер файлов")
    print(f"{'='*60}")
    print(f"Целевая директория: {source_path}")
    if dry_run:
        print("РЕЖИМ ПРОСМОТРА (dry-run) - файлы не будут перемещены")
    print(f"{'='*60}\n")

    # Получаем список всех файлов в корневой директории (не рекурсивно)
    files = [f for f in source_path.iterdir() if f.is_file()]

    if not files:
        print("Файлы не найдены в указанной директории.")
        return stats

    print(f"Найдено файлов: {len(files)}\n")

    for file_path in files:
        # Пропускаем сам скрипт и скрытые файлы
        if file_path.name.startswith('.'):
            if verbose:
                print(f"⊘ Пропущено (скрытый): {file_path.name}")
            stats['skipped'] += 1
            continue

        category = get_category(file_path)
        target_dir = source_path / category

        # Инициализируем счетчик категории
        if category not in stats['by_category']:
            stats['by_category'][category] = 0

        if dry_run:
            print(f"→ Будет перемещено: {file_path.name} → {category}/")
            stats['moved'] += 1
            stats['by_category'][category] += 1
        else:
            try:
                # Создаем папку категории если не существует
                target_dir.mkdir(exist_ok=True)

                # Обрабатываем дубликаты имен
                target_path = target_dir / file_path.name
                if target_path.exists():
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    stem = file_path.stem
                    suffix = file_path.suffix
                    new_name = f"{stem}_{timestamp}{suffix}"
                    target_path = target_dir / new_name
                    if verbose:
                        print(f"⚠ Файл {file_path.name} уже существует, переименован в {new_name}")

                # Перемещаем файл
                shutil.move(str(file_path), str(target_path))

                if verbose:
                    print(f"✓ Перемещено: {file_path.name} → {category}/")

                stats['moved'] += 1
                stats['by_category'][category] += 1

            except Exception as e:
                print(f"✗ Ошибка при перемещении {file_path.name}: {e}")
                stats['errors'] += 1

    # Вывод статистики
    print(f"\n{'='*60}")
    print("СТАТИСТИКА")
    print(f"{'='*60}")
    print(f"Всего обработано: {stats['moved'] + stats['skipped']}")
    print(f"Перемещено: {stats['moved']}")
    print(f"Пропущено: {stats['skipped']}")
    print(f"Ошибок: {stats['errors']}")

    if stats['by_category']:
        print("\nПо категориям:")
        for category, count in sorted(stats['by_category'].items()):
            print(f"  {category}: {count}")

    print(f"{'='*60}\n")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Умный органайзер файлов - автоматическая сортировка файлов по типам',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  %(prog)s                          # Организовать файлы в текущей директории
  %(prog)s /path/to/folder          # Организовать файлы в указанной директории
  %(prog)s --dry-run                # Показать что будет сделано без реального перемещения
  %(prog)s -q                       # Тихий режим (минимум вывода)
        """
    )

    parser.add_argument(
        'directory',
        nargs='?',
        default='.',
        help='Директория для организации (по умолчанию: текущая)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Режим просмотра: показать что будет сделано без реального перемещения файлов'
    )

    parser.add_argument(
        '-q', '--quiet',
        action='store_false',
        dest='verbose',
        help='Тихий режим: минимальный вывод информации'
    )

    args = parser.parse_args()

    try:
        organize_files(args.directory, dry_run=args.dry_run, verbose=args.verbose)
    except KeyboardInterrupt:
        print("\n\nОперация отменена пользователем.")
    except Exception as e:
        print(f"\n✗ Произошла ошибка: {e}")
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
