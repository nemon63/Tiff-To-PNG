# Texture Pipeline Workbench — Development Backlog

Актуализировано по текущему коду. Этот backlog не повторяет уже реализованный
MVP: он фиксирует оставшуюся работу и не даёт случайно «планировать» готовые
функции второй раз.

Обозначения: `[x]` — есть в текущем worktree, `[ ]` — ещё не реализовано.

## Закрытые вехи

### Intake, очередь и Inspector

- [x] доменные модели очереди, сканирование файлов и папок;
- [x] drag-and-drop в окно и в Queue Panel;
- [x] таблица очереди со статусом, типом карты, размером, разрешением и путём
  результата;
- [x] Asset Inspector: preview, метаданные, Map Type override, оценка export и
  предупреждения;
- [x] preflight Texture Set и группировка набора по имени/папке.

### Pipeline-конвертация

- [x] системные и пользовательские presets;
- [x] map-type detection и ручное переопределение;
- [x] colorspace/data role для PBR-карт;
- [x] naming rules и preview итогового Texture Set;
- [x] `ORM`, `RMA`, `MRA`, Unity URP и Unity HDRP packing/repacking;
- [x] `Traditional / Non-Packed Workflow`: распаковка поддерживаемых packed
  maps в отдельные карты Offline / Production;
- [x] validator: отсутствие обязательных карт, roughness/glossiness, normal
  orientation и основные pipeline-предупреждения.

### Graph Workbench и PBR Preview

- [x] image/channel graph, Output, Graph Export и применение graph template к
  Batch Queue;
- [x] ноды обработки: Levels, Remap, Clamp, Threshold, Blur, Dilate, Erode,
  Blend Channel, Luminance, Mix/Blend Image, Split/Combine RGBA, Set Alpha,
  Normal Map, Height to Normal, Normal Blend, Color Adjust, Transform 2D и
  Resize/Canvas;
- [x] контекстная справка каждой ноды;
- [x] Houdini-style gestures: Y-cut, shake-bypass, insert-on-wire, Undo/Redo;
- [x] `.texturegraph`: Save, Save As, Load, Recent, autosave и menu commands;
- [x] GPU PBR Preview, управление объектом/светом/zoom/pan, normal check;
- [x] Auto Watch и Auto Rebuild texture-нод.

### Интерактивность — первый этап

- [x] 256 px draft-preview во время перетаскивания numeric slider;
- [x] ограничение live updates до 20 fps и полный preview по отпусканию;
- [x] `latest request wins`: устаревший расчёт отменяется между нодами;
- [x] validation, autosave и лишняя перерисовка нод отложены до конца drag;
- [x] Mix/Blend/Mask в draft не вычисляются на исходном полном разрешении.

## P0 — довести отзывчивость Graph Workbench

### P0.1 Профилировщик graph evaluation

- [ ] записывать wall time по каждой ноде и суммарно CPU/GPU preview;
- [ ] показать последнюю стоимость рядом с нодой или в Inspector без засорения
  canvas;
- [ ] помечать cache hit/miss и разрешение, на котором работала нода;
- [ ] добавить regression-тесты на отсутствие полного-resolution вычисления в
  draft mode.

Критерий готовности: на тяжёлом графе понятно, какая именно нода или GPU upload
съедает время; оптимизация опирается на измерение, а не на догадки.

### P0.2 Инкрементальное обновление GPU PBR material

- [ ] передавать в `PbrMaterialData` revision/key каждой из четырёх карт;
- [ ] при изменении одной ветки обновлять только изменившуюся OpenGL texture;
- [ ] не пересоздавать mipmaps и texture objects для неизменившихся карт;
- [ ] проверить отключение/подключение карты, fallback material и смену
  normal convention.

Критерий готовности: изменение Roughness или AO не приводит к повторному upload
Base Color, Normal и Emissive.

### P0.3 Планировщик — второй этап только после измерений

- [ ] оценить стоимость создания короткоживущих `QThread` на реальном 4K графе;
- [ ] при подтверждённой выгоде заменить их на один persistent preview worker;
- [ ] сохранить гарантию «последний запрос побеждает», shutdown без зависаний и
  корректное владение Qt-объектами.

Не начинать C++-переписывание до P0.1: уже имеющиеся Pillow-операции не всегда
будут bottleneck.

## P1 — graph UX, который помогает большим сетям

### P1.1 Организация графа

- [ ] Frames / Backdrops с названием и цветом;
- [ ] Reroute-ноды для аккуратной укладки проводов;
- [ ] поиск ноды при добавлении и избранные ноды;
- [ ] быстрые команды Duplicate, Disable/Bypass, Rename и аккуратный Layout для
  выделения.

### P1.2 Reusable authoring

- [ ] graph templates с параметрами и явными входными/выходными контрактами;
- [ ] project output profiles, отделённые от глобальных presets;
- [ ] portable package: graph + относительные пути + manifest зависимостей;
- [ ] before/after и solo preview для ноды/выхода.

### P1.3 Валидация, видимая художнику

- [ ] проверка несовпадения разрешений и aspect ratio внутри Texture Set;
- [ ] duplicate/conflicting карты и конфликт channel semantics;
- [ ] сводное объяснение, почему набор не подходит текущему preset/graph
  template, с действием для исправления;
- [ ] экспортируемый QA report только после согласования формата отчёта.

## P2 — production I/O

### P2.1 Высокая точность и UDIM

- [ ] спроектировать требования к EXR, 16-bit и 32-bit, чтобы не потерять
  данные на пути Pillow/PNG;
- [ ] UDIM scanner, grouping и нумерация output;
- [ ] проверка graph/export на нескольких тайлах и пропущенных UDIM.

### P2.2 Автоматизация и воспроизводимость

- [ ] CLI execution сохранённого graph template;
- [ ] project/team preset repository;
- [ ] machine-readable sidecar metadata и история export jobs;
- [ ] правила безопасного Auto Export: collision policy, retry и понятный log.

## Отложено осознанно

- [ ] plugin API;
- [ ] произвольный импорт пользовательских mesh в PBR Preview;
- [ ] DCC/engine плагины;
- [ ] native C++/SIMD module;
- [ ] новые художественные фильтры без подтверждённого сценария.

Для каждой из этих тем сначала нужен конкретный workflow, измерение или
внешний контракт. Иначе они будут повышать сложность продукта быстрее, чем его
пользу.

## Правило выполнения

Каждый пункт закрывается только вместе с:

1. изменением в UI или сервисном контракте;
2. тестом на регрессию, где он уместен;
3. проверкой на реальном 2K/4K наборе, если задача затрагивает preview или
   export;
4. обновлением этого документа и `ROADMAP.md`, если поменялся продуктовый
   статус.
