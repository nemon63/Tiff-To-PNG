from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from image_converter.domain.models import ConversionOptions, TextureMapType
from image_converter.domain.node_graph import NodeGraphProject, NodeType
from image_converter.services.node_graph_executor import (
    GraphExecutionError,
    GraphExportSummary,
    NodeGraphExecutor,
)
from image_converter.services.map_types import detect_texture_map_type

if TYPE_CHECKING:
    from image_converter.ui.main_window import MainWindow


@dataclass(slots=True, frozen=True)
class GraphExportRequest:
    project: NodeGraphProject
    output_root: Path
    options: ConversionOptions
    output_node_id: str | None = None
    label: str = "Graph export"
    show_dialogs: bool = True
    auto_export: bool = False


@dataclass(slots=True, frozen=True)
class GraphBatchExportTask:
    output_root: Path
    source_paths: tuple[Path, ...]
    label: str
    texture_mapping: tuple[tuple[TextureMapType, Path], ...] = ()
    output_prefix: str = ""


@dataclass(slots=True, frozen=True)
class GraphBatchExportRequest:
    project: NodeGraphProject
    tasks: tuple[GraphBatchExportTask, ...]
    options: ConversionOptions
    label: str = "Apply graph to queue"


@dataclass(slots=True, frozen=True)
class GraphBatchExportResult:
    groups: tuple[tuple[tuple[Path, ...], GraphExportSummary], ...]

    @property
    def total(self) -> int:
        return sum(summary.total for _paths, summary in self.groups)

    @property
    def succeeded(self) -> int:
        return sum(summary.succeeded for _paths, summary in self.groups)

    @property
    def skipped(self) -> int:
        return sum(summary.skipped for _paths, summary in self.groups)

    @property
    def failed(self) -> int:
        return sum(summary.failed for _paths, summary in self.groups)

    def as_text(self) -> str:
        return (
            f"Graph batch export: наборов={len(self.groups)}, outputs={self.total}, "
            f"успешно={self.succeeded}, пропущено={self.skipped}, ошибок={self.failed}"
        )


