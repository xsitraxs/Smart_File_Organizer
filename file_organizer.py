#!/usr/bin/env python3
"""
Консольная версия умного органайзера файлов.
Использует organizer_core для логики.
Поддерживает конфигурацию, отмену действий и мониторинг.
"""

import argparse
import sys
from pathlib import Path
from organizer_core import (
    organize_files,
    undo_last_operation,
    start_monitoring,
    stop_monitoring,
    load_config
)


def main():
    parser = argparse.ArgumentParser(
        description="Умный органайзер файлов - автоматическая сортировка по типам",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python file_organizer.py /path/to/folder                     # Обычная сортировка
  python file_organizer.py /path/to/folder --dry-run           # Тестовый режим
  python file_organizer.py /path/to/folder --undo              # Отменить последнюю операцию
  python file_organizer.py /path/to/folder --monitor           # Режим мониторинга
  python file_organizer.py /path/to/folder --no-recursive      # Только корневая папка
        """
    )

    parser.add_argument("directory", nargs="?", default=".",
                        help="Директория для организации (по умолчанию: текущая)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Режим просмотра: показать что будет сделано, без реальных действий")
    parser.add_argument("--no-recursive", action="store_true",
                        help="Не обрабатывать подпапки рекурсивно")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Тихий режим: минимальный вывод")
    parser.add_argument("--undo", action="store_true",
                        help="Отменить последнюю операцию перемещения")
    parser.add_argument("--undo-count", type=int, default=-1,
                        help="Количество операций для отмены (по умолчанию: все)")
    parser.add_argument("--monitor", action="store_true",
                        help="Режим мониторинга: автоматическая сортировка новых файлов")
    parser.add_argument("--interval", type=int, default=10,
                        help="Интервал мониторинга в секундах (по умолчанию: 10)")
    parser.add_argument("--config", type=str,
                        help="Путь к файлу конфигурации (по умолчанию: config.json рядом со скриптом)")

    args = parser.parse_args()

    target_dir = Path(args.directory).resolve()

    if not target_dir.exists():
        print(f"❌ Ошибка: Директория не найдена: {target_dir}")
        sys.exit(1)

    # Загрузка конфигурации
    config = load_config()

    # Обработка режима отмены
    if args.undo:
        undo_last_operation(
            str(target_dir),
            count=args.undo_count,
            verbose=not args.quiet
        )
        return

    # Обработка режима мониторинга
    if args.monitor:
        print(f"🚀 Запуск мониторинга директории: {target_dir}")
        print(f"⏱ Интервал проверки: {args.interval} сек.")
        print("Нажмите Ctrl+C для остановки...\n")

        def log_callback(msg):
            if not args.quiet:
                print(msg)

        try:
            thread = start_monitoring(str(target_dir), interval=args.interval, callback=log_callback)
            while thread.is_alive():
                thread.join(timeout=1)
        except KeyboardInterrupt:
            print("\n⏹ Остановка мониторинга...")
            stop_monitoring()
            print("✅ Мониторинг остановлен.")
        return

    # Обычный режим сортировки
    recursive = not args.no_recursive

    if args.dry_run and config and "settings" in config:
        config["settings"]["dry_run"] = True

    try:
        organize_files(
            str(target_dir),
            dry_run=args.dry_run,
            verbose=not args.quiet,
            recursive=recursive,
            config=config
        )
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
