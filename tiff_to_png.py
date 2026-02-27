from __future__ import annotations

import argparse
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageSequence

SOURCE_EXTS = {
    ".tif",
    ".tiff",
    ".tga",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".gif",
    ".webp",
    ".psd",
}
FILE_DIALOG_PATTERN = "*.tif *.tiff *.tga *.jpg *.jpeg *.bmp *.gif *.webp *.psd"
RESAMPLING_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS


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


def normalize_image_mode(im: Image.Image, force_rgba: bool) -> Image.Image:
    mode = im.mode
    has_alpha = mode in ("RGBA", "LA") or (mode == "P" and "transparency" in im.info)

    if force_rgba:
        return im.convert("RGBA")

    if has_alpha:
        if mode == "P":
            return im.convert("RGBA")
        return im

    if mode not in ("RGB", "L"):
        return im.convert("RGB")
    return im


def apply_resize(im: Image.Image, resize_mode: str, resize_percent: int, max_side: int) -> Image.Image:
    if resize_mode == "none":
        return im

    width, height = im.size
    if width <= 0 or height <= 0:
        return im

    if resize_mode == "percent":
        scale = resize_percent / 100.0
        if scale <= 0:
            return im
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
    elif resize_mode == "max_side":
        longest = max(width, height)
        if max_side <= 0 or longest <= max_side:
            return im
        scale = max_side / float(longest)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
    else:
        return im

    if (new_w, new_h) == (width, height):
        return im

    return im.resize((new_w, new_h), RESAMPLING_LANCZOS)


def apply_png8(im: Image.Image, colors: int, dither: bool) -> Image.Image:
    dither_mode = Image.FLOYDSTEINBERG if dither else Image.NONE

    if "A" in im.getbands():
        rgba = im if im.mode == "RGBA" else im.convert("RGBA")
        return rgba.quantize(colors=colors, method=Image.FASTOCTREE, dither=dither_mode)

    rgb = im if im.mode == "RGB" else im.convert("RGB")
    return rgb.quantize(colors=colors, method=Image.MEDIANCUT, dither=dither_mode)


def convert_one(
    src: Path,
    dst: Path,
    force_rgba: bool,
    overwrite: bool,
    optimize: bool,
    compress_level: int,
    resize_mode: str,
    resize_percent: int,
    max_side: int,
    png8: bool,
    png8_colors: int,
    dither: bool,
) -> str:
    if dst.exists() and not overwrite:
        return f"пропуск (уже есть): {dst.name}"

    with Image.open(src) as im:
        # Use the first frame/page for multi-frame TIFFs.
        if getattr(im, "is_animated", False) or getattr(im, "n_frames", 1) > 1:
            im = ImageSequence.Iterator(im).__next__()

        im = normalize_image_mode(im, force_rgba)
        im = apply_resize(im, resize_mode=resize_mode, resize_percent=resize_percent, max_side=max_side)
        if png8:
            im = apply_png8(im, colors=png8_colors, dither=dither)

        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, format="PNG", optimize=optimize, compress_level=compress_level)

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
    delete_source: bool,
    optimize: bool,
    compress_level: int,
    resize_mode: str,
    resize_percent: int,
    max_side: int,
    png8: bool,
    png8_colors: int,
    dither: bool,
    logger,
) -> tuple[int, int]:
    total = 0
    ok = 0

    for src in iter_sources(inp, recursive):
        total += 1
        dst = build_dst_path(src, inp, out_root)
        try:
            msg = convert_one(
                src=src,
                dst=dst,
                force_rgba=force_rgba,
                overwrite=overwrite,
                optimize=optimize,
                compress_level=compress_level,
                resize_mode=resize_mode,
                resize_percent=resize_percent,
                max_side=max_side,
                png8=png8,
                png8_colors=png8_colors,
                dither=dither,
            )
            is_success = msg.startswith("успех")
            if is_success:
                ok += 1
            logger(f"{src.name} -> {msg}")

            if is_success and delete_source:
                try:
                    if src.resolve() == dst.resolve():
                        logger(f"{src.name} -> исходник не удален (совпадает с выходным файлом)")
                    else:
                        src.unlink()
                        logger(f"{src.name} -> исходник удален")
                except Exception as exc:
                    logger(f"{src.name} -> не удалось удалить исходник: {exc}")
        except Exception as exc:
            logger(f"{src.name} -> ОШИБКА: {exc}")

    return total, ok


