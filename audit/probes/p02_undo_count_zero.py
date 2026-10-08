#!/usr/bin/env python3
"""P02: undo_last_operation(count=0) отменяет ВСЁ и дублирует записи в логе.

`all_entries[-0:]` в Python == весь список. Плюс remaining = all_entries[:len-0]
= весь список, поэтому успешно отменённые записи остаются в undo-логе навсегда.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
for i in range(3):
    (root / f"f{i}.txt").write_text(f"data {i}")

print("до сортировки :", sorted(p.name for p in root.iterdir()))
oc.organize_files(str(root), verbose=False)
print("после сортировки: undo-лог =", oc.get_undo_count(str(root)))

stats = oc.undo_last_operation(str(root), count=0, verbose=False)
print("undo(count=0) ->", stats, "(ожидалось: ничего не отменять)")
print("файлов вернулось в корень:", sorted(p.name for p in root.iterdir()))
print("undo-лог после отмены count=0:", oc.get_undo_count(str(root)), "(ожидалось 0)")

c = 0
for _ in range(3):
    oc.organize_files(str(root), verbose=False)
    c = oc.get_undo_count(str(root))
print("после 3 повторных сортировок undo-лог =", c, "(файлов всего 3)")

print("\nРЕЗУЛЬТАТ:",
      "❌ count=0 отменяет всё" if stats["restored"] > 0 else "✅ count=0 безопасен",
      "|",
      "❌ лог раздувается" if c != 3 else "✅ лог консистентен")
