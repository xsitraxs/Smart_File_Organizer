#!/usr/bin/env python3
"""
Графический интерфейс (GUI) для умного органайзера файлов.
Импортирует основную логику из organizer_core.py.
"""

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import threading
from organizer_core import organize_files


class FileOrganizerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Умный органайзер файлов")
        self.root.geometry("800x600")

        self.source_path = tk.StringVar()
        self.is_recursive = tk.BooleanVar(value=True)
        self.is_dry_run = tk.BooleanVar(value=False)
        self.is_running = False

        self.create_widgets()

    def create_widgets(self):
        # Верхняя панель
        top_frame = ttk.Frame(self.root, padding="10")
        top_frame.pack(fill=tk.X)

        ttk.Label(top_frame, text="Папка:").pack(side=tk.LEFT)
        self.path_entry = ttk.Entry(top_frame, textvariable=self.source_path, width=50)
        self.path_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        ttk.Button(top_frame, text="Обзор", command=self.browse_folder).pack(side=tk.LEFT)

        # Опции
        opts_frame = ttk.Frame(self.root, padding="10")
        opts_frame.pack(fill=tk.X)

        ttk.Checkbutton(opts_frame, text="Рекурсивно (включая подпапки)", variable=self.is_recursive).pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(opts_frame, text="Тестовый режим (без перемещения)", variable=self.is_dry_run).pack(side=tk.LEFT, padx=10)

        # Кнопки управления
        btn_frame = ttk.Frame(self.root, padding="10")
        btn_frame.pack()

        self.start_btn = ttk.Button(btn_frame, text="Запустить сортировку", command=self.start_sorting)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        ttk.Button(btn_frame, text="Очистить лог", command=self.clear_log).pack(side=tk.LEFT, padx=5)

        # Прогресс бар
        self.progress = ttk.Progressbar(self.root, mode='indeterminate')
        self.progress.pack(fill=tk.X, padx=10, pady=5)

        # Лог
        log_frame = ttk.LabelFrame(self.root, text="Журнал операций", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=20, state='disabled')
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.source_path.set(folder)

    def log(self, message):
        self.root.after(0, self._log_safe, message)

    def _log_safe(self, message):
        """Безопасный логгинг из потока"""
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')

    def clear_log(self):
        self.log_text.config(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state='disabled')

    def start_sorting(self):
        if not self.source_path.get():
            messagebox.showwarning("Внимание", "Выберите папку для сортировки!")
            return

        if self.is_running:
            return

        self.is_running = True
        self.start_btn.config(state='disabled')
        self.progress.start()
        self.clear_log()
        self.log("="*40)
        self.log("Запуск процесса сортировки...")

        # Запуск в отдельном потоке, чтобы не блокировать GUI
        thread = threading.Thread(target=self.run_sorting_logic)
        thread.daemon = True
        thread.start()

    def run_sorting_logic(self):
        source_dir = self.source_path.get()
        recursive = self.is_recursive.get()
        dry_run = self.is_dry_run.get()

        try:
            # Вызываем основную функцию с callback для логирования
            stats = organize_files(
                source_dir,
                dry_run=dry_run,
                verbose=True,
                recursive=recursive,
                log_callback=self.log
            )

            self.log("="*40)
            self.log(f"Готово! Перемещено: {stats['moved']}, Ошибок: {stats['errors']}")
            if dry_run:
                self.log("Это был тестовый режим. Файлы не были перемещены.")

        except Exception as e:
            self.log(f"Критическая ошибка: {str(e)}")
            self.root.after(0, lambda: messagebox.showerror("Ошибка", str(e)))

        finally:
            self.root.after(0, self.finish_sorting)

    def finish_sorting(self):
        self.is_running = False
        self.start_btn.config(state='normal')
        self.progress.stop()
        messagebox.showinfo("Завершено", "Сортировка файлов завершена!")


if __name__ == "__main__":
    root = tk.Tk()
    app = FileOrganizerApp(root)
    root.mainloop()
