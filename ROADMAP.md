# Texture Pipeline Workbench — Roadmap

`ROADMAP.md` отвечает на вопрос «куда развивается продукт». Реальное состояние
задач, критерии готовности и приоритеты следующей работы находятся в
[`DEVELOPMENT_BACKLOG.md`](DEVELOPMENT_BACKLOG.md).

## Что уже является продуктом

Это больше не ранний TIFF-to-PNG конвертер. В репозитории реализованы два
связанных рабочих режима.

### Batch Converter

- очередь файлов и папок, drag-and-drop, статусы и группировка Texture Set;
- Asset Inspector с preview, метаданными, ручным Map Type и предупреждениями;
- системные и пользовательские presets, naming rules и настройки формата;
- распознавание PBR-карт, включая packed layouts `ORM`, `RMA`, `MRA`, Unity
  URP MetallicSmoothness и Unity HDRP Mask Map;
- преобразование **из любого поддерживаемого pipeline в любой другой**:
  packing, repacking и `Traditional / Non-Packed Workflow` для распаковки в
  отдельные Base Color, Normal, AO, Roughness и Metallic;
- preflight Texture Set, проверки roughness/glossiness и normal orientation;
- CLI для базовой пакетной обработки и release-сценарий для Windows.

### Graph Workbench

- сохраняемые `.texturegraph`-сцены: New, Open, Save, Save As, Recent и
  autosave рядом с сохранённой сценой;
- image-, channel- и output-ноды, графовый экспорт и применение graph template
  к совместимым наборам Batch Queue;
- авторские ноды обработки: Levels, Remap, Clamp, Threshold, Blur,
  Dilate/Erode, channel/image blend, Split/Combine RGBA, Set Alpha, Normal Map,
  Height to Normal, Normal Blend RNM, Color Adjust, Transform 2D и
  Resize/Canvas;
- Houdini-подобная работа с графом: node help, выделение проводов, `Y`-cut,
  shake-bypass с восстановлением прямого соединения, вставка ноды в провод,
  Undo/Redo;
- GPU PBR Preview со сферой/плоскостью, управлением камерой и светом,
  режимами диагностики карт и выбором normal convention;
- Auto Watch / Auto Rebuild для texture-нод.

## Ближайшая цель — отзывчивый authoring

Граф уже функционален; следующий качественный шаг — сделать его предсказуемым
на реальных 2K–4K наборах, а не только на простых графах.

1. Довести интерактивный scheduler: черновой preview при изменении параметра,
   отмена устаревшей ветки, один полный preview после отпускания control.
2. Измерять стоимость нод и выделить CPU/GPU bottleneck прямо в интерфейсе.
3. Не загружать заново неизменившиеся PBR-карты в OpenGL при изменении одной
   ветки материала.
4. После измерений оптимизировать только подтверждённо тяжёлые операции;
   нативный C++/SIMD backend рассматривать точечно, а не как первый ответ на
   UI-лаги.

Результат: художник свободно редактирует Levels, Color Adjust и цепочки
композитинга, а полное качество получает после завершения жеста.

## Следующая продуктовая цель — быстрый PBR-authoring

После стабилизации отзывчивости развиваем именно те инструменты, которые
сокращают ручную работу CG-художника.

- Frames / comments / reroute и быстрый поиск при добавлении ноды;
- reusable graph templates и project-level output profiles;
- расширенная валидация Texture Set: совпадение разрешений, colorspace,
  конфликт duplicate maps, packed-layout и normal/orientation;
- удобные preview-режимы: до/после, solo channel, сравнение normal OpenGL и
  DirectX;
- дополнительные анализаторы и ноды только после подтверждённого workflow:
  curvature, edge mask, sharpen, distance/height utilities.

Результат: Graph Workbench становится местом, где материал не только
конвертируют, но и быстро готовят к экспорту.

## Production I/O и автоматизация

Когда authoring и интерактивность устойчивы, следующий слой — надёжная работа
с большими и студийными данными.

- EXR и контролируемый путь для 16/32-bit данных;
- UDIM-aware intake, graph evaluation и export;
- project profiles, team presets и portable graph packages;
- воспроизводимый batch/CLI execution graph templates;
- отчёты QA и machine-readable sidecar metadata;
- надёжные watch/export jobs с защитой от path collisions и понятной историей
  запусков.

Результат: один и тот же граф и preset можно безопасно применять к библиотеке
ассетов или проектному стандарту.

## Долгосрочно

- точечные native backends для измеренно тяжёлых фильтров;
- интеграции с DCC и engine pipeline через стабильные файлы, presets и CLI;
- plugin API — только когда появятся повторяемые внешние сценарии, которые
  действительно нельзя закрыть graph templates и output profiles.

## Принцип принятия новых задач

Новая функция попадает в план, если она либо заметно ускоряет повседневную
работу художника, либо предотвращает дорогую pipeline-ошибку. Нельзя добавлять
ноду или интеграцию только потому, что она существует в другом DCC.
