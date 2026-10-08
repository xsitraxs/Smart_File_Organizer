#!/usr/bin/env python3
"""P10: измерение флейковости TestConcurrentOrganize.

Тест утверждает `assertEqual(len(worked), 1)` — «ровно один вызов отработал».
Но `skipped_concurrent` выставляется ТОЛЬКО если в момент вызова лок занят;
если потоки разошлись по времени, «сработавших» вызовов будет 2+, и тест падает.
Строка `assertGreaterEqual(len(skipped), 0)` не проверяет ничего (всегда истина).
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
runs = int(sys.argv[1]) if len(sys.argv) > 1 else 60
fails = 0
for i in range(runs):
    r = subprocess.run(
        [sys.executable, "-m", "unittest",
         "test_organizer_core.TestConcurrentOrganize.test_concurrent_calls_dont_corrupt_log"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    if r.returncode != 0:
        fails += 1
        if fails == 1:
            print("первое падение:\n", r.stderr[-800:])
print(f"\nпрогонов: {runs}, падений: {fails} ({fails / runs:.0%})")
print("РЕЗУЛЬТАТ:", "❌ тест нестабилен" if fails else "✅ стабилен на этой машине (но по построению недетерминирован)")
