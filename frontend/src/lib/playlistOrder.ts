/** A playlist's entry order with `moving` lifted out as one block, kept in
 *  their PLAYLIST order, and put back so the block's first track lands at the
 *  1-based `position` of the result. A position past the end puts the block
 *  last. A track listed twice in the playlist moves with both entries, since a
 *  selection is by track id. */
export function moveToPosition(order: string[], moving: ReadonlySet<string>, position: number): string[] {
  const block = order.filter((id) => moving.has(id))
  const rest = order.filter((id) => !moving.has(id))
  const at = Math.min(Math.max(Math.floor(position), 1) - 1, rest.length)
  return [...rest.slice(0, at), ...block, ...rest.slice(at)]
}

/** The highest position the block's first track can take: the block's own
 *  length is what sits behind it. */
export function maxPosition(order: string[], moving: ReadonlySet<string>): number {
  return order.length - order.filter((id) => moving.has(id)).length + 1
}
