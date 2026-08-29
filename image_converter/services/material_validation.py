from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from image_converter.domain.models import (
    ChannelPackLayout,
    ChannelPackingMode,
    ConversionOptions,
    QueueItem,
    TextureMapType,
)
from image_converter.domain.node_graph import (
    GraphConnection,
    GraphNode,
    GraphValidationIssue,
    GraphValidationSeverity,
    NodeGraph,
    NodeType,
    OutputProfile,
    PbrNormalConvention,
    PbrWorkflow,
)
from image_converter.services.map_types import (
    detect_channel_pack_layout,
    detect_texture_map_type,
    map_type_aliases,
    strip_channel_pack_suffix,
)


ROUGHNESS_GLOSSINESS_TYPES = frozenset(
    (TextureMapType.ROUGHNESS, TextureMapType.SMOOTHNESS)
)
PAIR_MATCH_THRESHOLD = 0.04
PAIR_REVIEW_THRESHOLD = 0.15
NORMAL_ORIENTATION_DIRECTX = "directx"
NORMAL_ORIENTATION_OPENGL = "opengl"

PACKED_CHANNEL_SEMANTICS: dict[
    ChannelPackLayout,
    dict[str, TextureMapType],
] = {
    ChannelPackLayout.ORM: {"g": TextureMapType.ROUGHNESS},
    ChannelPackLayout.RMA: {"r": TextureMapType.ROUGHNESS},
    ChannelPackLayout.MRA: {"g": TextureMapType.ROUGHNESS},
    ChannelPackLayout.UNITY_URP: {"a": TextureMapType.SMOOTHNESS},
    ChannelPackLayout.UNITY_HDRP: {"a": TextureMapType.SMOOTHNESS},
}

OUTPUT_CHANNEL_SEMANTICS: dict[
    OutputProfile,
    dict[str, TextureMapType],
] = {
    OutputProfile.UNITY_URP: {"a": TextureMapType.SMOOTHNESS},
    OutputProfile.UNITY_HDRP: {"a": TextureMapType.SMOOTHNESS},
    OutputProfile.UNREAL_ORM: {"g": TextureMapType.ROUGHNESS},
    OutputProfile.METAHUMAN_REPACK: {"g": TextureMapType.ROUGHNESS},
}

SEMANTIC_PRESERVING_CHANNEL_NODES = frozenset(
    (
        NodeType.LEVELS_CHANNEL,
        NodeType.REMAP_CHANNEL,
        NodeType.CLAMP_CHANNEL,
        NodeType.THRESHOLD_CHANNEL,
        NodeType.BLUR_CHANNEL,
        NodeType.DILATE_CHANNEL,
        NodeType.ERODE_CHANNEL,
    )
)


@dataclass(slots=True, frozen=True)
class _SemanticTrace:
    semantic: TextureMapType
    source_node_id: str
    source_socket_id: str
    source_path: Path
    packed_layout: ChannelPackLayout | None = None


@dataclass(slots=True, frozen=True)
class TextureSetValidationReport:
    label: str
    issues: tuple[str, ...]

    @property
    def is_ready(self) -> bool:
        return not self.issues


@dataclass(slots=True, frozen=True)
class TextureSetValidationResult:
    reports: tuple[TextureSetValidationReport, ...]
    warnings_by_path: dict[str, tuple[str, ...]]

    @property
    def ready_count(self) -> int:
        return sum(report.is_ready for report in self.reports)

    @property
    def warning_count(self) -> int:
        return len(self.reports) - self.ready_count

    def summary_text(self, *, preview_limit: int = 3) -> str:
        if not self.reports:
            return "Texture Set Validator: добавьте распознанные PBR-карты."
        lines = [
            f"Texture Set Validator: {len(self.reports)} набор(ов) · "
            f"готово {self.ready_count} · требуют внимания {self.warning_count}"
        ]
        problem_reports = [report for report in self.reports if report.issues]
        for report in problem_reports[:preview_limit]:
            lines.append(f"⚠ {report.label}: {report.issues[0]}")
        remainder = len(problem_reports) - preview_limit
        if remainder > 0:
            lines.append(f"… и ещё {remainder} набор(ов) с предупреждениями")
        if not problem_reports:
            lines.append("✓ Разрешения и состав наборов согласованы.")
        return "\n".join(lines)


