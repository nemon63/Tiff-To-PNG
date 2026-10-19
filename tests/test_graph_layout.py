from __future__ import annotations

import unittest
from dataclasses import replace

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPainterPathStroker

from image_converter.domain.node_graph import (
    GraphConnection, NodeType, SocketDirection, create_graph_node, socket_definitions,
)
from image_converter.services.graph_layout import NodeLayoutGeometry, connected_graph_layout
from image_converter.ui.graph_commands import AddNodesCommand
from image_converter.ui.node_editor import GraphWorkspace


def node(kind, name):
    result = create_graph_node(kind, title=name)
    result.node_id = name
    return result


def edge(source, target, source_socket="out", target_socket="in"):
    return GraphConnection(f"{source.node_id}-{target.node_id}-{target_socket}", source.node_id, source_socket, target.node_id, target_socket)


def geometry(nodes):
    result = {}
    for item in nodes:
        offsets, rows = {}, 2
        for direction in SocketDirection:
            sockets = [s for s in socket_definitions(item.node_type) if s.direction is direction]
            rows = max(rows, len(sockets))
            offsets.update({s.socket_id: 68 + i * 22 for i, s in enumerate(sockets)})
        result[item.node_id] = NodeLayoutGeometry(190, 68 + rows * 22, offsets)
    return result


def branching_material_graph():
    base, normal = node(NodeType.TEXTURE_INPUT, "Base Color"), node(NodeType.TEXTURE_INPUT, "Normal")
    ao, rough = node(NodeType.TEXTURE_INPUT, "AO"), node(NodeType.TEXTURE_INPUT, "Roughness")
    invert = [node(NodeType.INVERT_CHANNEL, f"Invert {i + 1}") for i in range(3)]
    combine, adjust = node(NodeType.COMBINE_RGBA, "Combine RGB"), node(NodeType.COLOR_ADJUST, "Color Adjust")
    levels, shader = node(NodeType.LEVELS_CHANNEL, "Roughness Levels"), node(NodeType.PBR_SHADER, "PBR Shader")
    output, view = node(NodeType.OUTPUT_RGBA, "Output"), node(NodeType.VIEW, "View")
    nodes = [base, normal, ao, rough, *invert, combine, adjust, levels, shader, output, view]
    edges = [edge(base, invert[i], ch) for i, ch in enumerate(("r", "g", "b"))]
    edges += [edge(invert[i], combine, "out", ch) for i, ch in enumerate(("r", "g", "b"))]
    edges += [edge(combine, adjust, "image", "image"), edge(adjust, shader, "out", "basecolor"),
              edge(normal, shader, "image", "normal"), edge(ao, shader, "r", "ao"),
              edge(rough, levels, "r"), edge(levels, shader, "out", "roughness"),
              edge(combine, output, "image", "image"), edge(levels, view)]
    return nodes, edges


