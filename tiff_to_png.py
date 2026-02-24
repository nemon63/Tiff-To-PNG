from __future__ import annotations

import argparse
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageSequence

SOURCE_EXTS = {".tif", ".tiff", ".tga"}


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = text
        self.tip_window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show_tip, add="+")
        widget.bind("<Leave>", self._hide_tip, add="+")
        widget.bind("<ButtonPress>", self._hide_tip, add="+")

    def _show_tip(self, _event=None):
        if self.tip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#fff7cc",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=3,
        )
        label.pack()

    def _hide_tip(self, _event=None):
        if self.tip_window is not None:
            self.tip_window.destroy()
            self.tip_window = None


def convert_one(src: Path, dst: Path, force_rgba: bool, overwrite: bool) -> str:
    if dst.exists() and not overwrite:
        return f"пропуск (уже есть): {dst.name}"

    with Image.open(src) as im:
        # Use the first frame/page for multi-frame TIFFs.
        if getattr(im, "is_animated", False) or getattr(im, "n_frames", 1) > 1:
            im = ImageSequence.Iterator(im).__next__()

        mode = im.mode
        has_alpha = mode in ("RGBA", "LA") or (mode == "P" and "transparency" in im.info)

        if force_rgba:
            im = im.convert("RGBA")
        else:
            if has_alpha and mode == "P":
                im = im.convert("RGBA")
            elif not has_alpha and mode not in ("RGB", "L"):
                im = im.convert("RGB")

        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, format="PNG", optimize=True, compress_level=6)

    return f"успех: {dst.name}"


def iter_sources(root: Path, recursive: bool):
    if root.is_file():
        if root.suffix.lower() in SOURCE_EXTS:
            yield root
        return

    # Always process files in the selected folder itself.
    for p in root.glob("*"):
        if p.is_file() and p.suffix.lower() in SOURCE_EXTS:
            yield p

    # If recursive mode is enabled, additionally process files in subfolders.
    if recursive:
        for p in root.rglob("*"):
            if p.parent != root and p.is_file() and p.suffix.lower() in SOURCE_EXTS:
                yield p


def build_dst_path(src: Path, inp: Path, out_root: Path | None) -> Path:
    if not out_root:
        return src.with_suffix(".png")

    if inp.is_dir():
        rel = src.relative_to(inp)
        return out_root / rel.with_suffix(".png")

    return out_root / src.with_suffix(".png").name


def run_batch(
    inp: Path,
    out_root: Path | None,
    recursive: bool,
    force_rgba: bool,
    overwrite: bool,
    logger,
) -> tuple[int, int]:
    total = 0
    ok = 0

    for src in iter_sources(inp, recursive):
        total += 1
        dst = build_dst_path(src, inp, out_root)
        try:
            msg = convert_one(src, dst, force_rgba, overwrite)
            if msg.startswith("успех"):
                ok += 1
            logger(f"{src.name} -> {msg}")
        except Exception as exc:
            logger(f"{src.name} -> ОШИБКА: {exc}")

    return total, ok


def run_cli() -> int:
    parser = argparse.ArgumentParser(
        description="Batch convert TIFF/TIF/TGA to PNG preserving alpha when present."
    )
    parser.add_argument("input", type=str, help="Input file or folder with .tif/.tiff/.tga")
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="Output folder. If empty, writes PNG next to the source.",
    )
    parser.add_argument("--recursive", action="store_true", help="Recurse into subfolders")
    parser.add_argument("--force-rgba", action="store_true", help="Force output as RGBA")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing PNGs")
    args = parser.parse_args()

    inp = Path(args.input)
    out_root = Path(args.out) if args.out else None

    if not inp.exists():
        print(f"Input does not exist: {inp}")
        return 1

    total, ok = run_batch(
        inp=inp,
        out_root=out_root,
        recursive=args.recursive,
        force_rgba=args.force_rgba,
        overwrite=args.overwrite,
        logger=print,
    )
    print(f"\nDone. total={total}, ok={ok}, failed={total - ok}")
    return 0


class ConverterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Конвертер TIFF/TGA в PNG")
        self.geometry("760x520")
        self.minsize(640, 420)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.force_rgba_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Готово")
        self._tooltips: list[Tooltip] = []

        self._build_ui()

    def _attach_tooltip(self, widget: tk.Widget, text: str):
        self._tooltips.append(Tooltip(widget, text))

    def _build_ui(self):
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        input_row = ttk.Frame(main)
        input_row.pack(fill="x", pady=(0, 8))
        ttk.Label(input_row, text="Вход (файл или папка):").pack(anchor="w")
        input_line = ttk.Frame(input_row)
        input_line.pack(fill="x", pady=(4, 0))
        self.input_entry = ttk.Entry(input_line, textvariable=self.input_var)
        self.input_entry.pack(side="left", fill="x", expand=True)
        input_file_btn = ttk.Button(input_line, text="Файл", command=self._pick_input_file)
        input_file_btn.pack(side="left", padx=(8, 0))
        input_folder_btn = ttk.Button(input_line, text="Папка", command=self._pick_input_folder)
        input_folder_btn.pack(side="left", padx=(6, 0))
        self._attach_tooltip(self.input_entry, "Укажите путь к TIFF/TGA файлу или папке с файлами.")
        self._attach_tooltip(input_file_btn, "Выбрать один файл TIFF/TGA.")
        self._attach_tooltip(input_folder_btn, "Выбрать папку для пакетной конвертации.")

        output_row = ttk.Frame(main)
        output_row.pack(fill="x", pady=(0, 8))
        ttk.Label(output_row, text="Выходная папка (необязательно):").pack(anchor="w")
        output_line = ttk.Frame(output_row)
        output_line.pack(fill="x", pady=(4, 0))
        self.output_entry = ttk.Entry(output_line, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True)
        output_btn = ttk.Button(output_line, text="Выбрать", command=self._pick_output_folder)
        output_btn.pack(side="left", padx=(8, 0))
        self._attach_tooltip(self.output_entry, "Если пусто, PNG сохраняются рядом с исходными файлами.")
        self._attach_tooltip(output_btn, "Выбрать папку, куда сохранять PNG.")

        options = ttk.LabelFrame(main, text="Параметры", padding=8)
        options.pack(fill="x", pady=(0, 8))
        recursive_chk = ttk.Checkbutton(options, text="Рекурсивно для папок", variable=self.recursive_var)
        recursive_chk.pack(anchor="w")
        force_rgba_chk = ttk.Checkbutton(options, text="Принудительно RGBA", variable=self.force_rgba_var)
        force_rgba_chk.pack(anchor="w")
        overwrite_chk = ttk.Checkbutton(
            options, text="Перезаписывать PNG, если уже есть", variable=self.overwrite_var
        )
        overwrite_chk.pack(
            anchor="w"
        )
        self._attach_tooltip(recursive_chk, "Обрабатывать также файлы во вложенных папках.")
        self._attach_tooltip(force_rgba_chk, "Всегда сохранять PNG в RGBA, даже без альфа-канала.")
        self._attach_tooltip(overwrite_chk, "Если PNG уже существует, заменить его новым файлом.")

        controls = ttk.Frame(main)
        controls.pack(fill="x", pady=(0, 8))
        self.convert_btn = ttk.Button(controls, text="Конвертировать", command=self._on_convert)
        self.convert_btn.pack(side="left")
        self._attach_tooltip(self.convert_btn, "Запустить конвертацию выбранных файлов в PNG.")

        ttk.Label(main, text="Журнал:").pack(anchor="w")
        log_wrap = ttk.Frame(main)
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_wrap, height=12, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="left", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self._attach_tooltip(self.log_text, "Здесь отображается ход конвертации и возможные ошибки.")

        status = ttk.Label(main, textvariable=self.status_var)
        status.pack(anchor="w", pady=(8, 0))

    def _pick_input_file(self):
        path = filedialog.askopenfilename(
            title="Выберите файл изображения",
            filetypes=[("Файлы TIFF/TGA", "*.tif *.tiff *.tga"), ("Все файлы", "*.*")],
        )
        if path:
            self.input_var.set(path)

    def _pick_input_folder(self):
        path = filedialog.askdirectory(title="Выберите входную папку")
        if path:
            self.input_var.set(path)

    def _pick_output_folder(self):
        path = filedialog.askdirectory(title="Выберите выходную папку")
        if path:
            self.output_var.set(path)

    def _append_log(self, line: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.update_idletasks()

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        self.convert_btn.configure(state=state)
        self.input_entry.configure(state=state)
        self.output_entry.configure(state=state)

    def _on_convert(self):
        input_value = self.input_var.get().strip()
        output_value = self.output_var.get().strip()

        if not input_value:
            messagebox.showerror("Ошибка", "Выберите входной файл или папку.")
            return

        inp = Path(input_value)
        if not inp.exists():
            messagebox.showerror("Ошибка", f"Входной путь не существует:\n{inp}")
            return

        out_root = Path(output_value) if output_value else None
        self.status_var.set("Конвертация...")
        self._set_running(True)
        self._append_log("---- Старт конвертации ----")

        try:
            total, ok = run_batch(
                inp=inp,
                out_root=out_root,
                recursive=self.recursive_var.get(),
                force_rgba=self.force_rgba_var.get(),
                overwrite=self.overwrite_var.get(),
                logger=self._append_log,
            )
            failed = total - ok
            summary = f"Готово. всего={total}, успешно={ok}, ошибок={failed}"
            self._append_log(summary)
            self.status_var.set(summary)
            messagebox.showinfo("Готово", summary)
        except Exception as exc:
            self._append_log(f"ОШИБКА: {exc}")
            self.status_var.set("Ошибка")
            messagebox.showerror("Ошибка", str(exc))
        finally:
            self._set_running(False)


def run_gui():
    app = ConverterApp()
    app.mainloop()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        raise SystemExit(run_cli())
    run_gui()
