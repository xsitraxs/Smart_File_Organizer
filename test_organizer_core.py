#!/usr/bin/env python3
"""
Юнит-тесты для organizer_core.py.

Запуск (из корня проекта):
    python3 -m unittest test_organizer_core
    python3 test_organizer_core.py
    pytest test_organizer_core.py -v
"""

import copy
import io as io_mod
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

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
    start_monitoring,
    stop_monitoring,
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
        # Свой временный каталог: литерал "/tmp/..." на Windows указал бы на
        # C:\tmp, которого на чистой машине может не быть.
        self._root = Path(tempfile.mkdtemp())
        self.source_dir = self._root / "source"
        self.source_dir.mkdir()

    def test_valid_path(self):
        """Валидный путь внутри директории."""
        file_path = self.source_dir / "subdir" / "file.txt"
        self.assertTrue(_validate_path_safety(file_path, self.source_dir))

    def test_path_traversal(self):
        """Запрет Path Traversal через '..'."""
        file_path = self.source_dir / ".." / "etc" / "passwd"
        self.assertFalse(_validate_path_safety(file_path, self.source_dir))

    def test_absolute_path(self):
        """Абсолютный путь вне директории."""
        # Каталог-сосед (не внутри source_dir) — работает на любой ОС
        outside = Path(tempfile.mkdtemp()) / "passwd"
        self.assertFalse(_validate_path_safety(outside, self.source_dir))

    def test_absolute_system_path(self):
        """Системный абсолютный путь вне директории отклоняется."""
        if os.name == "nt":
            self.skipTest("Unix-specific")
        self.assertFalse(_validate_path_safety(Path("/etc/passwd"), self.source_dir))

    def tearDown(self):
        shutil.rmtree(self._root, ignore_errors=True)


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

        organize_files(str(self.test_dir), verbose=False)
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
        with open(log_path, encoding="utf-8") as f:
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
        from organizer_core import _action_log
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

    def _config_with_strategy(self, strategy: str) -> dict[str, Any]:
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
        lines = [line for line in content.splitlines() if line.strip()]
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
    """Сравнение имён так, как его видит сама ФС (регистр + Unicode)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_case_handling(self):
        """Ответ пробника совпадает с фактическим поведением ФС."""
        from organizer_core import _fs_is_case_insensitive

        existing = {"File.TXT"}
        result = _get_unique_path(self.test_dir, "file.txt", existing_names=existing)
        if _fs_is_case_insensitive(self.test_dir):
            # Регистронезависимая ФС: «file.txt» и «File.TXT» — одно имя → суффикс
            self.assertEqual(result.name, "file_1.txt")
        else:
            # Регистрозависимая ФС (обычный Linux) — это разные файлы
            self.assertEqual(result.name, "file.txt")

    def test_precomputed_keys_are_used(self):
        """Переданный набор ключей используется без пересчёта и без I/O."""
        from organizer_core import _fs_is_case_insensitive, _name_key

        ci = _fs_is_case_insensitive(self.test_dir)
        existing = {"File.TXT"}
        keys = {_name_key("File.TXT", ci)}
        result = _get_unique_path(
            self.test_dir,
            "file.txt",
            existing_names=existing,
            existing_keys=keys,
        )
        self.assertEqual(result.name, "file_1.txt" if ci else "file.txt")

    def test_helpers_case_aware(self):
        """_name_in_set/_add_name работают с ключами сравнения."""
        from organizer_core import _add_name, _name_in_set

        names: set = set()
        keys: set = set()
        # Ключи построены для регистронезависимой ФС
        _add_name("Photo.JPG", names, keys, True)
        self.assertIn("Photo.JPG", names)
        self.assertIn("photo.jpg", keys)
        self.assertTrue(_name_in_set("photo.jpg", names, keys, True))
        self.assertTrue(_name_in_set("PHOTO.jpg", names, keys, True))
        # Несовпадающий регистр на регистрозависимой ФС — другое имя
        self.assertFalse(_name_in_set("photo.jpg", names, {"Photo.JPG"}, False))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestCaseInsensitiveBranchForced(unittest.TestCase):
    """Ветки для регистронезависимых ФС (Windows/macOS) на Linux-ФС.

    На регистрозависимой ФС эти пути кода не выполняются сами по себе,
    поэтому флаг подставляется принудительно — иначе поведение на Windows
    и macOS осталось бы непокрытым вплоть до CI на этих ОС.
    """

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        (self.test_dir / "images").mkdir()
        (self.test_dir / "images" / "Photo.JPG").write_text("OLD")
        (self.test_dir / "photo.jpg").write_text("NEW")

    def _organize(self, strategy: str) -> dict:
        from unittest import mock

        import organizer_core as oc

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["duplicate_strategy"] = strategy
        with mock.patch.object(oc, "_fs_is_case_insensitive", return_value=True):
            return oc.organize_files(str(self.test_dir), verbose=False, config=cfg)

    @staticmethod
    def _category_files(directory: Path) -> "dict[str, str]":
        return {
            p.name: p.read_text()
            for p in directory.iterdir()
            if p.is_file()
        }

    def test_rename_strategy_keeps_both_files(self):
        """Коллизия по регистру: оба файла сохранены, ни один не перезаписан."""
        stats = self._organize("rename")
        files = self._category_files(self.test_dir / "images")
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assertIn("OLD", files.values())
        self.assertIn("NEW", files.values())
        self.assertEqual(len(files), 2)

    def test_skip_strategy_keeps_existing_and_source(self):
        """skip: новый файл не тронут, старый на месте."""
        stats = self._organize("skip")
        files = self._category_files(self.test_dir / "images")
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(files, {"Photo.JPG": "OLD"})
        self.assertTrue((self.test_dir / "photo.jpg").exists(), "файл потерян при skip")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestRealCaseInsensitiveFS(unittest.TestCase):
    """Сценарии, которые требуют настоящей регистронезависимой ФС.

    На Linux такой ФС нет, поэтому здесь проверяем себя честно: если ФС всё
    же регистрозависимая — тест пропускается (на macOS/Windows CI он
    выполняется реально). Подделывать флаг нельзя: на Linux «photo.jpg» и
    «Photo.JPG» — разные файлы, и сценарий теряет смысл.
    """

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        (self.test_dir / "images").mkdir()
        (self.test_dir / "images" / "Photo.JPG").write_text("OLD")

    def _require_case_insensitive_fs(self) -> None:
        from organizer_core import _fs_is_case_insensitive

        if not _fs_is_case_insensitive(self.test_dir):
            self.skipTest("ФС регистрозависимая (Linux) — сценарий macOS/Windows")

    def test_replace_strategy_backs_up_case_collision(self):
        """replace на CI-ФС: «photo.jpg» находит «Photo.JPG» и уводит его в .bak."""
        self._require_case_insensitive_fs()
        (self.test_dir / "photo.jpg").write_text("NEW")

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["duplicate_strategy"] = "replace"
        stats = organize_files(str(self.test_dir), verbose=False, config=cfg)

        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["errors"], 0)
        files = self._category_files(self.test_dir / "images")
        self.assertEqual(files.get("photo.jpg"), "NEW", "новый файл не занял имя")
        backups = [text for name, text in files.items() if ".bak-" in name]
        self.assertEqual(backups, ["OLD"], "заменённый файл не сохранён в бэкап")

    def test_platform_default_fs_is_case_insensitive(self):
        """На Windows/macOS ФС по умолчанию нечувствительна к регистру.

        Это гарантия, что сценарии этого класса на CI действительно
        выполняются, а не пропускаются молча — иначе платформенные ветки
        остались бы непроверенными.
        """
        if os.name != "nt" and sys.platform != "darwin":
            self.skipTest("проверка имеет смысл только для Windows/macOS")
        from organizer_core import _fs_is_case_insensitive

        self.assertTrue(
            _fs_is_case_insensitive(self.test_dir),
            "ожидалась регистронезависимая ФС (APFS/NTFS по умолчанию)",
        )

    def test_no_silent_overwrite_of_existing_name(self):
        """Файл с тем же именем в другом регистре не перезаписывается молча."""
        self._require_case_insensitive_fs()
        (self.test_dir / "photo.jpg").write_text("NEW")

        stats = organize_files(str(self.test_dir), verbose=False)

        self.assertEqual(stats["duplicates"], 1)
        files = self._category_files(self.test_dir / "images")
        self.assertIn("OLD", files.values(), "старый файл потерян")
        self.assertIn("NEW", files.values(), "новый файл потерян")
        self.assertEqual(len(files), 2)

    @staticmethod
    def _category_files(directory: Path) -> "dict[str, str]":
        return {p.name: p.read_text() for p in directory.iterdir() if p.is_file()}

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestCaseSensitivityProbe(unittest.TestCase):
    """Тест реальной проверки ФС на чувствительность к регистру (вместо догадки)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_probe_matches_actual_fs_behaviour(self):
        """Пробник возвращает то же, что даёт настоящая проверка файлом."""
        probe_dir = self.test_dir / "probe_here"
        probe_dir.mkdir()

        # Независимая проверка: создаём файл со смешанным регистром и смотрим,
        # виден ли он под другим регистром — это и есть поведение этой ФС.
        (probe_dir / "MixedCase.TXT").write_text("x")
        actually_insensitive = (probe_dir / "mixedcase.txt").exists()

        from organizer_core import _probe_case_sensitivity

        self.assertEqual(
            _probe_case_sensitivity(probe_dir),
            actually_insensitive,
            f"Пробник разошёлся с реальным поведением ФС в {probe_dir}",
        )

    def test_probe_prefix_contains_uppercase(self):
        """Регрессия: без верхнего регистра в префиксе пробник всегда None.

        mkstemp добавляет только строчные символы, поэтому регистр обязан
        приходить из префикса — иначе lower-вариант совпадает с оригиналом
        и проверка бессмысленна.
        """
        from organizer_core import CASE_PROBE_PREFIX

        self.assertNotEqual(CASE_PROBE_PREFIX, CASE_PROBE_PREFIX.lower())

    def test_probe_leaves_no_files(self):
        """Пробный файл не остаётся в каталоге."""
        before = {p.name for p in self.test_dir.iterdir()}
        from organizer_core import _probe_case_sensitivity

        _probe_case_sensitivity(self.test_dir)
        self.assertEqual({p.name for p in self.test_dir.iterdir()}, before)

    def test_probe_returns_none_for_unavailable_dir(self):
        """Для недоступного каталога пробник возвращает None (нужен fallback)."""
        from organizer_core import _probe_case_sensitivity

        self.assertIsNone(_probe_case_sensitivity(self.test_dir / "no_such_dir"))

    def test_result_is_cached_per_directory(self):
        """Результат пробника кэшируется по каталогу."""
        from organizer_core import _case_sensitivity_cache, _fs_is_case_insensitive

        first = _fs_is_case_insensitive(self.test_dir)
        key = str(self.test_dir.resolve())
        self.assertIn(key, _case_sensitivity_cache)
        # Второй вызов отдаёт кэш (и не создаёт файлов-пробников)
        before = {p.name for p in self.test_dir.iterdir()}
        self.assertEqual(_fs_is_case_insensitive(self.test_dir), first)
        self.assertEqual({p.name for p in self.test_dir.iterdir()}, before)

    def test_no_directory_arg_falls_back_to_platform_guess(self):
        """Без каталога используется платформенная эвристика."""
        from organizer_core import _fs_is_case_insensitive, _platform_case_guess

        self.assertEqual(_fs_is_case_insensitive(), _platform_case_guess())

    def test_probe_agrees_with_organize_behaviour(self):
        """organize_files и пробник согласованы: коллизия регистра обрабатывается."""
        from organizer_core import _fs_is_case_insensitive

        (self.test_dir / "images").mkdir()
        (self.test_dir / "images" / "Photo.JPG").write_text("OLD")
        (self.test_dir / "photo.jpg").write_text("NEW")

        stats = organize_files(str(self.test_dir), verbose=False)
        ci = _fs_is_case_insensitive(self.test_dir / "images")
        if ci:
            # «photo.jpg» конфликтует с «Photo.JPG» → сохраняем оба
            self.assertEqual(stats["duplicates"], 1)
            self.assertEqual(len(list((self.test_dir / "images").iterdir())), 2)
        else:
            # Регистрозависимая ФС: это разные имена, конфликта нет
            self.assertEqual(stats["duplicates"], 0)
        # Данные не потеряны ни в одном случае
        contents = {p.read_text() for p in (self.test_dir / "images").iterdir() if p.is_file()}
        self.assertIn("NEW", contents)
        self.assertIn("OLD", contents)

    def test_probe_file_never_organized(self):
        """Пробный файл ФС не сортируется даже при ignore_hidden=false."""
        from organizer_core import CASE_PROBE_PREFIX

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["ignore_hidden"] = False
        probe = self.test_dir / f"{CASE_PROBE_PREFIX}ab12cd"
        probe.write_text("probe")
        (self.test_dir / "a.jpg").write_text("jpg")

        organize_files(str(self.test_dir), verbose=False, config=cfg)

        self.assertTrue(probe.exists(), "пробный файл уехал в категорию")
        for category in ("images", "other"):
            self.assertFalse((self.test_dir / category / probe.name).exists())

    def test_scheduler_ignores_probe_file(self):
        """Мониторинг не просыпается на пробном файле проверки регистра."""
        from organizer_core import CASE_PROBE_PREFIX, _DebounceScheduler

        flushes: list = []
        sched = _DebounceScheduler(on_flush=lambda n: flushes.append(n))
        try:
            sched.schedule(f"{self.test_dir}/{CASE_PROBE_PREFIX}zz99")
            self.assertEqual(sched.pending_count, 0, "пробный файл разбудил мониторинг")
            sched.schedule(f"{self.test_dir}/real_file.jpg")
            self.assertEqual(sched.pending_count, 1)
        finally:
            sched.cancel()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestUnicodeNormalization(unittest.TestCase):
    """Тесты Unicode-нормализации имён (macOS хранит имена в NFD)."""

    NFC_NAME = "caf\u00e9.txt"        # é одним кодпоинтом
    NFD_NAME = "cafe\u0301.txt"       # e + комбинирующий акут (как на APFS)

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_name_key_equalizes_nfc_and_nfd(self):
        """NFC и NFD формы одного имени дают одинаковый ключ сравнения."""
        from organizer_core import _name_key

        self.assertEqual(
            _name_key(self.NFC_NAME, True), _name_key(self.NFD_NAME, True)
        )
        self.assertEqual(
            _name_key(self.NFC_NAME, False), _name_key(self.NFD_NAME, False)
        )

    def test_name_key_respects_case_flag(self):
        """Ключ учитывает регистр только на регистронезависимой ФС."""
        from organizer_core import _name_key

        self.assertNotEqual(_name_key("Photo.JPG", False), _name_key("photo.jpg", False))
        self.assertEqual(_name_key("Photo.JPG", True), _name_key("photo.jpg", True))

    def test_normalize_name_is_idempotent(self):
        """Повторная нормализация не меняет результат."""
        from organizer_core import _normalize_name

        once = _normalize_name(self.NFD_NAME)
        self.assertEqual(once, self.NFC_NAME)
        self.assertEqual(_normalize_name(once), once)

    def test_collision_detected_across_unicode_forms(self):
        """Коллизия ловится между разными формами одного имени.

        Это ключевая регрессия: имя пользователя (NFC) и то же имя с диска
        (NFD) — одна запись ФС. Без нормализации мы считали их разными и
        могли молча перезаписать существующий файл.
        """
        from organizer_core import _add_name, _name_in_set

        names: set = set()
        keys: set = set()
        _add_name(self.NFD_NAME, names, keys, True)
        self.assertTrue(_name_in_set(self.NFC_NAME, names, keys, True))
        self.assertTrue(_name_in_set(self.NFD_NAME, names, keys, True))
        # Постороннее имя по-прежнему не считается коллизией
        self.assertFalse(_name_in_set("other.txt", names, keys, True))

    def test_unique_path_avoids_overwriting_across_forms(self):
        """_get_unique_path выдаёт суффикс, если имя занято в другой форме.

        На Linux NFC и NFD — разные файлы, поэтому суффикс здесь необязателен,
        но он безопасен: лучше лишний «_1», чем перезапись чужого файла.
        """
        existing = {self.NFD_NAME}
        result = _get_unique_path(
            self.test_dir, self.NFC_NAME, existing_names=existing
        )
        self.assertNotEqual(result.name, self.NFC_NAME)
        self.assertTrue(result.name.startswith("caf"))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestWindowsNames(unittest.TestCase):
    """Тесты зарезервированных имён Windows в конфиге."""

    def test_reserved_names_rejected(self):
        """con/nul/com1/lpt1 и т.п. не могут быть категориями."""
        from organizer_core import _validate_category_name

        for name in ("con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"):
            self.assertFalse(_validate_category_name(name), name)

    def test_reserved_check_is_case_insensitive(self):
        """Регистр не спасает: 'CON' тоже зарезервировано."""
        from organizer_core import _validate_category_name

        self.assertFalse(_validate_category_name("CON"))
        self.assertFalse(_validate_category_name("Lpt1"))

    def test_similar_names_accepted(self):
        """Похожие, но не зарезервированные имена разрешены."""
        from organizer_core import _validate_category_name

        for name in ("console", "com10", "lpt10", "nulll", "aux_files", "prn2"):
            self.assertTrue(_validate_category_name(name), name)

    def test_config_with_reserved_category_rejected(self):
        """Конфиг с категорией 'con' не проходит валидацию схемы."""
        from organizer_core import _validate_config_schema

        cfg = {"categories": {"con": ["txt"]}, "settings": {}}
        self.assertFalse(_validate_config_schema(cfg))


