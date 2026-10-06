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