class ConnectedLayoutTests(unittest.TestCase):
    def test_branch_order_follows_target_ports_not_names_or_old_positions(self):
        red = node(NodeType.CONSTANT_CHANNEL, "Z source")
        green = node(NodeType.CONSTANT_CHANNEL, "A source")
        red_op = node(NodeType.INVERT_CHANNEL, "A invert")
        green_op = node(NodeType.LEVELS_CHANNEL, "Z levels")
        output = node(NodeType.COMBINE_RGBA, "Output")
        nodes = [green, red, green_op, red_op, output]
        connections = [edge(red, red_op), edge(green, green_op),
                       edge(red_op, output, target_socket="r"), edge(green_op, output, target_socket="g")]
        sizes = geometry(nodes)
        positions = connected_graph_layout(nodes, connections, sizes)
        self.assertLess(positions[red.node_id][1], positions[green.node_id][1])
        self.assertLess(positions[red_op.node_id][1], positions[green_op.node_id][1])
        for connection in connections:
            self.assertLess(positions[connection.source_node_id][0], positions[connection.target_node_id][0])
        # Swapping actual connections must change layout, unlike type/name sorting.
        swapped = connections[:2] + [replace(connections[2], target_socket_id="g"), replace(connections[3], target_socket_id="r")]
        swapped_positions = connected_graph_layout(nodes, swapped, sizes)
        self.assertLess(swapped_positions[green.node_id][1], swapped_positions[red.node_id][1])

    def test_fanout_uses_source_socket_order(self):
        source = node(NodeType.TEXTURE_INPUT, "Texture")
        red, blue = node(NodeType.VIEW, "Z red"), node(NodeType.VIEW, "A blue")
        nodes = [source, blue, red]
        positions = connected_graph_layout(nodes, [edge(source, blue, "b"), edge(source, red, "r")], geometry(nodes))
        self.assertLess(positions[red.node_id][1], positions[blue.node_id][1])

    def test_independent_networks_do_not_interleave(self):
        a, b = node(NodeType.CONSTANT_CHANNEL, "A"), node(NodeType.CONSTANT_CHANNEL, "B")
        x, y = node(NodeType.VIEW, "Z"), node(NodeType.VIEW, "Y")
        nodes = [a, b, x, y]
        sizes = geometry(nodes)
        positions = connected_graph_layout(nodes, [edge(a, x), edge(b, y)], sizes)
        bottom_a = max(positions[n.node_id][1] + sizes[n.node_id].height for n in (a, x))
        top_b = min(positions[n.node_id][1] for n in (b, y))
        self.assertGreaterEqual(top_b - bottom_a, 90)

    def test_variable_node_sizes_never_overlap(self):
        sources = [node(NodeType.TEXTURE_INPUT, f"Source {i}") for i in range(8)]
        operations = [node(NodeType.INVERT_CHANNEL, f"Invert {i}") for i in range(8)]
        shader = node(NodeType.PBR_SHADER, "PBR")
        nodes = sources + operations + [shader]
        connections = [edge(s, op, "r") for s, op in zip(sources, operations)]
        connections += [edge(op, shader, target_socket=f"input{i}") for i, op in enumerate(operations)]
        sizes = geometry(nodes)
        for i, item in enumerate(nodes):
            sizes[item.node_id] = replace(sizes[item.node_id], width=190 + i * 9, height=120 + i * 13)
        positions = connected_graph_layout(nodes, connections, sizes, grid=20)
        for i, first in enumerate(nodes):
            x, y = positions[first.node_id]
            width, height = sizes[first.node_id].width, sizes[first.node_id].height
            for second in nodes[i + 1:]:
                sx, sy = positions[second.node_id]
                sw, sh = sizes[second.node_id].width, sizes[second.node_id].height
                self.assertFalse(x < sx + sw and sx < x + width and y < sy + sh and sy < y + height)

    def test_chain_aligns_connected_port_heights(self):
        source = node(NodeType.TEXTURE_INPUT, "Source")
        operation = node(NodeType.LEVELS_CHANNEL, "Levels")
        target = node(NodeType.VIEW, "View")
        nodes, connections = [source, operation, target], [edge(source, operation, "b"), edge(operation, target)]
        sizes = geometry(nodes)
        positions = connected_graph_layout(nodes, connections, sizes)
        for connection in connections:
            start = positions[connection.source_node_id][1] + sizes[connection.source_node_id].socket_y[connection.source_socket_id]
            end = positions[connection.target_node_id][1] + sizes[connection.target_node_id].socket_y[connection.target_socket_id]
            self.assertLessEqual(abs(start - end), 10)

    def test_short_direct_branches_stay_next_to_their_consumer(self):
        base, normal = node(NodeType.COLOR, "Base"), node(NodeType.COLOR, "Normal")
        first, second = node(NodeType.COLOR_ADJUST, "First"), node(NodeType.COLOR_ADJUST, "Second")
        shader = node(NodeType.PBR_SHADER, "PBR")
        nodes = [base, normal, first, second, shader]
        edges = [edge(base, first, "image", "image"), edge(first, second, "out", "image"),
                 edge(second, shader, "out", "basecolor"), edge(normal, shader, "image", "normal")]
        positions = connected_graph_layout(nodes, edges, geometry(nodes))
        self.assertEqual(positions[second.node_id][0], positions[normal.node_id][0])
        self.assertGreater(positions[normal.node_id][0], positions[base.node_id][0])

    def test_deterministic_despite_previous_positions_and_input_order(self):
        source, op, out = node(NodeType.COLOR, "Source"), node(NodeType.COLOR_ADJUST, "Adjust"), node(NodeType.VIEW, "View")
        nodes, edges = [source, op, out], [edge(source, op, "image", "image"), edge(op, out, "out", "image")]
        sizes = geometry(nodes)
        first = connected_graph_layout(nodes, edges, sizes)
        for i, item in enumerate(nodes):
            item.position = (i * 300, -i * 100)
        self.assertEqual(first, connected_graph_layout(list(reversed(nodes)), list(reversed(edges)), sizes))

    def test_cycles_and_dangling_edges_do_not_hang_or_modify_graph(self):
        nodes = [node(NodeType.INVERT_CHANNEL, name) for name in ("A", "B", "C")]
        edges = [edge(nodes[i], nodes[(i + 1) % 3]) for i in range(3)]
        edges += [GraphConnection("bad", "missing", "out", "A", "in"), edge(nodes[0], nodes[0])]
        before = list(edges)
        positions = connected_graph_layout(nodes, edges, geometry(nodes))
        self.assertEqual(3, len(set(positions.values())))
        self.assertEqual(before, edges)
        self.assertTrue(all(item.position == (0.0, 0.0) for item in nodes))

    def test_empty_graph_and_deep_chain(self):
        self.assertEqual({}, connected_graph_layout([], [], {}))
        nodes = [node(NodeType.INVERT_CHANNEL, f"N{i:04}") for i in range(1100)]
        edges = [edge(first, second) for first, second in zip(nodes, nodes[1:])]
        positions = connected_graph_layout(nodes, edges, geometry(nodes))
        self.assertEqual(1100, len(positions))
        self.assertLess(positions[nodes[0].node_id][0], positions[nodes[-1].node_id][0])