PACKED_AVAILABLE_MAP_TYPES: dict[ChannelPackLayout, frozenset[TextureMapType]] = {
    ChannelPackLayout.ORM: frozenset(
        (TextureMapType.AO, TextureMapType.ROUGHNESS, TextureMapType.METALLIC)
    ),
    ChannelPackLayout.RMA: frozenset(
        (TextureMapType.AO, TextureMapType.ROUGHNESS, TextureMapType.METALLIC)
    ),
    ChannelPackLayout.MRA: frozenset(
        (TextureMapType.AO, TextureMapType.ROUGHNESS, TextureMapType.METALLIC)
    ),
    ChannelPackLayout.UNITY_URP: frozenset(
        (TextureMapType.METALLIC, TextureMapType.SMOOTHNESS)
    ),
    ChannelPackLayout.UNITY_HDRP: frozenset(
        (
            TextureMapType.METALLIC,
            TextureMapType.AO,
            TextureMapType.DETAIL_MASK,
            TextureMapType.SMOOTHNESS,
        )
    ),
}


def filename_semantic_warning(item: QueueItem) -> str | None:
    """Report a manual Roughness/Smoothness assignment that contradicts the filename."""

    if item.effective_packed_layout is not None:
        return None
    assigned = item.effective_map_type
    detected = detect_texture_map_type(item.path)
    if assigned not in ROUGHNESS_GLOSSINESS_TYPES:
        return None
    if detected not in ROUGHNESS_GLOSSINESS_TYPES or detected is assigned:
        return None
    return (
        f"Вероятная ошибка: по имени это {detected.label}, но карта назначена как "
        f"{assigned.label}. Между Roughness и Glossiness/Smoothness нужна инверсия."
    )


def texture_set_pair_warnings(
    items: list[QueueItem] | tuple[QueueItem, ...],
) -> dict[str, tuple[str, ...]]:
    """Compare Roughness/Glossiness pairs and return warnings keyed by normalized path."""

    groups: dict[tuple[str, str], dict[TextureMapType, list[QueueItem]]] = {}
    for item in items:
        map_type = item.effective_map_type
        if map_type not in ROUGHNESS_GLOSSINESS_TYPES:
            continue
        group_key = _texture_set_key(item.path, map_type)
        groups.setdefault(group_key, {}).setdefault(map_type, []).append(item)

    warnings: dict[str, list[str]] = {}
    for maps_by_type in groups.values():
        roughness_items = maps_by_type.get(TextureMapType.ROUGHNESS, ())
        smoothness_items = maps_by_type.get(TextureMapType.SMOOTHNESS, ())
        if len(roughness_items) != 1 or len(smoothness_items) != 1:
            continue
        roughness_item = roughness_items[0]
        smoothness_item = smoothness_items[0]
        comparison = _compare_roughness_smoothness_samples(
            _validation_sample(roughness_item),
            _validation_sample(smoothness_item),
        )
        if comparison is None:
            continue
        direct_error, inverse_error = comparison
        message = ""
        if (
            direct_error <= PAIR_MATCH_THRESHOLD
            and inverse_error > direct_error + 0.08
        ):
            message = (
                "Вероятная ошибка: Roughness и Glossiness/Smoothness почти одинаковы, "
                "хотя должны быть инверсными. Проверьте назначение или добавьте Invert."
            )
        elif inverse_error > PAIR_REVIEW_THRESHOLD:
            message = (
                "Требует проверки: найденные Roughness и Glossiness/Smoothness не являются "
                "взаимно обратными картами. Возможно, одна из них названа неверно."
            )
        if not message:
            continue
        for item in (roughness_item, smoothness_item):
            warnings.setdefault(normalized_path_key(item.path), []).append(message)

    return {key: tuple(dict.fromkeys(values)) for key, values in warnings.items()}


