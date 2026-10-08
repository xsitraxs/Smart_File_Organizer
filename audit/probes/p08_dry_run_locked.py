#!/usr/bin/env python3
"""P08: dry_run=true из config.json невозможно выключить ни из CLI, ни из GUI.

organizer_core: use_dry_run = settings["dry_run"] or dry_run  (ИЛИ, а не переопределение)
file_organizer_gui: cfg["settings"]["dry_run"] = True — только если галочка стоит.
Итог: пользователь с dry_run=true в конфиге никогда не сможет реально отсортировать
файлы, и никакого предупреждения не получит (файлы просто «как бы перемещаются»).
"""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
(root / "a.txt").write_text("x")
cfg = copy.deepcopy(oc.DEFAULT_CONFIG)
cfg["settings"]["dry_run"] = True

st = oc.organize_files(str(root), dry_run=False, verbose=False, config=cfg)
print("organize_files(dry_run=False) со settings.dry_run=True ->", st)
print("файл на месте:", (root / "a.txt").exists(), "| папка documents создана:", (root / "documents").exists())

# GUI: воспроизводим его логику выбора конфига
gui_cfg = copy.deepcopy(oc.DEFAULT_CONFIG)
gui_cfg["settings"]["dry_run"] = True
if False:  # is_dry_run.get() == False, галочка снята
    gui_cfg["settings"]["dry_run"] = True
print("конфиг, который GUI передаст в ядро при СНЯТОЙ галочке dry-run:",
      gui_cfg["settings"]["dry_run"], "-> сортировка снова будет холостой")
