# Codex Handoff: как воспроизвести наш пайплайн сборки в другом репозитории

## Цель

Этот документ нужен для другого Codex и другого Windows-приложения.

Ожидаемый результат:

1. В репозитории должен появиться `build_release.bat`.
2. Батник должен запускать Python-скрипт сборки.
3. Сборка приложения должна идти через чистый `PyInstaller`, без `auto_py_to_exe`.
4. После сборки приложения должен автоматически собираться установщик через `Inno Setup Compiler`.
5. Готовый билд должен лежать в `D:\_BUILD\<APP_PRODUCT>\`.
6. Готовый установщик должен лежать в `D:\_BUILD\<APP_PRODUCT>\installer\`.
7. Приложение после сборки должно запускаться без дополнительных ручных установок библиотек на машине пользователя.

---

## Базовая структура решения

В новом репозитории release pipeline должен состоять из четырех ключевых файлов:

- `build_release.bat`
- `tools/build_release.py`
- `tools/installer.iss`
- `app_meta.py` или аналогичного файла с метаданными приложения

Дополнительно могут использоваться:

- `hooks/hook-OpenGL.py`
- иконка приложения, например `my_app.ico`
- дополнительные папки данных, которые нужно положить внутрь сборки
- локальная папка `vendor\` или `build\runtime\` для внешних runtime-файлов

Важно:

- старые `.spec` и конфиги GUI-сборщиков могут оставаться как legacy-артефакты, но не должны быть основой нового release pipeline;
- Новый пайплайн должен строиться именно на `PyInstaller` CLI + `Inno Setup`.
- `auto_py_to_exe` не нужен.

---

## Что обязательно должно делать решение в новом репозитории

### 1. Батник должен быть тонкой оберткой

`build_release.bat` не должен содержать логику сборки.
Он должен:

- определить корень репозитория;
- найти Python внутри `.venv`;
- запустить `tools/build_release.py`;
- вернуть код ошибки наружу;
- печатать понятные сообщения `[ERROR]` / `[OK]`.

Минимальный шаблон:

```bat
@echo off
setlocal

set "ROOT=%~dp0"
if "%VENV_DIR%"=="" set "VENV_DIR=.venv"
set "PYTHON_EXE=%ROOT%%VENV_DIR%\Scripts\python.exe"
if "%VENV_DIR:~1,1%"==":" set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo [ERROR] venv not found: "%PYTHON_EXE%"
  echo Activate/create virtual environment first.
  exit /b 2
)

set "SCRIPT=%ROOT%tools\build_release.py"
if not exist "%SCRIPT%" (
  echo [ERROR] Script not found: "%SCRIPT%"
  exit /b 2
)

"%PYTHON_EXE%" "%SCRIPT%" %*

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo [ERROR] Release build failed with code %RC%.
  exit /b %RC%
)

echo [OK] Release build finished.
exit /b 0
```

---

### 2. Метаданные приложения должны жить в одном месте

Нужен файл наподобие `app_meta.py`, из которого Python-сборщик читает:

- техническое имя продукта;
- пользовательское имя;
- версию.

Пример:

```python
APP_PRODUCT = "MY_APP"
APP_NAME = "My Application"
APP_VERSION = "1.2.3"

APP_TITLE = f"{APP_NAME} {APP_VERSION}"
APP_ID = f"{APP_PRODUCT}_{APP_VERSION}"
```

Почему это важно:

- имя exe должно зависеть от `APP_PRODUCT`;
- имя инсталлятора и версия должны зависеть от `APP_VERSION`;
- отображаемое имя в установщике должно зависеть от `APP_NAME`.

---

### 3. Главная логика сборки должна жить в Python

Файл `tools/build_release.py` должен делать всю основную работу.

Он должен:

1. Определить корень проекта.
2. Прочитать `APP_PRODUCT`, `APP_NAME`, `APP_VERSION` из `app_meta.py`.
3. Выполнить preflight-проверки: entry-point, иконка, Inno Setup, runtime-файлы, внешние exe/dll.
4. Только после успешного preflight при необходимости bump-ать версию.
5. Очистить временную build-папку.
6. Очистить конечную папку `D:\_BUILD\<APP_PRODUCT>`.
7. Собрать приложение через `PyInstaller`.
8. Скопировать итог из pyinstaller `dist` в `D:\_BUILD\<APP_PRODUCT>`.
9. Собрать инсталлятор через `ISCC.exe`.
10. Положить установщик в `D:\_BUILD\<APP_PRODUCT>\installer`.
11. Печатать итоговые команды целиком, чтобы их можно было отладить.

Критически важно:

- не увеличивать номер версии до проверки обязательных зависимостей;
- не очищать папки с runtime-источниками до того, как они скопированы в staging;
- при ошибке возвращать ненулевой `exit code`;
- `--dry-run` не должен менять файлы, bump-ать версию или чистить папки.

---

## Целевая структура выходных папок

Другой Codex должен сделать именно такую структуру:

```text
D:\_BUILD\
  <APP_PRODUCT>\
    <app files from PyInstaller>
    installer\
      <setup.exe>
