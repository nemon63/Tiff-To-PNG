from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


class NodeType(str, Enum):
    TEXTURE_INPUT = "texture_input"
    COLOR = "color"
    CONSTANT_CHANNEL = "constant_channel"
    INVERT_CHANNEL = "invert_channel"
    LEVELS_CHANNEL = "levels_channel"
    REMAP_CHANNEL = "remap_channel"
    CLAMP_CHANNEL = "clamp_channel"
    THRESHOLD_CHANNEL = "threshold_channel"
    BLUR_CHANNEL = "blur_channel"
    DILATE_CHANNEL = "dilate_channel"
    ERODE_CHANNEL = "erode_channel"
    BLEND_CHANNEL = "blend_channel"
    LUMINANCE = "luminance"
    MIX_IMAGE = "mix_image"
    BLEND_IMAGE = "blend_image"
    SPLIT_RGBA = "split_rgba"
    COMBINE_RGBA = "combine_rgba"
    SET_ALPHA = "set_alpha"
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


class OutputAlphaInputMode(str, Enum):
    MULTIPLY = "multiply"
    REPLACE = "replace"


class OutputProfile(str, Enum):
    GENERIC_RGBA = "generic_rgba"
    UNITY_URP = "unity_urp"
    UNITY_HDRP = "unity_hdrp"
    UNREAL_ORM = "unreal_orm"
    METAHUMAN_REPACK = "metahuman_repack"


class TextureNodeColorSpace(str, Enum):
    AUTO = "auto"
    SRGB = "srgb"
    LINEAR = "linear"


class TextureDataRole(str, Enum):
    COLOR = "color"
    DATA = "data"
    NORMAL = "normal"
    MASK = "mask"


class GraphValidationSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


OPERATION_NODE_TYPES = (
    NodeType.INVERT_CHANNEL,
    NodeType.LEVELS_CHANNEL,
    NodeType.REMAP_CHANNEL,
    NodeType.CLAMP_CHANNEL,
    NodeType.THRESHOLD_CHANNEL,
    NodeType.BLUR_CHANNEL,
    NodeType.DILATE_CHANNEL,
    NodeType.ERODE_CHANNEL,
    NodeType.BLEND_CHANNEL,
    NodeType.LUMINANCE,
    NodeType.MIX_IMAGE,
    NodeType.BLEND_IMAGE,
)


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


@dataclass(slots=True, frozen=True)
class GraphValidationIssue:
    severity: GraphValidationSeverity
    message: str
    node_id: str = ""
    socket_id: str = ""


def make_node_id() -> str:
    return f"node_{uuid4().hex[:12]}"


def make_connection_id() -> str:
    return f"conn_{uuid4().hex[:12]}"


def node_type_label(node_type: NodeType) -> str:
    mapping = {
        NodeType.TEXTURE_INPUT: "Texture",
        NodeType.COLOR: "Color",
        NodeType.CONSTANT_CHANNEL: "Constant",
        NodeType.INVERT_CHANNEL: "Invert",
        NodeType.LEVELS_CHANNEL: "Levels",
        NodeType.REMAP_CHANNEL: "Remap",
        NodeType.CLAMP_CHANNEL: "Clamp",
        NodeType.THRESHOLD_CHANNEL: "Threshold",
        NodeType.BLUR_CHANNEL: "Blur",
        NodeType.DILATE_CHANNEL: "Dilate",
        NodeType.ERODE_CHANNEL: "Erode",
        NodeType.BLEND_CHANNEL: "Blend Channel",
        NodeType.LUMINANCE: "Luminance",
        NodeType.MIX_IMAGE: "Mix Image",
        NodeType.BLEND_IMAGE: "Blend Image",
        NodeType.SPLIT_RGBA: "Split RGBA",
        NodeType.COMBINE_RGBA: "Combine RGBA",
        NodeType.SET_ALPHA: "Apply Mask",
        NodeType.VIEW: "View",
        NodeType.OUTPUT_RGBA: "Output",
    }
    return mapping[node_type]


def default_node_title(node_type: NodeType) -> str:
    return node_type_label(node_type)


