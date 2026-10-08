#!/usr/bin/env python3
"""P05: watchdog-режим реагирует только на on_created и пропускает on_moved.

Файл, перемещённый/переименованный В папку (mv из другой директории, .part -> .iso
после завершения загрузки, распаковка архива в temp и т.п.), не запускает сортировку.
Polling-режим (без watchdog) такие файлы видит, потому что сравнивает снимки.

Запуск без watchdog:   python3 p05_watchdog_on_moved.py poll
Запуск с watchdog:     /tmp/auditvenv/bin/python p05_watchdog_on_moved.py watchdog
"""
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import organizer_core as oc  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
have_watchdog = False
try:
    import watchdog  # noqa: F401
    have_watchdog = True
except ImportError:
    pass

watch = Path(tempfile.mkdtemp())
outside = Path(tempfile.mkdtemp())
(watch / ".keep").write_text("")   # скрытый, organizer его игнорирует

cfg = json.loads(json.dumps(oc.DEFAULT_CONFIG))
cfg["settings"]["use_watchdog"] = (mode == "watchdog")
cfg["settings"]["recursive"] = False

logs = []
oc.start_monitoring(str(watch), interval=2, callback=logs.append, config=cfg)
time.sleep(1.0)
print(f"режим={mode} (watchdog установлен: {have_watchdog}) — мониторинг запущен")

# 1. Перемещаем готовый файл В папку (это on_moved, не on_created)
(outside / "photo.jpg").write_text("jpeg")
shutil.move(str(outside / "photo.jpg"), str(watch / "photo.jpg"))
print("→ mv photo.jpg в папку; ждём 8 c...")
time.sleep(8)
moved = (watch / "images" / "photo.jpg").exists()

# 2. Для контроля: файл, созданный прямо в папке (on_created)
(watch / "note.txt").write_text("fresh")
time.sleep(6)
created = (watch / "documents" / "note.txt").exists()

oc.stop_monitoring()
time.sleep(1)

print("файл, перемещённый В папку, отсортирован:", moved)
print("файл, созданный в папке,    отсортирован:", created)
print("лог мониторинга:", logs)
print("\nРЕЗУЛЬТАТ:", ("❌ пропущен on_moved (сортировка не сработала)" if not moved
                      else "✅ on_moved обработан"))