```

Промежуточные pyinstaller-артефакты можно держать внутри репозитория:

```text
build\
  pyinstaller\
    <APP_VERSION>\
      dist\
      work\
```

Такую структуру удобно повторять во всех проектах: временные артефакты остаются внутри репозитория, а готовый релиз всегда лежит в стабильном внешнем каталоге.

---

## Версионирование и build number

Версия должна читаться из одного источника правды: `app_meta.py`, `app.py`, `pyproject.toml` или другого явного файла проекта.

Если сборщик умеет автоматически увеличивать версию или build number, порядок должен быть безопасным:

1. Загрузить текущую версию.
2. Выполнить preflight-проверки.
3. В `--dry-run` только показать будущую версию, но ничего не менять.
4. В реальной сборке изменить версию только после успешного preflight.
5. После изменения версии перечитать метаданные из файла.
6. Использовать новую версию в PyInstaller version file и Inno Setup.

Если PyInstaller или Inno Setup упали после bump-а, откатывать версию автоматически не обязательно, но в логе должно быть понятно, на каком шаге произошла ошибка.
Если preflight упал, версия не должна меняться вообще.

Для Windows exe желательно генерировать `VSVersionInfo`, чтобы свойства файла показывали ту же версию, что и установщик.

---

## Как должен выглядеть Python build script

Ниже не обязательно копировать код слово в слово, но поведение должно быть таким же.

### Обязательные константы

Скрипт должен иметь настраиваемые значения примерно такого вида:

```python
APP_META_FILE = "app_meta.py"
DEFAULT_OUTPUT_ROOT = Path(r"D:\_BUILD")
DEFAULT_BUILD_ROOT = Path("build") / "pyinstaller"
DEFAULT_INSTALLER_SCRIPT = Path("tools") / "installer.iss"
DEFAULT_INNO_COMPILER = Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")
```

Рекомендация:

- подпапку внутри `D:\_BUILD` лучше делать равной `APP_PRODUCT`;
- не зашивать в новом проекте чужое имя папки, если это уже другой продукт.

---

### Обязательные CLI-аргументы

Скрипт должен поддерживать хотя бы:

- `--no-bump-version`
- `--bump-part patch|minor|major|none`
- `--output-root`
- `--inno-compiler`
- `--installer-script`
- `--dry-run`

Это полезно для:

- CI;
- локальной отладки;
- ручной сборки релизов;
- предсказуемого воспроизведения шагов другим Codex.

Рекомендуемые дополнительные аргументы:

- `--require-<runtime>` для опциональных backend-ов, которые иногда нужно сделать обязательными;
- `--skip-installer`, если нужно проверить только PyInstaller-часть;
- `--keep-build`, если нужно не чистить временные артефакты при отладке;
- `--verbose`, если проекту полезен подробный лог preflight.

Правило для `--dry-run`:

- печатать план, пути и команды;
- не bump-ать версию;
- не создавать/не удалять папки;
- не перезаписывать version-файлы;
- не запускать PyInstaller и Inno Setup.

---

### Что должен делать шаг PyInstaller

Скрипт должен собирать команду вида:

```text
python -m PyInstaller --noconfirm --onedir --noconsole --clean ...
```

Обязательные параметры:

- `--noconfirm`
- `--onedir`
- `--noconsole`
- `--clean`
- `--name <APP_PRODUCT>`
- `--distpath <build-dist-dir>`
- `--workpath <build-work-dir>`
- `--specpath <build-work-dir>`

Если в проекте есть иконка, добавить:

- `--icon <icon.ico>`

Если есть кастомные hooks, добавить:

- `--additional-hooks-dir <hooks-dir>`

Если есть папки данных, добавить для каждой:

- `--add-data "<source>;<dest>"`

Если есть скрытые зависимости, добавить:

- `--hidden-import <module>`

Если есть пакеты, которые PyInstaller плохо подбирает автоматически, добавить:

- `--collect-all <package>`

Если нужно исключить конфликтующие пакеты, добавить:

- `--exclude-module <module>`

Финальным аргументом должен идти entry-point, обычно:

- `main.py`

---

## Автономность и runtime-зависимости

Главное требование для release pipeline: установленная программа должна работать на машине пользователя без Python, IDE и ручной доустановки библиотек.

Для этого Codex должен разделить зависимости на две группы:

- обязательные runtime-зависимости, без которых приложение не выполняет базовый сценарий;
- опциональные runtime-зависимости, которые включают отдельный backend, импорт, preview, codec, plugin или интеграцию.

Примеры обязательных runtime-файлов:

- внешние `.exe`, которые приложение вызывает через `subprocess`;
- `.dll`, без которых импорт или запуск падает;
- Qt plugins, imageformats, platforms, multimedia backend-и;
- модели, шаблоны, темы, базы данных, config-файлы;
- ffmpeg/ffprobe, если без них основной сценарий приложения считается неполным.

Примеры опциональных runtime-файлов:

- альтернативный video/audio backend;
- импорт редкого формата;
- интеграция с внешней программой, которая не нужна для обычного запуска;
- экспериментальные плагины.

Рекомендуемая политика:

- обязательная зависимость не найдена: сборка должна падать до bump версии;
- опциональная зависимость не найдена: сборка может продолжаться, но должна печатать `[WARN]`;
- если опциональную зависимость нужно сделать обязательной для конкретного релиза, добавить флаг вида `--require-<feature>`;
- в логах явно писать, какие пути были проверены.

Где искать runtime-файлы:

```text
build\runtime\<name>\
vendor\<name>\
D:\_BUILD\<APP_PRODUCT>\vendor\<name>\
старый локальный dist, если он есть и подходит
стандартные системные пути, например C:\Program Files\...
PATH, если это CLI-бинарник
```

Как staging должен работать:

1. Найти источник runtime-файлов.
2. Проверить минимальный набор файлов.
3. Скопировать их в `build\runtime\<name>`.
4. Передать `build\runtime\<name>` в PyInstaller через `--add-data`.
5. После сборки проверить, что в `D:\_BUILD\<APP_PRODUCT>\vendor\<name>` реально появились нужные файлы.

Нельзя:

- считать системно установленную программу достаточной для автономного релиза;
- добавлять `--add-data` на папку, которой нет;
- чистить `build\runtime` целиком, если пользователь мог положить туда runtime-источники;
- молча пропускать обязательные runtime-файлы.

Хорошая проверка после сборки:

```text
D:\_BUILD\<APP_PRODUCT>\vendor\<name>\...
```

Например для внешнего CLI:

```text
vendor\ffmpeg\ffmpeg.exe
vendor\ffmpeg\ffprobe.exe
```

Например для DLL runtime:

```text
vendor\vlc\libvlc.dll
vendor\vlc\libvlccore.dll
vendor\vlc\plugins\
```

Названия здесь только пример. В новом проекте Codex должен определить реальные runtime-зависимости самостоятельно.

---

## Как адаптировать зависимости под конкретный проект

Ниже пример того, как могут выглядеть проектные списки зависимостей.
Их нельзя копировать без анализа: в каждом репозитории Codex должен сам найти реальные папки данных, hidden imports, collect-all пакеты и excludes.

### Папки данных

```python
REQUIRED_DATAS = (
    ("nav", "nav"),
    ("templates", "templates"),
)
```

### Hidden imports

```python
REQUIRED_HIDDEN_IMPORTS = (
    "spellchecker",
    "fbx",
    "OpenGL",
    "OpenGL.GL",
    "OpenGL.GLU",
    "OpenGL.arrays.vbo",
    "OpenGL.platform.win32",
    "OpenGL_accelerate",
    "PyQt6.QtOpenGL",
    "PyQt6.QtOpenGLWidgets",
    "PyQt6.QtPrintSupport",
    "PyQt6.QtSvg",
)
```

### Collect all

```python
REQUIRED_COLLECT_ALL = (
    "spellchecker",
)
```

### Excludes

```python
REQUIRED_EXCLUDES = (
    "PyQt5",
)
```

Правило:

- не копировать этот список бездумно;
- но обязательно проанализировать проект и явно прописать проблемные зависимости, чтобы итоговая сборка работала на чистой машине.
- если зависимости не удалось подтвердить автоматически, явно написать риск в финальном ответе.

---

## Когда нужен custom hook

Если приложение использует PyOpenGL, Qt OpenGL, специфические DLL или платформенные backend-ы, может понадобиться `hooks/hook-<package>.py`.

У нас есть пример:

```python
from PyInstaller.compat import is_win
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

