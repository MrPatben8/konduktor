/** A playlist's entry order with `moving` lifted out as one block and put
 *  back so the block's first track lands at the 1-based `position` of the
 *  result. A position past the end puts the block last. The block keeps the
 *  tracks' PLAYLIST order, or with `blockOrder` (the ids as a sorted view
 *  shows them) that order instead — so a sort can be written into the
 *  playlist. A track listed twice in the playlist moves with both entries,
 *  since a selection is by track id; in `blockOrder` they stay together. */
export function moveToPosition(
  order: string[],
  moving: ReadonlySet<string>,
  position: number,
  blockOrder?: readonly string[],
): string[] {
  const block = blockOrder ? orderBlock(order, moving, blockOrder) : order.filter((id) => moving.has(id))
  const rest = order.filter((id) => !moving.has(id))
  const at = Math.min(Math.max(Math.floor(position), 1) - 1, rest.length)
  return [...rest.slice(0, at), ...block, ...rest.slice(at)]
}

/** The highest position the block's first track can take: the block's own
 *  length is what sits behind it. */
export function maxPosition(order: string[], moving: ReadonlySet<string>): number {
  return order.length - order.filter((id) => moving.has(id)).length + 1
}

/** The moving entries in `blockOrder`'s order, each track's repeats together.
 *  A moving track `blockOrder` lacks (none should) follows in playlist order. */
function orderBlock(order: string[], moving: ReadonlySet<string>, blockOrder: readonly string[]): string[] {
  const counts = new Map<string, number>()
  for (const id of order) if (moving.has(id)) counts.set(id, (counts.get(id) ?? 0) + 1)
  const block: string[] = []
  for (const id of [...blockOrder, ...order]) {
    const n = counts.get(id)
    if (!n) continue
    for (let i = 0; i < n; i++) block.push(id)
    counts.delete(id)
  }
  return block
}
