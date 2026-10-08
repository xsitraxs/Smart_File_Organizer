#!/usr/bin/env python3
"""P09: case-insensitive защита в _get_unique_path недостижима из основного потока.

organize_files сам решает, дубликат ли файл, сравнением `file_path.name in existing_names`
(регистрозависимо) и вызывает _get_unique_path ТОЛЬКО после срабатывания этой проверки.
Значит на Windows имя-дубликат в другом регистре до защиты не доходит:
  - Windows: shutil.move -> os.rename -> FileExistsError -> «Ошибка» и файл не отсортирован;
  - macOS (APFS/HFS+ по умолчанию регистронезависимы): os.rename ЗАМЕНЯЕТ файл -> потеря данных.
Тест в репозитории (TestCaseInsensitiveDedup) проверяет _get_unique_path изолированно,
и потому этот байпас не ловит.

Скрипт показывает: 1) функция защиту умеет; 2) спай показывает, что её не вызывают.
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
(root / "documents").mkdir()
(root / "documents" / "Report.TXT").write_text("старый файл пользователя")
(root / "sub").mkdir()
(root / "sub" / "report.txt").write_text("новый файл")  # едет в documents/

# 1) Что умеет сама функция при переданном наборе имён
existing = {p.name for p in (root / "documents").iterdir()}
print("_get_unique_path с набором", existing, "->",
      oc._get_unique_path(root / "documents", "report.txt", existing).name)
print("(на регистронезависимой ФС Windows/macOS это спасло бы существующий файл)")

# 2) А теперь реальный поток: спай
calls = []
orig = oc._get_unique_path

def spy(target_dir, filename, existing_names=None):
    calls.append(filename)
    return orig(target_dir, filename, existing_names)

oc._get_unique_path = spy
try:
    logs = []
    stats = oc.organize_files(str(root), verbose=False, log_callback=logs.append)
finally:
    oc._get_unique_path = orig

print("stats:", stats)
print("вызовов _get_unique_path из organize_files:", calls)
print("в documents/ сейчас:", sorted(p.name for p in (root / "documents").iterdir()))
print("\nРЕЗУЛЬТАТ:", "❌ защита от регистра обойдена (spy не вызывался, дубликат не обработан)"
      if not calls else "✅ защита задействована")
