from __future__ import annotations

import argparse
import os
import importlib.util
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_META_FILE = "app_meta.py"
DEFAULT_OUTPUT_ROOT = Path(r"D:\_BUILD")
DEFAULT_BUILD_ROOT = Path("build") / "pyinstaller"
DEFAULT_INSTALLER_SCRIPT = Path("tools") / "installer.iss"
DEFAULT_INNO_COMPILER = Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")
DEFAULT_ENTRY_POINT = Path("tiff_to_png.py")
DEFAULT_ICON_FILE = Path("ico") / "favicon.ico"
DEFAULT_REQUIREMENTS_FILE = Path("requirements.txt")
REQUIRED_BUNDLED_DATA = ((Path("ico"), "ico"),)
REQUIRED_RUNTIME_FILES = (Path("app_settings.json"),)
OPTIONAL_RUNTIME_FILES = (Path("conversion_presets.json"),)
PYINSTALLER_HIDDEN_IMPORTS = (
    "PyQt6.sip",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PIL._tkinter_finder",
)
PYINSTALLER_COLLECT_ALL = ("PIL",)
INNO_BUILD_ATTEMPTS = 3
INNO_RETRY_DELAY_SECONDS = 2.0


class BuildError(RuntimeError):
    """Raised when the release build cannot continue."""


@dataclass(frozen=True)
class AppMeta:
    product: str
    name: str
    version: str
    publisher: str
    installer_guid: str

    @property
    def exe_name(self) -> str:
        return f"{self.product}.exe"


