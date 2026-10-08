#!/usr/bin/env bash
# Полный прогон аудита Smart File Organizer: тесты, статика, репро-скрипты.
# Требуется Python 3.10+, для статики — venv с mypy/ruff:
#   python3 -m venv /tmp/auditvenv && /tmp/auditvenv/bin/pip install mypy ruff pytest watchdog
set -u
cd "$(dirname "$0")/.."          # корень репозитория
VENV=${VENV:-/tmp/auditvenv}
PY=${PY:-python3}

echo "### 1. Штатные тесты"
$PY -m unittest test_organizer_core -v 2>&1 | tail -5

echo; echo "### 2. mypy (конфиг проекта, strict)"
"$VENV/bin/mypy" organizer_core.py file_organizer.py file_organizer_gui.py test_organizer_core.py 2>&1 | tail -5

echo; echo "### 3. ruff"
"$VENV/bin/ruff" check organizer_core.py file_organizer.py file_organizer_gui.py test_organizer_core.py --statistics 2>&1 | tail -12

echo; echo "### 4. Репро-скрипты"
mkdir -p audit/evidence
for script in audit/probes/p*.py; do
    name=$(basename "$script" .py)
    echo "── $name"
    case "$name" in
        p05_watchdog_on_moved) "$VENV/bin/python" "$script" watchdog > "audit/evidence/$name.txt" 2>&1 ;;
        p05b_watchdog_events|p10_flaky_concurrency_test) "$VENV/bin/python" "$script" > "audit/evidence/$name.txt" 2>&1 ;;
        *) $PY "$script" > "audit/evidence/$name.txt" 2>&1 ;;
    esac
    tail -4 "audit/evidence/$name.txt"
done
echo; echo "Готово. Полные выводы: audit/evidence/"
