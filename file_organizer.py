#!/usr/bin/env python3
"""
Консольная версия умного органайзера файлов.
Импортирует основную логику из organizer_core.py.
"""

import argparse
from organizer_core import organize_files


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

    parser.add_argument(
        '--no-recursive',
        action='store_false',
        dest='recursive',
        help='Не обрабатывать подпапки (только файлы в корневой директории)'
    )

    args = parser.parse_args()

    try:
        organize_files(args.directory, dry_run=args.dry_run, verbose=args.verbose, recursive=args.recursive)
    except KeyboardInterrupt:
        print("\n\nОперация отменена пользователем.")
    except Exception as e:
        print(f"\n✗ Произошла ошибка: {e}")
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
