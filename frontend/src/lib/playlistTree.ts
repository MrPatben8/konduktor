import type { PlaylistNode } from '../api'

/** Every selectable playlist at or under a node, in tree order. A folder
 *  contributes its nested playlists, a playlist itself — which is what adding a
 *  node to an export means: the export stores playlist ids, and a folder is a
 *  shape in the tree, not a thing with tracks. */
export function playlistIdsUnder(node: PlaylistNode): string[] {
  const here = node.kind === 'folder' ? [] : [node.id]
  return [...here, ...(node.children ?? []).flatMap(playlistIdsUnder)]
}

/** The ids of a node and everything nested in it — where a dragged folder
 *  cannot go. */
export function nodeIdsUnder(node: PlaylistNode): Set<string> {
  const ids = new Set<string>()
  const walk = (n: PlaylistNode) => {
    ids.add(n.id)
    n.children.forEach(walk)
  }
  walk(node)
  return ids
}

/** The node with this id, anywhere in the tree. */
export function findNode(nodes: PlaylistNode[], id: string): PlaylistNode | undefined {
  for (const n of nodes) {
    if (n.id === id) return n
    const found = findNode(n.children, id)
    if (found) return found
  }
  return undefined
}

/** Old id → new id for a node and everything nested in it, after a rename or
 *  move that may have changed them (a platform addressing folders by path).
 *  Neither changes a subtree's shape, so the two are matched by position. */
export function idsAfter(before: PlaylistNode, after: PlaylistNode): Map<string, string> {
  const map = new Map<string, string>()
  const walk = (a: PlaylistNode, b: PlaylistNode) => {
    map.set(a.id, b.id)
    a.children.forEach((c, i) => b.children[i] && walk(c, b.children[i]))
  }
  walk(before, after)
  return map
}
