#!/usr/bin/env python3
"""
Консольная версия умного органайзера файлов.
Использует organizer_core для логики.
"""

import argparse
import logging
import sys
from pathlib import Path

from organizer_core import (
    get_undo_count,
    load_config,
    organize_files,
    start_monitoring,
    stop_monitoring,
    undo_last_operation,
)

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Умный органайзер файлов — автоматическая сортировка по типам",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python file_organizer.py ~/Downloads              # Сортировка
  python file_organizer.py ~/Downloads --dry-run    # Тестовый прогон
  python file_organizer.py ~/Downloads --undo       # Отменить все перемещения
  python file_organizer.py ~/Downloads --undo-count 5  # Отменить последние 5
  python file_organizer.py ~/Downloads --monitor    # Мониторинг новых файлов
  python file_organizer.py ~/Downloads --no-recursive  # Только корень папки
        """,
    )

    parser.add_argument(
        "directory", nargs="?", default=".",
        help="Директория для организации (по умолчанию: текущая)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Показать план без реальных действий",
    )
    parser.add_argument(
        "--no-recursive", action="store_true",
        help="Обрабатывать только файлы в корне папки",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Минимальный вывод",
    )
    parser.add_argument(
        "--undo", action="store_true",
        help="Отменить перемещения (читает персистентный лог — работает после перезапуска)",
    )
    parser.add_argument(
        "--undo-count", type=int, default=-1, metavar="N",
        help="Сколько последних операций отменить (-1 = все)",
    )
    parser.add_argument(
        "--monitor", action="store_true",
        help="Мониторинг: автосортировка при появлении новых файлов",
    )
    parser.add_argument(
        "--interval", type=int, default=10, metavar="SEC",
        help="Интервал polling-мониторинга в секундах (по умолчанию: 10)",
    )
    parser.add_argument(
        "--config", type=str, metavar="PATH",
        help="Путь к config.json (по умолчанию: рядом со скриптом)",
    )
    parser.add_argument(
        "--undo-info", action="store_true",
        help="Показать количество доступных для отмены операций",
    )

    args = parser.parse_args()
    target_dir = Path(args.directory).resolve()

    if not target_dir.exists():
        logger.error(f"Директория не найдена: {target_dir}")
        sys.exit(1)

    config = load_config()

    # Информация об undo
    if args.undo_info:
        n = get_undo_count(str(target_dir))
        logger.info(f"Доступно операций для отмены: {n}")
        return

    # Режим отмены
    if args.undo:
        undo_last_operation(
            str(target_dir),
            count=args.undo_count,
            verbose=not args.quiet,
        )
        return

    # Режим мониторинга
    if args.monitor:
        logger.info(f"🚀 Запуск мониторинга: {target_dir}")
        logger.info(f"⏱  Интервал: {args.interval} с.  Ctrl+C для остановки.\n")

        def _cb(msg: str) -> None:
            if not args.quiet:
                logger.info(msg)

        try:
            thread = start_monitoring(
                str(target_dir),
                interval=args.interval,
                callback=_cb,
                config=config,
            )
            while thread.is_alive():
                thread.join(timeout=1)
        except KeyboardInterrupt:
            logger.info("\n⏹  Остановка мониторинга...")
            stop_monitoring()
            logger.info("✅ Готово.")
        return

    # Обычная сортировка
    # recursive: None = берётся из конфига; False = явно передаём False
    recursive: bool | None = False if args.no_recursive else None

    try:
        organize_files(
            str(target_dir),
            dry_run=args.dry_run,
            verbose=not args.quiet,
            recursive=recursive,
            config=config,
        )
    except Exception as exc:
        logger.error(f"❌ Критическая ошибка: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
