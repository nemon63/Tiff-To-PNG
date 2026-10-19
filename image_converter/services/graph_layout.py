from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from heapq import heappop, heappush
from statistics import mean

from image_converter.domain.node_graph import GraphConnection, GraphNode


@dataclass(frozen=True, slots=True)
class NodeLayoutGeometry:
    width: float
    height: float
    socket_y: dict[str, float]


def _separate(order, desired, geometry, gap):
    """Least-squares vertical placement with minimum node separation (PAVA)."""
    offsets, blocks = [], []
    offset = 0.0
    for index, node_id in enumerate(order):
        offsets.append(offset)
        blocks.append([index, index + 1, desired[node_id] - offset, 1])
        while len(blocks) > 1 and blocks[-2][2] / blocks[-2][3] > blocks[-1][2] / blocks[-1][3]:
            right = blocks.pop()
            left = blocks[-1]
            left[1] = right[1]
            left[2] += right[2]
            left[3] += right[3]
        offset += geometry[node_id].height + gap
    return {
        order[index]: total / count + offsets[index]
        for start, end, total, count in blocks for index in range(start, end)
    }


def connected_graph_layout(
    nodes: list[GraphNode], connections: list[GraphConnection],
    geometry: dict[str, NodeLayoutGeometry], *, horizontal_gap=100.0,
    vertical_gap=40.0, component_gap=100.0, grid=10.0,
) -> dict[str, tuple[float, float]]:
    """Port-aware layered layout, independent of the nodes' previous positions.

    Bounded forward/backward sweeps reorder branches using connected sockets,
    then align ports without overlapping node bodies. This is a heuristic, not
    an optimal wire router: dense/cyclic graphs can still contain crossings.
    """
    if not nodes:
        return {}
    by_id = {node.node_id: node for node in nodes}
    geometry = dict(geometry)
    sort_keys = {n.node_id: (n.title.casefold(), n.node_id) for n in nodes}
    stable_key = sort_keys.__getitem__
    incoming, outgoing, neighbors = defaultdict(list), defaultdict(list), defaultdict(set)
    for edge in connections:
        source, target = edge.source_node_id, edge.target_node_id
        if source not in by_id or target not in by_id or source == target:
            continue
        incoming[target].append(edge)
        outgoing[source].append(edge)
        neighbors[source].add(target)
        neighbors[target].add(source)

    components, visited = [], set()
    for seed in sorted(by_id, key=stable_key):
        if seed in visited:
            continue
        queue, component = deque([seed]), []
        visited.add(seed)
        while queue:
            node_id = queue.popleft()
            component.append(node_id)
            for neighbor in sorted(neighbors[node_id], key=stable_key):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        components.append(component)

    positions, component_top = {}, 0.0
    for component in components:
        remaining = set(component)
        indegree = {node_id: len(incoming[node_id]) for node_id in component}
        depth = dict.fromkeys(component, 0)
        ready, visit_order = [], []
        for node_id in component:
            if not indegree[node_id]:
                heappush(ready, (stable_key(node_id), node_id))
        while remaining:
            if not ready:
                # Invalid cycles must not hang Layout. Break a back edge only
                # for positioning; the actual graph is never modified.
                node_id = min(remaining, key=stable_key)
                heappush(ready, (stable_key(node_id), node_id))
            _, node_id = heappop(ready)
            if node_id not in remaining:
                continue
            remaining.remove(node_id)
            visit_order.append(node_id)
            for edge in outgoing[node_id]:
                target = edge.target_node_id
                if target in remaining:
                    depth[target] = max(depth[target], depth[node_id] + 1)
                    indegree[target] -= 1
                    if indegree[target] == 0:
                        heappush(ready, (stable_key(target), target))

        # Keep short branches close to their consumers instead of forcing all
        # textures into the leftmost column. Direct Normal/AO -> PBR wires then
        # need not run through the whole Base Color processing chain.
        latest = dict.fromkeys(component, max(depth.values()))
        for node_id in reversed(visit_order):
            targets = [e.target_node_id for e in outgoing[node_id] if depth[e.target_node_id] > depth[node_id]]
            if targets:
                latest[node_id] = min(latest[target] - 1 for target in targets)
        for node_id in visit_order:
            if not outgoing[node_id] and incoming[node_id]:
                # Side outputs/views stay beside their input, not necessarily
                # beside the deepest shader at the far right of the graph.
                latest[node_id] = max(latest[e.source_node_id] for e in incoming[node_id]) + 1
        depth = latest

        # Reserve narrow lanes for wires spanning several columns. These are
        # layout-only vertices, never real nodes/reroutes or serialized objects.
        # Bound their number so a dense graph cannot allocate O(nodes * edges).
        component_edges = sorted(
            (e for n in component for e in outgoing[n]),
            key=lambda e: (e.source_node_id, e.source_socket_id, e.target_node_id, e.target_socket_id, e.connection_id),
        )
        for node_id in component:
            incoming[node_id], outgoing[node_id] = [], []
        budget = min(4096, max(64, len(component) * 4))
        for edge_index, edge in enumerate(component_edges):
            span = depth[edge.target_node_id] - depth[edge.source_node_id] - 1
            source, source_socket = edge.source_node_id, edge.source_socket_id
            if 0 < span <= budget:
                budget -= span
                for level in range(depth[source] + 1, depth[edge.target_node_id]):
                    virtual = f"__layout_wire_{edge_index}_{level}"
                    while virtual in geometry:
                        virtual += "_"
                    geometry[virtual] = NodeLayoutGeometry(0, 0, {"in": 0, "out": 0})
                    sort_keys[virtual] = ("~wire", virtual)
                    depth[virtual] = level
                    component.append(virtual)
                    segment = GraphConnection("layout", source, source_socket, virtual, "in")
                    outgoing[source].append(segment)
                    incoming[virtual].append(segment)
                    source, source_socket = virtual, "out"
            segment = GraphConnection("layout", source, source_socket, edge.target_node_id, edge.target_socket_id)
            outgoing[source].append(segment)
            incoming[edge.target_node_id].append(segment)

        columns = defaultdict(list)
        for node_id in component:
            columns[depth[node_id]].append(node_id)
        levels = sorted(columns)
        y = {}
        for level in levels:
            columns[level].sort(key=stable_key)
            height = sum(geometry[n].height for n in columns[level]) + vertical_gap * (len(columns[level]) - 1)
            top = -height / 2
            for node_id in columns[level]:
                y[node_id] = top
                top += geometry[node_id].height + vertical_gap

        def desired_top(node_id, forward):
            anchors = []
            edges = incoming[node_id] if forward else outgoing[node_id]
            for edge in edges:
                neighbor = edge.source_node_id if forward else edge.target_node_id
                if (forward and depth[neighbor] >= depth[node_id]) or (not forward and depth[neighbor] <= depth[node_id]):
                    continue
                own_socket = edge.target_socket_id if forward else edge.source_socket_id
                other_socket = edge.source_socket_id if forward else edge.target_socket_id
                own_y = geometry[node_id].socket_y.get(own_socket, geometry[node_id].height / 2)
                other_y = geometry[neighbor].socket_y.get(other_socket, geometry[neighbor].height / 2)
                anchors.append(y[neighbor] + other_y - own_y)
            return mean(anchors) if anchors else y[node_id]

        for _ in range(8):
            for forward in (True, False):
                for level in levels if forward else reversed(levels):
                    order = columns[level]
                    desired = {n: desired_top(n, forward) for n in order}
                    old_order = {n: i for i, n in enumerate(order)}
                    order.sort(key=lambda n: (desired[n], old_order[n]))
                    y.update(_separate(order, desired, geometry, vertical_gap))

        x, min_y = 0.0, min(y.values())
        for level in levels:
            column_width = max(geometry[n].width for n in columns[level])
            for node_id in columns[level]:
                # Align outgoing ports: texture nodes are wider than operators.
                node_x = x + column_width - geometry[node_id].width
                positions[node_id] = (round(node_x / grid) * grid, round((y[node_id] - min_y + component_top) / grid) * grid)
            x += column_width + horizontal_gap
        component_top = max(positions[n][1] + geometry[n].height for n in component) + component_gap
    return {node_id: positions[node_id] for node_id in by_id}
