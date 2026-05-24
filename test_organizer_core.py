#!/usr/bin/env python3
"""
Юнит-тесты для organizer_core.py.

Запуск:
    python -m pytest tests/test_organizer_core.py -v

Или без pytest:
    python tests/test_organizer_core.py
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from organizer_core import (
    DEFAULT_CONFIG,
    UNDO_LOG_FILENAME,
    _build_ext_map,
    _get_category,
    _get_unique_path,
    _validate_path_safety,
    get_undo_count,
    load_config,
    organize_files,
    undo_last_operation,
)


class TestExtMap(unittest.TestCase):
    """Тесты маппинга расширений."""

    def test_build_ext_map(self):
        """Проверка построения маппинга расширений."""
        ext_map = _build_ext_map(DEFAULT_CONFIG)
        self.assertEqual(ext_map[".jpg"], "images")
        self.assertEqual(ext_map[".png"], "images")
        self.assertEqual(ext_map[".pdf"], "documents")
        self.assertEqual(ext_map[".mp4"], "videos")
        self.assertEqual(ext_map[".py"], "code")

    def test_get_category(self):
        """Проверка определения категории файла."""
        ext_map = _build_ext_map(DEFAULT_CONFIG)
        self.assertEqual(_get_category(Path("test.jpg"), ext_map), "images")
        self.assertEqual(_get_category(Path("doc.pdf"), ext_map), "documents")
        self.assertEqual(_get_category(Path("unknown.xyz"), ext_map), "other")


class TestPathSafety(unittest.TestCase):
    """Тесты безопасности путей."""

    def setUp(self):
        self.source_dir = Path("/tmp/test_source").resolve()

    def test_valid_path(self):
        """Валидный путь внутри директории."""
        file_path = self.source_dir / "subdir" / "file.txt"
        self.assertTrue(_validate_path_safety(file_path, self.source_dir))

    def test_path_traversal(self):
        """Запрет Path Traversal через '..'."""
        # Создаём тестовую структуру
        self.source_dir.mkdir(exist_ok=True)
        file_path = self.source_dir / ".." / "etc" / "passwd"
        self.assertFalse(_validate_path_safety(file_path, self.source_dir))

    def test_absolute_path(self):
        """Абсолютный путь вне директории."""
        file_path = Path("/etc/passwd")
        self.assertFalse(_validate_path_safety(file_path, self.source_dir))

    def tearDown(self):
        if self.source_dir.exists():
            shutil.rmtree(self.source_dir, ignore_errors=True)


class TestUniquePath(unittest.TestCase):
    """Тесты уникальных путей."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_no_conflict(self):
        """Файл без конфликта имён."""
        result = _get_unique_path(self.test_dir, "file.txt")
        self.assertEqual(result.name, "file.txt")

    def test_with_conflict(self):
        """Файл с конфликтом имён."""
        (self.test_dir / "file.txt").touch()
        result = _get_unique_path(self.test_dir, "file.txt")
        self.assertEqual(result.name, "file_1.txt")

    def test_multiple_conflicts(self):
        """Несколько конфликтов имён."""
        (self.test_dir / "file.txt").touch()
        (self.test_dir / "file_1.txt").touch()
        result = _get_unique_path(self.test_dir, "file.txt")
        self.assertEqual(result.name, "file_2.txt")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestOrganizeFiles(unittest.TestCase):
    """Тесты организации файлов."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        # Создаём тестовые файлы
        (self.test_dir / "photo.jpg").write_text("image data")
        (self.test_dir / "document.pdf").write_text("pdf data")
        (self.test_dir / "script.py").write_text("python code")
        (self.test_dir / "unknown.xyz").write_text("unknown")

    def test_organize_basic(self):
        """Базовая организация файлов."""
        stats = organize_files(str(self.test_dir), verbose=False)
        self.assertEqual(stats["moved"], 4)
        self.assertEqual(stats["errors"], 0)

        # Проверяем, что файлы перемещены в правильные папки
        self.assertTrue((self.test_dir / "images" / "photo.jpg").exists())
        self.assertTrue((self.test_dir / "documents" / "document.pdf").exists())
        self.assertTrue((self.test_dir / "code" / "script.py").exists())
        self.assertTrue((self.test_dir / "other" / "unknown.xyz").exists())

    def test_dry_run(self):
        """Тестовый режим (dry-run)."""
        stats = organize_files(str(self.test_dir), dry_run=True, verbose=False)
        self.assertEqual(stats["moved"], 4)

        # Файлы должны остаться на месте
        self.assertTrue((self.test_dir / "photo.jpg").exists())
        self.assertFalse((self.test_dir / "images").exists())

    def test_non_recursive(self):
        """Нерекурсивный режим."""
        # Создаём подпапку с файлом
        subdir = self.test_dir / "subdir"
        subdir.mkdir()
        (subdir / "nested.jpg").write_text("nested image")

        stats = organize_files(str(self.test_dir), recursive=False, verbose=False)
        # Должны обработаться только файлы в корне (4 шт), но не в подпапке
        self.assertEqual(stats["moved"], 4)
        self.assertTrue((subdir / "nested.jpg").exists())

    def test_ignore_script_files(self):
        """Игнорирование файлов скриптов."""
        (self.test_dir / "organizer_core.py").write_text("# core")
        (self.test_dir / "config.json").write_text("{}")

        stats = organize_files(str(self.test_dir), verbose=False)
        # Скрипты не должны перемещаться
        self.assertTrue((self.test_dir / "organizer_core.py").exists())
        self.assertTrue((self.test_dir / "config.json").exists())

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestUndoOperation(unittest.TestCase):
    """Тесты отмены операций."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        # Создаём тестовые файлы
        (self.test_dir / "file1.txt").write_text("content 1")
        (self.test_dir / "file2.doc").write_text("content 2")

    def test_undo_basic(self):
        """Базовая отмена операции."""
        # Организуем
        stats = organize_files(str(self.test_dir), verbose=False)
        self.assertEqual(stats["moved"], 2)

        # Проверяем, что файлы перемещены
        self.assertTrue((self.test_dir / "documents" / "file1.txt").exists())
        self.assertTrue((self.test_dir / "documents" / "file2.doc").exists())

        # Отменяем
        undo_stats = undo_last_operation(str(self.test_dir), verbose=False)
        self.assertEqual(undo_stats["restored"], 2)
        self.assertEqual(undo_stats["errors"], 0)

        # Проверяем, что файлы возвращены
        self.assertTrue((self.test_dir / "file1.txt").exists())
        self.assertTrue((self.test_dir / "file2.doc").exists())
        self.assertFalse((self.test_dir / "documents" / "file1.txt").exists())

    def test_undo_partial(self):
        """Частичная отмена (count)."""
        # Организуем
        organize_files(str(self.test_dir), verbose=False)

        # Отменяем только последнюю операцию
        undo_stats = undo_last_operation(str(self.test_dir), count=1, verbose=False)
        self.assertEqual(undo_stats["restored"], 1)

        # Один файл должен вернуться, другой остаться
        remaining_file = "file1.txt" if (self.test_dir / "documents" / "file1.txt").exists() else "file2.doc"
        restored_file = "file2.doc" if remaining_file == "file1.txt" else "file1.txt"

        self.assertTrue((self.test_dir / restored_file).exists())
        self.assertTrue((self.test_dir / "documents" / remaining_file).exists())

    def test_undo_persistent(self):
        """Персистентность лога отмены (после 'перезапуска')."""
        # Организуем
        organize_files(str(self.test_dir), verbose=False)

        # Эмулируем перезагрузку - просто проверяем, что лог читается
        count = get_undo_count(str(self.test_dir))
        self.assertEqual(count, 2)

        # Отменяем
        undo_last_operation(str(self.test_dir), verbose=False)

        # Лог должен быть очищен
        count = get_undo_count(str(self.test_dir))
        self.assertEqual(count, 0)

    def test_undo_log_contains_relative_paths(self):
        """Лог отмены содержит относительные пути."""
        organize_files(str(self.test_dir), verbose=False)

        # Читаем лог напрямую
        log_path = self.test_dir / UNDO_LOG_FILENAME
        self.assertTrue(log_path.exists())

        with open(log_path, "r", encoding="utf-8") as f:
            entries = json.load(f)

        self.assertEqual(len(entries), 2)
        for entry in entries:
            # Пути должны быть относительными (не начинаться с /)
            self.assertFalse(entry["source"].startswith("/"))
            self.assertFalse(entry["destination"].startswith("/"))
            # destination должен содержать категорию
            self.assertIn("documents/", entry["destination"])

    def test_undo_after_restart(self):
        """Отмена работает после эмуляции перезапуска."""
        # Организуем
        organize_files(str(self.test_dir), verbose=False)

        # Проверяем файлы перемещены
        self.assertTrue((self.test_dir / "documents" / "file1.txt").exists())

        # Эмулируем полный сброс состояния (как после перезапуска)
        # В реальном сценарии это происходит автоматически благодаря персистентному логу
        from organizer_core import _action_log, _load_undo_log
        _action_log.clear()  # Очищаем in-memory лог

        # Отмена должна работать из персистентного лога
        undo_stats = undo_last_operation(str(self.test_dir), verbose=False)
        self.assertEqual(undo_stats["restored"], 2)
        self.assertTrue((self.test_dir / "file1.txt").exists())

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestConfigLoading(unittest.TestCase):
    """Тесты загрузки конфигурации."""

    def test_default_config(self):
        """Конфиг по умолчанию."""
        config = load_config()
        self.assertIn("categories", config)
        self.assertIn("settings", config)
        self.assertEqual(config["settings"]["recursive"], True)

    def test_ext_map_completeness(self):
        """Полнота маппинга расширений."""
        ext_map = _build_ext_map(DEFAULT_CONFIG)
        # Проверяем основные категории
        categories = set(ext_map.values())
        self.assertIn("images", categories)
        self.assertIn("documents", categories)
        self.assertIn("videos", categories)
        self.assertIn("code", categories)


def run_tests():
    """Запуск тестов без pytest."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Добавляем все тесты
    suite.addTests(loader.loadTestsFromTestCase(TestExtMap))
    suite.addTests(loader.loadTestsFromTestCase(TestPathSafety))
    suite.addTests(loader.loadTestsFromTestCase(TestUniquePath))
    suite.addTests(loader.loadTestsFromTestCase(TestOrganizeFiles))
    suite.addTests(loader.loadTestsFromTestCase(TestUndoOperation))
    suite.addTests(loader.loadTestsFromTestCase(TestConfigLoading))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    exit(run_tests())
