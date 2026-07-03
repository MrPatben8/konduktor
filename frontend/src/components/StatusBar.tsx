interface Props {
  showing: number
  total: number
  sourceName: string
  loading?: boolean
}

export function StatusBar({ showing, total, sourceName, loading }: Props) {
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
      <span className="ml-auto text-faint">konduktor · phase 3</span>
    </div>
  )
}
