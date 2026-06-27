from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy

from PyQt6.QtGui import QUndoCommand

from image_converter.domain.node_graph import (
    GraphConnection,
    GraphNode,
    NodeGraph,
    find_node,
)

GraphChangedCallback = Callable[[bool], None]


def clone_graph_node(node: GraphNode, *, node_id: str | None = None) -> GraphNode:
    return GraphNode(
        node_id=node_id or node.node_id,
        node_type=node.node_type,
        title=node.title,
        position=tuple(node.position),
        properties=deepcopy(node.properties),
    )


def clone_graph_connection(
    connection: GraphConnection,
    *,
    connection_id: str | None = None,
    source_node_id: str | None = None,
    target_node_id: str | None = None,
) -> GraphConnection:
    return GraphConnection(
        connection_id=connection_id or connection.connection_id,
        source_node_id=source_node_id or connection.source_node_id,
        source_socket_id=connection.source_socket_id,
        target_node_id=target_node_id or connection.target_node_id,
        target_socket_id=connection.target_socket_id,
    )


class GraphCommand(QUndoCommand):
    def __init__(
        self,
        graph: NodeGraph,
        on_changed: GraphChangedCallback,
        text: str,
        *,
        needs_rebuild: bool = True,
    ):
        super().__init__(text)
        self.graph = graph
        self._on_changed = on_changed
        self._needs_rebuild = needs_rebuild

    def _emit_changed(self) -> None:
        self._on_changed(self._needs_rebuild)


class AddNodesCommand(GraphCommand):
    def __init__(
        self,
        graph: NodeGraph,
        on_changed: GraphChangedCallback,
        nodes: Iterable[GraphNode],
        connections: Iterable[GraphConnection] = (),
        *,
        text: str = "Add node",
    ):
        super().__init__(graph, on_changed, text, needs_rebuild=True)
        self.nodes = list(nodes)
        self.connections = list(connections)
        self._node_ids = {node.node_id for node in self.nodes}
        self._connection_ids = {connection.connection_id for connection in self.connections}

    def redo(self) -> None:
        existing_node_ids = {node.node_id for node in self.graph.nodes}
        existing_connection_ids = {connection.connection_id for connection in self.graph.connections}
        self.graph.nodes.extend(node for node in self.nodes if node.node_id not in existing_node_ids)
        self.graph.connections.extend(
            connection
            for connection in self.connections
            if connection.connection_id not in existing_connection_ids
        )
        self._emit_changed()

    def undo(self) -> None:
        self.graph.nodes = [node for node in self.graph.nodes if node.node_id not in self._node_ids]
        self.graph.connections = [
            connection
            for connection in self.graph.connections
            if connection.connection_id not in self._connection_ids
            and connection.source_node_id not in self._node_ids
            and connection.target_node_id not in self._node_ids
        ]
        self._emit_changed()


class DeleteItemsCommand(GraphCommand):
    def __init__(
        self,
        graph: NodeGraph,
        on_changed: GraphChangedCallback,
        *,
        node_ids: Iterable[str] = (),
        connection_ids: Iterable[str] = (),
    ):
        super().__init__(graph, on_changed, "Delete selection", needs_rebuild=True)
        self._node_ids = set(node_ids)
        requested_connection_ids = set(connection_ids)
        self.nodes = [node for node in graph.nodes if node.node_id in self._node_ids]
        self.connections = [
            connection
            for connection in graph.connections
            if connection.connection_id in requested_connection_ids
            or connection.source_node_id in self._node_ids
            or connection.target_node_id in self._node_ids
        ]
        self._connection_ids = {connection.connection_id for connection in self.connections}

    def redo(self) -> None:
        self.graph.nodes = [node for node in self.graph.nodes if node.node_id not in self._node_ids]
        self.graph.connections = [
            connection
            for connection in self.graph.connections
            if connection.connection_id not in self._connection_ids
        ]
        self._emit_changed()

    def undo(self) -> None:
        existing_node_ids = {node.node_id for node in self.graph.nodes}
        existing_connection_ids = {connection.connection_id for connection in self.graph.connections}
        self.graph.nodes.extend(node for node in self.nodes if node.node_id not in existing_node_ids)
        self.graph.connections.extend(
            connection
            for connection in self.connections
            if connection.connection_id not in existing_connection_ids
        )
        self._emit_changed()


class RemoveConnectionsCommand(GraphCommand):
    def __init__(
        self,
        graph: NodeGraph,
        on_changed: GraphChangedCallback,
        connections: Iterable[GraphConnection],
        *,
        text: str = "Remove connection",
    ):
        super().__init__(graph, on_changed, text, needs_rebuild=True)
        self.connections = list(connections)
        self._connection_ids = {connection.connection_id for connection in self.connections}

    def redo(self) -> None:
        self.graph.connections = [
            connection
            for connection in self.graph.connections
            if connection.connection_id not in self._connection_ids
        ]
        self._emit_changed()

    def undo(self) -> None:
        existing_connection_ids = {connection.connection_id for connection in self.graph.connections}
        self.graph.connections.extend(
            connection
            for connection in self.connections
            if connection.connection_id not in existing_connection_ids
        )
        self._emit_changed()