def default_node_properties(node_type: NodeType) -> dict[str, Any]:
    if node_type is NodeType.TEXTURE_INPUT:
        return {
            "path": "",
            "color_space": TextureNodeColorSpace.AUTO.value,
            "data_role": TextureDataRole.DATA.value,
        }
    if node_type is NodeType.COLOR:
        return {
            "red": 255,
            "green": 255,
            "blue": 255,
            "alpha": 255,
            "width": 1024,
            "height": 1024,
        }
    if node_type is NodeType.CONSTANT_CHANNEL:
        return {"value": 255}
    if node_type is NodeType.INVERT_CHANNEL:
        return {"enabled": True}
    if node_type is NodeType.LEVELS_CHANNEL:
        return {
            "enabled": True,
            "black": 0,
            "white": 255,
            "gamma": 1.0,
            "out_min": 0,
            "out_max": 255,
        }
    if node_type is NodeType.REMAP_CHANNEL:
        return {
            "enabled": True,
            "in_min": 0,
            "in_max": 255,
            "out_min": 0,
            "out_max": 255,
        }
    if node_type is NodeType.CLAMP_CHANNEL:
        return {"enabled": True, "min": 0, "max": 255}
    if node_type is NodeType.THRESHOLD_CHANNEL:
        return {"enabled": True, "threshold": 128}
    if node_type is NodeType.BLUR_CHANNEL:
        return {"enabled": True, "radius": 1}
    if node_type is NodeType.DILATE_CHANNEL:
        return {"enabled": True, "radius": 1}
    if node_type is NodeType.ERODE_CHANNEL:
        return {"enabled": True, "radius": 1}
    if node_type is NodeType.BLEND_CHANNEL:
        return {"enabled": True, "mode": "multiply", "opacity": 100}
    if node_type is NodeType.MIX_IMAGE:
        return {
            "enabled": True,
            "factor": 50,
            "resolution_source": "a",
            "resolution_width": 1024,
            "resolution_height": 1024,
            "mask_filter": "bilinear",
        }
    if node_type is NodeType.BLEND_IMAGE:
        return {
            "enabled": True,
            "mode": "multiply",
            "opacity": 100,
            "resolution_source": "a",
            "resolution_width": 1024,
            "resolution_height": 1024,
            "mask_filter": "bilinear",
        }
    if node_type is NodeType.LUMINANCE:
        return {"enabled": True}
    if node_type is NodeType.VIEW:
        return {}
    if node_type is NodeType.OUTPUT_RGBA:
        return {
            "filename": "packed.png",
            "output_path": "",
            "mode": OutputMode.RGBA.value,
            "alpha_input_mode": OutputAlphaInputMode.MULTIPLY.value,
            "profile": OutputProfile.GENERIC_RGBA.value,
            "enabled": True,
            "resolution_source": "auto",
            "resolution_width": 1024,
            "resolution_height": 1024,
        }
    if node_type is NodeType.SET_ALPHA:
        return {
            "mask_mode": "replace_alpha",
            "mask_filter": "bilinear",
        }
    return {}


def resettable_node_property_keys(node_type: NodeType) -> tuple[str, ...]:
    mapping = {
        NodeType.COLOR: ("red", "green", "blue", "alpha", "width", "height"),
        NodeType.CONSTANT_CHANNEL: ("value",),
        NodeType.LEVELS_CHANNEL: ("black", "white", "gamma", "out_min", "out_max"),
        NodeType.REMAP_CHANNEL: ("in_min", "in_max", "out_min", "out_max"),
        NodeType.CLAMP_CHANNEL: ("min", "max"),
        NodeType.THRESHOLD_CHANNEL: ("threshold",),
        NodeType.BLUR_CHANNEL: ("radius",),
        NodeType.DILATE_CHANNEL: ("radius",),
        NodeType.ERODE_CHANNEL: ("radius",),
        NodeType.BLEND_CHANNEL: ("mode", "opacity"),
        NodeType.MIX_IMAGE: (
            "factor",
            "resolution_source",
            "resolution_width",
            "resolution_height",
            "mask_filter",
        ),
        NodeType.BLEND_IMAGE: (
            "mode",
            "opacity",
            "resolution_source",
            "resolution_width",
            "resolution_height",
            "mask_filter",
        ),
        NodeType.SET_ALPHA: ("mask_mode", "mask_filter"),
    }
    return mapping.get(node_type, ())


def node_has_resettable_parameters(node_type: NodeType) -> bool:
    return bool(resettable_node_property_keys(node_type))


def node_has_enable_flag(node_type: NodeType) -> bool:
    return node_type in OPERATION_NODE_TYPES


def reset_node_parameters(node: GraphNode) -> None:
    defaults = default_node_properties(node.node_type)
    for key in resettable_node_property_keys(node.node_type):
        if key in defaults:
            node.properties[key] = defaults[key]


