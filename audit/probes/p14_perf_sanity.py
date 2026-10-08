#!/usr/bin/env python3
"""P14: перф-сanity на 3000 файлов (organize + undo)."""
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
N = 3000
for i in range(N):
    sub = root / f"d{i % 50}"
    sub.mkdir(exist_ok=True)
    (sub / f"f{i}.txt").write_text("x")

t0 = time.perf_counter()
oc.organize_files(str(root), verbose=False)
t1 = time.perf_counter()
t = oc.undo_last_operation(str(root), verbose=False)
t2 = time.perf_counter()

print(f"organize {N} файлов: {t1 - t0:.2f} c ({(t1 - t0) / N * 1000:.2f} мс/файл)")
print(f"undo     {N} файлов: {t2 - t1:.2f} c")
print("восстановлено:", t["restored"], "ошибок:", t["errors"])