def validate_texture_sets(
    items: list[QueueItem] | tuple[QueueItem, ...],
    options: ConversionOptions,
) -> TextureSetValidationResult:
    groups = _group_texture_set_items(items)
    pair_warnings = texture_set_pair_warnings(items)
    warnings_by_path: dict[str, list[str]] = {
        key: list(values) for key, values in pair_warnings.items()
    }
    reports: list[TextureSetValidationReport] = []

    for (_parent_key, base_name), group_items in groups.items():
        group_issues: list[str] = []
        group_path_keys = {normalized_path_key(item.path) for item in group_items}
        for path_key in group_path_keys:
            group_issues.extend(pair_warnings.get(path_key, ()))

        for item in group_items:
            for normal_issue in _normal_map_issues(item, options):
                group_issues.append(normal_issue)
                _append_path_warning(warnings_by_path, item.path, normal_issue)

        resolution_result = _resolution_issue(group_items)
        if resolution_result:
            resolution_issue, affected_items = resolution_result
            group_issues.append(resolution_issue)
            for item in affected_items:
                _append_path_warning(warnings_by_path, item.path, resolution_issue)

        for duplicate_items, duplicate_issue in _duplicate_map_issues(group_items):
            group_issues.append(duplicate_issue)
            for item in duplicate_items:
                _append_path_warning(warnings_by_path, item.path, duplicate_issue)

        missing_labels = _missing_workflow_map_labels(group_items, options)
        if missing_labels:
            missing_issue = "нет обязательных карт: " + ", ".join(missing_labels)
            group_issues.append(missing_issue)
            representative = _validation_representative(group_items)
            if representative is not None:
                _append_path_warning(
                    warnings_by_path,
                    representative.path,
                    "Texture Set неполный: " + missing_issue,
                )

        reports.append(
            TextureSetValidationReport(
                label=(
                    group_items[0].path.parent.name or "Texture Set"
                    if base_name == "__material__"
                    else base_name
                ),
                issues=tuple(dict.fromkeys(group_issues)),
            )
        )

    reports.sort(key=lambda report: report.label.casefold())
    return TextureSetValidationResult(
        reports=tuple(reports),
        warnings_by_path={
            key: tuple(dict.fromkeys(values))
            for key, values in warnings_by_path.items()
        },
    )


def graph_roughness_glossiness_issues(
    graph: NodeGraph,
) -> tuple[GraphValidationIssue, ...]:
    nodes_by_id = {node.node_id: node for node in graph.nodes}
    incoming = {
        (connection.target_node_id, connection.target_socket_id): connection
        for connection in graph.connections
    }
    issues: list[GraphValidationIssue] = []

    for output_node in graph.nodes:
        if (
            output_node.node_type is not NodeType.OUTPUT_RGBA
            or not output_node.properties.get("enabled", True)
        ):
            continue
        try:
            profile = OutputProfile(
                str(output_node.properties.get("profile", OutputProfile.GENERIC_RGBA.value))
            )
        except ValueError:
            continue
        expected_channels = OUTPUT_CHANNEL_SEMANTICS.get(profile, {})
        for socket_id, expected_semantic in expected_channels.items():
            connection = incoming.get((output_node.node_id, socket_id))
            if connection is None:
                continue
            trace = _trace_channel_semantic(
                connection,
                nodes_by_id,
                incoming,
                set(),
            )
            if trace is None:
                continue
            if trace.semantic is TextureMapType.UNKNOWN:
                issues.append(
                    GraphValidationIssue(
                        GraphValidationSeverity.WARNING,
                        f"Требует проверки: {output_node.title}.{socket_id.upper()} ожидает "
                        f"{expected_semantic.label}, но семантика текстуры "
                        f"'{trace.source_path.name}' не определяется по имени.",
                        trace.source_node_id,
                        trace.source_socket_id,
                    )
                )
                continue
            if trace.semantic is expected_semantic:
                continue
            packed_mismatch = trace.packed_layout is not None
            severity = (
                GraphValidationSeverity.ERROR
                if packed_mismatch
                else GraphValidationSeverity.WARNING
            )
            confidence_label = "Ошибка схемы" if packed_mismatch else "Вероятная ошибка"
            issues.append(
                GraphValidationIssue(
                    severity,
                    f"{confidence_label}: {output_node.title}.{socket_id.upper()} ожидает "
                    f"{expected_semantic.label}, но получает {trace.semantic.label} из "
                    f"'{trace.source_path.name}'. Добавьте включённую Invert node или "
                    "измените назначение карты.",
                    trace.source_node_id,
                    trace.source_socket_id,
                )
            )

    return tuple(issues)


