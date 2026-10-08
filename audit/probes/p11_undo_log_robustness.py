#!/usr/bin/env python3
"""P11: устойчивость undo к битому/чужому .organizer_undo.json.

undo_last_operation читает `entry["destination"]`/`entry["source"]` через [] —
при отсутствии ключа летит KeyError и отмена падает целиком (в GUI — диалог ошибки).
При этом загрузчик считает такую строку вполне валидной (isinstance(obj, dict)).
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
(root / "documents").mkdir()
(root / "documents" / "ok.txt").write_text("ok")

# Валидная запись + запись без "destination" (такую может оставить сторонний/битый файл)
log = root / oc.UNDO_LOG_FILENAME
log.write_text(
    json.dumps({"action": "move", "source": "ok.txt", "destination": "documents/ok.txt",
                "original_name": "ok.txt"}) + "\n" +
    json.dumps({"action": "move", "source": "ghost.txt", "original_name": "ghost.txt"}) + "\n",
    encoding="utf-8",
)
print("get_undo_count:", oc.get_undo_count(str(root)), "(битая запись считается валидной)")
try:
    print("undo ->", oc.undo_last_operation(str(root), verbose=False))
except Exception as exc:
    print("CRASH:", type(exc).__name__, exc)
print("файл восстановлен:", (root / "ok.txt").exists())