if is_win:
    hiddenimports = ["OpenGL.platform.win32"]
else:
    hiddenimports = []

hiddenimports += collect_submodules("OpenGL.arrays")

datas = []
if is_win:
    datas = collect_data_files(
        "OpenGL",
        includes=[
            "DLLS/*.txt",
            "DLLS/freeglut64.vc14.dll",
            "DLLS/gle64.vc14.dll",
        ],
    )
```

Если новый проект использует нестандартные runtime-файлы, Codex должен:

1. найти, что PyInstaller не подхватывает;
2. добавить hook;
3. подключить `--additional-hooks-dir`.

---

## Зачем после PyInstaller делается копирование в D:\_BUILD

Важный архитектурный момент:

- `PyInstaller` собирает во временную внутреннюю папку `build\pyinstaller\<version>\dist\<APP_PRODUCT>\`
- затем содержимое копируется в стабильную release-папку `D:\_BUILD\<APP_PRODUCT>\`
- уже из этой стабильной папки Inno Setup делает установщик

Так лучше, чем собирать инсталлер прямо из временного `dist`, потому что:

- проще руками проверять итоговый билд;
- проще хранить единый release-output в одном месте;
- проще делать повторную упаковку;
- проще отлаживать missing files.

---

## Как должен вызываться Inno Setup

Python-скрипт должен найти `ISCC.exe`.

По умолчанию можно ожидать путь:

```text
C:\Program Files (x86)\Inno Setup 6\ISCC.exe
```

Но должен быть CLI-переопределяемый аргумент:

- `--inno-compiler`

Запускать компилятор нужно с define-параметрами:

```text
/DMyAppName=...
/DMyAppVersion=...
/DMyAppExeName=...
/DMyAppBuildDir=...
/DMyInstallerOutputDir=...
/DMySetupIconFile=...
```

Это позволяет держать `installer.iss` общим и переиспользуемым.

---

## Каким должен быть installer.iss

Нужен универсальный `tools/installer.iss`, который принимает значения через define-переменные.

Базовый шаблон:

```iss
#ifndef MyAppName
#define MyAppName "MY_APP"
#endif

