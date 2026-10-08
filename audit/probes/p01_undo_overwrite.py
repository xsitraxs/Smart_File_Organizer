#!/usr/bin/env python3
"""P01: undo молча перезаписывает файл, появившийся на исходном месте (потеря данных).

Сценарий: пользователь сортирует папку, затем на то же имя кладёт НОВЫЙ файл,
затем нажимает «Отменить». На POSIX shutil.move -> os.rename заменяет цель
без предупреждения: новый файл исчезает, а лог отмены считает операцию успешной.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
(root / "report.txt").write_text("ВЕРСИЯ 1 — оригинал, уйдёт в documents/")

print("1) organize_files:", oc.organize_files(str(root), verbose=False))
print("   documents/report.txt:", (root / "documents" / "report.txt").read_text())

# Пользователь создаёт новый файл с тем же именем на прежнем месте
(root / "report.txt").write_text("ВЕРСИЯ 2 — новый файл пользователя, создан ПОСЛЕ сортировки")

print("2) undo_last_operation:", oc.undo_last_operation(str(root), verbose=False))
print("   root/report.txt:", (root / "report.txt").read_text())
print("   documents/report.txt существует:", (root / "documents" / "report.txt").exists())
print("   остаток undo-лога:", oc.get_undo_count(str(root)))

lost = "ВЕРСИЯ 2" not in (root / "report.txt").read_text()
print("\nРЕЗУЛЬТАТ:", "❌ ПОТЕРЯ ДАННЫХ — новый файл затёрт без предупреждения" if lost
      else "✅ новый файл сохранён")