class GraphExportWorker(QObject):
    log_message = pyqtSignal(str)
    finished = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)
    completed = pyqtSignal()

    def __init__(self, request: GraphExportRequest | GraphBatchExportRequest):
        super().__init__()
        self._request = request

    def run(self) -> None:
        try:
            if isinstance(self._request, GraphBatchExportRequest):
                result = self._run_batch(self._request)
            else:
                result = self._run_export(self._request)
            self.finished.emit(self._request, result)
        except Exception as exc:
            self.failed.emit(self._request, str(exc))
        finally:
            self.completed.emit()

    def _run_export(self, request: GraphExportRequest) -> GraphExportSummary:
        executor = NodeGraphExecutor()
        if request.output_node_id is None:
            return executor.export_enabled_outputs(
                request.project,
                request.output_root,
                request.options,
                self.log_message.emit,
            )
        output_node = next(
            (node for node in request.project.graph.nodes if node.node_id == request.output_node_id),
            None,
        )
        if output_node is None:
            raise GraphExecutionError("Selected Output node is missing from the export snapshot.")
        return executor.export_output(
            request.project,
            output_node,
            request.output_root,
            request.options,
            self.log_message.emit,
        )

    def _run_batch(self, request: GraphBatchExportRequest) -> GraphBatchExportResult:
        executor = NodeGraphExecutor()
        destinations: dict[str, list[tuple[str, Path]]] = {}
        prepared_tasks: list[tuple[GraphBatchExportTask, NodeGraphProject]] = []
        for task in request.tasks:
            project = self._project_for_batch_task(request.project, task)
            plan = executor.plan_enabled_outputs(project.graph, task.output_root)
            if not plan.is_valid:
                raise GraphExecutionError(plan.collision_message())
            for item in plan.items:
                key = os.path.normcase(os.path.normpath(str(item.destination.resolve(strict=False))))
                destinations.setdefault(key, []).append((f"{task.label}: {item.output_name}", item.destination))
            prepared_tasks.append((task, project))
        collisions = [items for items in destinations.values() if len(items) > 1]
        if collisions:
            lines = ["Несколько Output-нод указывают на один файл:"]
            for items in collisions:
                lines.append(f"{items[0][1]}: {', '.join(label for label, _path in items)}")
            raise GraphExecutionError("\n".join(lines))

        groups: list[tuple[tuple[Path, ...], GraphExportSummary]] = []
        for task, project in prepared_tasks:
            self.log_message.emit(f"-- Набор: {task.label} ({len(task.source_paths)} файлов)")
            summary = executor.export_enabled_outputs(
                project,
                task.output_root,
                request.options,
                self.log_message.emit,
            )
            groups.append((task.source_paths, summary))
        return GraphBatchExportResult(tuple(groups))

    @staticmethod
    def _project_for_batch_task(
        template: NodeGraphProject,
        task: GraphBatchExportTask,
    ) -> NodeGraphProject:
        project = deepcopy(template)
        nodes_by_map_type = {}
        for node in project.graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            map_type = detect_texture_map_type(Path(str(node.properties.get("path", ""))))
            if map_type is not TextureMapType.UNKNOWN:
                nodes_by_map_type.setdefault(map_type, node)
        for map_type, replacement_path in task.texture_mapping:
            node = nodes_by_map_type.get(map_type)
            if node is not None:
                node.properties["path"] = str(replacement_path)

        safe_prefix = task.output_prefix.replace("\\", "_").replace("/", "_").strip()
        if safe_prefix:
            for node in project.graph.nodes:
                if node.node_type is not NodeType.OUTPUT_RGBA:
                    continue
                raw_output_path = str(node.properties.get("output_path", "")).strip()
                raw_filename = str(node.properties.get("filename", "")).strip()
                if raw_output_path:
                    output_path = Path(raw_output_path)
                    node.properties["output_path"] = str(
                        output_path.with_name(f"{safe_prefix}_{output_path.name}")
                    )
                elif raw_filename:
                    node.properties["filename"] = f"{safe_prefix}_{raw_filename}"
                else:
                    node.properties["filename"] = f"{safe_prefix}_{node.title}.png"
        return project


class GraphExportController(QObject):
    def __init__(self, window: MainWindow):
        super().__init__(window)
        self._window = window
        self._thread: QThread | None = None
        self._worker: GraphExportWorker | None = None
        self._pending_auto_request: GraphExportRequest | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    def submit(self, request: GraphExportRequest | GraphBatchExportRequest) -> bool:
        if self._thread is not None:
            if isinstance(request, GraphExportRequest) and request.auto_export:
                self._pending_auto_request = request
                self._window.set_status("Auto Export: сохранён последний запрос.")
            return False
        if not self._window.job_coordinator.try_acquire_exclusive(self):
            if isinstance(request, GraphExportRequest) and request.auto_export:
                self._pending_auto_request = request
                QTimer.singleShot(250, self._submit_pending_auto)
            return False

        self._window.set_graph_job_running(True)
        self._thread = QThread(self)
        self._worker = GraphExportWorker(request)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log_message.connect(self._window.append_log)
        self._worker.finished.connect(self._window.on_graph_job_finished)
        self._worker.failed.connect(self._window.on_graph_job_failed)
        self._worker.completed.connect(self._thread.quit)
        self._worker.completed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._reset_worker_state)
        self._thread.start()
        return True

    def _reset_worker_state(self) -> None:
        self._thread = None
        self._worker = None
        self._window.job_coordinator.release_exclusive(self)
        self._window.set_graph_job_running(False)
        if self._pending_auto_request is not None:
            QTimer.singleShot(0, self._submit_pending_auto)

    def _submit_pending_auto(self) -> None:
        request = self._pending_auto_request
        if request is None or self._thread is not None:
            return
        if self._window.job_coordinator.exclusive_job_running:
            QTimer.singleShot(250, self._submit_pending_auto)
            return
        self._pending_auto_request = None
        self.submit(request)