def pbr_expected_normal_orientation(node: GraphNode) -> str | None:
    try:
        convention = PbrNormalConvention(
            str(node.properties.get("normal_convention", PbrNormalConvention.WORKFLOW.value))
        )
    except ValueError:
        convention = PbrNormalConvention.WORKFLOW
    if convention is PbrNormalConvention.DIRECTX:
        return NORMAL_ORIENTATION_DIRECTX
    if convention is PbrNormalConvention.OPENGL:
        return NORMAL_ORIENTATION_OPENGL
    try:
        workflow = PbrWorkflow(
            str(node.properties.get("workflow", PbrWorkflow.TRADITIONAL.value))
        )
    except ValueError:
        workflow = PbrWorkflow.TRADITIONAL
    if workflow in (
        PbrWorkflow.UNREAL_ORM,
        PbrWorkflow.UNREAL_MRA,
        PbrWorkflow.UNREAL_RMA,
    ):
        return NORMAL_ORIENTATION_DIRECTX
    if workflow in (PbrWorkflow.UNITY_URP, PbrWorkflow.UNITY_HDRP):
        return NORMAL_ORIENTATION_OPENGL
    return None


def trace_pbr_normal_orientation(
    graph: NodeGraph,
    shader_node: GraphNode,
) -> tuple[str | None, Path | None]:
    nodes_by_id = {node.node_id: node for node in graph.nodes}
    incoming = {
        (connection.target_node_id, connection.target_socket_id): connection
        for connection in graph.connections
    }
    connection = incoming.get((shader_node.node_id, "normal"))
    if connection is None:
        return None, None
    return _trace_image_normal_orientation(connection, nodes_by_id, incoming, set())


def graph_normal_orientation_issues(
    graph: NodeGraph,
) -> tuple[GraphValidationIssue, ...]:
    issues: list[GraphValidationIssue] = []
    for node in graph.nodes:
        if node.node_type is not NodeType.PBR_SHADER:
            continue
        expected = pbr_expected_normal_orientation(node)
        detected, source_path = trace_pbr_normal_orientation(graph, node)
        if expected is None or detected is None or expected == detected:
            continue
        expected_label = _normal_orientation_label(expected)
        detected_label = _normal_orientation_label(detected)
        source_label = source_path.name if source_path is not None else "Normal branch"
        issues.append(
            GraphValidationIssue(
                GraphValidationSeverity.WARNING,
                f"{node.title}: workflow ожидает {expected_label}, но ветка "
                f"'{source_label}' даёт {detected_label}. Добавьте или отключите "
                "Flip Green в Normal Map node.",
                node.node_id,
                "normal",
            )
        )
    return tuple(issues)