class ReplaceInputConnectionCommand(GraphCommand):
    def __init__(
        self,
        graph: NodeGraph,
        on_changed: GraphChangedCallback,
        connection: GraphConnection,
        *,
        remove_connections: Iterable[GraphConnection] = (),
    ):
        super().__init__(graph, on_changed, "Connect nodes", needs_rebuild=True)
        self.connection = connection
        removed = list(remove_connections)
        for candidate in graph.connections:
            if (
                candidate.target_node_id == connection.target_node_id
                and candidate.target_socket_id == connection.target_socket_id
            ):
                removed.append(candidate)
        self.removed_connections = list({item.connection_id: item for item in removed}.values())
        self._removed_ids = {item.connection_id for item in self.removed_connections}

    def redo(self) -> None:
        self.graph.connections = [
            connection
            for connection in self.graph.connections
            if connection.connection_id not in self._removed_ids
            and not (
                connection.target_node_id == self.connection.target_node_id
                and connection.target_socket_id == self.connection.target_socket_id
            )
        ]
        if all(item.connection_id != self.connection.connection_id for item in self.graph.connections):
            self.graph.connections.append(self.connection)
        self._emit_changed()

    def undo(self) -> None:
        self.graph.connections = [
            connection
            for connection in self.graph.connections
            if connection.connection_id != self.connection.connection_id
        ]
        existing_connection_ids = {connection.connection_id for connection in self.graph.connections}
        self.graph.connections.extend(
            connection
            for connection in self.removed_connections
            if connection.connection_id not in existing_connection_ids
        )
        self._emit_changed()


class InsertNodeInConnectionCommand(GraphCommand):
    def __init__(
        self,
        graph: NodeGraph,
        on_changed: GraphChangedCallback,
        old_connection: GraphConnection,
        node: GraphNode,
        input_connection: GraphConnection,
        output_connection: GraphConnection,
    ):
        super().__init__(graph, on_changed, "Insert node in wire", needs_rebuild=True)
        self.old_connection = old_connection
        self.node = node
        self.input_connection = input_connection
        self.output_connection = output_connection

    def redo(self) -> None:
        self.graph.connections = [
            connection
            for connection in self.graph.connections
            if connection.connection_id != self.old_connection.connection_id
        ]
        if find_node(self.graph, self.node.node_id) is None:
            self.graph.nodes.append(self.node)
        existing_connection_ids = {connection.connection_id for connection in self.graph.connections}
        for connection in (self.input_connection, self.output_connection):
            if connection.connection_id not in existing_connection_ids:
                self.graph.connections.append(connection)
        self._emit_changed()

    def undo(self) -> None:
        new_ids = {
            self.input_connection.connection_id,
            self.output_connection.connection_id,
        }
        self.graph.nodes = [node for node in self.graph.nodes if node.node_id != self.node.node_id]
        self.graph.connections = [
            connection
            for connection in self.graph.connections
            if connection.connection_id not in new_ids
        ]
        if all(
            connection.connection_id != self.old_connection.connection_id
            for connection in self.graph.connections
        ):
            self.graph.connections.append(self.old_connection)
        self._emit_changed()


class SetNodeStateCommand(GraphCommand):
    _COMMAND_ID = 1001

    def __init__(
        self,
        graph: NodeGraph,
        on_changed: GraphChangedCallback,
        node: GraphNode,
        *,
        title: str,
        properties: dict,
        text: str = "Edit node",
        needs_rebuild: bool = False,
    ):
        super().__init__(graph, on_changed, text, needs_rebuild=needs_rebuild)
        self.node_id = node.node_id
        self.before_title = node.title
        self.before_properties = deepcopy(node.properties)
        self.after_title = title
        self.after_properties = deepcopy(properties)

    def redo(self) -> None:
        self._apply(self.after_title, self.after_properties)

    def undo(self) -> None:
        self._apply(self.before_title, self.before_properties)

    def id(self) -> int:
        return self._COMMAND_ID

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, SetNodeStateCommand):
            return False
        if self.node_id != other.node_id:
            return False
        if self._needs_rebuild != other._needs_rebuild:
            return False
        self.after_title = other.after_title
        self.after_properties = deepcopy(other.after_properties)
        return True

    def _apply(self, title: str, properties: dict) -> None:
        node = find_node(self.graph, self.node_id)
        if node is None:
            return
        node.title = title
        node.properties = deepcopy(properties)
        self._emit_changed()


class SetDisplayFlagCommand(GraphCommand):
    def __init__(
        self,
        graph: NodeGraph,
        on_changed: GraphChangedCallback,
        node_id: str,
    ):
        super().__init__(graph, on_changed, "Set display flag", needs_rebuild=False)
        self.node_id = node_id
        self.before = {
            node.node_id: bool(node.properties.get("display", False))
            for node in graph.nodes
        }

    def redo(self) -> None:
        for node in self.graph.nodes:
            node.properties["display"] = node.node_id == self.node_id
        self._emit_changed()

    def undo(self) -> None:
        for node in self.graph.nodes:
            node.properties["display"] = self.before.get(node.node_id, False)
        self._emit_changed()


class MoveNodesCommand(GraphCommand):
    def __init__(
        self,
        graph: NodeGraph,
        on_changed: GraphChangedCallback,
        before_positions: dict[str, tuple[float, float]],
        after_positions: dict[str, tuple[float, float]],
    ):
        super().__init__(graph, on_changed, "Move nodes", needs_rebuild=True)
        self.before_positions = dict(before_positions)
        self.after_positions = dict(after_positions)

    def redo(self) -> None:
        self._apply(self.after_positions)

    def undo(self) -> None:
        self._apply(self.before_positions)

    def _apply(self, positions: dict[str, tuple[float, float]]) -> None:
        for node in self.graph.nodes:
            if node.node_id in positions:
                node.position = positions[node.node_id]
        self._emit_changed()
