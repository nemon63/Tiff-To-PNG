# Tiff-To-PNG

Модульное приложение на `PyQt6` для пакетной конвертации изображений в PNG.

## Требования

- Windows
- Python 3.11+ 

## Создание виртуального окружения

Если окружение еще не создано:

```powershell
cd d:\Python\Tiff-To-PNG
python -m venv .venv
```

## Установка зависимостей

### В существующее окружение `.venv`

```powershell
cd d:\Python\Tiff-To-PNG
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Если `pip` упирается в SOCKS-прокси

На некоторых системах `pip` подхватывает системный `socks5` proxy. Тогда установку лучше выполнять так:

```powershell
cd d:\Python\Tiff-To-PNG
$env:NO_PROXY='*'
$env:no_proxy='*'
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Активация окружения

При желании можно активировать окружение перед запуском:

```powershell
cd d:\Python\Tiff-To-PNG
.\.venv\Scripts\Activate.ps1
```

Если PowerShell блокирует активацию:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

## Запуск приложения

### GUI

```powershell
cd d:\Python\Tiff-To-PNG
.\.venv\Scripts\python.exe tiff_to_png.py
```

Приложение автоматически сохраняет последние пути, параметры конвертации и размер окна в `app_settings.json`.

### CLI

Пример запуска из командной строки:

```powershell
cd d:\Python\Tiff-To-PNG
.\.venv\Scripts\python.exe tiff_to_png.py .\input --out .\output --recursive
```

## Полезные параметры CLI

- `--out` — выходная папка
- `--recursive` — обработка подпапок
- `--force-rgba` — принудительный режим `RGBA`
- `--overwrite` — перезапись существующих `PNG`
- `--delete-source` — удалить исходник после успешной конвертации
- `--resize-percent` — масштабирование в процентах
- `--max-side` — ограничение длинной стороны
- `--png8` — сохранить как `PNG-8`

Полная справка:

```powershell
.\.venv\Scripts\python.exe tiff_to_png.py --help
```