def _trace_image_normal_orientation(
    connection: GraphConnection,
    nodes_by_id: dict[str, GraphNode],
    incoming: dict[tuple[str, str], GraphConnection],
    visited: set[tuple[str, str]],
) -> tuple[str | None, Path | None]:
    source_key = (connection.source_node_id, connection.source_socket_id)
    if source_key in visited:
        return None, None
    visited.add(source_key)
    node = nodes_by_id.get(connection.source_node_id)
    if node is None:
        return None, None
    if node.node_type is NodeType.TEXTURE_INPUT:
        path = Path(str(node.properties.get("path", "")))
        return detect_normal_map_orientation(path), path
    if node.node_type is NodeType.NORMAL_MAP:
        upstream = incoming.get((node.node_id, "image"))
        if upstream is None:
            return None, None
        orientation, path = _trace_image_normal_orientation(
            upstream,
            nodes_by_id,
            incoming,
            visited,
        )
        if (
            orientation is not None
            and node.properties.get("enabled", True)
            and node.properties.get("flip_green", False)
        ):
            orientation = (
                NORMAL_ORIENTATION_OPENGL
                if orientation == NORMAL_ORIENTATION_DIRECTX
                else NORMAL_ORIENTATION_DIRECTX
            )
        return orientation, path
    if node.node_type is NodeType.SET_ALPHA:
        upstream = incoming.get((node.node_id, "image"))
        if upstream is not None:
            return _trace_image_normal_orientation(
                upstream,
                nodes_by_id,
                incoming,
                visited,
            )
    return None, None


def normalized_path_key(path: Path) -> str:
    return str(path.resolve(strict=False)).casefold()


def texture_set_items_for_item(
    items: list[QueueItem] | tuple[QueueItem, ...],
    selected_item: QueueItem | None,
) -> tuple[QueueItem, ...]:
    if selected_item is None:
        return ()
    selected_key = normalized_path_key(selected_item.path)
    for group_items in _group_texture_set_items(items).values():
        if any(normalized_path_key(item.path) == selected_key for item in group_items):
            return tuple(group_items)
    return (selected_item,)


def detect_normal_map_orientation(path: Path) -> str | None:
    normalized = _normalized_stem(path.stem)
    tokens = {token for token in normalized.split("_") if token}
    collapsed = normalized.replace("_", "")
    directx = bool(
        {"dx", "directx"} & tokens
        or collapsed.endswith("normaldx")
        or collapsed.endswith("normaldirectx")
    )
    opengl = bool(
        {"gl", "ogl", "opengl"} & tokens
        or collapsed.endswith("normalgl")
        or collapsed.endswith("normalogl")
        or collapsed.endswith("normalopengl")
    )
    if directx == opengl:
        return None
    return NORMAL_ORIENTATION_DIRECTX if directx else NORMAL_ORIENTATION_OPENGL


def _trace_channel_semantic(
    connection: GraphConnection,
    nodes_by_id: dict[str, GraphNode],
    incoming: dict[tuple[str, str], GraphConnection],
    visited: set[tuple[str, str]],
) -> _SemanticTrace | None:
    source_key = (connection.source_node_id, connection.source_socket_id)
    if source_key in visited:
        return None
    visited.add(source_key)

    node = nodes_by_id.get(connection.source_node_id)
    if node is None:
        return None
    if node.node_type is NodeType.TEXTURE_INPUT:
        path = Path(str(node.properties.get("path", "")))
        return _SemanticTrace(
            _texture_socket_semantic(path, connection.source_socket_id),
            node.node_id,
            connection.source_socket_id,
            path,
            detect_channel_pack_layout(path),
        )

    if node.node_type is NodeType.INVERT_CHANNEL:
        upstream = incoming.get((node.node_id, "in"))
        if upstream is None:
            return None
        trace = _trace_channel_semantic(upstream, nodes_by_id, incoming, visited)
        if trace is None or not node.properties.get("enabled", True):
            return trace
        return _SemanticTrace(
            _opposite_semantic(trace.semantic),
            trace.source_node_id,
            trace.source_socket_id,
            trace.source_path,
            trace.packed_layout,
        )

    if node.node_type in SEMANTIC_PRESERVING_CHANNEL_NODES:
        upstream = incoming.get((node.node_id, "in"))
        if upstream is None:
            return None
        trace = _trace_channel_semantic(upstream, nodes_by_id, incoming, visited)
        if (
            trace is not None
            and node.properties.get("enabled", True)
            and node.node_type in (NodeType.LEVELS_CHANNEL, NodeType.REMAP_CHANNEL)
            and _node_reverses_output_range(node)
        ):
            return _SemanticTrace(
                _opposite_semantic(trace.semantic),
                trace.source_node_id,
                trace.source_socket_id,
                trace.source_path,
                trace.packed_layout,
            )
        return trace
    return None


