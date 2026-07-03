import { useQuery } from '@tanstack/react-query'
import { api } from '../api'

export interface Filters {
  search: string
  genre: string
  key: string
  bpmMin: string
  bpmMax: string
  ratingMin: number
  hasCues: 'any' | 'yes' | 'no'
}

export const emptyFilters: Filters = {
  search: '',
  genre: '',
  key: '',
  bpmMin: '',
  bpmMax: '',
  ratingMin: 0,
  hasCues: 'any',
}

interface Props {
  filters: Filters
  onChange: (f: Filters) => void
}

const selectCls =
  'rounded-md border border-line bg-ink-850 px-2.5 py-1.5 text-sm text-text outline-none focus:border-accent hover:border-ink-600 transition-colors'

export function Toolbar({ filters, onChange }: Props) {
  const { data: facets } = useQuery({ queryKey: ['facets'], queryFn: api.facets })
  const set = (patch: Partial<Filters>) => onChange({ ...filters, ...patch })
  const active =
    filters.search ||
    filters.genre ||
    filters.key ||
    filters.bpmMin ||
    filters.bpmMax ||
    filters.ratingMin > 0 ||
    filters.hasCues !== 'any'

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-line bg-ink-900 px-4 py-3">
      <div className="relative">
        <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint">
          ⌕
        </span>
        <input
          value={filters.search}
          onChange={(e) => set({ search: e.target.value })}
          placeholder="Search artist, title, album…"
          className="w-64 rounded-md border border-line bg-ink-850 py-1.5 pl-8 pr-3 text-sm text-text outline-none placeholder:text-faint focus:border-accent"
        />
      </div>

      <select
        value={filters.genre}
        onChange={(e) => set({ genre: e.target.value })}
        className={selectCls}
      >
        <option value="">All genres</option>
        {facets?.genres.map((g) => (
          <option key={g.name} value={g.name}>
            {g.name.trim() || '(blank)'} · {g.count}
          </option>
        ))}
      </select>

      <select
        value={filters.key}
        onChange={(e) => set({ key: e.target.value })}
        className={selectCls}
      >
        <option value="">All keys</option>
        {facets?.keys.map((k) => (
          <option key={k.name} value={k.name}>
            {k.name} · {k.count}
          </option>
        ))}
      </select>

      <div className="flex items-center gap-1">
        <input
          value={filters.bpmMin}
          onChange={(e) => set({ bpmMin: e.target.value.replace(/[^\d.]/g, '') })}
          placeholder="min"
          className="w-14 rounded-md border border-line bg-ink-850 px-2 py-1.5 text-center text-sm tabular-nums outline-none focus:border-accent placeholder:text-faint"
        />
        <span className="text-xs text-faint">BPM</span>
        <input
          value={filters.bpmMax}
          onChange={(e) => set({ bpmMax: e.target.value.replace(/[^\d.]/g, '') })}
          placeholder="max"
          className="w-14 rounded-md border border-line bg-ink-850 px-2 py-1.5 text-center text-sm tabular-nums outline-none focus:border-accent placeholder:text-faint"
        />
      </div>

      <select
        value={filters.ratingMin}
        onChange={(e) => set({ ratingMin: Number(e.target.value) })}
        className={selectCls}
      >
        <option value={0}>Any rating</option>
        {[1, 2, 3, 4, 5].map((r) => (
          <option key={r} value={r}>
            {'★'.repeat(r)}+
          </option>
        ))}
      </select>

      <select
        value={filters.hasCues}
        onChange={(e) => set({ hasCues: e.target.value as Filters['hasCues'] })}
        className={selectCls}
      >
        <option value="any">Any cues</option>
        <option value="yes">Has cues</option>
        <option value="no">No cues</option>
      </select>

      {active && (
        <button
          onClick={() => onChange(emptyFilters)}
          className="ml-auto rounded-md px-2.5 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
        >
          Clear filters
        </button>
      )}
    </div>
  )
}
