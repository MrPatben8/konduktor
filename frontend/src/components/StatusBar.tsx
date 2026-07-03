interface Props {
  showing: number
  total: number
  sourceName: string
  loading?: boolean
  collectionName?: string | null
  onChangeCollection?: () => void
}

export function StatusBar({
  showing,
  total,
  sourceName,
  loading,
  collectionName,
  onChangeCollection,
}: Props) {
  return (
    <div className="flex items-center gap-3 border-t border-line bg-ink-900 px-4 py-2 text-xs text-muted">
      <span className="font-medium text-text">{sourceName}</span>
      <span className="text-faint">·</span>
      {loading ? (
        <span>Loading…</span>
      ) : (
        <span className="tabular-nums">
          {showing === total ? (
            <>{total.toLocaleString()} tracks</>
          ) : (
            <>
              {showing.toLocaleString()} of {total.toLocaleString()} tracks
            </>
          )}
        </span>
      )}
      {collectionName && (
        <button
          onClick={onChangeCollection}
          className="ml-auto flex items-center gap-1.5 rounded px-2 py-0.5 text-faint hover:bg-ink-800 hover:text-text"
          title="Change collection"
        >
          <span className="text-[11px]">⎘</span>
          <span className="max-w-[220px] truncate font-mono text-[11px]">{collectionName}</span>
        </button>
      )}
    </div>
  )
}