def _texture_socket_semantic(path: Path, socket_id: str) -> TextureMapType:
    packed_layout = detect_channel_pack_layout(path)
    if packed_layout is not None:
        return PACKED_CHANNEL_SEMANTICS.get(packed_layout, {}).get(
            socket_id.lower(),
            TextureMapType.UNKNOWN,
        )
    detected = detect_texture_map_type(path)
    if detected in ROUGHNESS_GLOSSINESS_TYPES:
        return detected
    return TextureMapType.UNKNOWN


def _opposite_semantic(map_type: TextureMapType) -> TextureMapType:
    if map_type is TextureMapType.ROUGHNESS:
        return TextureMapType.SMOOTHNESS
    if map_type is TextureMapType.SMOOTHNESS:
        return TextureMapType.ROUGHNESS
    return map_type


def _node_reverses_output_range(node: GraphNode) -> bool:
    try:
        return int(node.properties.get("out_min", 0)) > int(
            node.properties.get("out_max", 255)
        )
    except (TypeError, ValueError):
        return False


def _texture_set_key(path: Path, map_type: TextureMapType) -> tuple[str, str]:
    stem = _normalized_stem(path.stem)
    for alias in sorted(map_type_aliases(map_type), key=len, reverse=True):
        normalized_alias = _normalized_stem(alias)
        if stem == normalized_alias:
            stem = ""
            break
        suffix = f"_{normalized_alias}"
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)].strip("_")
            break
        collapsed_alias = normalized_alias.replace("_", "")
        if len(collapsed_alias) >= 4 and stem.replace("_", "").endswith(collapsed_alias):
            stem = stem[: -len(normalized_alias)].strip("_")
            break
    return normalized_path_key(path.parent), stem or "__material__"


def _group_texture_set_items(
    items: list[QueueItem] | tuple[QueueItem, ...],
) -> dict[tuple[str, str], list[QueueItem]]:
    groups: dict[tuple[str, str], list[QueueItem]] = {}
    for item in items:
        packed_layout = item.effective_packed_layout
        if packed_layout is not None:
            base_name = strip_channel_pack_suffix(item.path.stem)
            key = (
                normalized_path_key(item.path.parent),
                base_name or "__material__",
            )
        elif item.effective_map_type is not TextureMapType.UNKNOWN:
            key = _texture_set_key(item.path, item.effective_map_type)
        else:
            continue
        groups.setdefault(key, []).append(item)
    return groups


def _resolution_issue(
    items: list[QueueItem],
) -> tuple[str, tuple[QueueItem, ...]] | None:
    dimensions = {
        (item.metadata.width, item.metadata.height)
        for item in items
        if item.metadata is not None
        and item.metadata.width > 0
        and item.metadata.height > 0
    }
    if len(dimensions) <= 1:
        return None
    candidates = [
        item
        for item in items
        if item.metadata is not None
        and item.metadata.width > 0
        and item.metadata.height > 0
    ]
    reference = min(
        candidates,
        key=lambda item: (
            item.effective_map_type is not TextureMapType.BASECOLOR,
            item.effective_map_type is not TextureMapType.NORMAL,
            item.path.name.casefold(),
        ),
    )
    assert reference.metadata is not None
    reference_size = (reference.metadata.width, reference.metadata.height)
    affected_items = tuple(
        item
        for item in candidates
        if item.metadata is not None
        and (item.metadata.width, item.metadata.height) != reference_size
    )
    details = ", ".join(
        f"{item.path.name}={item.metadata.width}x{item.metadata.height}"
        for item in sorted(items, key=lambda value: value.path.name.casefold())
        if item.metadata is not None
        and item.metadata.width > 0
        and item.metadata.height > 0
    )
    return "разные разрешения: " + details, affected_items


