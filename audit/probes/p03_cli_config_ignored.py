#!/usr/bin/env python3
"""P03: CLI-флаг --config принимается, но игнорируется.

file_organizer.py парсит --config, однако вызывает load_config() без аргумента,
и тот всегда читает CONFIG_FILE рядом с organizer_core.py.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

work = Path(tempfile.mkdtemp())
(work / "note.txt").write_text("hello")
custom = Path(tempfile.mkdtemp()) / "custom_config.json"
custom.write_text(json.dumps({
    "categories": {"notes": ["txt"], "other": []},
    "settings": {"recursive": False, "dry_run": False},
}, ensure_ascii=False))

r = subprocess.run(
    [sys.executable, str(REPO / "file_organizer.py"), str(work), "--config", str(custom), "-q"],
    capture_output=True, text=True, cwd=str(REPO),
)
print("exit code:", r.returncode)
print("stdout:", repr(r.stdout[:300]))
print("stderr:", repr(r.stderr[:300]))
print("созданные папки:", sorted(p.name for p in work.iterdir()))
print("\nОжидалось (если бы --config работал): папка 'notes'.")
print("РЕЗУЛЬТАТ:", "❌ --config полностью игнорируется" if not (work / "notes").exists() else "✅ работает")
