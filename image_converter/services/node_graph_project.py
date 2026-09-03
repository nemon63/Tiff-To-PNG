from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from image_converter.domain.node_graph import (
    GraphConnection,
    GraphNode,
    NodeGraph,
    NodeGraphProject,
    NodeType,
    default_node_properties,
)

GRAPH_PROJECT_FILENAME = "graph.texturegraph.json"
GRAPH_PROJECT_EXTENSION = ".texturegraph"
GRAPH_PROJECT_FILE_FILTER = (
    "Texture Graph Project (*.texturegraph *.texturegraph.json);;All Files (*)"
)


class NodeGraphProjectRepository:
    def save(self, project: NodeGraphProject, location: Path) -> Path:
        project_path = self.resolve_project_path(location)
        if not project_path.suffix:
            project_path = project_path.with_suffix(GRAPH_PROJECT_EXTENSION)
        project_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._serialize_project(project, project_path.parent)
        project_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return project_path

    def load(self, location: Path) -> NodeGraphProject:
        project_path = self.resolve_project_path(location)
        raw_data = json.loads(project_path.read_text(encoding="utf-8"))
        if not isinstance(raw_data, dict):
            raise ValueError("Invalid graph project file.")
        fallback_name = (
            project_path.parent.name
            if project_path.name == GRAPH_PROJECT_FILENAME
            else project_path.stem
        )
        return self._deserialize_project(
            raw_data,
            project_path.parent,
            fallback_name,
        )

    @staticmethod
    def resolve_project_path(location: Path) -> Path:
        if location.is_dir():
            return location / GRAPH_PROJECT_FILENAME
        return location

    def _serialize_project(
        self,
        project: NodeGraphProject,
        bundle_dir: Path,
    ) -> dict[str, Any]:
        return {
            "version": project.version,
            "name": project.name,
            "graph": {
                "viewport_center": list(project.graph.viewport_center),
                "viewport_zoom": project.graph.viewport_zoom,
                "nodes": [
                    self._serialize_node(node, bundle_dir) for node in project.graph.nodes
                ],
                "connections": [
                    {
                        "id": connection.connection_id,
                        "source_node_id": connection.source_node_id,
                        "source_socket_id": connection.source_socket_id,
                        "target_node_id": connection.target_node_id,
                        "target_socket_id": connection.target_socket_id,
                    }
                    for connection in project.graph.connections
                ],
            },
        }

    def _serialize_node(self, node: GraphNode, bundle_dir: Path) -> dict[str, Any]:
        properties = dict(node.properties)
        if node.node_type is NodeType.TEXTURE_INPUT and properties.get("path"):
            properties["path"] = self._serialize_path(Path(str(properties["path"])), bundle_dir)

        return {
            "id": node.node_id,
            "type": node.node_type.value,
            "title": node.title,
            "position": list(node.position),
            "properties": properties,
        }

    def _deserialize_project(
        self,
        data: dict[str, Any],
        project_dir: Path,
        fallback_name: str,
    ) -> NodeGraphProject:
        graph_data = data.get("graph", {})
        if not isinstance(graph_data, dict):
            graph_data = {}

        nodes = [
            self._deserialize_node(raw_node, project_dir)
            for raw_node in graph_data.get("nodes", [])
            if isinstance(raw_node, dict)
        ]
        connections = [
            GraphConnection(
                connection_id=str(raw_connection.get("id", "")),
                source_node_id=str(raw_connection.get("source_node_id", "")),
                source_socket_id=str(raw_connection.get("source_socket_id", "")),
                target_node_id=str(raw_connection.get("target_node_id", "")),
                target_socket_id=str(raw_connection.get("target_socket_id", "")),
            )
            for raw_connection in graph_data.get("connections", [])
            if isinstance(raw_connection, dict)
        ]

        graph = NodeGraph(
            nodes=nodes,
            connections=[
                connection
                for connection in connections
                if connection.connection_id
                and connection.source_node_id
                and connection.target_node_id
            ],
            viewport_center=self._coerce_pair(graph_data.get("viewport_center"), (0.0, 0.0)),
            viewport_zoom=self._coerce_float(graph_data.get("viewport_zoom"), 1.0),
        )
        return NodeGraphProject(
            name=str(data.get("name") or fallback_name),
            version=self._coerce_int(data.get("version"), 1),
            graph=graph,
        )

    def _deserialize_node(self, data: dict[str, Any], bundle_dir: Path) -> GraphNode:
        try:
            node_type = NodeType(str(data.get("type", NodeType.TEXTURE_INPUT.value)))
        except ValueError:
            node_type = NodeType.TEXTURE_INPUT

        raw_properties = data.get("properties", {})
        if not isinstance(raw_properties, dict):
            raw_properties = {}
        properties = {"display": False}
        properties.update(default_node_properties(node_type))
        properties.update(dict(raw_properties))
        if node_type is NodeType.TEXTURE_INPUT and properties.get("path"):
            properties["path"] = str(self._deserialize_path(str(properties["path"]), bundle_dir))

        return GraphNode(
            node_id=str(data.get("id", "")),
            node_type=node_type,
            title=str(data.get("title") or node_type.value),
            position=self._coerce_pair(data.get("position"), (0.0, 0.0)),
            properties=properties,
        )

    @staticmethod
    def _serialize_path(path: Path, bundle_dir: Path) -> str:
        try:
            resolved_path = path.resolve()
            resolved_bundle = bundle_dir.resolve()
            return str(resolved_path.relative_to(resolved_bundle))
        except (OSError, ValueError):
            return str(path)

    @staticmethod
    def _deserialize_path(value: str, bundle_dir: Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return bundle_dir / path

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _coerce_pair(cls, value: Any, default: tuple[float, float]) -> tuple[float, float]:
        try:
            return (
                cls._coerce_float(value[0], default[0]),
                cls._coerce_float(value[1], default[1]),
            )
        except (TypeError, IndexError):
            return default