def _duplicate_map_issues(
    items: list[QueueItem],
) -> tuple[tuple[list[QueueItem], str], ...]:
    by_map_type: dict[TextureMapType, list[QueueItem]] = {}
    packed_items: list[QueueItem] = []
    for item in items:
        if item.effective_packed_layout is not None:
            packed_items.append(item)
            continue
        map_type = item.effective_map_type
        if map_type is TextureMapType.UNKNOWN:
            continue
        by_map_type.setdefault(map_type, []).append(item)

    issues: list[tuple[list[QueueItem], str]] = []
    for map_type, duplicates in by_map_type.items():
        if len(duplicates) <= 1:
            continue
        names = ", ".join(item.path.name for item in duplicates)
        issues.append(
            (
                duplicates,
                f"несколько карт {map_type.label}: {names}",
            )
        )
    if len(packed_items) > 1:
        names = ", ".join(item.path.name for item in packed_items)
        issues.append((packed_items, f"несколько packed-текстур: {names}"))
    return tuple(issues)


def _missing_workflow_map_labels(
    items: list[QueueItem],
    options: ConversionOptions,
) -> tuple[str, ...]:
    required: set[TextureMapType] = set()
    requires_surface = False
    requires_roughness_family = False

    if options.unpack_packed and not options.packing.enabled:
        required.update(
            (
                TextureMapType.BASECOLOR,
                TextureMapType.NORMAL,
                TextureMapType.AO,
                TextureMapType.METALLIC,
            )
        )
        requires_roughness_family = True
    elif options.packing.enabled:
        requires_surface = options.packing.mode is not ChannelPackingMode.PACK_ONLY
        if requires_surface:
            required.update((TextureMapType.BASECOLOR, TextureMapType.NORMAL))
        if options.packing.layout in (
            ChannelPackLayout.ORM,
            ChannelPackLayout.RMA,
            ChannelPackLayout.MRA,
        ):
            required.update((TextureMapType.AO, TextureMapType.METALLIC))
            requires_roughness_family = True
        elif options.packing.layout is ChannelPackLayout.UNITY_URP:
            required.add(TextureMapType.METALLIC)
            requires_roughness_family = True
        elif options.packing.layout is ChannelPackLayout.UNITY_HDRP:
            required.update((TextureMapType.AO, TextureMapType.METALLIC))
            requires_roughness_family = True
    else:
        return ()

    available: set[TextureMapType] = set()
    for item in items:
        packed_layout = item.effective_packed_layout
        if packed_layout is not None:
            available.update(PACKED_AVAILABLE_MAP_TYPES.get(packed_layout, ()))
        elif item.effective_map_type is not TextureMapType.UNKNOWN:
            available.add(item.effective_map_type)

    missing = [
        map_type.label
        for map_type in (
            TextureMapType.BASECOLOR,
            TextureMapType.NORMAL,
            TextureMapType.AO,
            TextureMapType.METALLIC,
        )
        if map_type in required and map_type not in available
    ]
    if requires_roughness_family and not (
        ROUGHNESS_GLOSSINESS_TYPES & available
    ):
        missing.append("Roughness или Smoothness")
    return tuple(missing)