@dataclass(frozen=True)
class BuildLayout:
    project_root: Path
    output_root: Path
    build_root: Path
    release_dir: Path
    installer_dir: Path
    version_build_root: Path
    dist_root: Path
    work_root: Path
    spec_root: Path
    version_file: Path
    installer_script: Path
    inno_compiler: Path
    entry_point: Path
    icon_file: Path
    app_meta_file: Path
    requirements_file: Path

    @property
    def dist_app_dir(self) -> Path:
        return self.dist_root


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a standalone release and installer.")
    parser.add_argument(
        "--bump-part",
        choices=("patch", "minor", "major", "none"),
        default="none",
        help="Increment app version after preflight and before the build.",
    )
    parser.add_argument(
        "--no-bump-version",
        action="store_true",
        help="Do not modify APP_VERSION even if --bump-part is provided.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Final release root. Default: {DEFAULT_OUTPUT_ROOT}",
    )
    parser.add_argument(
        "--installer-script",
        type=Path,
        default=DEFAULT_INSTALLER_SCRIPT,
        help=f"Inno Setup script path. Default: {DEFAULT_INSTALLER_SCRIPT}",
    )
    parser.add_argument(
        "--inno-compiler",
        type=Path,
        default=DEFAULT_INNO_COMPILER,
        help=f"ISCC.exe path. Default: {DEFAULT_INNO_COMPILER}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and commands without changing files or launching tools.",
    )
    parser.add_argument(
        "--skip-installer",
        action="store_true",
        help="Build the application only and skip the Inno Setup step.",
    )
    parser.add_argument(
        "--keep-build",
        action="store_true",
        help="Do not delete intermediate build folders before the build.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print additional diagnostic information.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.no_bump_version:
        args.bump_part = "none"

    project_root = Path(__file__).resolve().parents[1]
    layout_seed = build_layout(project_root, args.output_root, "0.0.0", args)
    ensure_build_dependencies(layout_seed, args)
    app_meta = load_app_meta(layout_seed.app_meta_file)

    preflight(app_meta, build_layout(project_root, args.output_root, app_meta.version, args), args)

    target_version = app_meta.version
    if args.bump_part != "none":
        target_version = bump_version(app_meta.version, args.bump_part)
        if args.dry_run:
            print(f"[INFO] dry-run: version would be bumped from {app_meta.version} to {target_version}")
        else:
            update_app_version(layout_seed.app_meta_file, target_version)
            app_meta = load_app_meta(layout_seed.app_meta_file)
            print(f"[OK] APP_VERSION updated to {app_meta.version}")

    layout = build_layout(project_root, args.output_root, target_version, args)
    print_plan(app_meta, layout, args)

    if args.dry_run:
        build_pyinstaller_command(app_meta, layout)
        if not args.skip_installer:
            build_inno_command(app_meta, layout)
        return 0

    prepare_directories(layout, args)
    write_version_file(app_meta, layout.version_file)

    pyinstaller_command = build_pyinstaller_command(app_meta, layout)
    run_command(pyinstaller_command, cwd=layout.project_root)
    copy_release_payload(app_meta, layout)
    verify_release_payload(app_meta, layout)

    if not args.skip_installer:
        installer_command = build_inno_command(app_meta, layout)
        run_command(
            installer_command,
            cwd=layout.project_root,
            attempts=INNO_BUILD_ATTEMPTS,
            retry_delay_seconds=INNO_RETRY_DELAY_SECONDS,
            retry_hint=(
                "Inno Setup output may be temporarily locked by antivirus or indexing."
            ),
        )
        verify_installer_output(app_meta, layout)

    print(f"[OK] Release directory: {layout.release_dir}")
    if not args.skip_installer:
        print(f"[OK] Installer directory: {layout.installer_dir}")
    return 0


def build_layout(project_root: Path, output_root: Path, version: str, args: argparse.Namespace) -> BuildLayout:
    app_meta_file = project_root / APP_META_FILE
    build_root = project_root / DEFAULT_BUILD_ROOT
    version_build_root = build_root / version
    release_dir = output_root / load_app_product(app_meta_file)
    installer_dir = release_dir / "installer"
    return BuildLayout(
        project_root=project_root,
        output_root=output_root,
        build_root=build_root,
        release_dir=release_dir,
        installer_dir=installer_dir,
        version_build_root=version_build_root,
        dist_root=version_build_root / "dist",
        work_root=version_build_root / "work",
        spec_root=version_build_root / "spec",
        version_file=version_build_root / "version_info.txt",
        installer_script=(project_root / args.installer_script).resolve()
        if not args.installer_script.is_absolute()
        else args.installer_script,
        inno_compiler=args.inno_compiler,
        entry_point=project_root / DEFAULT_ENTRY_POINT,
        icon_file=project_root / DEFAULT_ICON_FILE,
        app_meta_file=app_meta_file,
        requirements_file=project_root / DEFAULT_REQUIREMENTS_FILE,
    )


def load_app_product(app_meta_file: Path) -> str:
    if not app_meta_file.exists():
        return "APP"
    text = app_meta_file.read_text(encoding="utf-8")
    match = re.search(r'^APP_PRODUCT\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    return match.group(1) if match else "APP"


def load_app_meta(app_meta_file: Path) -> AppMeta:
    if not app_meta_file.exists():
        raise BuildError(f"App metadata file not found: {app_meta_file}")
    spec = importlib.util.spec_from_file_location("app_meta", app_meta_file)
    if spec is None or spec.loader is None:
        raise BuildError(f"Unable to load app metadata from: {app_meta_file}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        product = str(module.APP_PRODUCT)
        name = str(module.APP_NAME)
        version = str(module.APP_VERSION)
    except AttributeError as exc:
        raise BuildError(f"Missing required metadata attribute: {exc}") from exc

    publisher = str(getattr(module, "APP_PUBLISHER", name))
    installer_guid = str(getattr(module, "APP_INSTALLER_GUID", "")).strip()
    if not installer_guid:
        raise BuildError("APP_INSTALLER_GUID must be defined in app_meta.py")

    return AppMeta(
        product=product,
        name=name,
        version=version,
        publisher=publisher,
        installer_guid=installer_guid,
    )


def preflight(app_meta: AppMeta, layout: BuildLayout, args: argparse.Namespace) -> None:
    errors: list[str] = []
    warnings: list[str] = []

    required_paths = (
        ("entry point", layout.entry_point),
        ("icon", layout.icon_file),
        ("app metadata", layout.app_meta_file),
    )
    for label, path in required_paths:
        if not path.exists():
            errors.append(f"Missing {label}: {path}")

    if not layout.requirements_file.exists():
        warnings.append(f"requirements file not found: {layout.requirements_file}")

    if not layout.installer_script.exists():
        errors.append(f"Installer script not found: {layout.installer_script}")

    if not args.skip_installer and not layout.inno_compiler.exists():
        errors.append(f"Inno Setup compiler not found: {layout.inno_compiler}")

    missing_runtime = [path for path in REQUIRED_RUNTIME_FILES if not (layout.project_root / path).exists()]
    for runtime_path in missing_runtime:
        errors.append(f"Required runtime file not found: {layout.project_root / runtime_path}")

    missing_optional_runtime = [
        path for path in OPTIONAL_RUNTIME_FILES if not (layout.project_root / path).exists()
    ]
    for runtime_path in missing_optional_runtime:
        warnings.append(f"Optional runtime file not found: {layout.project_root / runtime_path}")

    pyinstaller_check = [sys.executable, "-m", "PyInstaller", "--version"]
    try:
        result = subprocess.run(
            pyinstaller_check,
            cwd=layout.project_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        errors.append(f"Unable to run PyInstaller preflight: {exc}")
    else:
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout).strip()
            message = f"PyInstaller is not available: {stderr}" if stderr else (
                "PyInstaller is not available in the selected Python environment."
            )
            if args.dry_run:
                warnings.append(message)
            else:
                errors.append(message)
        elif args.verbose:
            version_output = (result.stdout or result.stderr).strip()
            if version_output:
                print(f"[INFO] PyInstaller version: {version_output}")

    if warnings:
        for warning in warnings:
            print(f"[WARN] {warning}")

    if errors:
        for error in errors:
            print(f"[ERROR] {error}")
        raise BuildError("Preflight failed.")

    print(
        f"[OK] Preflight passed for {app_meta.name} {app_meta.version}"
        + (" (installer skipped)" if args.skip_installer else "")
    )


def ensure_build_dependencies(layout: BuildLayout, args: argparse.Namespace) -> None:
    if args.dry_run:
        return

    if layout.requirements_file.exists():
        install_requirements = [sys.executable, "-m", "pip", "install", "-r", str(layout.requirements_file)]
        print(f"[CMD] {format_command(install_requirements)}")
        run_command(install_requirements, cwd=layout.project_root)

    pyinstaller_check = [sys.executable, "-m", "PyInstaller", "--version"]
    result = subprocess.run(
        pyinstaller_check,
        cwd=layout.project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return

    install_pyinstaller = [sys.executable, "-m", "pip", "install", "pyinstaller"]
    print(f"[CMD] {format_command(install_pyinstaller)}")
    run_command(install_pyinstaller, cwd=layout.project_root)


def bump_version(version: str, part: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if match is None:
        raise BuildError(f"Unsupported APP_VERSION format: {version!r}. Expected MAJOR.MINOR.PATCH.")

    major, minor, patch = (int(group) for group in match.groups())
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "patch":
        patch += 1
    elif part == "none":
        return version
    else:
        raise BuildError(f"Unsupported bump part: {part}")
    return f"{major}.{minor}.{patch}"


def update_app_version(app_meta_file: Path, new_version: str) -> None:
    text = app_meta_file.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'(^APP_VERSION\s*=\s*")([^"]+)(")',
        rf"\g<1>{new_version}\g<3>",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise BuildError(f"Failed to update APP_VERSION in {app_meta_file}")
    app_meta_file.write_text(updated, encoding="utf-8")


def print_plan(app_meta: AppMeta, layout: BuildLayout, args: argparse.Namespace) -> None:
    print("[INFO] Release plan")
    print(f"[INFO] Product: {app_meta.product}")
    print(f"[INFO] Name: {app_meta.name}")
    print(f"[INFO] Version: {app_meta.version}")
    print(f"[INFO] Entry point: {layout.entry_point}")
    print(f"[INFO] Icon: {layout.icon_file}")
    print(f"[INFO] Intermediate build root: {layout.version_build_root}")
    print(f"[INFO] Release directory: {layout.release_dir}")
    if args.skip_installer:
        print("[INFO] Installer: skipped")
    else:
        print(f"[INFO] Installer script: {layout.installer_script}")
        print(f"[INFO] Inno compiler: {layout.inno_compiler}")
        print(f"[INFO] Installer directory: {layout.installer_dir}")


def prepare_directories(layout: BuildLayout, args: argparse.Namespace) -> None:
    if not args.keep_build:
        safe_rmtree(layout.version_build_root, layout.project_root)
    safe_rmtree(layout.release_dir, layout.output_root)
    layout.dist_root.mkdir(parents=True, exist_ok=True)
    layout.work_root.mkdir(parents=True, exist_ok=True)
    layout.spec_root.mkdir(parents=True, exist_ok=True)
    layout.installer_dir.mkdir(parents=True, exist_ok=True)


def safe_rmtree(target: Path, boundary: Path) -> None:
    if not target.exists():
        return
    target_resolved = target.resolve()
    boundary_resolved = boundary.resolve()
    if target_resolved == boundary_resolved:
        raise BuildError(f"Refusing to delete boundary directory: {target_resolved}")
    if boundary_resolved not in target_resolved.parents:
        raise BuildError(f"Refusing to delete path outside boundary: {target_resolved}")
    shutil.rmtree(target_resolved)


def write_version_file(app_meta: AppMeta, version_file: Path) -> None:
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(build_version_info(app_meta), encoding="utf-8")


def build_version_info(app_meta: AppMeta) -> str:
    numbers = normalize_version_tuple(app_meta.version)
    dotted = ".".join(str(number) for number in numbers)
    exe_name = app_meta.exe_name
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', '{escape_version_string(app_meta.publisher)}'),
          StringStruct('FileDescription', '{escape_version_string(app_meta.name)}'),
          StringStruct('FileVersion', '{dotted}'),
          StringStruct('InternalName', '{escape_version_string(app_meta.product)}'),
          StringStruct('OriginalFilename', '{escape_version_string(exe_name)}'),
          StringStruct('ProductName', '{escape_version_string(app_meta.name)}'),
          StringStruct('ProductVersion', '{dotted}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)"""


def normalize_version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = version.split(".")
    if len(parts) != 3:
        raise BuildError(f"Unsupported APP_VERSION format: {version!r}. Expected MAJOR.MINOR.PATCH.")
    values = [int(part) for part in parts]
    values.append(0)
    return tuple(values)  # type: ignore[return-value]


def escape_version_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_pyinstaller_command(app_meta: AppMeta, layout: BuildLayout) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--noconsole",
        "--clean",
        "--name",
        app_meta.product,
        "--distpath",
        str(layout.dist_root),
        "--workpath",
        str(layout.work_root),
        "--specpath",
        str(layout.spec_root),
        "--icon",
        str(layout.icon_file),
        "--version-file",
        str(layout.version_file),
    ]
    for source_path, destination in REQUIRED_BUNDLED_DATA:
        command.extend(["--add-data", f"{layout.project_root / source_path};{destination}"])
    for package_name in PYINSTALLER_COLLECT_ALL:
        command.extend(["--collect-all", package_name])
    for module_name in PYINSTALLER_HIDDEN_IMPORTS:
        command.extend(["--hidden-import", module_name])
    command.append(str(layout.entry_point))
    print(f"[CMD] {format_command(command)}")
    return command


def build_inno_command(app_meta: AppMeta, layout: BuildLayout) -> list[str]:
    command = [
        str(layout.inno_compiler),
        str(layout.installer_script),
        f"/DMyAppId={{{{{app_meta.installer_guid}}}}}",
        f"/DMyAppProduct={app_meta.product}",
        f"/DMyAppName={app_meta.name}",
        f"/DMyAppVersion={app_meta.version}",
        f"/DMyAppExeName={app_meta.exe_name}",
        f"/DMyAppPublisher={app_meta.publisher}",
        f"/DMyAppBuildDir={layout.release_dir}",
        f"/DMyInstallerOutputDir={layout.installer_dir}",
        f"/DMySetupIconFile={layout.icon_file}",
    ]
    print(f"[CMD] {format_command(command)}")
    return command


def format_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def run_command(
    command: list[str],
    *,
    cwd: Path,
    attempts: int = 1,
    retry_delay_seconds: float = 0.0,
    retry_hint: str = "",
) -> None:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    env = os.environ.copy()
    if len(command) >= 4 and command[0] == sys.executable and command[1:3] == ["-m", "pip"]:
        env.setdefault("NO_PROXY", "*")
        env.setdefault("no_proxy", "*")

    for attempt in range(1, attempts + 1):
        result = subprocess.run(command, cwd=cwd, check=False, env=env)
        if result.returncode == 0:
            return
        if attempt == attempts:
            raise BuildError(
                f"Command failed with exit code {result.returncode}: "
                f"{format_command(command)}"
            )

        print(
            f"[WARN] Command failed with exit code {result.returncode} "
            f"(attempt {attempt}/{attempts})."
        )
        if retry_hint:
            print(f"[WARN] {retry_hint}")
        if retry_delay_seconds > 0:
            print(f"[INFO] Retrying in {retry_delay_seconds:g} seconds...")
            time.sleep(retry_delay_seconds)


def copy_release_payload(app_meta: AppMeta, layout: BuildLayout) -> None:
    built_release_dir = layout.dist_app_dir / app_meta.product
    if not built_release_dir.exists():
        raise BuildError(f"PyInstaller output not found: {built_release_dir}")
    shutil.copytree(built_release_dir, layout.release_dir, dirs_exist_ok=True)
    for relative_path in REQUIRED_RUNTIME_FILES:
        source = layout.project_root / relative_path
        destination = layout.release_dir / relative_path.name
        shutil.copy2(source, destination)
    for relative_path in OPTIONAL_RUNTIME_FILES:
        source = layout.project_root / relative_path
        if source.exists():
            destination = layout.release_dir / relative_path.name
            shutil.copy2(source, destination)


def verify_release_payload(app_meta: AppMeta, layout: BuildLayout) -> None:
    expected_files = [layout.release_dir / app_meta.exe_name]
    expected_files.extend(layout.release_dir / path.name for path in REQUIRED_RUNTIME_FILES)
    missing_files = [path for path in expected_files if not path.exists()]
    if missing_files:
        raise BuildError("Release payload is incomplete:\n" + "\n".join(str(path) for path in missing_files))
    print(f"[OK] Release payload verified in {layout.release_dir}")


def verify_installer_output(app_meta: AppMeta, layout: BuildLayout) -> None:
    expected_installer = layout.installer_dir / f"{app_meta.product}_{app_meta.version}_setup.exe"
    if not expected_installer.exists():
        raise BuildError(f"Installer not found: {expected_installer}")
    print(f"[OK] Installer verified: {expected_installer}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1)
