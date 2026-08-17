# Памятка по `build_release.bat`

## Что делает скрипт

`build_release.bat` запускает полный release pipeline:

1. использует Python из `.venv`
2. запускает `tools/build_release.py`
3. проверяет зависимости и обязательные файлы
4. собирает standalone-билд через `PyInstaller`
5. копирует результат в `D:\_BUILD\TexturePipelineWorkbench`
6. собирает установщик через `Inno Setup`
7. кладет установщик в `D:\_BUILD\TexturePipelineWorkbench\installer`

## Базовый запуск

Из корня репозитория:

```bat
build_release.bat
```

Если нужно явно без изменения версии:

```bat
build_release.bat --no-bump-version
```

## Полезные флаги

### `--dry-run`

Показывает план сборки и итоговые команды, но ничего не меняет:

```bat
build_release.bat --dry-run
```

Что важно:

- не меняет файлы
- не bump-ает версию
- не чистит папки
- не запускает `PyInstaller`
- не запускает `Inno Setup`

### `--skip-installer`

Собирает только приложение, без инсталлятора:

```bat
build_release.bat --skip-installer
```

Полезно, если нужно быстро проверить только `PyInstaller`-часть.

### `--bump-part patch|minor|major|none`

Увеличивает `APP_VERSION` в `app_meta.py` после успешного preflight и до сборки.

Примеры:

```bat
build_release.bat --bump-part patch
build_release.bat --bump-part minor
build_release.bat --bump-part major
```

Логика:

- `patch`: `0.1.0 -> 0.1.1`
- `minor`: `0.1.0 -> 0.2.0`
- `major`: `0.1.0 -> 1.0.0`
- `none`: версию не меняет

### `--no-bump-version`

Явно запрещает изменение версии, даже если обычно вы хотите собирать без bump:

```bat
build_release.bat --no-bump-version
```

### `--output-root`

Меняет корневую папку для готового релиза:

```bat
build_release.bat --output-root D:\TempBuilds
```

Тогда билд окажется в:

```text
D:\TempBuilds\TexturePipelineWorkbench
```

### `--installer-script`

Позволяет указать другой `.iss`-скрипт:

```bat
build_release.bat --installer-script tools\installer.iss
```

### `--inno-compiler`

Позволяет переопределить путь к `ISCC.exe`:

```bat
build_release.bat --inno-compiler "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

### `--keep-build`

Не удаляет промежуточные build-артефакты внутри репозитория:

```bat
build_release.bat --keep-build
```

Полезно для отладки `PyInstaller`.

### `--verbose`

Печатает больше диагностической информации в preflight:

```bat
build_release.bat --verbose
```

## Частые сценарии

### Проверить пайплайн без изменений

```bat
build_release.bat --dry-run
```

### Собрать релиз и инсталлер

```bat
build_release.bat --no-bump-version
```

### Собрать только билд приложения

```bat
build_release.bat --skip-installer --no-bump-version
```

### Собрать релиз и поднять patch-версию

```bat
build_release.bat --bump-part patch
```

## Где лежат результаты

Готовый билд:

```text
D:\_BUILD\TexturePipelineWorkbench
```

Готовый инсталлер:

```text
D:\_BUILD\TexturePipelineWorkbench\installer
```

Имя установщика по умолчанию:

```text
TexturePipelineWorkbench_<APP_VERSION>_setup.exe
```

## Что должно быть в проекте

Для успешной сборки нужны:

- `.venv\Scripts\python.exe`
- `app_meta.py`
- `tools/build_release.py`
- `tools/installer.iss`
- `tiff_to_png.py`
- `ico\favicon.ico`
- `app_settings.json`

Опционально:

- `conversion_presets.json`

Если его нет, скрипт только выдаст предупреждение.

## Откуда берется версия

Версия читается из:

```text
app_meta.py
```

Основные поля:

- `APP_PRODUCT`
- `APP_NAME`
- `APP_VERSION`
- `APP_PUBLISHER`
- `APP_INSTALLER_GUID`

## Что делать при ошибке

1. Сначала прогоните:

```bat
build_release.bat --dry-run
```

2. Проверьте, что существует:

- `.venv`
- `ico\favicon.ico`
- `app_settings.json`
- `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`

3. Если нужен только exe без установщика, попробуйте:

```bat
build_release.bat --skip-installer --no-bump-version
```

4. Если отлаживаете упаковку, используйте:

```bat
build_release.bat --keep-build --verbose
```

5. При временной ошибке Inno Setup `EndUpdateResource failed (110)` скрипт
   автоматически повторяет сборку установщика до трех раз. Если все попытки
   завершились ошибкой, проверьте антивирус и индексатор для конкретной папки
   `D:\_BUILD\TexturePipelineWorkbench\installer`.

## Замечание по окружению

Скрипт сам использует `pip` и при необходимости ставит `PyInstaller` в `.venv`.
Для среды, где `pip` может упираться в SOCKS/proxy, внутри build-скрипта уже учтен запуск с `NO_PROXY=*`.
