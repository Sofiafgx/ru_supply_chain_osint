'use strict';
// Topology only: edge direction and production quantities are not interpreted.
(function(root) {
  function analyse(nodes, edges, removedId) {
    const ids = nodes.map(n => typeof n === 'string' ? n : n.id);
    const known = new Set(ids);
    if (!known.has(removedId)) throw new Error('Вузол не знайдено');
    const links = new Map(ids.map(id => [id, new Set()]));
    const validEdges = edges.filter(e => known.has(e.from) && known.has(e.to));
    for (const e of validEdges) if (e.from !== e.to) {
      links.get(e.from).add(e.to); links.get(e.to).add(e.from);
    }
    function components(blocked) {
      const membership = new Map(), groups = [];
      let isolated = 0, pairs = 0;
      for (const id of ids) {
        if (id === blocked || membership.has(id)) continue;
        const group = [id], index = groups.length;
        membership.set(id, index);
        for (let cursor = 0; cursor < group.length; cursor++) {
          for (const next of links.get(group[cursor])) {
            if (next === blocked || membership.has(next)) continue;
            membership.set(next, index); group.push(next);
          }
        }
        groups.push(group);
        if (group.length === 1) isolated++;
        pairs += group.length * (group.length - 1) / 2;
      }
      return {groups, membership, stats: {
        nodes: ids.length - (blocked == null ? 0 : 1),
        edges: validEdges.filter(e => e.from !== blocked && e.to !== blocked).length,
        components: groups.length, largest: groups.reduce((n, g) => Math.max(n, g.length), 0),
        isolated, connectedPairs: pairs
      }};
    }
    const before = components(null), after = components(removedId);
    const original = before.groups[before.membership.get(removedId)];
    const fragmentIndices = new Set(original.filter(id => id !== removedId).map(id => after.membership.get(id)));
    const fragments = [...fragmentIndices].map(i => after.groups[i]).sort((a, b) => b.length - a.length || a[0].localeCompare(b[0]));
    const fragmentById = new Map();
    fragments.forEach((g, i) => g.forEach(id => fragmentById.set(id, i)));
    const removedEdges = validEdges.filter(e => e.from === removedId || e.to === removedId);
    const newlyIsolated = original.filter(id => id !== removedId && links.get(id).size > 0 &&
      [...links.get(id)].every(next => next === removedId));
    return {
      removedId, before: before.stats, after: after.stats,
      originalSize: original.length, fragments, fragmentById,
      removedEdges: removedEdges.length,
      neighbours: [...links.get(removedId)],
      detached: fragments.slice(1).reduce((n, g) => n + g.length, 0),
      newlyIsolated, pairLoss: before.stats.connectedPairs - after.stats.connectedPairs
    };
  }
  const api = {analyse};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NetworkSimulation = api;
})(typeof window === 'undefined' ? globalThis : window);
