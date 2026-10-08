#!/usr/bin/env python3
"""P07: in-memory лог _action_log растёт без границ (утечка памяти в мониторинге).

Данные нужны только тестам (это написано в комментарии к переменной),
но в долгоживущем процессе мониторинга список копится вечно и чистится
исключительно в undo_last_operation.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
N = 300
for i in range(N):
    (root / f"s{i}.txt").write_text("x" * 100)
    oc.organize_files(str(root), verbose=False)   # как это делает цикл мониторинга

print(f"после {N} вызовов organize_files: len(_action_log) = {len(oc._action_log)}")
print("потребители _action_log в продовом коде:",
      "(нет — только тест импортирует переменную)" )
print("\nРЕЗУЛЬТАТ:", "❌ неограниченный рост памяти" if len(oc._action_log) >= N else "✅ ограничен")
