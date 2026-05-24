#!/usr/bin/env python3
"""
Графический интерфейс (GUI) для умного органайзера файлов.
Использует organizer_core для логики.
Поддерживает конфигурацию, отмену действий и мониторинг.
"""

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import threading
from organizer_core import (
    organize_files,
    undo_last_operation,
    start_monitoring,
    stop_monitoring,
    load_config
)


class FileOrganizerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Умный органайзер файлов")
        self.root.geometry("900x650")

        self.source_path = tk.StringVar()
        self.is_recursive = tk.BooleanVar(value=True)
        self.is_dry_run = tk.BooleanVar(value=False)
        self.is_monitoring = tk.BooleanVar(value=False)
        self.monitor_interval = tk.IntVar(value=10)
        self.is_running = False
        self.monitor_thread = None

        self.config = load_config()

        self.create_widgets()

    def create_widgets(self):
        # Верхняя панель
        top_frame = ttk.Frame(self.root, padding="10")
        top_frame.pack(fill=tk.X)

        ttk.Label(top_frame, text="Папка:", font=("Arial", 10, "bold")).pack(side=tk.LEFT)
        self.path_entry = ttk.Entry(top_frame, textvariable=self.source_path, width=60)
        self.path_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        ttk.Button(top_frame, text="Обзор", command=self.browse_folder).pack(side=tk.LEFT)

        # Опции
        opts_frame = ttk.LabelFrame(self.root, text="Опции", padding="10")
        opts_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Checkbutton(opts_frame, text="Рекурсивно (включая подпапки)", variable=self.is_recursive).pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(opts_frame, text="Тестовый режим (без перемещения)", variable=self.is_dry_run).pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(opts_frame, text="Мониторинг (авто-сортировка)", variable=self.is_monitoring).pack(side=tk.LEFT, padx=10)

        mon_frame = ttk.Frame(opts_frame)
        mon_frame.pack(side=tk.LEFT, padx=10)
        ttk.Label(mon_frame, text="Интервал (сек):").pack(side=tk.LEFT)
        ttk.Spinbox(mon_frame, from_=5, to=60, textvariable=self.monitor_interval, width=5).pack(side=tk.LEFT, padx=5)

        # Кнопки управления
        btn_frame = ttk.Frame(self.root, padding="10")
        btn_frame.pack()

        self.start_btn = ttk.Button(btn_frame, text="▶ Запустить", command=self.start_sorting)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="⏹ Стоп", command=self.stop_monitoring, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.undo_btn = ttk.Button(btn_frame, text="↩ Отменить", command=self.undo_operation)
        self.undo_btn.pack(side=tk.LEFT, padx=5)

        # Лог
        log_frame = ttk.LabelFrame(self.root, text="Журнал операций", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=20, wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Статус бар
        self.status_var = tk.StringVar(value="Готов к работе")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def browse_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку для сортировки")
        if folder:
            self.source_path.set(folder)

    def log(self, message):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def start_sorting(self):
        if not self.source_path.get():
            messagebox.showwarning("Предупреждение", "Выберите папку для сортировки!")
            return

        if self.is_running:
            messagebox.showwarning("Предупреждение", "Операция уже выполняется!")
            return

        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.undo_btn.config(state=tk.DISABLED)
        self.log_text.delete(1.0, tk.END)

        if self.is_monitoring.get():
            self.start_monitoring_mode()
        else:
            thread = threading.Thread(target=self.run_sorting)
            thread.daemon = True
            thread.start()

    def run_sorting(self):
        try:
            config = self.config.copy()
            if self.is_dry_run.get():
                config["settings"]["dry_run"] = True
            config["settings"]["recursive"] = self.is_recursive.get()

            self.status_var.set("Сортировка файлов...")
            organize_files(
                self.source_path.get(),
                verbose=False,
                log_callback=self.log,
                config=config
            )
            self.status_var.set("✅ Сортировка завершена")
            messagebox.showinfo("Готово", "Сортировка файлов завершена!")
        except Exception as e:
            self.log(f"❌ Ошибка: {e}")
            self.status_var.set("❌ Ошибка")
            messagebox.showerror("Ошибка", str(e))
        finally:
            self.is_running = False
            self.root.after(0, lambda: [self.start_btn.config(state=tk.NORMAL), self.undo_btn.config(state=tk.NORMAL)])

    def start_monitoring_mode(self):
        try:
            self.status_var.set(f"🔍 Мониторинг запущен (интервал: {self.monitor_interval.get()} сек)")
            self.log(f"🚀 Запуск мониторинга: {self.source_path.get()}")
            self.stop_btn.config(state=tk.NORMAL)

            self.monitor_thread = start_monitoring(
                self.source_path.get(),
                interval=self.monitor_interval.get(),
                callback=self.log
            )
        except Exception as e:
            self.log(f"❌ Ошибка мониторинга: {e}")
            self.status_var.set("❌ Ошибка мониторинга")
            self.is_running = False
            self.start_btn.config(state=tk.NORMAL)

    def stop_monitoring(self):
        if self.is_monitoring.get():
            stop_monitoring()
            self.log("⏹ Мониторинг остановлен пользователем")
            self.status_var.set("Мониторинг остановлен")
            self.stop_btn.config(state=tk.DISABLED)
            self.is_running = False
            self.start_btn.config(state=tk.NORMAL)

    def undo_operation(self):
        if not self.source_path.get():
            messagebox.showwarning("Предупреждение", "Выберите папку для отмены!")
            return

        if messagebox.askyesno("Подтверждение", "Отменить последнюю операцию перемещения?"):
            try:
                self.log("\n--- Отмена операций ---")
                undo_last_operation(self.source_path.get(), log_callback=self.log)
                self.status_var.set("✅ Отмена выполнена")
            except Exception as e:
                self.log(f"❌ Ошибка отмены: {e}")
                messagebox.showerror("Ошибка", str(e))


def main():
    root = tk.Tk()
    app = FileOrganizerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
