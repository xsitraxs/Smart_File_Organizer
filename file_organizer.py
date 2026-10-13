#!/usr/bin/env python3
"""
GUI-версия умного органайзера файлов.

Исправлено по сравнению с предыдущей версией:
- messagebox больше не вызывается из воркер-потока (использует root.after)
- config передаётся как глубокая копия — оригинал не мутируется
- Прогресс-бар с процентами
- Отображение количества операций, доступных для отмены
- Кнопка «Открыть папку» для быстрого доступа
- Безопасная остановка мониторинга
"""

import copy
import os
import platform
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from organizer_core import (
    is_protected_dir,
    get_undo_count,
    load_config,
    organize_files,
    start_monitoring,
    stop_monitoring,
    undo_last_operation,
)


class FileOrganizerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Умный органайзер файлов")
        self.root.geometry("920x680")
        self.root.resizable(True, True)

        self.source_path = tk.StringVar()
        self.is_recursive = tk.BooleanVar(value=True)
        self.is_dry_run = tk.BooleanVar(value=False)
        self.is_monitoring = tk.BooleanVar(value=False)
        self.monitor_interval = tk.IntVar(value=10)

        self._is_running = False
        self._config = load_config()

        self._build_ui()

        # Корректная остановка мониторинга при закрытии окна
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Обновляем счётчик undo при смене пути
        self.source_path.trace_add("write", lambda *_: self._update_undo_label())

    # ── Построение интерфейса ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Папка ──────────────────────────────────────────────────────────────
        top = ttk.Frame(self.root, padding="10 10 10 0")
        top.pack(fill=tk.X)

        ttk.Label(top, text="Папка:", font=("TkDefaultFont", 10, "bold")).pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.source_path, width=55).pack(
            side=tk.LEFT, padx=5, fill=tk.X, expand=True
        )
        ttk.Button(top, text="Обзор…", command=self._browse).pack(side=tk.LEFT)
        ttk.Button(top, text="📂", command=self._open_folder, width=3).pack(side=tk.LEFT, padx=2)

        # ── Опции ─────────────────────────────────────────────────────────────
        opts = ttk.LabelFrame(self.root, text="Опции", padding="10 6")
        opts.pack(fill=tk.X, padx=10, pady=6)

        ttk.Checkbutton(opts, text="Рекурсивно (подпапки)", variable=self.is_recursive).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Checkbutton(opts, text="Тестовый режим (dry-run)", variable=self.is_dry_run).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Checkbutton(opts, text="Мониторинг", variable=self.is_monitoring).pack(
            side=tk.LEFT, padx=8
        )

        mon = ttk.Frame(opts)
        mon.pack(side=tk.LEFT, padx=4)
        ttk.Label(mon, text="Интервал (с):").pack(side=tk.LEFT)
        ttk.Spinbox(mon, from_=2, to=300, textvariable=self.monitor_interval, width=5).pack(
            side=tk.LEFT, padx=4
        )

        # ── Кнопки ────────────────────────────────────────────────────────────
        btn_row = ttk.Frame(self.root, padding="8 2")
        btn_row.pack()

        self._btn_start = ttk.Button(btn_row, text="▶  Запустить", command=self._start, width=16)
        self._btn_start.pack(side=tk.LEFT, padx=4)

        self._btn_stop = ttk.Button(
            btn_row, text="⏹  Стоп", command=self._stop_monitor, state=tk.DISABLED, width=10
        )
        self._btn_stop.pack(side=tk.LEFT, padx=4)

        self._btn_undo = ttk.Button(btn_row, text="↩  Отменить", command=self._undo, width=14)
        self._btn_undo.pack(side=tk.LEFT, padx=4)

        self._undo_label = ttk.Label(btn_row, text="", foreground="gray")
        self._undo_label.pack(side=tk.LEFT, padx=6)

        # ── Прогресс ──────────────────────────────────────────────────────────
        prog_row = ttk.Frame(self.root, padding="10 2")
        prog_row.pack(fill=tk.X)

        self._progress = ttk.Progressbar(prog_row, mode="determinate", length=400)
        self._progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._prog_label = ttk.Label(prog_row, text="", width=10, anchor=tk.E)
        self._prog_label.pack(side=tk.LEFT, padx=6)

        # ── Лог ───────────────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self.root, text="Журнал", padding="8")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=20, wrap=tk.WORD, font=("Consolas", 9), state=tk.DISABLED
        )
        self._log_text.pack(fill=tk.BOTH, expand=True)

        ttk.Button(log_frame, text="Очистить лог", command=self._clear_log).pack(
            anchor=tk.E, pady=2
        )

        # ── Статус-бар ────────────────────────────────────────────────────────
        self._status = tk.StringVar(value="Готов")
        ttk.Label(
            self.root, textvariable=self._status, relief=tk.SUNKEN, anchor=tk.W, padding="4 2"
        ).pack(fill=tk.X, side=tk.BOTTOM)

    # ── Вспомогательные методы ─────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        """Добавляет строку в лог (thread-safe через root.after)."""
        self.root.after(0, self._log_append, msg)

    def _log_append(self, msg: str) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, msg + "\n")
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _set_status(self, msg: str) -> None:
        self.root.after(0, self._status.set, msg)

    def _set_progress(self, current: int, total: int) -> None:
        pct = int(current / total * 100) if total else 0
        self.root.after(0, self._progress.config, {"value": pct})
        self.root.after(0, self._prog_label.config, {"text": f"{current}/{total}"})

    def _reset_progress(self) -> None:
        self._progress["value"] = 0
        self._prog_label.config(text="")

    def _update_undo_label(self) -> None:
        path = self.source_path.get()
        if not path:
            self._undo_label.config(text="")
            return
        try:
            n = get_undo_count(path)
            self._undo_label.config(
                text=f"({n} оп. для отмены)" if n else "",
                foreground="gray",
            )
        except Exception:
            self._undo_label.config(text="")

    def _lock_ui(self) -> None:
        self._btn_start.config(state=tk.DISABLED)
        self._btn_undo.config(state=tk.DISABLED)
        self._is_running = True

    def _unlock_ui(self) -> None:
        self._btn_start.config(state=tk.NORMAL)
        self._btn_undo.config(state=tk.NORMAL)
        self._btn_stop.config(state=tk.DISABLED)
        self._is_running = False
        self._update_undo_label()

    def _on_close(self) -> None:
        """Корректное завершение: останавливаем мониторинг перед закрытием окна."""
        if self._is_running:
            stop_monitoring()
        self.root.destroy()

    def _browse(self) -> None:
        folder = filedialog.askdirectory(title="Выберите папку")
        if folder:
            self.source_path.set(folder)

    def _open_folder(self) -> None:
        path = self.source_path.get()
        if not path:
            return

        # Безопасное открытие папки без Command Injection
        # Валидация пути перед использованием
        try:
            path_obj = Path(path).resolve()

            # Проверка, что путь существует и это директория
            if not path_obj.exists():
                messagebox.showerror("Ошибка", f"Папка не найдена: {path}")
                return
            if not path_obj.is_dir():
                messagebox.showerror("Ошибка", f"Не является папкой: {path}")
                return

            # Проверка на защищённые системные директории
            if is_protected_dir(path_obj):
                messagebox.showerror("Ошибка", "Отказ в открытии защищённой системной директории")
                return

            if platform.system() == "Windows":
                # os.startfile безопаснее ctypes.windll.shell32.ShellExecuteW:
                # не подвержен edge cases со специальными символами в пути
                os.startfile(str(path_obj))
            elif platform.system() == "Darwin":
                # subprocess.run вместо Popen для безопасности
                subprocess.run(["open", str(path_obj)], check=True, timeout=5)
            else:
                # subprocess.run вместо Popen для безопасности
                subprocess.run(["xdg-open", str(path_obj)], check=True, timeout=5)
        except subprocess.TimeoutExpired:
            messagebox.showerror("Ошибка", "Превышено время ожидания при открытии папки")
        except subprocess.CalledProcessError as exc:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку: {exc}")
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))

    # ── Основные действия ──────────────────────────────────────────────────────

    def _start(self) -> None:
        path = self.source_path.get().strip()
        if not path:
            messagebox.showwarning("Предупреждение", "Выберите папку!")
            return
        if self._is_running:
            messagebox.showwarning("Предупреждение", "Операция уже выполняется.")
            return

        self._lock_ui()
        self._clear_log()
        self._reset_progress()

        if self.is_monitoring.get():
            self._start_monitor(path)
        else:
            threading.Thread(target=self._run_sort, args=(path,), daemon=True).start()

    def _run_sort(self, path: str) -> None:
        """Сортировка в фоновом потоке."""
        try:
            cfg = copy.deepcopy(self._config)
            if self.is_dry_run.get():
                cfg["settings"]["dry_run"] = True
            cfg["settings"]["recursive"] = self.is_recursive.get()

            self._set_status("Сортировка…")
            stats = organize_files(
                path,
                verbose=False,
                log_callback=self._log,
                progress_callback=self._set_progress,
                config=cfg,
            )
            self._set_status(
                f"✅ Готово — перемещено: {stats['moved']}, ошибок: {stats['errors']}"
            )
            # messagebox — только из главного потока
            self.root.after(
                0,
                messagebox.showinfo,
                "Готово",
                f"Перемещено файлов: {stats['moved']}\n"
                f"Дубликатов: {stats['duplicates']}\n"
                f"Ошибок: {stats['errors']}",
            )
        except Exception as exc:
            self._log(f"❌ Ошибка: {exc}")
            self._set_status("❌ Ошибка")
            self.root.after(0, messagebox.showerror, "Ошибка", str(exc))
        finally:
            self.root.after(0, self._unlock_ui)

    def _start_monitor(self, path: str) -> None:
        """Запуск мониторинга."""
        try:
            cfg = copy.deepcopy(self._config)
            interval = self.monitor_interval.get()
            self._set_status(f"🔍 Мониторинг активен (интервал {interval} с)")
            self._log(f"🚀 Мониторинг запущен: {path}")
            self._btn_stop.config(state=tk.NORMAL)

            start_monitoring(path, interval=interval, callback=self._log, config=cfg)
        except Exception as exc:
            self._log(f"❌ Ошибка мониторинга: {exc}")
            self._set_status("❌ Ошибка мониторинга")
            self.root.after(0, self._unlock_ui)

    def _stop_monitor(self) -> None:
        stop_monitoring()
        self._log("⏹  Мониторинг остановлен.")
        self._set_status("Мониторинг остановлен")
        self.root.after(0, self._unlock_ui)

    def _undo(self) -> None:
        path = self.source_path.get().strip()
        if not path:
            messagebox.showwarning("Предупреждение", "Выберите папку!")
            return

        n = get_undo_count(path)
        if n == 0:
            messagebox.showinfo("Отмена", "Нет операций для отмены.")
            return

        if not messagebox.askyesno(
            "Подтверждение",
            f"Отменить все {n} операций перемещения?\n\n"
            "Файлы будут возвращены на исходные места.",
        ):
            return

        self._lock_ui()
        threading.Thread(target=self._run_undo, args=(path,), daemon=True).start()

    def _run_undo(self, path: str) -> None:
        try:
            self._set_status("Отмена операций…")
            self._log("\n── Отмена операций ──")
            stats = undo_last_operation(path, log_callback=self._log)
            self._set_status(
                f"✅ Отмена завершена — восстановлено: {stats['restored']}, ошибок: {stats['errors']}"
            )
            self.root.after(
                0,
                messagebox.showinfo,
                "Отмена завершена",
                f"Восстановлено файлов: {stats['restored']}\nОшибок: {stats['errors']}",
            )
        except Exception as exc:
            self._log(f"❌ Ошибка отмены: {exc}")
            self._set_status("❌ Ошибка отмены")
            self.root.after(0, messagebox.showerror, "Ошибка", str(exc))
        finally:
            self.root.after(0, self._unlock_ui)


def main() -> None:
    root = tk.Tk()
    FileOrganizerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