def run_cli() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Batch convert TIFF/TGA/JPEG/BMP/GIF/WEBP/PSD to PNG "
            "with optional resize and PNG-8 quantization."
        )
    )
    parser.add_argument(
        "input",
        type=str,
        help="Input file or folder with .tif/.tiff/.tga/.jpg/.jpeg/.bmp/.gif/.webp/.psd",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="Output folder. If empty, writes PNG next to the source.",
    )
    parser.add_argument("--recursive", action="store_true", help="Recurse into subfolders")
    parser.add_argument("--force-rgba", action="store_true", help="Force output as RGBA")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing PNGs")
    parser.add_argument("--delete-source", action="store_true", help="Delete source after success")
    parser.add_argument(
        "--compress-level",
        type=int,
        default=6,
        choices=range(0, 10),
        metavar="0..9",
        help="PNG compress level: 0 fastest/largest, 9 slowest/smallest",
    )
    parser.add_argument(
        "--optimize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable PNG optimize pass",
    )
    resize_group = parser.add_mutually_exclusive_group()
    resize_group.add_argument(
        "--resize-percent",
        type=int,
        default=None,
        help="Resize to percent of original size (e.g. 50)",
    )
    resize_group.add_argument(
        "--max-side",
        type=int,
        default=None,
        help="Resize so longer side is <= this value in px",
    )
    parser.add_argument("--png8", action="store_true", help="Save as indexed PNG-8")
    parser.add_argument(
        "--png8-colors",
        type=int,
        default=256,
        metavar="2..256",
        help="Palette size for PNG-8",
    )
    parser.add_argument(
        "--dither",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable dithering for PNG-8",
    )
    args = parser.parse_args()

    inp = Path(args.input)
    out_root = Path(args.out) if args.out else None

    if not inp.exists():
        print(f"Input does not exist: {inp}")
        return 1

    if args.resize_percent is not None and args.resize_percent <= 0:
        print("Argument error: --resize-percent must be > 0")
        return 2
    if args.max_side is not None and args.max_side <= 0:
        print("Argument error: --max-side must be > 0")
        return 2
    if not (2 <= args.png8_colors <= 256):
        print("Argument error: --png8-colors must be in range 2..256")
        return 2

    resize_mode = "none"
    resize_percent = 100
    max_side = 0
    if args.resize_percent is not None:
        resize_mode = "percent"
        resize_percent = args.resize_percent
    elif args.max_side is not None:
        resize_mode = "max_side"
        max_side = args.max_side

    total, ok = run_batch(
        inp=inp,
        out_root=out_root,
        recursive=args.recursive,
        force_rgba=args.force_rgba,
        overwrite=args.overwrite,
        delete_source=args.delete_source,
        optimize=args.optimize,
        compress_level=args.compress_level,
        resize_mode=resize_mode,
        resize_percent=resize_percent,
        max_side=max_side,
        png8=args.png8,
        png8_colors=args.png8_colors,
        dither=args.dither,
        logger=print,
    )
    print(f"\nDone. total={total}, ok={ok}, failed={total - ok}")
    return 0


class ConverterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Конвертер изображений в PNG")
        self.geometry("900x760")
        self.minsize(820, 620)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.force_rgba_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.delete_source_var = tk.BooleanVar(value=False)
        self.optimize_var = tk.BooleanVar(value=True)
        self.compress_level_var = tk.IntVar(value=6)
        self.resize_mode_var = tk.StringVar(value="none")
        self.resize_percent_var = tk.IntVar(value=100)
        self.max_side_var = tk.IntVar(value=2048)
        self.png8_var = tk.BooleanVar(value=False)
        self.png8_colors_var = tk.IntVar(value=256)
        self.dither_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Готово")
        self._tooltips: list[Tooltip] = []

        self._build_ui()

    def _attach_tooltip(self, widget: tk.Widget, text: str):
        self._tooltips.append(Tooltip(widget, text))

    def _set_input_path(self, path: str):
        self.input_var.set(path)
        self._auto_fill_output_from_input(force=True)

    def _auto_fill_output_from_input(self, force: bool = False):
        if not force and self.output_var.get().strip():
            return

        raw_input = self.input_var.get().strip()
        if not raw_input:
            return

        inp = Path(raw_input)
        if inp.exists():
            out = inp if inp.is_dir() else inp.parent
        else:
            out = inp.parent if inp.suffix else inp

        if str(out):
            self.output_var.set(str(out))

    def _update_resize_state(self):
        mode = self.resize_mode_var.get()
        self.resize_percent_spin.configure(state="normal" if mode == "percent" else "disabled")
        self.max_side_spin.configure(state="normal" if mode == "max_side" else "disabled")

    def _update_png8_state(self):
        state = "normal" if self.png8_var.get() else "disabled"
        self.png8_colors_spin.configure(state=state)
        self.dither_chk.configure(state=state)

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
        self.input_entry.bind("<FocusOut>", lambda _e: self._auto_fill_output_from_input(force=False))
        self.input_entry.bind("<Return>", lambda _e: self._auto_fill_output_from_input(force=False))
        self.input_file_btn = ttk.Button(input_line, text="Файл", command=self._pick_input_file)
        self.input_file_btn.pack(side="left", padx=(8, 0))
        self.input_folder_btn = ttk.Button(input_line, text="Папка", command=self._pick_input_folder)
        self.input_folder_btn.pack(side="left", padx=(6, 0))
        self._attach_tooltip(
            self.input_entry,
            "Путь к входному файлу или папке.\n"
            "Поддержка: TIFF, TGA, JPEG, BMP, GIF, WEBP, PSD.\n"
            "Можно указать вручную или выбрать через диалог.",
        )
        self._attach_tooltip(self.input_file_btn, "Выбрать один файл для конвертации.")
        self._attach_tooltip(
            self.input_folder_btn,
            "Выбрать папку для пакетной обработки.\n"
            "При включенной рекурсии будут обработаны подпапки.",
        )

        output_row = ttk.Frame(main)
        output_row.pack(fill="x", pady=(0, 8))
        ttk.Label(output_row, text="Выходная папка (заполняется автоматически):").pack(anchor="w")
        output_line = ttk.Frame(output_row)
        output_line.pack(fill="x", pady=(4, 0))
        self.output_entry = ttk.Entry(output_line, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True)
        self.output_btn = ttk.Button(output_line, text="Выбрать", command=self._pick_output_folder)
        self.output_btn.pack(side="left", padx=(8, 0))
        self._attach_tooltip(
            self.output_entry,
            "Папка для сохранения PNG.\n"
            "По умолчанию берется папка входного файла/папки,\n"
            "но ее можно изменить вручную.",
        )
        self._attach_tooltip(self.output_btn, "Выбрать другую выходную папку.")

        options_basic = ttk.LabelFrame(main, text="Основные параметры", padding=8)
        options_basic.pack(fill="x", pady=(0, 8))
        self.recursive_chk = ttk.Checkbutton(
            options_basic, text="Рекурсивно для папок", variable=self.recursive_var
        )
        self.recursive_chk.pack(anchor="w")
        self.force_rgba_chk = ttk.Checkbutton(
            options_basic, text="Принудительно RGBA", variable=self.force_rgba_var
        )
        self.force_rgba_chk.pack(anchor="w")
        self.overwrite_chk = ttk.Checkbutton(
            options_basic, text="Перезаписывать PNG, если уже есть", variable=self.overwrite_var
        )
        self.overwrite_chk.pack(anchor="w")
        self.delete_source_chk = ttk.Checkbutton(
            options_basic,
            text="Удалять исходники после успешной конвертации",
            variable=self.delete_source_var,
        )
        self.delete_source_chk.pack(anchor="w")

        self._attach_tooltip(
            self.recursive_chk,
            "Для входной папки: обрабатывать также файлы во вложенных папках.",
        )
        self._attach_tooltip(
            self.force_rgba_chk,
            "Всегда сохранять PNG в режиме RGBA.\n"
            "Полезно, если нужен гарантированный альфа-канал.",
        )
        self._attach_tooltip(
            self.overwrite_chk,
            "Если PNG уже существует, заменять его новым.\n"
            "Если выключено, существующие PNG будут пропущены.",
        )
        self._attach_tooltip(
            self.delete_source_chk,
            "Удалять исходный файл только после успешной конвертации.\n"
            "Ошибочные и пропущенные файлы не удаляются.",
        )

        options_png = ttk.LabelFrame(main, text="Параметры PNG", padding=8)
        options_png.pack(fill="x", pady=(0, 8))

        png_line1 = ttk.Frame(options_png)
        png_line1.pack(fill="x", pady=(0, 4))
        self.optimize_chk = ttk.Checkbutton(
            png_line1,
            text="Optimize (доп. оптимизация PNG)",
            variable=self.optimize_var,
        )
        self.optimize_chk.pack(side="left")

        png_line2 = ttk.Frame(options_png)
        png_line2.pack(fill="x", pady=(0, 4))
        ttk.Label(png_line2, text="Степень сжатия (0..9):").pack(side="left")
        self.compress_spin = ttk.Spinbox(
            png_line2,
            from_=0,
            to=9,
            textvariable=self.compress_level_var,
            width=5,
        )
        self.compress_spin.pack(side="left", padx=(8, 0))

        png_line3 = ttk.Frame(options_png)
        png_line3.pack(fill="x", pady=(0, 0))
        self.png8_chk = ttk.Checkbutton(
            png_line3,
            text="PNG-8 (палитра)",
            variable=self.png8_var,
            command=self._update_png8_state,
        )
        self.png8_chk.pack(side="left")
        ttk.Label(png_line3, text="Цветов:").pack(side="left", padx=(16, 4))
        self.png8_colors_spin = ttk.Spinbox(
            png_line3,
            from_=2,
            to=256,
            textvariable=self.png8_colors_var,
            width=6,
        )
        self.png8_colors_spin.pack(side="left")
        self.dither_chk = ttk.Checkbutton(png_line3, text="Dithering", variable=self.dither_var)
        self.dither_chk.pack(side="left", padx=(12, 0))

        self._attach_tooltip(
            self.optimize_chk,
            "Дополнительная оптимизация PNG.\n"
            "Обычно уменьшает размер файла, но может замедлить конвертацию.",
        )
        self._attach_tooltip(
            self.compress_spin,
            "Уровень сжатия PNG:\n"
            "0 = быстрее, файл больше\n"
            "9 = медленнее, файл меньше\n"
            "На практике 5-7 обычно оптимальный баланс.",
        )
        self._attach_tooltip(
            self.png8_chk,
            "Сохранять PNG в индексированной палитре (до 256 цветов).\n"
            "Сильно уменьшает размер, но может ухудшить цветопередачу.",
        )
        self._attach_tooltip(
            self.png8_colors_spin,
            "Количество цветов палитры PNG-8 (2..256).\n"
            "Меньше цветов = меньше размер, но больше потери качества.",
        )
        self._attach_tooltip(
            self.dither_chk,
            "Сглаживает переходы цветов в PNG-8 шумовым узором.\n"
            "Обычно картинка выглядит лучше, но файл может стать чуть больше.",
        )

        options_resize = ttk.LabelFrame(main, text="Изменение размера", padding=8)
        options_resize.pack(fill="x", pady=(0, 8))

        self.resize_none_rb = ttk.Radiobutton(
            options_resize,
            text="Без изменения",
            variable=self.resize_mode_var,
            value="none",
            command=self._update_resize_state,
        )
        self.resize_none_rb.pack(anchor="w")

        resize_percent_line = ttk.Frame(options_resize)
        resize_percent_line.pack(fill="x", pady=(2, 0))
        self.resize_percent_rb = ttk.Radiobutton(
            resize_percent_line,
            text="Процент от оригинала:",
            variable=self.resize_mode_var,
            value="percent",
            command=self._update_resize_state,
        )
        self.resize_percent_rb.pack(side="left")
        self.resize_percent_spin = ttk.Spinbox(
            resize_percent_line,
            from_=1,
            to=1000,
            textvariable=self.resize_percent_var,
            width=8,
        )
        self.resize_percent_spin.pack(side="left", padx=(8, 4))
        ttk.Label(resize_percent_line, text="%").pack(side="left")

        resize_max_line = ttk.Frame(options_resize)
        resize_max_line.pack(fill="x", pady=(2, 0))
        self.resize_max_rb = ttk.Radiobutton(
            resize_max_line,
            text="Ограничить макс. сторону:",
            variable=self.resize_mode_var,
            value="max_side",
            command=self._update_resize_state,
        )
        self.resize_max_rb.pack(side="left")
        self.max_side_spin = ttk.Spinbox(
            resize_max_line,
            from_=1,
            to=20000,
            textvariable=self.max_side_var,
            width=8,
        )
        self.max_side_spin.pack(side="left", padx=(8, 4))
        ttk.Label(resize_max_line, text="px").pack(side="left")

        self._attach_tooltip(
            self.resize_none_rb,
            "Оставить исходный размер без масштабирования.",
        )
        self._attach_tooltip(
            self.resize_percent_spin,
            "Масштаб изображения в процентах.\n"
            "50 = уменьшить в 2 раза, 200 = увеличить в 2 раза.",
        )
        self._attach_tooltip(
            self.max_side_spin,
            "Ограничение на длинную сторону изображения в пикселях.\n"
            "Пропорции сохраняются автоматически.",
        )

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
        self._update_resize_state()
        self._update_png8_state()

    def _pick_input_file(self):
        path = filedialog.askopenfilename(
            title="Выберите файл изображения",
            filetypes=[("Поддерживаемые изображения", FILE_DIALOG_PATTERN), ("Все файлы", "*.*")],
        )
        if path:
            self._set_input_path(path)

    def _pick_input_folder(self):
        path = filedialog.askdirectory(title="Выберите входную папку")
        if path:
            self._set_input_path(path)

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
        for widget in (
            self.convert_btn,
            self.input_entry,
            self.output_entry,
            self.input_file_btn,
            self.input_folder_btn,
            self.output_btn,
            self.recursive_chk,
            self.force_rgba_chk,
            self.overwrite_chk,
            self.delete_source_chk,
            self.optimize_chk,
            self.compress_spin,
            self.png8_chk,
            self.png8_colors_spin,
            self.dither_chk,
            self.resize_none_rb,
            self.resize_percent_rb,
            self.resize_percent_spin,
            self.resize_max_rb,
            self.max_side_spin,
        ):
            widget.configure(state=state)

        if not running:
            self._update_resize_state()
            self._update_png8_state()

    def _on_convert(self):
        input_value = self.input_var.get().strip()

        if not input_value:
            messagebox.showerror("Ошибка", "Выберите входной файл или папку.")
            return

        inp = Path(input_value)
        if not inp.exists():
            messagebox.showerror("Ошибка", f"Входной путь не существует:\n{inp}")
            return

        if inp.is_file() and inp.suffix.lower() not in SOURCE_EXTS:
            messagebox.showerror(
                "Ошибка",
                f"Неподдерживаемый формат: {inp.suffix}\n"
                f"Поддерживаются: {', '.join(sorted(SOURCE_EXTS))}",
            )
            return

        try:
            compress_level = int(self.compress_level_var.get())
        except Exception:
            messagebox.showerror("Ошибка", "Степень сжатия должна быть целым числом 0..9.")
            return
        if compress_level < 0 or compress_level > 9:
            messagebox.showerror("Ошибка", "Степень сжатия должна быть в диапазоне 0..9.")
            return

        try:
            png8_colors = int(self.png8_colors_var.get())
        except Exception:
            messagebox.showerror("Ошибка", "Количество цветов PNG-8 должно быть целым числом.")
            return
        if png8_colors < 2 or png8_colors > 256:
            messagebox.showerror("Ошибка", "Количество цветов PNG-8 должно быть в диапазоне 2..256.")
            return

        resize_mode = self.resize_mode_var.get()
        resize_percent = 100
        max_side = 0
        if resize_mode == "percent":
            try:
                resize_percent = int(self.resize_percent_var.get())
            except Exception:
                messagebox.showerror("Ошибка", "Процент масштабирования должен быть целым числом.")
                return
            if resize_percent <= 0:
                messagebox.showerror("Ошибка", "Процент масштабирования должен быть больше 0.")
                return
        elif resize_mode == "max_side":
            try:
                max_side = int(self.max_side_var.get())
            except Exception:
                messagebox.showerror("Ошибка", "Максимальная сторона должна быть целым числом.")
                return
            if max_side <= 0:
                messagebox.showerror("Ошибка", "Максимальная сторона должна быть больше 0.")
                return

        if self.delete_source_var.get():
            confirmed = messagebox.askyesno(
                "Подтверждение удаления",
                "Исходные файлы будут удаляться после успешной конвертации. Продолжить?",
            )
            if not confirmed:
                return

        self._auto_fill_output_from_input(force=False)
        output_value = self.output_var.get().strip()
        out_root = Path(output_value) if output_value else None
        if out_root is not None and out_root.exists() and out_root.is_file():
            messagebox.showerror("Ошибка", "Выходной путь указывает на файл, а не на папку.")
            return

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
                delete_source=self.delete_source_var.get(),
                optimize=self.optimize_var.get(),
                compress_level=compress_level,
                resize_mode=resize_mode,
                resize_percent=resize_percent,
                max_side=max_side,
                png8=self.png8_var.get(),
                png8_colors=png8_colors,
                dither=self.dither_var.get(),
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
