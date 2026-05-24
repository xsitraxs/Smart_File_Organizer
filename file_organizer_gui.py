--- file_organizer_gui.py (原始)


+++ file_organizer_gui.py (修改后)
import os
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from datetime import datetime
import threading

# Конфигурация расширений
CATEGORIES = {
    'images': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp', '.ico'],
    'documents': ['.pdf', '.doc', '.docx', '.txt', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.rtf'],
    'videos': ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'],
    'audio': ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma'],
    'archives': ['.zip', '.rar', '.7z', '.tar', '.gz', '.bz2'],
    'code': ['.py', '.js', '.html', '.css', '.java', '.cpp', '.c', '.h', '.php', '.json', '.xml', '.sh'],
    'installers': ['.exe', '.msi', '.dmg', '.pkg', '.deb', '.rpm'],
    'fonts': ['.ttf', '.otf', '.woff', '.woff2'],
    'torrents': ['.torrent', '.magnet'],
}

# Файлы, которые нужно игнорировать
IGNORE_FILES = ['file_organizer.py', 'file_organizer_gui.py', '.gitignore', 'README.md']

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
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
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

        files_processed = 0
        files_moved = 0
        errors = 0

        try:
            if recursive:
                walker = os.walk(source_dir)
            else:
                # Эмуляция os.walk только для текущего уровня
                walker = [(source_dir, [], [f for f in os.listdir(source_dir) if os.path.isfile(os.path.join(source_dir, f))])]

            for root, dirs, files in walker:
                # Игнорируем системные папки и папки категорий, если они внутри
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in CATEGORIES]

                for filename in files:
                    if filename.startswith('.'):
                        continue
                    if filename in IGNORE_FILES:
                        continue
                    if filename == os.path.basename(__file__):
                        continue

                    file_path = os.path.join(root, filename)
                    ext = os.path.splitext(filename)[1].lower()

                    category = 'other'
                    for cat, extensions in CATEGORIES.items():
                        if ext in extensions:
                            category = cat
                            break

                    # Если файл уже в правильной папке, пропускаем
                    parent_folder = os.path.basename(root)
                    if parent_folder == category:
                        continue

                    target_dir = os.path.join(source_dir, category)

                    if dry_run:
                        self.log(f"[DRY RUN] Переместить: {file_path} -> {target_dir}")
                        files_moved += 1
                    else:
                        try:
                            if not os.path.exists(target_dir):
                                os.makedirs(target_dir)

                            final_name = self.get_unique_filename(target_dir, filename)
                            final_path = os.path.join(target_dir, final_name)

                            shutil.move(file_path, final_path)
                            self.log(f"Перемещено: {filename} -> {category}/{final_name}")
                            files_moved += 1

                            # Попытка удалить пустые папки после перемещения (только если рекурсивно)
                            if recursive:
                                self.remove_empty_dirs(root, source_dir)

                        except Exception as e:
                            self.log(f"Ошибка при перемещении {filename}: {str(e)}")
                            errors += 1

                    files_processed += 1

            self.log("="*40)
            self.log(f"Готово! Обработано: {files_processed}, Перемещено: {files_moved}, Ошибок: {errors}")
            if dry_run:
                self.log("Это был тестовый режим. Файлы не были перемещены.")

        except Exception as e:
            self.log(f"Критическая ошибка: {str(e)}")
            messagebox.showerror("Ошибка", str(e))

        finally:
            self.root.after(0, self.finish_sorting)

    def get_unique_filename(self, directory, filename):
        base, ext = os.path.splitext(filename)
        counter = 1
        new_name = filename
        while os.path.exists(os.path.join(directory, new_name)):
            new_name = f"{base}_{counter}{ext}"
            counter += 1
        return new_name

    def remove_empty_dirs(self, current_dir, base_dir):
        """Удаляет пустые папки, если они не являются базовой директорией"""
        try:
            if current_dir == base_dir:
                return
            if not os.listdir(current_dir):
                os.rmdir(current_dir)
                self.log(f"Удалена пустая папка: {current_dir}")
                parent = os.path.dirname(current_dir)
                if parent != base_dir:
                    self.remove_empty_dirs(parent, base_dir)
        except Exception:
            pass

    def finish_sorting(self):
        self.is_running = False
        self.start_btn.config(state='normal')
        self.progress.stop()
        messagebox.showinfo("Завершено", "Сортировка файлов завершена!")

if __name__ == "__main__":
    root = tk.Tk()
    app = FileOrganizerApp(root)
    root.mainloop()
