#!/usr/bin/env python3
"""
CLI-версия умного органайзера файлов.

Предоставляет консольный интерфейс для:
- Сортировки файлов (обычный режим, dry-run, нерекурсивный, тихий)
- Отмены операций (--undo, --undo-count N, --undo-info)
- Мониторинга директории (--monitor)
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

from organizer_core import (
    _harden_console_encoding,
    get_undo_count,
    load_config,
    load_config_from_path,
    organize_files,
    start_monitoring,
    stop_monitoring,
    undo_last_operation,
)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="file_organizer",
        description="Умный органайзер файлов — автоматическая сортировка по категориям",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  %(prog)s ~/Downloads                    # Сортировка папки Загрузки
  %(prog)s . --dry-run                    # Проверка плана без изменений
  %(prog)s ~/Downloads --undo             # Отмена последней операции
  %(prog)s ~/Downloads --undo-count 3     # Отмена последних 3 операций
  %(prog)s ~/Downloads --undo-info        # Информация об доступных отменах
  %(prog)s ~/Downloads --monitor          # Режим мониторинга
  %(prog)s ~/Downloads -q                 # Тихий режим
  %(prog)s ~/Downloads --no-recursive     # Только корень папки
        """,
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Путь к директории для сортировки (по умолчанию: текущая)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Тестовый режим: показать план без реального перемещения файлов",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Тихий режим: минимум вывода (только ошибки и итог)",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Не рекурсивно: обрабатывать только файлы в корне папки",
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="Отменить последнюю операцию сортировки",
    )
    parser.add_argument(
        "--undo-count",
        type=int,
        metavar="N",
        help="Отменить последние N операций (по умолчанию: 1, -1 = все)",
    )
    parser.add_argument(
        "--undo-info",
        action="store_true",
        help="Показать количество доступных операций для отмены",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Запустить мониторинг папки (автоматическая сортировка новых файлов)",
    )
    parser.add_argument(
        "--monitor-interval",
        type=int,
        default=10,
        metavar="SECONDS",
        help="Интервал проверки в секундах для polling-режима (по умолчанию: 10)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="Путь к альтернативному файлу конфигурации",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="Smart File Organizer 1.0",
    )
    return parser


def resolve_path(path_str: str) -> Path:
    """Resolve path, expanding ~ and making absolute."""
    return Path(path_str).expanduser().resolve()


def main(argv: Optional[list[str]] = None) -> int:
    # Windows: перенаправленный вывод по умолчанию в cp1252 — русский текст
    # в print() падал бы с UnicodeEncodeError. Настраиваем потоки заранее.
    _harden_console_encoding()
    parser = create_parser()
    args = parser.parse_args(argv)

    source_path = resolve_path(args.path)

    # Validate path exists for operations that need it
    if not args.undo_info and not (args.undo or args.undo_count is not None):
        if not source_path.exists():
            print(f"Ошибка: директория не найдена: {source_path}", file=sys.stderr)
            return 1
        if not source_path.is_dir():
            print(f"Ошибка: не является директорией: {source_path}", file=sys.stderr)
            return 1

    # Load config (--config: явно указанный файл вместо config.json рядом с модулем)
    try:
        config = load_config_from_path(args.config) if args.config else load_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1

    # Handle mutually exclusive operations
    operations = sum([
        args.undo,
        args.undo_count is not None,
        args.undo_info,
        args.monitor,
    ])
    if operations > 1:
        parser.error("Можно указать только одну операцию: --undo, --undo-count, --undo-info или --monitor")

    try:
        if args.undo_info:
            count = get_undo_count(str(source_path))
            print(f"Доступно операций для отмены: {count}")
            return 0

        if args.undo:
            stats = undo_last_operation(
                str(source_path),
                count=1,
                verbose=not args.quiet,
                config=config,
            )
            if not args.quiet:
                print(f"Восстановлено: {stats['restored']}, ошибок: {stats['errors']}")
            return 0 if stats['errors'] == 0 else 1

        if args.undo_count is not None:
            count = args.undo_count
            stats = undo_last_operation(
                str(source_path),
                count=count,
                verbose=not args.quiet,
                config=config,
            )
            if not args.quiet:
                print(f"Восстановлено: {stats['restored']}, ошибок: {stats['errors']}")
            return 0 if stats['errors'] == 0 else 1

        if args.monitor:
            if not args.quiet:
                print(f"Запуск мониторинга: {source_path} (интервал: {args.monitor_interval} с)")
                print("Нажмите Ctrl+C для остановки")
            try:
                thread = start_monitoring(
                    str(source_path),
                    interval=args.monitor_interval,
                    config=config,
                )
                thread.join()
            except KeyboardInterrupt:
                print("\nОстановка мониторинга...")
                stop_monitoring()
            return 0

        # Normal organize operation
        stats = organize_files(
            str(source_path),
            dry_run=args.dry_run,
            verbose=not args.quiet,
            recursive=not args.no_recursive,
            config=config,
        )

        if not args.quiet:
            print(f"\nПеремещено: {stats['moved']}")
            print(f"Пропущено: {stats['skipped']}")
            print(f"Дубликатов: {stats['duplicates']}")
            print(f"Ошибок: {stats['errors']}")
            if stats.get('empty_dirs_removed'):
                print(f"Пустых папок удалено: {stats['empty_dirs_removed']}")

        return 0 if stats['errors'] == 0 else 1

    except FileNotFoundError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Неожиданная ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
