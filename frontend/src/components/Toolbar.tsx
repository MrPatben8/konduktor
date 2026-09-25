import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useCaps } from '../lib/capabilities'
import { Icon } from '../lib/icons'

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
  'btn-glass rounded-full px-3 py-1.5 text-sm text-text outline-none focus:ring-1 focus:ring-accent'

export function Toolbar({
  filters,
  onChange,
}: Props) {
  const caps = useCaps()
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
    <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
      <div className="relative">
        <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint">
          <Icon name="search" size={15} />
        </span>
        <input
          value={filters.search}
          onChange={(e) => set({ search: e.target.value })}
          placeholder="Search artist, title, album…"
          className="well w-72 rounded-full py-1.5 pl-9 pr-3 text-sm text-text outline-none placeholder:text-faint focus:ring-1 focus:ring-accent"
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
          className="well w-14 rounded-full px-2 py-1.5 text-center font-mono text-sm outline-none focus:ring-1 focus:ring-accent placeholder:text-faint"
        />
        <span className="text-xs text-faint">BPM</span>
        <input
          value={filters.bpmMax}
          onChange={(e) => set({ bpmMax: e.target.value.replace(/[^\d.]/g, '') })}
          placeholder="max"
          className="well w-14 rounded-full px-2 py-1.5 text-center font-mono text-sm outline-none focus:ring-1 focus:ring-accent placeholder:text-faint"
        />
      </div>

      <select
        value={filters.ratingMin}
        onChange={(e) => set({ ratingMin: Number(e.target.value) })}
        className={selectCls}
      >
        <option value={0}>Any rating</option>
        {Array.from({ length: caps.tracks.rating_max }, (_, i) => i + 1).map((r) => (
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

      <div className="ml-auto flex items-center gap-2">
        {active && (
          <button
            onClick={() => onChange(emptyFilters)}
            className="rounded-full px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
          >
            Clear filters
          </button>
        )}
      </div>
    </div>
  )
}
