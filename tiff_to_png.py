from __future__ import annotations

import argparse
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageSequence

TIFF_EXTS = {".tif", ".tiff"}


def convert_one(src: Path, dst: Path, force_rgba: bool, overwrite: bool) -> str:
    if dst.exists() and not overwrite:
        return f"skip (exists): {dst.name}"

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

    return f"ok: {dst.name}"


def iter_sources(root: Path, recursive: bool):
    if root.is_file():
        if root.suffix.lower() in TIFF_EXTS:
            yield root
        return

    if recursive:
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in TIFF_EXTS:
                yield p
    else:
        for p in root.glob("*"):
            if p.is_file() and p.suffix.lower() in TIFF_EXTS:
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
            if msg.startswith("ok"):
                ok += 1
            logger(f"{src.name} -> {msg}")
        except Exception as exc:
            logger(f"{src.name} -> ERROR: {exc}")

    return total, ok


def run_cli() -> int:
    parser = argparse.ArgumentParser(
        description="Batch convert TIFF/TIF to PNG preserving alpha when present."
    )
    parser.add_argument("input", type=str, help="Input file or folder with .tif/.tiff")
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
        self.title("TIFF to PNG Converter")
        self.geometry("760x520")
        self.minsize(640, 420)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.force_rgba_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        input_row = ttk.Frame(main)
        input_row.pack(fill="x", pady=(0, 8))
        ttk.Label(input_row, text="Input (file or folder):").pack(anchor="w")
        input_line = ttk.Frame(input_row)
        input_line.pack(fill="x", pady=(4, 0))
        self.input_entry = ttk.Entry(input_line, textvariable=self.input_var)
        self.input_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(input_line, text="File", command=self._pick_input_file).pack(side="left", padx=(8, 0))
        ttk.Button(input_line, text="Folder", command=self._pick_input_folder).pack(side="left", padx=(6, 0))

        output_row = ttk.Frame(main)
        output_row.pack(fill="x", pady=(0, 8))
        ttk.Label(output_row, text="Output folder (optional):").pack(anchor="w")
        output_line = ttk.Frame(output_row)
        output_line.pack(fill="x", pady=(4, 0))
        self.output_entry = ttk.Entry(output_line, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(output_line, text="Browse", command=self._pick_output_folder).pack(
            side="left", padx=(8, 0)
        )

        options = ttk.LabelFrame(main, text="Options", padding=8)
        options.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(options, text="Recursive for folders", variable=self.recursive_var).pack(
            anchor="w"
        )
        ttk.Checkbutton(options, text="Force RGBA", variable=self.force_rgba_var).pack(anchor="w")
        ttk.Checkbutton(options, text="Overwrite PNG if exists", variable=self.overwrite_var).pack(
            anchor="w"
        )

        controls = ttk.Frame(main)
        controls.pack(fill="x", pady=(0, 8))
        self.convert_btn = ttk.Button(controls, text="Convert", command=self._on_convert)
        self.convert_btn.pack(side="left")

        ttk.Label(main, text="Log:").pack(anchor="w")
        log_wrap = ttk.Frame(main)
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_wrap, height=12, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="left", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        status = ttk.Label(main, textvariable=self.status_var)
        status.pack(anchor="w", pady=(8, 0))

    def _pick_input_file(self):
        path = filedialog.askopenfilename(
            title="Select TIFF file",
            filetypes=[("TIFF files", "*.tif *.tiff"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)

    def _pick_input_folder(self):
        path = filedialog.askdirectory(title="Select input folder")
        if path:
            self.input_var.set(path)

    def _pick_output_folder(self):
        path = filedialog.askdirectory(title="Select output folder")
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
            messagebox.showerror("Error", "Select input file or folder.")
            return

        inp = Path(input_value)
        if not inp.exists():
            messagebox.showerror("Error", f"Input does not exist:\n{inp}")
            return

        out_root = Path(output_value) if output_value else None
        self.status_var.set("Converting...")
        self._set_running(True)
        self._append_log("---- Conversion started ----")

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
            summary = f"Done. total={total}, ok={ok}, failed={failed}"
            self._append_log(summary)
            self.status_var.set(summary)
            messagebox.showinfo("Finished", summary)
        except Exception as exc:
            self._append_log(f"ERROR: {exc}")
            self.status_var.set("Failed")
            messagebox.showerror("Error", str(exc))
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
