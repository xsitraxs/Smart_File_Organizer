#!/usr/bin/env python3
"""P05b: какие события watchdog реально получает при mv файла в папку и от
собственных перемещений органайзера (обратная связь «сортировка -> события -> сортировка»).

Запуск только venv-питоном, где есть watchdog.
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

root = Path(tempfile.mkdtemp())
outside = Path(tempfile.mkdtemp())
events = []

class Spy(FileSystemEventHandler):
    def on_any_event(self, event):
        events.append((event.event_type, str(Path(event.src_path).name),
                       str(Path(getattr(event, "dest_path", "") or "").name),
                       "dir" if event.is_directory else "file"))

obs = Observer()
obs.schedule(Spy(), str(root), recursive=True)
obs.start()
time.sleep(0.5)

(outside / "photo.jpg").write_text("x")
shutil.move(str(outside / "photo.jpg"), str(root / "photo.jpg"))  # mv ВНУТРЬ
time.sleep(1.0)
print("события после 'mv файла в папку':")
for e in events:
    print("   ", e)

events.clear()
(root / "images").mkdir()
shutil.move(str(root / "photo.jpg"), str(root / "images" / "photo.jpg"))  # как это делает org.
time.sleep(1.0)
print("\nсобытия от собственного перемещения органайзера (существующие папки):")
for e in events:
    print("   ", e)

obs.stop()
obs.join()
