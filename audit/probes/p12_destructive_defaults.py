#!/usr/bin/env python3
"""P12: дефолты (recursive=true, clean_empty_dirs=true) уничтожают структуру папок.

realistic-сценарий: пользователь запускает сортировку на рабочей папке/домашнем каталоге.
Файлы разъезжаются по categories-папкам в корне, а исходные подпапки удаляются,
как только опустели. Никакого предупреждения, ни исключений, ни бэкапа.
Плюс: /home, /root, /tmp, / не входят в PROTECTED_ABS_PATHS -> защита не срабатывает.
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
tree = {
    "Projects/app/src/main.py": "code",
    "Projects/app/README.md": "doc",
    "Photos/2024/summer/img.jpg": "photo",
    "Photos/2024/winter/img2.jpg": "photo",
    "Documents/contracts/scan.pdf": "pdf",
    "Music/album/song.mp3": "mp3",
    "Downloads/archive.zip": "zip",
}
for rel in tree:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(rel)

def show(title):
    print(title)
    for p in sorted(root.rglob("*")):
        if p.name.startswith("."):
            continue
        depth = len(p.relative_to(root).parts) - 1
        print("   " + "  " * depth + ("📁 " if p.is_dir() else "   ") + p.name)

show("ДО (структура пользователя):")
print("\nзапуск: organize_files(recursive=True, clean_empty_dirs=True) — как в config.json\n")
logs = []
stats = oc.organize_files(str(root), verbose=False, log_callback=logs.append)
print("статистика:", stats)
show("\nПОСЛЕ:")
print("\nисходные папки Projects/, Photos/, Documents/, Music/, Downloads/ уничтожены:",
      not any((root / d).exists() for d in ["Projects", "Photos", "Documents", "Music", "Downloads"]))
print("защита сработала на", "/home, /root, /tmp, /:",
      oc.is_protected_dir(root))  # tmp-каталог -> False
