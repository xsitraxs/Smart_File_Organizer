#!/usr/bin/env python3
"""P06: поведение с симлинками.

Ожидание из README («Проверка symlink: валидация назначения символических ссылок»)
и реальность:
  * symlink на файл ВНЕ папки -> пропускается как ОШИБКА (шум в статистике на каждом запуске);
  * symlink на файл ВНУТРИ папки -> переносится как ссылка (ok), но st_size берётся
    от цели, поэтому symlink на «слишком большой» файл отбраковывается лимитом 1 ГБ;
  * симлинк-директории не обходятся (is_dir(follow_symlinks=False)) — файлы внутри
    остаются неотсортированными, и об этом ни слова в логах.
"""
import os
import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import organizer_core as oc  # noqa: E402

root = Path(tempfile.mkdtemp())
outside_target = Path(tempfile.mkdtemp()) / "secret.txt"
outside_target.write_text("внешние данные")

(root / "inside.txt").write_text("inside")
os.symlink(outside_target, root / "link_outside.txt")      # ссылка наружу
os.symlink(root / "inside.txt", root / "link_inside.txt")  # ссылка внутрь

real_dir = root / "realdir"
real_dir.mkdir()
(real_dir / "deep.jpg").write_text("deep")
os.symlink(real_dir, root / "link_dir")                    # ссылка на директорию

logs = []
stats = oc.organize_files(str(root), verbose=False, log_callback=logs.append)
print("stats:", stats)
for line in logs:
    if "Пропущен" in line or "Ошибка" in line or "✗" in line:
        print("  log:", line.strip())
print("корень:", sorted(p.name for p in root.iterdir()))
print("images/:", sorted(p.name for p in (root / "images").iterdir()) if (root / "images").exists() else None)
print("ссылка-на-директорию осталась в корне:", (root / "link_dir").exists())
print("файл внутри симлинк-директории отсортирован:", (root / "images" / "deep.jpg").exists())
print("link_inside.txt в images:", (root / "images" / "link_inside.txt").is_symlink()
      if (root / "images" / "link_inside.txt").exists() else None)
