#!/usr/bin/env python3
"""P13: эвристика «папка с именем категории = уже отсортировано» даёт ложные срабатывания.

_should_skip() пропускает всё, у чего ПЕРВЫЙ элемент относительного пути (без учёта
регистра!) совпадает с именем категории. Последствия:
  * реальные пользовательские папки Documents/ Photos/ Music/ Videos/ (Windows, macOS)
    молча не сортируются вообще;
  * файл в корне без расширения, названный "images"/"code"/"other", тоже пропускается
    и даже не попадает в счётчик skipped.
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
(root / "Documents").mkdir()          # настоящая папка пользователя (Windows/macOS)
(root / "Documents" / "contract.pdf").write_text("важный договор")
(root / "Music").mkdir()
(root / "Music" / "track.mp3").write_text("audio")
(root / "my_notes").mkdir()
(root / "my_notes" / "todo.txt").write_text("todo")
(root / "images").write_text("файл без расширения")   # не папка!
(root / "report.pdf").write_text("pdf")

logs = []
stats = oc.organize_files(str(root), verbose=False, log_callback=logs.append)
print("статистика:", stats)
print()
print("Documents/contract.pdf отсортирован:", (root / "documents" / "contract.pdf").exists())
print("   -> файл остался на месте:", (root / "Documents" / "contract.pdf").exists())
print("Music/track.mp3 отсортирован:", (root / "audio" / "track.mp3").exists())
print("   -> файл остался на месте:", (root / "Music" / "track.mp3").exists())
print("my_notes/todo.txt отсортирован:", (root / "documents" / "todo.txt").exists())
print('файл "images" пропущен молча (в skipped не попал):', (root / "images").is_file())
print("skipped по статистике:", stats["skipped"])