def _normal_map_issues(
    item: QueueItem,
    options: ConversionOptions,
) -> tuple[str, ...]:
    if item.effective_map_type is not TextureMapType.NORMAL:
        return ()
    issues: list[str] = []
    metadata = item.metadata
    if metadata is not None:
        if metadata.mode.upper() not in {"RGB", "RGBA"}:
            issues.append(
                f"Normal Map имеет режим {metadata.mode}; ожидается RGB или RGBA."
            )
        statistics = metadata.normal_map_statistics
        if statistics is not None:
            mean_red, mean_green, mean_blue = statistics.mean_rgb
            structural_details: list[str] = []
            if mean_blue < 140.0 or mean_blue <= max(mean_red, mean_green):
                structural_details.append("нет характерного преобладания синего канала")
            if statistics.negative_z_ratio > 0.10:
                structural_details.append(
                    f"{statistics.negative_z_ratio:.0%} texels имеют отрицательный Z"
                )
            if statistics.vector_length_outlier_ratio > 0.25:
                structural_details.append(
                    f"{statistics.vector_length_outlier_ratio:.0%} векторов заметно не нормализованы"
                )
            if structural_details:
                issues.append(
                    "Требует проверки: изображение не похоже на обычную tangent-space "
                    "Normal Map (" + "; ".join(structural_details) + ")."
                )

    expected_orientation = _expected_normal_orientation(options)
    detected_orientation = detect_normal_map_orientation(item.path)
    if (
        expected_orientation is not None
        and detected_orientation is not None
        and expected_orientation != detected_orientation
    ):
        expected_label = _normal_orientation_label(expected_orientation)
        detected_label = _normal_orientation_label(detected_orientation)
        issues.append(
            f"Normal Map помечена как {detected_label}, но выбранный workflow ожидает "
            f"{expected_label}. Требуется Flip Green перед экспортом."
        )
    return tuple(issues)


def _expected_normal_orientation(options: ConversionOptions) -> str | None:
    if not options.packing.enabled:
        return None
    if options.packing.layout in (
        ChannelPackLayout.ORM,
        ChannelPackLayout.RMA,
        ChannelPackLayout.MRA,
    ):
        return NORMAL_ORIENTATION_DIRECTX
    if options.packing.layout in (
        ChannelPackLayout.UNITY_URP,
        ChannelPackLayout.UNITY_HDRP,
    ):
        return NORMAL_ORIENTATION_OPENGL
    return None


def _normal_orientation_label(orientation: str) -> str:
    if orientation == NORMAL_ORIENTATION_DIRECTX:
        return "DirectX (Y−)"
    return "OpenGL (Y+)"


def _validation_representative(items: list[QueueItem]) -> QueueItem | None:
    if not items:
        return None
    return min(
        items,
        key=lambda item: (
            item.effective_map_type is not TextureMapType.BASECOLOR,
            item.path.name.casefold(),
        ),
    )


def _append_path_warning(
    warnings_by_path: dict[str, list[str]],
    path: Path,
    message: str,
) -> None:
    warnings_by_path.setdefault(normalized_path_key(path), []).append(message)


def _normalized_stem(stem: str) -> str:
    with_camel_breaks = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", stem)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", with_camel_breaks).strip("_").lower()
    return re.sub(r"_+", "_", normalized)


def _validation_sample(item: QueueItem) -> bytes | None:
    if item.metadata is None:
        return None
    return item.metadata.validation_grayscale_sample


def _compare_roughness_smoothness_samples(
    roughness: bytes | None,
    smoothness: bytes | None,
) -> tuple[float, float] | None:
    if roughness is None or smoothness is None or len(roughness) != len(smoothness):
        return None
    sample_count = len(roughness)
    if sample_count == 0:
        return None
    direct_error = sum(abs(r - s) for r, s in zip(roughness, smoothness))
    inverse_error = sum(abs(r - (255 - s)) for r, s in zip(roughness, smoothness))
    normalizer = 255.0 * sample_count
    return direct_error / normalizer, inverse_error / normalizer
