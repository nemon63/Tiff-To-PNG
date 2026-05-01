from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


class NodeType(str, Enum):
    TEXTURE_INPUT = "texture_input"
    CONSTANT_CHANNEL = "constant_channel"
    INVERT_CHANNEL = "invert_channel"
    VIEW = "view"
    OUTPUT_RGBA = "output_rgba"


class SocketDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


class SocketType(str, Enum):
    IMAGE = "image"
    CHANNEL = "channel"


class OutputMode(str, Enum):
    RGB = "rgb"
    RGBA = "rgba"


@dataclass(slots=True, frozen=True)
class GraphSocket:
    socket_id: str
    name: str
    direction: SocketDirection
    socket_type: SocketType


@dataclass(slots=True)
class GraphNode:
    node_id: str
    node_type: NodeType
    title: str
    position: tuple[float, float] = (0.0, 0.0)
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GraphConnection:
    connection_id: str
    source_node_id: str
    source_socket_id: str
    target_node_id: str
    target_socket_id: str


@dataclass(slots=True)
class NodeGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    connections: list[GraphConnection] = field(default_factory=list)
    viewport_center: tuple[float, float] = (0.0, 0.0)
    viewport_zoom: float = 1.0


@dataclass(slots=True)
class NodeGraphProject:
    name: str = "Untitled Graph"
    graph: NodeGraph = field(default_factory=NodeGraph)
    version: int = 1


def make_node_id() -> str:
    return f"node_{uuid4().hex[:12]}"


def make_connection_id() -> str:
    return f"conn_{uuid4().hex[:12]}"


def node_type_label(node_type: NodeType) -> str:
    mapping = {
        NodeType.TEXTURE_INPUT: "Texture",
        NodeType.CONSTANT_CHANNEL: "Constant",
        NodeType.INVERT_CHANNEL: "Invert",
        NodeType.VIEW: "View",
        NodeType.OUTPUT_RGBA: "Output",
    }
    return mapping[node_type]


def default_node_title(node_type: NodeType) -> str:
    return node_type_label(node_type)


def default_node_properties(node_type: NodeType) -> dict[str, Any]:
    if node_type is NodeType.TEXTURE_INPUT:
        return {"path": ""}
    if node_type is NodeType.CONSTANT_CHANNEL:
        return {"value": 255}
    if node_type is NodeType.INVERT_CHANNEL:
        return {}
    if node_type is NodeType.VIEW:
        return {}
    if node_type is NodeType.OUTPUT_RGBA:
        return {
            "filename": "packed.png",
            "output_path": "",
            "mode": OutputMode.RGBA.value,
            "enabled": True,
        }
    return {}


def create_graph_node(
    node_type: NodeType,
    *,
    title: str | None = None,
    position: tuple[float, float] = (0.0, 0.0),
    properties: dict[str, Any] | None = None,
) -> GraphNode:
    node_properties = default_node_properties(node_type)
    if properties:
        node_properties.update(properties)
    return GraphNode(
        node_id=make_node_id(),
        node_type=node_type,
        title=title or default_node_title(node_type),
        position=position,
        properties=node_properties,
    )


def socket_definitions(node_type: NodeType) -> tuple[GraphSocket, ...]:
    if node_type is NodeType.TEXTURE_INPUT:
        return (
            GraphSocket("r", "R", SocketDirection.OUTPUT, SocketType.CHANNEL),
            GraphSocket("g", "G", SocketDirection.OUTPUT, SocketType.CHANNEL),
            GraphSocket("b", "B", SocketDirection.OUTPUT, SocketType.CHANNEL),
            GraphSocket("a", "A", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.CONSTANT_CHANNEL:
        return (GraphSocket("out", "Value", SocketDirection.OUTPUT, SocketType.CHANNEL),)
    if node_type is NodeType.INVERT_CHANNEL:
        return (
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "Out", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.VIEW:
        return (
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.OUTPUT_RGBA:
        return (
            GraphSocket("r", "R", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("g", "G", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("b", "B", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("a", "A", SocketDirection.INPUT, SocketType.CHANNEL),
        )
    return ()


def find_node(graph: NodeGraph, node_id: str) -> GraphNode | None:
    return next((node for node in graph.nodes if node.node_id == node_id), None)


def incoming_connection(
    graph: NodeGraph,
    *,
    target_node_id: str,
    target_socket_id: str,
) -> GraphConnection | None:
    return next(
        (
            connection
            for connection in graph.connections
            if connection.target_node_id == target_node_id
            and connection.target_socket_id == target_socket_id
        ),
        None,
    )


def remove_node(graph: NodeGraph, node_id: str) -> None:
    graph.nodes = [node for node in graph.nodes if node.node_id != node_id]
    graph.connections = [
        connection
        for connection in graph.connections
        if connection.source_node_id != node_id and connection.target_node_id != node_id
    ]


def replace_input_connection(graph: NodeGraph, connection: GraphConnection) -> None:
    graph.connections = [
        candidate
        for candidate in graph.connections
        if not (
            candidate.target_node_id == connection.target_node_id
            and candidate.target_socket_id == connection.target_socket_id
        )
    ]
    graph.connections.append(connection)