def create_graph_node(
    node_type: NodeType,
    *,
    title: str | None = None,
    position: tuple[float, float] = (0.0, 0.0),
    properties: dict[str, Any] | None = None,
) -> GraphNode:
    node_properties = {"display": False}
    node_properties.update(default_node_properties(node_type))
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
            GraphSocket("image", "RGBA", SocketDirection.OUTPUT, SocketType.IMAGE),
            GraphSocket("r", "R", SocketDirection.OUTPUT, SocketType.CHANNEL),
            GraphSocket("g", "G", SocketDirection.OUTPUT, SocketType.CHANNEL),
            GraphSocket("b", "B", SocketDirection.OUTPUT, SocketType.CHANNEL),
            GraphSocket("a", "A", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.COLOR:
        return (
            GraphSocket("image", "RGBA", SocketDirection.OUTPUT, SocketType.IMAGE),
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
    if node_type is NodeType.LEVELS_CHANNEL:
        return (
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "Out", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.REMAP_CHANNEL:
        return (
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "Out", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.CLAMP_CHANNEL:
        return (
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "Out", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.THRESHOLD_CHANNEL:
        return (
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "Out", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.BLUR_CHANNEL:
        return (
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "Out", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.DILATE_CHANNEL:
        return (
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "Out", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.ERODE_CHANNEL:
        return (
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "Out", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.BLEND_CHANNEL:
        return (
            GraphSocket("a", "A", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("b", "B", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "Out", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.LUMINANCE:
        return (
            GraphSocket("r", "R", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("g", "G", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("b", "B", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "L", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.MIX_IMAGE:
        return (
            GraphSocket("a", "A", SocketDirection.INPUT, SocketType.IMAGE),
            GraphSocket("b", "B", SocketDirection.INPUT, SocketType.IMAGE),
            GraphSocket("mask", "Mask", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("image", "RGBA", SocketDirection.OUTPUT, SocketType.IMAGE),
        )
    if node_type is NodeType.BLEND_IMAGE:
        return (
            GraphSocket("a", "A", SocketDirection.INPUT, SocketType.IMAGE),
            GraphSocket("b", "B", SocketDirection.INPUT, SocketType.IMAGE),
            GraphSocket("mask", "Mask", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("image", "RGBA", SocketDirection.OUTPUT, SocketType.IMAGE),
        )
    if node_type is NodeType.SPLIT_RGBA:
        return (
            GraphSocket("image", "RGBA", SocketDirection.INPUT, SocketType.IMAGE),
            GraphSocket("r", "R", SocketDirection.OUTPUT, SocketType.CHANNEL),
            GraphSocket("g", "G", SocketDirection.OUTPUT, SocketType.CHANNEL),
            GraphSocket("b", "B", SocketDirection.OUTPUT, SocketType.CHANNEL),
            GraphSocket("a", "A", SocketDirection.OUTPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.COMBINE_RGBA:
        return (
            GraphSocket("r", "R", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("g", "G", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("b", "B", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("a", "A", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("image", "RGBA", SocketDirection.OUTPUT, SocketType.IMAGE),
        )
    if node_type is NodeType.SET_ALPHA:
        return (
            GraphSocket("image", "Image", SocketDirection.INPUT, SocketType.IMAGE),
            GraphSocket("alpha", "Mask", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("out", "RGBA", SocketDirection.OUTPUT, SocketType.IMAGE),
        )
    if node_type is NodeType.VIEW:
        return (
            GraphSocket("image", "Image", SocketDirection.INPUT, SocketType.IMAGE),
            GraphSocket("in", "In", SocketDirection.INPUT, SocketType.CHANNEL),
        )
    if node_type is NodeType.OUTPUT_RGBA:
        return (
            GraphSocket("image", "Image", SocketDirection.INPUT, SocketType.IMAGE),
            GraphSocket("r", "R", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("g", "G", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("b", "B", SocketDirection.INPUT, SocketType.CHANNEL),
            GraphSocket("a", "A", SocketDirection.INPUT, SocketType.CHANNEL),
        )
    return ()


def node_bypass_socket_pair(node_type: NodeType) -> tuple[str, str] | None:
    """Return the primary input/output pair used to bypass a processing node."""
    if node_type in (
        NodeType.INVERT_CHANNEL,
        NodeType.LEVELS_CHANNEL,
        NodeType.REMAP_CHANNEL,
        NodeType.CLAMP_CHANNEL,
        NodeType.THRESHOLD_CHANNEL,
        NodeType.BLUR_CHANNEL,
        NodeType.DILATE_CHANNEL,
        NodeType.ERODE_CHANNEL,
    ):
        return ("in", "out")
    if node_type is NodeType.BLEND_CHANNEL:
        return ("a", "out")
    if node_type is NodeType.LUMINANCE:
        return ("r", "out")
    if node_type in (NodeType.MIX_IMAGE, NodeType.BLEND_IMAGE):
        return ("a", "image")
    if node_type is NodeType.SET_ALPHA:
        return ("image", "out")
    return None


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
