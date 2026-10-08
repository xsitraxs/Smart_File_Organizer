#!/usr/bin/env python3
"""P04: консольный CLI не настраивает logging -> все INFO-сообщения CLI пропадают.

logging.lastResort печатает только WARNING+ и без форматтера, поэтому
--undo-info, шапка --monitor, подсказки и итоги просто не видны пользователю.
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
work = Path(tempfile.mkdtemp())
(work / "a.txt").write_text("x")

def run(args, timeout=20):
    return subprocess.run([sys.executable, str(REPO / "file_organizer.py"), *args],
                          capture_output=True, text=True, cwd=str(REPO), timeout=timeout)

r = run([str(work), "--undo-info"])
print("--undo-info  -> rc=%s stdout=%r stderr=%r" % (r.returncode, r.stdout, r.stderr))

r = run([str(work)])  # обычная сортировка с verbose=True
print("сортировка   -> rc=%s stdout=%r" % (r.returncode, r.stdout[:200]))
print("   (весь вывод сортировки ушёл в logger.info и был отброшен)")
print("stderr сортировки:", repr(r.stderr[:300]))

# --monitor: поднимаем и глушим SIGINT
p = subprocess.Popen([sys.executable, str(REPO / "file_organizer.py"), str(work), "--monitor"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(REPO))
time.sleep(3)
p.send_signal(2)
out, err = p.communicate(timeout=15)
print("--monitor    -> stdout=%r" % out[:200])
print("                stderr=%r" % err[:200])

print("\nРЕЗУЛЬТАТ:",
      "❌ CLI не выводит ни одного INFO-сообщения (нет logging.basicConfig)"
      if not out.strip() and not err.strip() else "✅ вывод есть")
