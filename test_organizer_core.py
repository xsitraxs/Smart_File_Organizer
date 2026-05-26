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
    is_protected_dir,
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

        # Лог теперь в формате JSONL: одна JSON-запись на строку
        with open(log_path, "r", encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]

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



class TestDuplicateStrategies(unittest.TestCase):
    """Тесты duplicate_strategy: skip, replace, rename."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        (self.test_dir / "documents").mkdir()
        # Файл-исходник
        (self.test_dir / "report.pdf").write_text("new content")
        # Файл-уже-там
        (self.test_dir / "documents" / "report.pdf").write_text("old content")

    def _config_with_strategy(self, strategy: str) -> Dict[str, Any]:
        import copy
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["duplicate_strategy"] = strategy
        return cfg

    def test_rename_strategy(self):
        stats = organize_files(
            str(self.test_dir), verbose=False,
            config=self._config_with_strategy("rename"),
        )
        # Старый файл сохранился, новый переименован
        self.assertEqual(
            (self.test_dir / "documents" / "report.pdf").read_text(), "old content"
        )
        self.assertEqual(
            (self.test_dir / "documents" / "report_1.pdf").read_text(), "new content"
        )
        self.assertEqual(stats["duplicates"], 1)

    def test_skip_strategy(self):
        stats = organize_files(
            str(self.test_dir), verbose=False,
            config=self._config_with_strategy("skip"),
        )
        # Исходный файл остался на месте
        self.assertTrue((self.test_dir / "report.pdf").exists())
        # Старый файл в documents не тронут
        self.assertEqual(
            (self.test_dir / "documents" / "report.pdf").read_text(), "old content"
        )
        self.assertEqual(stats["skipped"], 1)

    def test_replace_strategy(self):
        stats = organize_files(
            str(self.test_dir), verbose=False,
            config=self._config_with_strategy("replace"),
        )
        # Старый файл заменён новым
        self.assertEqual(
            (self.test_dir / "documents" / "report.pdf").read_text(), "new content"
        )
        # Дубль _1 не должен был появиться
        self.assertFalse((self.test_dir / "documents" / "report_1.pdf").exists())
        self.assertEqual(stats["duplicates"], 1)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestProtectedDir(unittest.TestCase):
    """Тесты проверки защищённых директорий (фикс ложных срабатываний по имени)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_user_folder_named_home_not_protected(self):
        """Пользовательская папка 'home' внутри tmp НЕ должна считаться защищённой."""
        fake = self.test_dir / "home"
        fake.mkdir()
        self.assertFalse(is_protected_dir(fake))

    def test_user_folder_named_bin_not_protected(self):
        fake = self.test_dir / "bin"
        fake.mkdir()
        self.assertFalse(is_protected_dir(fake))

    def test_user_folder_named_etc_not_protected(self):
        fake = self.test_dir / "etc"
        fake.mkdir()
        self.assertFalse(is_protected_dir(fake))

    def test_real_etc_is_protected(self):
        """Системные пути по-прежнему защищены."""
        if os.name == "nt":
            self.skipTest("Unix-specific")
        # Не создаём — проверяем именно по абсолютному пути
        self.assertTrue(is_protected_dir(Path("/etc")))
        self.assertTrue(is_protected_dir(Path("/usr/bin")))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestUndoLogFormat(unittest.TestCase):
    """Тесты нового JSONL-формата undo-лога + обратной совместимости."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        (self.test_dir / "a.txt").write_text("a")
        (self.test_dir / "b.txt").write_text("b")
        (self.test_dir / "c.txt").write_text("c")

    def test_jsonl_format(self):
        """Лог записывается как JSONL: одна запись на строку."""
        organize_files(str(self.test_dir), verbose=False)
        log_path = self.test_dir / UNDO_LOG_FILENAME
        content = log_path.read_text(encoding="utf-8")
        lines = [l for l in content.splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)
        # Каждая строка — валидный JSON-объект
        for line in lines:
            obj = json.loads(line)
            self.assertEqual(obj["action"], "move")

    def test_backward_compat_old_json_array(self):
        """Старый формат (JSON-массив) читается корректно."""
        from organizer_core import _load_undo_log
        log_path = self.test_dir / UNDO_LOG_FILENAME
        # Эмулируем лог в старом формате
        old_entries = [
            {"action": "move", "source": "a.txt", "destination": "documents/a.txt",
             "original_name": "a.txt", "timestamp": "2026-01-01T00:00:00"},
            {"action": "move", "source": "b.txt", "destination": "documents/b.txt",
             "original_name": "b.txt", "timestamp": "2026-01-01T00:00:01"},
        ]
        log_path.write_text(json.dumps(old_entries, indent=2), encoding="utf-8")
        loaded = _load_undo_log(self.test_dir)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["original_name"], "a.txt")

    def test_corrupted_line_skipped(self):
        """Битая строка в JSONL не ломает загрузку всего лога."""
        from organizer_core import _load_undo_log
        log_path = self.test_dir / UNDO_LOG_FILENAME
        log_path.write_text(
            '{"action": "move", "source": "a.txt", "destination": "d/a.txt", "original_name": "a.txt"}\n'
            'this is not json\n'
            '{"action": "move", "source": "b.txt", "destination": "d/b.txt", "original_name": "b.txt"}\n',
            encoding="utf-8",
        )
        loaded = _load_undo_log(self.test_dir)
        self.assertEqual(len(loaded), 2)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestCaseInsensitiveDedup(unittest.TestCase):
    """Тест case-insensitive проверки имён в _get_unique_path (актуально для Win/macOS)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_case_handling(self):
        """На Windows file.txt и FILE.TXT считаются одним именем."""
        existing = {"File.TXT"}
        result = _get_unique_path(self.test_dir, "file.txt", existing_names=existing)
        if os.name == "nt":
            # На Windows должен получить суффикс
            self.assertEqual(result.name, "file_1.txt")
        else:
            # На Linux/macOS-case-sensitive — это разные файлы
            self.assertEqual(result.name, "file.txt")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestConcurrentOrganize(unittest.TestCase):
    """Тест защиты от параллельных organize_files (баг с гонками в мониторинге)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        for i in range(20):
            (self.test_dir / f"file_{i}.txt").write_text(f"content {i}")

    def test_concurrent_calls_dont_corrupt_log(self):
        """Два параллельных вызова organize_files не должны повредить undo-лог."""
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(organize_files, str(self.test_dir), verbose=False)
                for _ in range(4)
            ]
            results = [f.result() for f in futures]
        # Ровно один вызов отработал нормально, остальные были пропущены
        skipped = [r for r in results if r.get("skipped_concurrent")]
        worked = [r for r in results if not r.get("skipped_concurrent")]
        self.assertEqual(len(worked), 1)
        self.assertGreaterEqual(len(skipped), 0)  # 3 пропущенных
        # Все 20 файлов перемещены и не дублированы
        moved_files = list((self.test_dir / "documents").iterdir())
        self.assertEqual(len(moved_files), 20)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


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
    suite.addTests(loader.loadTestsFromTestCase(TestDuplicateStrategies))
    suite.addTests(loader.loadTestsFromTestCase(TestProtectedDir))
    suite.addTests(loader.loadTestsFromTestCase(TestUndoLogFormat))
    suite.addTests(loader.loadTestsFromTestCase(TestCaseInsensitiveDedup))
    suite.addTests(loader.loadTestsFromTestCase(TestConcurrentOrganize))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    exit(run_tests())