class TestPathLengthGuard(unittest.TestCase):
    """Тесты защиты от путей длиннее лимита ОС (Windows MAX_PATH, macOS)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_short_path_is_fine(self):
        from organizer_core import _path_too_long

        self.assertFalse(_path_too_long(self.test_dir / "a.txt"))

    def test_limit_depends_on_platform(self):
        from organizer_core import _max_path_length

        limit = _max_path_length()
        if os.name == "nt":
            self.assertEqual(limit, 259)
        elif sys.platform == "darwin":
            self.assertEqual(limit, 1024)
        else:
            self.assertIsNone(limit)  # на Linux лимита длины пути нет

    def test_long_path_detected_when_limit_is_small(self):
        """При маленьком лимите длинный путь считается превышающим."""
        from unittest import mock

        import organizer_core as oc

        long_path = self.test_dir / ("x" * 300)
        with mock.patch.object(oc, "_max_path_length", return_value=10):
            self.assertTrue(oc._path_too_long(long_path))

        # Без патча ответ платформенный: на Windows лимит 259 символов и такой
        # путь действительно длинный, на macOS (1024) и Linux (нет лимита) — нет.
        limit = oc._max_path_length()
        expected = limit is not None and len(str(long_path)) > limit
        self.assertEqual(oc._path_too_long(long_path), expected)

    def test_organize_skips_too_long_paths_without_data_loss(self):
        """Файлы с превышением лимита не перемещаются и не теряются."""
        from unittest import mock

        import organizer_core as oc

        (self.test_dir / "a.txt").write_text("content A")
        (self.test_dir / "b.txt").write_text("content B")

        with mock.patch.object(oc, "_max_path_length", return_value=1):
            stats = organize_files(str(self.test_dir), verbose=False)

        self.assertEqual(stats["moved"], 0)
        self.assertGreaterEqual(stats["errors"], 2)
        # Оба файла на месте, содержимое цело
        self.assertEqual((self.test_dir / "a.txt").read_text(), "content A")
        self.assertEqual((self.test_dir / "b.txt").read_text(), "content B")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestEncodingSafeStream(unittest.TestCase):
    """Тесты устойчивости логов к узким консольным кодировкам (Windows cp1252)."""

    @staticmethod
    def _narrow_stream() -> tuple:
        """TextIOWrapper в cp1252 — как консоль Windows по умолчанию.

        newline="" — иначе Windows-версия TextIOWrapper сам переводит \n в \r\n.
        """
        buf = io_mod.BytesIO()
        return io_mod.TextIOWrapper(buf, encoding="cp1252", newline=""), buf

    def test_cyrillic_does_not_crash_narrow_stream(self):
        """Печать кириллицы в cp1252 не падает с UnicodeEncodeError."""
        from organizer_core import _EncodingSafeStream

        stream, buf = self._narrow_stream()
        safe = _EncodingSafeStream(stream)
        safe.write("Перемещено: 2\n")  # раньше → UnicodeEncodeError
        safe.flush()
        stream.flush()
        self.assertIn(b"2", buf.getvalue())

    def test_box_drawing_and_emoji_replaced(self):
        """Рамки и эмодзи заменяются, а не роняют запись."""
        from organizer_core import _EncodingSafeStream

        stream, buf = self._narrow_stream()
        safe = _EncodingSafeStream(stream)
        safe.write("── ✓ файл ➜ папка\n")
        safe.flush()
        stream.flush()
        self.assertTrue(buf.getvalue())  # что-то записалось без исключения

    def test_attributes_are_proxied(self):
        """Прочие атрибуты потока доступны через обёртку."""
        from organizer_core import _EncodingSafeStream

        stream, _ = self._narrow_stream()
        safe = _EncodingSafeStream(stream)
        self.assertEqual(safe.encoding, "cp1252")
        self.assertFalse(safe.isatty())

    def test_writes_pass_through_for_wide_encodings(self):
        """Для широкой кодировки текст пишется без изменений."""
        from organizer_core import _EncodingSafeStream

        buf = io_mod.BytesIO()
        wide = io_mod.TextIOWrapper(buf, encoding="utf-8", newline="")
        safe = _EncodingSafeStream(wide)
        safe.write("Перемещено: 2\n")
        safe.flush()
        wide.flush()
        self.assertEqual(buf.getvalue().decode("utf-8"), "Перемещено: 2\n")


class TestConsoleEncodingHardening(unittest.TestCase):
    """Тесты _harden_console_encoding — вывод CLI в узких локалях (Windows)."""

    class _FakeStream:
        """Поток-заглушка, запоминающий вызовы reconfigure()."""

        def __init__(self, is_tty: bool, encoding: str = "cp1252") -> None:
            self._is_tty = is_tty
            self.encoding = encoding
            self.calls: list = []

        def isatty(self) -> bool:
            return self._is_tty

        def reconfigure(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    def _call(self, stdout: object, stderr: object) -> None:
        from unittest import mock

        import organizer_core as oc

        with mock.patch.object(oc.sys, "stdout", stdout), \
                mock.patch.object(oc.sys, "stderr", stderr):
            oc._harden_console_encoding()

    def test_pipe_output_switches_to_utf8(self):
        """Перенаправленный вывод → UTF-8, чтобы кириллица писалась как есть."""
        out = self._FakeStream(is_tty=False)
        err = self._FakeStream(is_tty=False)
        self._call(out, err)
        for stream in (out, err):
            self.assertEqual(stream.calls, [{"encoding": "utf-8", "errors": "replace"}])

    def test_tty_keeps_its_encoding(self):
        """Консоль не переводим на UTF-8 — только страхуемся от исключений."""
        out = self._FakeStream(is_tty=True, encoding="cp866")
        err = self._FakeStream(is_tty=True, encoding="cp866")
        self._call(out, err)
        for stream in (out, err):
            self.assertEqual(stream.calls, [{"errors": "replace"}])
            self.assertEqual(stream.encoding, "cp866")

    def test_stream_without_reconfigure_is_skipped(self):
        """Потоки без reconfigure (StringIO) не ломают настройку."""
        class _Plain:
            encoding = "utf-8"

            def isatty(self) -> bool:
                return False

        self._call(_Plain(), _Plain())  # не должно бросать

    def test_reconfigure_errors_are_suppressed(self):
        """Ошибка внутри reconfigure не роняет запуск программы."""

        class _Broken:
            """Поток, у которого reconfigure всегда падает."""

            encoding = "cp1252"

            def isatty(self) -> bool:
                return False

            def reconfigure(self, **kwargs: object) -> None:
                raise ValueError("stream уже используется")

        self._call(_Broken(), _Broken())  # не должно бросать

    def test_cli_writes_cyrillic_despite_narrow_pipe(self):
        """Регрессия: CLI под cp1252-пайпом печатает читаемый UTF-8, а не escape."""
        import subprocess as sp

        env = {**os.environ, "PYTHONIOENCODING": "cp1252:strict"}
        cli = str(Path(__file__).parent / "file_organizer.py")

        with tempfile.TemporaryDirectory() as d:
            missing = str(Path(d) / "no_such_dir")
            r = sp.run(
                [sys.executable, cli, missing],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace", env=env,
            )
        self.assertEqual(r.returncode, 1)
        self.assertIn("не найдена", r.stderr)
        self.assertNotIn("\\u041e", r.stderr)

    def test_cli_summary_is_readable_under_narrow_pipe(self):
        """Итоговая статистика CLI тоже переживает узкую кодировку."""
        import subprocess as sp

        env = {**os.environ, "PYTHONIOENCODING": "cp1252:strict"}
        cli = str(Path(__file__).parent / "file_organizer.py")

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "photo.jpg").write_text("jpg")
            r = sp.run(
                [sys.executable, cli, d],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace", env=env,
            )
            self.assertEqual(r.returncode, 0)
            self.assertIn("Перемещено", r.stdout)
            self.assertTrue((Path(d) / "images" / "photo.jpg").exists())


class TestPortableJournalPaths(unittest.TestCase):
    """Тесты переносимости журнала отмены между ОС."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        (self.test_dir / "a.txt").write_text("a")
        (self.test_dir / "sub").mkdir()
        (self.test_dir / "sub" / "b.pdf").write_text("b")

    def test_journal_uses_posix_separators(self):
        """Пути в журнале — в POSIX-виде (без '\\'), читаемые на любой ОС."""
        organize_files(str(self.test_dir), verbose=False)
        log_path = self.test_dir / UNDO_LOG_FILENAME
        raw = log_path.read_text(encoding="utf-8")
        self.assertNotIn("\\", raw, "в журнале есть обратные слеши — не переносимо")

        for line in raw.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            self.assertNotIn("\\", entry["source"])
            self.assertNotIn("\\", entry["destination"])

    def test_backup_entries_are_posix_too(self):
        """Записи о бэкапах (replace) тоже в POSIX-виде."""
        import copy as _copy

        (self.test_dir / "documents").mkdir()
        (self.test_dir / "documents" / "a.txt").write_text("OLD")
        cfg = _copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["duplicate_strategy"] = "replace"

        organize_files(str(self.test_dir), verbose=False, config=cfg)
        raw = (self.test_dir / UNDO_LOG_FILENAME).read_text(encoding="utf-8")
        for line in raw.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("action") == "backup":
                self.assertNotIn("\\", entry["source"])
                self.assertNotIn("\\", entry["destination"])

    def test_undo_still_works_with_posix_paths(self):
        """Откат корректно читает POSIX-пути из журнала."""
        organize_files(str(self.test_dir), verbose=False)
        stats = undo_last_operation(str(self.test_dir), verbose=False)
        self.assertEqual(stats["restored"], 2)
        self.assertEqual(stats["errors"], 0)
        self.assertTrue((self.test_dir / "a.txt").exists())
        self.assertTrue((self.test_dir / "sub" / "b.pdf").exists())

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestConcurrentOrganize(unittest.TestCase):
    """Тест защиты от параллельных organize_files (баг с гонками в мониторинге)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        for i in range(20):
            (self.test_dir / f"file_{i}.txt").write_text(f"content {i}")

    def test_concurrent_calls_dont_corrupt_log(self):
        """
        Параллельные вызовы organize_files не должны повредить undo-лог.

        M3 fix: тест проверяет ИНВАРИАНТ, а не тайминг. Ранее он требовал,
        чтобы ровно один вызов «отработал», и падал, когда потоки успевали
        выполниться последовательно (0 файлов у одного из них) — флаки.
        Корректный инвариант:
        - суммарно перемещены все 20 файлов ровно один раз;
        - undo-журнал содержит ровно 20 записей без дублей;
        - не осталось файлов в корне папки;
        - ни один вызов не вернул повреждённую статистику.
        """
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(organize_files, str(self.test_dir), verbose=False)
                for _ in range(4)
            ]
            results = [f.result() for f in futures]

        # Каждый вызов вернул валидную статистику, безотносительно тайминга
        for r in results:
            for key in ("moved", "skipped", "errors", "duplicates"):
                self.assertIn(key, r)
                self.assertIsInstance(r[key], int)

        # Инвариант №1: все 20 файлов перемещены ровно один раз
        total_moved = sum(r["moved"] for r in results)
        self.assertEqual(total_moved, 20)

        # Инвариант №2: в целевой папке ровно 20 уникальных файлов
        moved_files = sorted((self.test_dir / "documents").iterdir())
        self.assertEqual(len(moved_files), 20)
        self.assertEqual(len({f.name for f in moved_files}), 20)

        # Инвариант №3: в корне не осталось необработанных файлов
        leftover = [
            p.name for p in self.test_dir.iterdir()
            if p.is_file() and p.name != UNDO_LOG_FILENAME
        ]
        self.assertEqual(leftover, [])

        # Инвариант №4: undo-журнал содержит ровно 20 записей, все валидные
        from organizer_core import _load_undo_log
        entries = _load_undo_log(self.test_dir)
        self.assertEqual(len(entries), 20)
        for entry in entries:
            self.assertEqual(entry.get("action"), "move")
            self.assertIn("source", entry)
            self.assertIn("destination", entry)
        # Ни одна запись не дублируется по целевому пути
        destinations = [e["destination"] for e in entries]
        self.assertEqual(len(destinations), len(set(destinations)))

        # Инвариант №5: полный undo полностью откатывает сортировку
        undo_stats = undo_last_operation(str(self.test_dir), verbose=False)
        self.assertEqual(undo_stats["restored"], 20)
        for i in range(20):
            self.assertTrue((self.test_dir / f"file_{i}.txt").exists())

    def test_different_dirs_do_not_block_each_other(self):
        """M4: блокировка по папке — сортировка dir2 не блокируется сортировкой dir1."""
        import concurrent.futures

        dir1 = Path(tempfile.mkdtemp())
        dir2 = Path(tempfile.mkdtemp())
        try:
            for d in (dir1, dir2):
                for i in range(10):
                    (d / f"f_{i}.txt").write_text("x")

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(organize_files, str(dir1), verbose=False)
                f2 = pool.submit(organize_files, str(dir2), verbose=False)
                r1, r2 = f1.result(), f2.result()

            # Ни один из вызовов не должен быть «пропущен из-за конкурентности»
            self.assertFalse(r1.get("skipped_concurrent", False))
            self.assertFalse(r2.get("skipped_concurrent", False))
            self.assertEqual(r1["moved"], 10)
            self.assertEqual(r2["moved"], 10)
        finally:
            shutil.rmtree(dir1, ignore_errors=True)
            shutil.rmtree(dir2, ignore_errors=True)

    def test_concurrent_organize_and_undo_safe(self):
        """M2: одновременные organize и undo не должны повреждать журнал."""
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(organize_files, str(self.test_dir), verbose=False),
                pool.submit(undo_last_operation, str(self.test_dir), -1, False),
                pool.submit(organize_files, str(self.test_dir), verbose=False),
                pool.submit(undo_last_operation, str(self.test_dir), -1, False),
            ]
            # Ни один вызов не должен выбросить исключение
            for f in futures:
                f.result()

        # Журнал либо пуст, либо содержит только валидные записи
        from organizer_core import _load_undo_log
        entries = _load_undo_log(self.test_dir)
        for entry in entries:
            self.assertEqual(entry.get("action"), "move")
            self.assertIn("source", entry)
            self.assertIn("destination", entry)

        # Суммарно все 20 файлов на месте: либо в корне, либо в категории — без потерь
        in_root = {
            p.name for p in self.test_dir.iterdir()
            if p.is_file() and p.name != UNDO_LOG_FILENAME
        }
        in_docs = (
            {p.name for p in (self.test_dir / "documents").iterdir()}
            if (self.test_dir / "documents").exists()
            else set()
        )
        self.assertEqual(len(in_root | in_docs), 20)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestCategoryResort(unittest.TestCase):
    """Тесты M1: файлы внутри папок-категорий пересортируются по факту."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_miscategorized_file_resorted(self):
        """photo.jpg внутри videos/ должен переехать в images/ (M1)."""
        (self.test_dir / "videos").mkdir()
        (self.test_dir / "videos" / "photo.jpg").write_text("jpg")

        stats = organize_files(str(self.test_dir), verbose=False)
        self.assertEqual(stats["moved"], 1)
        self.assertTrue((self.test_dir / "images" / "photo.jpg").exists())
        self.assertFalse((self.test_dir / "videos" / "photo.jpg").exists())

    def test_correctly_placed_file_not_touched(self):
        """Файл в своей категории (в т.ч. во вложенной папке) не трогается (M1)."""
        (self.test_dir / "documents" / "sub").mkdir(parents=True)
        (self.test_dir / "documents" / "sub" / "note.txt").write_text("txt")
        (self.test_dir / "audio").mkdir()
        (self.test_dir / "audio" / "song.mp3").write_text("mp3")

        stats = organize_files(str(self.test_dir), verbose=False)
        self.assertEqual(stats["moved"], 0)
        self.assertTrue((self.test_dir / "documents" / "sub" / "note.txt").exists())
        self.assertTrue((self.test_dir / "audio" / "song.mp3").exists())

    def test_second_run_is_noop(self):
        """Повторный прогон не должен «пинг-понговать» файлами (M1)."""
        (self.test_dir / "videos").mkdir()
        (self.test_dir / "videos" / "photo.jpg").write_text("jpg")
        organize_files(str(self.test_dir), verbose=False)

        stats2 = organize_files(str(self.test_dir), verbose=False)
        self.assertEqual(stats2["moved"], 0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestUndoCountValidation(unittest.TestCase):
    """Тесты валидации count в undo_last_operation (C2)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        (self.test_dir / "a.txt").write_text("a")
        (self.test_dir / "b.txt").write_text("b")
        organize_files(str(self.test_dir), verbose=False)

    def test_undo_count_zero_restores_nothing(self):
        """count=0 не должен отменять НИЧЕГО и не портить журнал (C2)."""
        before = get_undo_count(str(self.test_dir))
        stats = undo_last_operation(str(self.test_dir), count=0, verbose=False)
        self.assertEqual(stats["restored"], 0)
        self.assertEqual(stats["errors"], 0)
        # Журнал должен остаться нетронутым
        self.assertEqual(get_undo_count(str(self.test_dir)), before)
        # Файлы всё ещё в категории
        self.assertTrue((self.test_dir / "documents" / "a.txt").exists())

    def test_undo_negative_other_than_minus_one_is_noop(self):
        """count=-5 не должен молча ничего делать с порчей состояния (C2)."""
        before = get_undo_count(str(self.test_dir))
        stats = undo_last_operation(str(self.test_dir), count=-5, verbose=False)
        self.assertEqual(stats["restored"], 0)
        self.assertEqual(get_undo_count(str(self.test_dir)), before)

    def test_undo_count_greater_than_available(self):
        """count > доступных операций отменяет все доступные (C2)."""
        stats = undo_last_operation(str(self.test_dir), count=999, verbose=False)
        self.assertEqual(stats["restored"], 2)
        self.assertEqual(get_undo_count(str(self.test_dir)), 0)

    def test_undo_minus_one_restores_all(self):
        """count=-1 по-прежнему отменяет всё (C2)."""
        stats = undo_last_operation(str(self.test_dir), count=-1, verbose=False)
        self.assertEqual(stats["restored"], 2)
        self.assertEqual(get_undo_count(str(self.test_dir)), 0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestUndoDoesNotOverwrite(unittest.TestCase):
    """Тесты защиты от перезаписи файлов при undo (C3)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_undo_does_not_overwrite_new_user_file(self):
        """Undo не должен молча затирать новый файл на исходном месте (C3)."""
        (self.test_dir / "a.txt").write_text("ORIGINAL")
        organize_files(str(self.test_dir), verbose=False)

        # Пользователь создал новый файл с тем же именем
        (self.test_dir / "a.txt").write_text("NEW USER FILE")

        stats = undo_last_operation(str(self.test_dir), verbose=False)
        self.assertEqual(stats["restored"], 1)
        self.assertEqual(stats["errors"], 0)

        # Новый файл НЕ должен быть перезаписан
        self.assertEqual((self.test_dir / "a.txt").read_text(), "NEW USER FILE")
        # Старый файл должен быть восстановлен под другим именем
        candidates = [
            p for p in self.test_dir.iterdir()
            if p.is_file() and p.name not in ("a.txt", UNDO_LOG_FILENAME)
        ]
        self.assertTrue(candidates, "Восстановленный файл не найден")
        self.assertEqual(candidates[0].read_text(), "ORIGINAL")

    def test_undo_skip_strategy_leaves_both(self):
        """При duplicate_strategy=skip undo не трогает существующий файл (C3)."""
        import copy as _copy
        cfg = _copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["duplicate_strategy"] = "skip"

        (self.test_dir / "a.txt").write_text("ORIGINAL")
        organize_files(str(self.test_dir), verbose=False, config=cfg)
        (self.test_dir / "a.txt").write_text("NEW USER FILE")

        stats = undo_last_operation(
            str(self.test_dir), verbose=False, config=cfg
        )
        # Файл не восстановлен (пропущен) — но и не потерян
        self.assertEqual((self.test_dir / "a.txt").read_text(), "NEW USER FILE")
        self.assertEqual(stats["errors"], 1)
        # Неудачная запись остаётся в журнале для повторной попытки
        self.assertGreaterEqual(get_undo_count(str(self.test_dir)), 1)

    def test_undo_restores_normally_when_no_conflict(self):
        """Обычный undo без конфликта продолжает работать."""
        (self.test_dir / "a.txt").write_text("ORIGINAL")
        organize_files(str(self.test_dir), verbose=False)
        stats = undo_last_operation(str(self.test_dir), verbose=False)
        self.assertEqual(stats["restored"], 1)
        self.assertEqual((self.test_dir / "a.txt").read_text(), "ORIGINAL")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestMalformedUndoEntries(unittest.TestCase):
    """Тесты устойчивости undo к семантически неполным записям (H4)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        (self.test_dir / "a.txt").write_text("a")

    def test_entry_without_destination_skipped(self):
        """Запись без destination не должна ронять undo с KeyError (H4)."""
        log_path = self.test_dir / UNDO_LOG_FILENAME
        log_path.write_text(
            '{"action": "move", "source": "a.txt", "original_name": "a.txt"}\n',
            encoding="utf-8",
        )
        stats = undo_last_operation(str(self.test_dir), verbose=False)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["restored"], 0)

    def test_entry_without_source_skipped(self):
        """Запись без source не должна ронять undo (H4)."""
        log_path = self.test_dir / UNDO_LOG_FILENAME
        log_path.write_text(
            '{"action": "move", "destination": "documents/a.txt", "original_name": "a.txt"}\n',
            encoding="utf-8",
        )
        stats = undo_last_operation(str(self.test_dir), verbose=False)
        self.assertEqual(stats["errors"], 1)

    def test_valid_entries_processed_alongside_broken(self):
        """Валидные записи обрабатываются, несмотря на битую."""
        (self.test_dir / "b.pdf").write_text("b")
        organize_files(str(self.test_dir), verbose=False)
        # Дописываем битую запись в конец
        log_path = self.test_dir / UNDO_LOG_FILENAME
        with open(log_path, "a", encoding="utf-8") as f:
            f.write('{"action": "move", "original_name": "broken"}\n')
        stats = undo_last_operation(str(self.test_dir), verbose=False)
        self.assertEqual(stats["restored"], 2)
        self.assertEqual(stats["errors"], 1)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestCategorySafety(unittest.TestCase):
    """Тесты безопасности имён категорий и целей (H1)."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_category_name_traversal_rejected_by_schema(self):
        """Имя категории с '..' не проходит валидацию схемы (H1)."""
        from organizer_core import _validate_config_schema
        cfg = {
            "categories": {"../escaped": ["pdf"]},
            "settings": {},
        }
        self.assertFalse(_validate_config_schema(cfg))

    def test_category_name_absolute_path_rejected(self):
        """Абсолютный путь как имя категории отклоняется (H1)."""
        from organizer_core import _validate_config_schema
        cfg = {"categories": {"/tmp/escaped": ["pdf"]}, "settings": {}}
        self.assertFalse(_validate_config_schema(cfg))

    def test_category_name_with_slash_rejected(self):
        """Имя категории со слешем отклоняется (H1)."""
        from organizer_core import _validate_config_schema
        cfg = {"categories": {"sub/dir": ["pdf"]}, "settings": {}}
        self.assertFalse(_validate_config_schema(cfg))

    def test_normal_category_names_accepted(self):
        """Обычные имена категорий проходят валидацию."""
        from organizer_core import _validate_config_schema
        self.assertTrue(_validate_config_schema(DEFAULT_CONFIG))

    def test_symlink_category_dir_does_not_escape(self):
        """Симлинк-категория наружу не должна уводить файлы за пределы папки (H1)."""
        outside = Path(tempfile.mkdtemp())
        try:
            (self.test_dir / "documents").symlink_to(outside)
            (self.test_dir / "report.pdf").write_text("PDF")
            stats = organize_files(str(self.test_dir), verbose=False)
            self.assertEqual(stats["moved"], 0)
            self.assertGreaterEqual(stats["errors"], 1)
            # Ничего не должно оказаться снаружи
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestCategoryDirBlockedByFile(unittest.TestCase):
    """Тест H3: файл с именем категории не должен ронять весь прогон."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_run_continues_when_category_dir_blocked(self):
        """Файл 'other' блокирует категорию other, остальные обрабатываются (H3).

        Файл без расширения с именем категории сам попадает в категорию `other`
        и пытается создать папку `other` — mkdir падает с FileExistsError.
        Прогон обязан продолжиться, посчитав ошибку, а не прерваться.
        """
        (self.test_dir / "other").write_text("blocker-file")
        (self.test_dir / "unknown.xyz").write_text("unknown")  # → other (заблокировано)
        (self.test_dir / "photo.jpg").write_text("JPG")        # → images (работает)

        stats = organize_files(str(self.test_dir), verbose=False)
        # Прогон не упал, статистика вернулась
        self.assertIsInstance(stats, dict)
        # Картинка обработана
        self.assertTrue((self.test_dir / "images" / "photo.jpg").exists())
        # Ошибка по заблокированной категории посчитана
        self.assertGreaterEqual(stats["errors"], 1)
        # Файл-блокер цел, неперемещённый файл тоже
        self.assertEqual((self.test_dir / "other").read_text(), "blocker-file")
        self.assertTrue((self.test_dir / "unknown.xyz").exists())

    def test_file_named_category_is_not_falsely_skipped(self):
        """Файл в корне с именем категории сортируется, а не молча пропускается (M1)."""
        (self.test_dir / "documents").write_text("not a folder")
        stats = organize_files(str(self.test_dir), verbose=False)
        # Файл без расширения → other; не должен «залипнуть» из-за имени
        self.assertEqual(stats["moved"], 1)
        self.assertTrue((self.test_dir / "other" / "documents").exists())

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestReplaceStrategyReversible(unittest.TestCase):
    """Тест H2: replace создаёт бэкап, заменённый файл не теряется."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        (self.test_dir / "documents").mkdir()
        (self.test_dir / "documents" / "report.pdf").write_text("OLD CONTENT")
        (self.test_dir / "report.pdf").write_text("NEW CONTENT")

    def _cfg(self, strategy: str) -> dict[str, Any]:
        import copy as _copy
        cfg = _copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["duplicate_strategy"] = strategy
        return cfg

    def test_replace_creates_backup(self):
        """При replace старый файл не уничтожается, а сохраняется как бэкап (H2)."""
        stats = organize_files(
            str(self.test_dir), verbose=False, config=self._cfg("replace")
        )
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(
            (self.test_dir / "documents" / "report.pdf").read_text(), "NEW CONTENT"
        )
        backups = list((self.test_dir / "documents").glob("report.pdf.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "OLD CONTENT")

    def test_replace_backup_recorded_in_undo_log(self):
        """Информация о бэкапе записывается в undo-журнал (H2)."""
        organize_files(str(self.test_dir), verbose=False, config=self._cfg("replace"))
        from organizer_core import _load_undo_log
        entries = _load_undo_log(self.test_dir)
        backup_entries = [e for e in entries if e.get("action") == "backup"]
        self.assertEqual(len(backup_entries), 1)
        self.assertIn("report.pdf.bak-", backup_entries[0]["source"])

    def test_replace_is_fully_reversible_by_undo(self):
        """Полный undo после replace возвращает и старый, и новый файл (H2)."""
        organize_files(
            str(self.test_dir), verbose=False, config=self._cfg("replace")
        )
        stats = undo_last_operation(
            str(self.test_dir), verbose=False, config=self._cfg("replace")
        )
        # Восстановлены оба: и перемещённый файл, и заменённый (из бэкапа)
        self.assertEqual(stats["restored"], 2)
        self.assertEqual(stats["errors"], 0)

        self.assertEqual((self.test_dir / "report.pdf").read_text(), "NEW CONTENT")
        self.assertEqual(
            (self.test_dir / "documents" / "report.pdf").read_text(), "OLD CONTENT"
        )
        # Бэкап-файл не остаётся мусором
        self.assertEqual(list((self.test_dir / "documents").glob("*.bak-*")), [])

    def test_replace_partial_undo_keeps_backup(self):
        """Частичный undo (только move) не теряет бэкап и не ломает журнал (H2)."""
        organize_files(
            str(self.test_dir), verbose=False, config=self._cfg("replace")
        )
        # Отменяем только последнюю операцию (перемещение нового файла)
        undo_last_operation(
            str(self.test_dir), count=1, verbose=False, config=self._cfg("replace")
        )
        # Бэкап остался на месте — данные не потеряны
        backups = list((self.test_dir / "documents").glob("report.pdf.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "OLD CONTENT")

        # Повторный полный undo доводит восстановление до конца
        stats = undo_last_operation(
            str(self.test_dir), verbose=False, config=self._cfg("replace")
        )
        self.assertEqual(stats["restored"], 1)
        self.assertEqual(
            (self.test_dir / "documents" / "report.pdf").read_text(), "OLD CONTENT"
        )

    def test_two_replaces_same_second_keep_both_backups(self):
        """Два replace за одну секунду не должны затирать бэкапы друг друга (H2).

        Порядок обработки файлов недетерминирован (стековый обход), поэтому
        проверяем инвариант сохранности данных: содержимое всех трёх файлов
        (заменённого и обоих источников) должно присутствовать на диске.
        """
        sub = self.test_dir / "sub"
        sub.mkdir()
        # Второй файл с тем же именем, который тоже попадёт в documents/
        (self.test_dir / "documents" / "report.pdf").write_text("OLD CONTENT")
        (sub / "report.pdf").write_text("SECOND CONTENT")

        stats = organize_files(
            str(self.test_dir), verbose=False, config=self._cfg("replace")
        )
        self.assertEqual(stats["errors"], 0)

        backups = sorted((self.test_dir / "documents").glob("report.pdf*.bak-*"))
        # Каждый replace сохранил свой бэкап: два файла + текущий = три содержимых
        self.assertEqual(len(backups), 2)
        contents = {b.read_text() for b in backups}
        contents.add((self.test_dir / "documents" / "report.pdf").read_text())
        self.assertEqual(contents, {"OLD CONTENT", "SECOND CONTENT", "NEW CONTENT"})

    def test_double_replace_fully_reversible(self):
        """Полный undo после двух replace восстанавливает исходное состояние (H2)."""
        sub = self.test_dir / "sub"
        sub.mkdir()
        (self.test_dir / "documents" / "report.pdf").write_text("OLD CONTENT")
        (sub / "report.pdf").write_text("SECOND CONTENT")

        organize_files(
            str(self.test_dir), verbose=False, config=self._cfg("replace")
        )
        undo_last_operation(
            str(self.test_dir), verbose=False, config=self._cfg("replace")
        )

        self.assertEqual((self.test_dir / "report.pdf").read_text(), "NEW CONTENT")
        self.assertEqual((sub / "report.pdf").read_text(), "SECOND CONTENT")
        self.assertEqual(
            (self.test_dir / "documents" / "report.pdf").read_text(), "OLD CONTENT"
        )
        self.assertEqual(list((self.test_dir / "documents").glob("*.bak-*")), [])

    def test_replace_without_existing_file_still_works(self):
        """Обычный replace без бэкапа (файла не было) работает как раньше."""
        (self.test_dir / "documents" / "report.pdf").unlink()
        stats = organize_files(
            str(self.test_dir), verbose=False, config=self._cfg("replace")
        )
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(
            (self.test_dir / "documents" / "report.pdf").read_text(), "NEW CONTENT"
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestConfigValidation(unittest.TestCase):
    """Тесты валидации конфига (H4)."""

    def test_non_dict_config_rejected(self):
        """Конфиг-массив не проходит валидацию (H4)."""
        from organizer_core import _validate_config_schema
        self.assertFalse(_validate_config_schema(["not a dict"]))

    def test_category_exts_must_be_list(self):
        """Строка вместо списка расширений отклоняется (H4)."""
        from organizer_core import _validate_config_schema
        cfg = {"categories": {"images": "jpg"}, "settings": {}}
        self.assertFalse(_validate_config_schema(cfg))

    def test_category_exts_must_be_strings(self):
        """Нестроковые расширения отклоняются (H4)."""
        from organizer_core import _validate_config_schema
        cfg = {"categories": {"images": [123]}, "settings": {}}
        self.assertFalse(_validate_config_schema(cfg))

    def test_invalid_strategy_rejected(self):
        """Невалидная duplicate_strategy отклоняется (H4)."""
        from organizer_core import _validate_config_schema
        cfg = {"categories": {"other": []}, "settings": {"duplicate_strategy": "xyz"}}
        self.assertFalse(_validate_config_schema(cfg))

    def test_invalid_max_file_size_rejected(self):
        """Строковый max_file_size отклоняется (H4)."""
        from organizer_core import _validate_config_schema
        cfg = {"categories": {"other": []}, "settings": {"max_file_size": "1GB"}}
        self.assertFalse(_validate_config_schema(cfg))

    def test_invalid_config_param_raises(self):
        """Невалидный config, переданный параметром, не должен молча игнорироваться (H4)."""
        with tempfile.TemporaryDirectory() as d:
            test_dir = Path(d)
            (test_dir / "a.txt").write_text("a")
            bad_cfg = {"categories": {"images": "jpg"}, "settings": {}}
            with self.assertRaises(ValueError):
                organize_files(str(test_dir), verbose=False, config=bad_cfg)

    def test_organize_rejects_protected_dir(self):
        """H5: корень ФС запрещён как рабочая директория."""
        if os.name == "nt":
            self.skipTest("Unix-specific")
        with self.assertRaises(ValueError):
            organize_files("/", verbose=False)


class TestProtectedDirHardening(unittest.TestCase):
    """Тесты H5: защита корня ФС, /root, домашнего каталога."""

    def test_root_is_protected(self):
        if os.name == "nt":
            self.skipTest("Unix-specific")
        self.assertTrue(is_protected_dir(Path("/")))

    def test_home_root_is_protected(self):
        """H5: /home защищён как цель."""
        if os.name == "nt":
            self.skipTest("Unix-specific")
        self.assertTrue(is_protected_dir(Path("/home")))

    def test_user_home_is_protected(self):
        """H5: домашний каталог пользователя защищён."""
        self.assertTrue(is_protected_dir(Path.home()))

    def test_user_subdirectory_not_protected(self):
        """H5: обычные пользовательские подпапки НЕ защищены."""
        if os.name == "nt":
            self.skipTest("Unix-specific")
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "sub"
            sub.mkdir()
            self.assertFalse(is_protected_dir(sub))

    def test_tmp_subdir_not_protected(self):
        """H5: подпапки /tmp — пользовательские, не защищены."""
        if os.name == "nt":
            self.skipTest("Unix-specific")
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(is_protected_dir(Path(d)))


class TestProtectedDirResolution(unittest.TestCase):
    """Защита системных каталогов с учётом симлинков (macOS)."""

    def test_protected_sets_include_resolved_forms(self):
        """Регрессия (macOS): /etc и /var — симлинки в /private/... .

        Наборы защищённых путей обязаны содержать resolve()-формы, иначе
        organize_files("/etc") и organize_files("/var/log") на macOS
        проходили бы проверку — это дыра в защите.
        """
        import contextlib

        import organizer_core as oc

        combined = oc._PROTECTED_EXACT | oc._PROTECTED_TREES
        checked = 0
        for path in sorted(combined):
            if not path.exists():
                continue
            with contextlib.suppress(OSError, RuntimeError):
                checked += 1
                self.assertIn(
                    path.resolve(), combined,
                    f"resolve()-форма {path} → {path.resolve()} не защищена",
                )
        self.assertGreater(checked, 0, "не проверено ни одного пути")

    def test_symlinked_protected_path_is_still_protected(self):
        """Путь через симлинк на системный каталог защищён (как на macOS)."""
        from unittest import mock

        import organizer_core as oc

        root = Path(tempfile.mkdtemp())
        try:
            real = root / "private" / "etc"
            real.mkdir(parents=True)
            link = root / "etc"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("симлинки недоступны на этой системе")

            # resolve()-форма: на macOS /var → /private/var, на Windows temp
            # может отдаваться коротким именем 8.3 — сравнение идёт по ней.
            real_resolved = real.resolve()
            with mock.patch.object(oc, "_PROTECTED_EXACT", {real_resolved}), \
                    mock.patch.object(oc, "_PROTECTED_TREES", {real_resolved}), \
                    mock.patch.object(oc, "_USER_TEMP_TREES", set()):
                self.assertTrue(oc.is_protected_dir(link))
                self.assertTrue(oc.is_protected_dir(real / "passwd"))
                self.assertTrue(oc.is_protected_dir(real))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_macos_temp_inside_protected_var_is_allowed(self):
        """macOS: /var/folders — пользовательский temp, хотя лежит в /private/var.

        Без этого исключения tempfile.mkdtemp() на macOS считался бы защищённым
        и органайзер не смог бы рассортировать временную папку.
        """
        from unittest import mock

        import organizer_core as oc

        if os.name == "nt":
            self.skipTest("Пути /private/var — только POSIX (macOS)")
        fake_temp = Path("/private/var/folders/zz/T/tmp123")  # не создаём
        with mock.patch.object(oc, "_PROTECTED_TREES", {Path("/private/var")}):
            self.assertTrue(oc.is_protected_dir(Path("/private/var/log")))
            self.assertFalse(oc.is_protected_dir(fake_temp))


class TestMonitoring(unittest.TestCase):
    """Тесты мониторинга (H6) — настройки, старт/стоп без утечек."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.logs: list = []

    def _cb(self, msg: str) -> None:
        self.logs.append(msg)

    def test_start_stop_polling_completes(self):
        """Мониторинг в polling-режиме запускается и останавливается полностью (H6)."""
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["use_watchdog"] = False

        t = start_monitoring(
            str(self.test_dir), interval=1, callback=self._cb, config=cfg
        )
        self.assertTrue(t.is_alive())
        stop_monitoring()
        self.assertFalse(t.is_alive(), "Поток мониторинга не был остановлен")

    def test_config_none_does_not_force_watchdog(self):
        """start_monitoring(config=None) читает use_watchdog из конфига (H6)."""
        # Не должно бросать исключений даже без watchdog
        t = start_monitoring(str(self.test_dir), interval=1, callback=self._cb)
        stop_monitoring()
        self.assertFalse(t.is_alive())

    def test_polling_reacts_to_new_file(self):
        """Polling-мониторинг замечает новые файлы и сортирует их (H6)."""
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["use_watchdog"] = False

        start_monitoring(str(self.test_dir), interval=1, callback=self._cb, config=cfg)
        try:
            (self.test_dir / "new_photo.jpg").write_text("JPG")
            deadline = time.time() + 15
            moved = False
            while time.time() < deadline:
                if (self.test_dir / "images" / "new_photo.jpg").exists():
                    moved = True
                    break
                time.sleep(0.25)
            self.assertTrue(moved, f"Новый файл не отсортирован. Логи: {self.logs}")
        finally:
            stop_monitoring()

    def test_stop_cancels_pending_sort(self):
        """После stop_monitoring отложенная сортировка не выполняется (H6)."""
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["use_watchdog"] = False

        start_monitoring(str(self.test_dir), interval=1, callback=self._cb, config=cfg)
        (self.test_dir / "late_photo.jpg").write_text("JPG")
        stop_monitoring()

        # Даём время любому «хвосту» мониторинга выполниться
        time.sleep(2.5)
        self.assertFalse(
            (self.test_dir / "images" / "late_photo.jpg").exists(),
            "Сортировка запустилась после остановки мониторинга",
        )

    def test_debounce_scheduler_collapses_events(self):
        """Несколько событий подряд → один flush после debounce (H6)."""
        from organizer_core import _DebounceScheduler

        flushes: list = []
        sched = _DebounceScheduler(on_flush=lambda n: flushes.append(n))
        try:
            for i in range(5):
                sched.schedule(f"/tmp/dir/file_{i}.zip")
            self.assertEqual(sched.pending_count, 5)
            # Ждём срабатывания debounce (min 1.0 c)
            deadline = time.time() + 6
            while not flushes and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(flushes), 1, "Сортировка должна запуститься один раз")
            self.assertEqual(flushes[0], 5)
        finally:
            sched.cancel()

    def test_debounce_scheduler_cancel_prevents_flush(self):
        """cancel() отменяет отложенный flush — сортировка не запускается (H6)."""
        from organizer_core import _DebounceScheduler

        flushes: list = []
        sched = _DebounceScheduler(on_flush=lambda n: flushes.append(n))
        sched.schedule("/tmp/dir/late.zip")
        sched.cancel()
        time.sleep(1.5)
        self.assertEqual(flushes, [])
        # Новые события после cancel тоже игнорируются
        sched.schedule("/tmp/dir/after_cancel.zip")
        time.sleep(0.2)
        self.assertEqual(sched.pending_count, 0)

    def test_debounce_scheduler_skips_incomplete_downloads(self):
        """События по .crdownload/.part не планируют сортировку (H6)."""
        from organizer_core import _DebounceScheduler

        flushes: list = []
        sched = _DebounceScheduler(on_flush=lambda n: flushes.append(n))
        try:
            sched.schedule("/tmp/dir/big.zip.crdownload")
            sched.schedule("/tmp/dir/big.zip.part")
            sched.schedule("/tmp/dir/big.zip")
            self.assertEqual(sched.pending_count, 1)
        finally:
            sched.cancel()

    def test_debounce_respects_stop_flag(self):
        """Если мониторинг остановлен, новые события не планируются (H6)."""
        from organizer_core import _DebounceScheduler

        flushes: list = []
        sched = _DebounceScheduler(
            on_flush=lambda n: flushes.append(n), is_stopped=lambda: True
        )
        sched.schedule("/tmp/dir/file.zip")
        self.assertEqual(sched.pending_count, 0)
        time.sleep(0.1)
        self.assertEqual(flushes, [])

    def test_path_from_event_uses_dest_for_moved(self):
        """Для события moved берётся dest_path (H6)."""

        class _Evt:
            def __init__(self, src: str, dest: str = "") -> None:
                self.src_path = src
                self.dest_path = dest
                self.is_directory = False

        from organizer_core import _path_from_event

        self.assertEqual(_path_from_event(_Evt("/a/x.zip")), "/a/x.zip")
        self.assertEqual(
            _path_from_event(_Evt("/a/x.crdownload", "/a/x.zip")), "/a/x.zip"
        )

    def test_incomplete_suffix_detection(self):
        """Маркеры незавершённой загрузки определяются корректно (H6)."""
        from organizer_core import _is_incomplete_path

        self.assertTrue(_is_incomplete_path("/a/file.crdownload"))
        self.assertTrue(_is_incomplete_path("/a/file.PART"))
        self.assertTrue(_is_incomplete_path("/a/file.download"))
        self.assertFalse(_is_incomplete_path("/a/file.zip"))
        self.assertFalse(_is_incomplete_path("/a/partial.zip"))

    def test_file_is_stable_detects_unchanged(self):
        """_file_is_stable считает неизменяемый файл стабильным (H6)."""
        from organizer_core import _file_is_stable
        f = self.test_dir / "stable.bin"
        f.write_text("data")
        self.assertTrue(_file_is_stable(str(f), stable_seconds=0.1))

    def test_file_is_stable_detects_growth(self):
        """_file_is_stable замечает дозапись файла (H6)."""
        from organizer_core import _file_is_stable
        f = self.test_dir / "growing.bin"
        f.write_text("data")

        def grow() -> None:
            time.sleep(0.05)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write("more data")

        t = threading.Thread(target=grow)
        t.start()
        self.assertFalse(_file_is_stable(str(f), stable_seconds=0.3))
        t.join()

    def tearDown(self):
        stop_monitoring()
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestCli(unittest.TestCase):
    """Тесты CLI (C1): команды README должны работать без GUI/tkinter."""

    CLI = str(Path(__file__).parent / "file_organizer.py")

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        (self.test_dir / "photo.jpg").write_text("jpg")
        (self.test_dir / "doc.pdf").write_text("pdf")

    def _run(
        self, *args: str, env: "dict[str, str] | None" = None
    ) -> "subprocess.CompletedProcess[str]":
        """Запуск CLI. Вывод всегда читаем как UTF-8: на Windows труба по
        умолчанию отдаёт cp1252, и русский текст иначе не декодируется."""
        return subprocess.run(
            [sys.executable, self.CLI, *args],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace", env=env,
        )

    def test_cli_help(self):
        """--help печатает справку и завершается успешно (C1)."""
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)
        self.assertIn("--undo-count", r.stdout)

    def test_cli_dry_run_does_not_move(self):
        """--dry-run показывает план и ничего не перемещает (C1)."""
        r = self._run(str(self.test_dir), "--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.test_dir / "photo.jpg").exists())
        self.assertFalse((self.test_dir / "images").exists())

    def test_cli_organize_and_undo_info(self):
        """Сортировка и --undo-info работают через CLI (C1)."""
        r = self._run(str(self.test_dir), "-q")
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.test_dir / "images" / "photo.jpg").exists())

        r = self._run(str(self.test_dir), "--undo-info")
        self.assertEqual(r.returncode, 0)
        self.assertIn("2", r.stdout)

    def test_cli_undo_count(self):
        """--undo-count отменяет указанное число операций (C1)."""
        self._run(str(self.test_dir), "-q")
        r = self._run(str(self.test_dir), "--undo-count", "1", "-q")
        self.assertEqual(r.returncode, 0)
        # Один файл вернулся, второй остался в категории
        restored = (self.test_dir / "photo.jpg").exists() or (self.test_dir / "doc.pdf").exists()
        self.assertTrue(restored)

    def test_cli_no_recursive(self):
        """--no-recursive обрабатывает только корень папки (C1)."""
        sub = self.test_dir / "sub"
        sub.mkdir()
        (sub / "nested.jpg").write_text("jpg")

        self._run(str(self.test_dir), "--no-recursive", "-q")
        self.assertTrue((sub / "nested.jpg").exists())

    def test_cli_config_flag(self):
        """--config использует указанный файл вместо config.json (C1)."""
        cfg = self.test_dir / "custom.json"
        cfg.write_text(json.dumps({
            "categories": {"music": ["mp3"], "other": []},
            "settings": {"recursive": True},
        }), encoding="utf-8")
        (self.test_dir / "song.mp3").write_text("mp3")

        r = self._run(str(self.test_dir), "--config", str(cfg), "-q")
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.test_dir / "music" / "song.mp3").exists())

    def test_cli_missing_config_reports_error(self):
        """Несуществующий --config даёт понятную ошибку и код возврата 1 (C1)."""
        r = self._run(str(self.test_dir), "--config", str(self.test_dir / "nope.json"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("не найден", r.stderr)

    def test_cli_missing_dir_reports_error(self):
        """Несуществующая папка даёт ошибку и код возврата 1 (C1)."""
        r = self._run(str(self.test_dir / "no_such_dir"))
        self.assertEqual(r.returncode, 1)

    def test_cli_conflicting_operations_rejected(self):
        """--undo и --monitor вместе отклоняются (C1)."""
        r = self._run(str(self.test_dir), "--undo", "--monitor")
        self.assertEqual(r.returncode, 2)

    def test_cli_undo_count_zero_is_safe(self):
        """--undo-count 0 ничего не отменяет (C2 через CLI)."""
        self._run(str(self.test_dir), "-q")
        r = self._run(str(self.test_dir), "--undo-count", "0")
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.test_dir / "images" / "photo.jpg").exists())

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestScriptFilesScope(unittest.TestCase):
    """Тест M12: служебные имена пропускаются только в корне папки."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def test_script_file_in_root_skipped(self):
        """organizer_core.py в корне сортируемой папки не трогается (M12)."""
        (self.test_dir / "organizer_core.py").write_text("# core")
        (self.test_dir / "config.json").write_text("{}")
        stats = organize_files(str(self.test_dir), verbose=False)
        self.assertEqual(stats["moved"], 0)
        self.assertTrue((self.test_dir / "organizer_core.py").exists())

    def test_user_file_with_script_name_in_subfolder_sorted(self):
        """Пользовательский config.json в подпапке должен сортироваться (M12)."""
        sub = self.test_dir / "sub"
        sub.mkdir()
        (sub / "config.json").write_text("{}")

        stats = organize_files(str(self.test_dir), verbose=False)
        self.assertEqual(stats["moved"], 1)
        self.assertFalse((sub / "config.json").exists())
        self.assertTrue((self.test_dir / "code" / "config.json").exists())

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestProgressReporting(unittest.TestCase):
    """Тест M6: прогресс отражает реальные перемещения, а не только скан."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        for i in range(8):
            (self.test_dir / f"f{i}.txt").write_text("x")

    def test_progress_is_monotonic_and_reaches_100(self):
        """Прогресс монотонен и доходит до 100% только после перемещений (M6)."""
        events: list = []
        organize_files(
            str(self.test_dir),
            verbose=False,
            progress_callback=lambda c, t: events.append((c, t)),
        )
        self.assertTrue(events)
        ratios = [c / t for c, t in events]
        self.assertTrue(all(b >= a for a, b in zip(ratios, ratios[1:])), ratios)
        self.assertEqual(ratios[-1], 1.0)

    def test_progress_not_complete_before_moves(self):
        """Прогресс не должен быть 100% на этапе фильтрации (M6)."""
        events: list = []
        organize_files(
            str(self.test_dir),
            verbose=False,
            progress_callback=lambda c, t: events.append((c, t)),
        )
        # Первое событие — самое начало, задолго до 100%
        first_ratio = events[0][0] / events[0][1]
        self.assertLess(first_ratio, 0.5)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestScanErrors(unittest.TestCase):
    """Тест M8: недоступные подпапки учитываются в статистике."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    @unittest.skipIf(os.name == "nt", "нужны права non-root")
    def test_unreadable_subdir_counted(self):
        """Недоступная подпапка попадает в errors, а не исчезает молча (M8)."""
        if getattr(os, "geteuid", lambda: 1)() == 0:
            self.skipTest("тест не работает под root")
        locked = self.test_dir / "locked"
        locked.mkdir()
        (locked / "hidden.jpg").write_text("jpg")
        os.chmod(locked, 0o000)
        try:
            stats = organize_files(str(self.test_dir), verbose=False)
            self.assertGreaterEqual(stats["errors"], 1)
        finally:
            os.chmod(locked, 0o755)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestLogPathMasking(unittest.TestCase):
    """Тест M11: полный путь не попадает в лог."""

    def test_dir_masked_for_log(self):
        """Директория в логе маскируется (~/... или имя) (M11)."""
        from organizer_core import _mask_dir_for_log

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            masked = _mask_dir_for_log(root)
            # Абсолютный путь (с именем пользователя) в лог не попадает
            self.assertNotEqual(masked, str(root))
            if masked.startswith("~"):
                # Windows: temp лежит внутри домашней папки → сокращается до ~/...
                self.assertTrue(masked.startswith("~/") or masked.startswith("~\\"))
                self.assertNotIn(Path.home().name, masked.split("\\")[0] + "/")
            else:
                # Вне домашней папки (Linux/macOS) — только имя каталога
                self.assertEqual(masked, root.name)

    def test_file_path_masked_relative(self):
        """Путь файла в логе — относительный внутри рабочей папки (M11)."""
        from organizer_core import _mask_path_for_log

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            masked = _mask_path_for_log(root / "sub" / "file.txt", root)
            self.assertEqual(masked, "sub/file.txt")


def run_tests():
    """Запуск тестов без pytest."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    exit(run_tests())