class LayoutIntegrationTests(unittest.TestCase):
    def test_material_branches_do_not_run_wires_through_other_nodes(self):
        app = QApplication.instance() or QApplication([])
        workspace = GraphWorkspace()
        try:
            nodes, edges = branching_material_graph()
            workspace._push_graph_command(AddNodesCommand(
                workspace.project.graph, workspace._on_graph_command_changed, nodes, edges,
            ))
            workspace.auto_layout_nodes()
            stroker = QPainterPathStroker()
            stroker.setWidth(4)
            for connection in edges:
                path = stroker.createStroke(workspace._scene.connection_items[connection.connection_id].path())
                for item in workspace._scene.node_items.values():
                    if item.node.node_id not in (connection.source_node_id, connection.target_node_id):
                        self.assertFalse(path.intersects(item.sceneBoundingRect()), (connection.connection_id, item.node.title))
            self.assertEqual(len(nodes), len(workspace.project.graph.nodes))
            self.assertEqual(edges, workspace.project.graph.connections)
        finally:
            workspace.shutdown_background_jobs(wait_ms=1000)
            workspace.close()
            workspace.deleteLater()
            app.processEvents()

    def test_actual_geometry_repeat_layout_selection_and_undo_redo(self):
        app = QApplication.instance() or QApplication([])
        workspace = GraphWorkspace()
        try:
            source = node(NodeType.TEXTURE_INPUT, "Texture")
            op = node(NodeType.LEVELS_CHANNEL, "Levels")
            shader = node(NodeType.PBR_SHADER, "PBR")
            nodes = [source, op, shader]
            edges = [edge(source, op, "r"), edge(op, shader, target_socket="roughness"), edge(source, shader, "image", "basecolor")]
            workspace._push_graph_command(AddNodesCommand(
                workspace.project.graph, workspace._on_graph_command_changed, nodes, edges,
            ), select_node_ids=[op.node_id])
            workspace.auto_layout_nodes()
            after = {n.node_id: n.position for n in nodes}
            count = workspace.undo_stack.count()
            workspace.auto_layout_nodes()
            self.assertEqual(after, {n.node_id: n.position for n in nodes})
            self.assertEqual(count, workspace.undo_stack.count())
            self.assertEqual([op.node_id], workspace._scene.selected_node_ids())
            items = list(workspace._scene.node_items.values())
            for i, first in enumerate(items):
                for second in items[i + 1:]:
                    self.assertFalse(first.sceneBoundingRect().intersects(second.sceneBoundingRect()))
            workspace.undo_stack.undo()
            self.assertTrue(all(n.position == (0.0, 0.0) for n in nodes))
            workspace.undo_stack.redo()
            self.assertEqual(after, {n.node_id: n.position for n in nodes})
            self.assertEqual(edges, workspace.project.graph.connections)
        finally:
            workspace.shutdown_background_jobs(wait_ms=1000)
            workspace.close()
            workspace.deleteLater()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