#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif

#ifndef MyAppExeName
#define MyAppExeName "MY_APP.exe"
#endif

#ifndef MyAppBuildDir
#define MyAppBuildDir "D:\_BUILD\MY_APP"
#endif

#ifndef MyInstallerOutputDir
#define MyInstallerOutputDir "D:\_BUILD\MY_APP\installer"
#endif

#ifndef MySetupIconFile
#define MySetupIconFile "C:\path\to\icon.ico"
#endif

[Setup]
AppId={{PUT-GUID-HERE}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir={#MyInstallerOutputDir}
OutputBaseFilename={#MyAppName}_{#MyAppVersion}_setup
SetupIconFile={#MySetupIconFile}
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MyAppBuildDir}\*"; DestDir: "{app}"; Excludes: "installer\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
```

Обязательно:

- использовать отдельный `AppId` GUID для каждого нового приложения;
- брать файлы из `D:\_BUILD\<APP_PRODUCT>`;
- исключать подпапку `installer\*`, чтобы не упаковать установщик внутрь самого себя.

---

## Алгоритм работы build_release.py

Другой Codex должен реализовать примерно такой порядок:

1. Найти корень репозитория.
2. Прочитать `app_meta.py`.
3. Найти entry-point, icon, resources, installer script, Inno Setup.
4. Найти и проверить обязательные runtime-зависимости.
5. Предупредить об отсутствующих опциональных runtime-зависимостях.
6. Посчитать целевую версию.
7. Если сборка не `--dry-run`, bump-нуть версию только после успешного preflight.
8. Подготовить:
   - `build\pyinstaller\<version>\dist`
   - `build\pyinstaller\<version>\work`
   - `D:\_BUILD\<APP_PRODUCT>`
   - `D:\_BUILD\<APP_PRODUCT>\installer`
9. Подготовить staging для внешних runtime-файлов.
10. Собрать полную команду PyInstaller.
11. Напечатать эту команду в лог.
12. Выполнить PyInstaller.
13. Проверить, что папка `dist\<APP_PRODUCT>` реально появилась.
14. Скопировать ее содержимое в `D:\_BUILD\<APP_PRODUCT>`.
15. Проверить, что обязательные runtime-файлы есть уже в финальной release-папке.
16. Собрать команду Inno Setup.
17. Напечатать ее в лог.
18. Выполнить Inno Setup.
19. Проверить, что setup `.exe` появился в `installer`.
20. Вернуть корректный `exit code`.

---

## Что Codex должен обязательно проверить в новом проекте

Перед тем как писать сборщик, он должен сам найти в репозитории:

- точку входа приложения;
- иконку;
- папки ресурсов;
- бинарные зависимости;
- скрытые импорты;
- пакеты, которые надо `collect-all`;
- пакеты, которые надо исключить;
- нужен ли custom hook;
- нужно ли копировать DLL отдельно.

Если проект использует:

- `PyQt6`
- `OpenGL`
- `fbx`
- плагины
- шаблоны
- базы данных
- json/yaml-конфиги
- внешние exe/dll

то все это должно попасть в сборку явно или через hooks.

---

## Критерий готовности

Работа считается завершенной только если выполнено все ниже:

1. `build_release.bat` запускается из корня репозитория.
2. Сборка идет через `.venv\Scripts\python.exe`.
3. PyInstaller успешно создает рабочий `onedir` билд.
4. Билд копируется в `D:\_BUILD\<APP_PRODUCT>`.
5. Inno Setup успешно создает установщик.
6. Установщик лежит в `D:\_BUILD\<APP_PRODUCT>\installer`.
7. Приложение запускается на машине без IDE и без ручной доустановки Python-зависимостей.
8. В логах сборки видны обе полные команды:
   - PyInstaller
   - Inno Setup
9. Обязательные runtime-файлы реально лежат внутри `D:\_BUILD\<APP_PRODUCT>`, а не только установлены на build-машине.
10. `--dry-run` проходит и не меняет рабочее дерево.
11. Если есть auto-bump версии, неудачный preflight не меняет версию.
12. PyInstaller warnings просмотрены; нерелевантные предупреждения можно оставить, но рисковые нужно устранить или явно описать.
13. Если возможно, выполнен smoke-test собранного `.exe` из `D:\_BUILD\<APP_PRODUCT>`.

---

## Чего делать не надо

- не строить pipeline вокруг `auto_py_to_exe`;
- не делать основную логику прямо в `.bat`;
- не хардкодить временные пути внутрь `dist` как единственное место релиза;
- не полагаться на `.spec`, если можно собрать все через Python CLI;
- не оставлять hidden imports и data files на усмотрение PyInstaller, если проект уже известен как сложный;
- не смешивать build output и installer output в непонятную структуру.

---

## Готовый prompt для другого Codex

Ниже текст, который можно дать другому Codex вместе с этим `.md`:

```text
Нужно реализовать в этом репозитории release pipeline по образцу из приложенного документа.

Требования:
1. Сделай build_release.bat как тонкую обертку над Python-скриптом.
2. Сделай tools/build_release.py, который:
   - читает APP_PRODUCT / APP_NAME / APP_VERSION из app_meta.py или аналогичного файла;
   - выполняет preflight до bump версии;
   - собирает приложение через PyInstaller;
   - складывает итоговый билд в D:\_BUILD\<APP_PRODUCT>;
   - затем собирает установщик через Inno Setup;
   - складывает установщик в D:\_BUILD\<APP_PRODUCT>\installer;
   - печатает полные команды сборки;
   - поддерживает безопасный --dry-run без изменений файлов;
   - возвращает корректный exit code.
3. Сделай tools/installer.iss с параметризацией через /D-переменные.
4. Не используй auto_py_to_exe.
5. Не делай ставку на legacy .spec, если можно собрать через Python-скрипт.
6. Сам проанализируй проект и добавь:
   - hidden imports,
   - add-data,
   - collect-all,
   - excludes,
   - hooks,
   - DLL/resources,
   чтобы собранное приложение работало на чистой Windows-машине без ручной установки зависимостей.
7. Раздели runtime-зависимости на обязательные и опциональные:
   - обязательные должны валить сборку на preflight;
   - опциональные должны давать [WARN] или иметь флаг --require-<feature>.
8. После изменений:
   - покажи, какие файлы созданы или изменены;
   - покажи итоговые команды PyInstaller и Inno Setup;
   - запусти релевантную проверку или dry-run;
   - проверь, что обязательные runtime-файлы лежат в итоговой release-папке;
   - отдельно перечисли риски, если какие-то зависимости не удалось автоматически подтвердить.
```

---

## Короткое резюме

Если свести все к одному правилу, то другой Codex должен воспроизвести вот такой пайплайн:

`build_release.bat` -> `tools/build_release.py` -> `PyInstaller` -> копирование в `D:\_BUILD\<APP_PRODUCT>` -> `Inno Setup` -> `D:\_BUILD\<APP_PRODUCT>\installer`

Итог должен быть не просто "сборка прошла", а полноценный переносимый Windows-билд и установщик.
