import type { ReactNode } from 'react'
import { Icon } from '../lib/icons'

/** One choice in the library picker: an icon tile, a title, one line of facts. */
export function OptionCard({
  icon,
  lit,
  title,
  subtitle,
  disabled,
  busy,
  onClick,
}: {
  icon: ReactNode
  /** A small lit dot on the icon tile: "something is here". */
  lit?: boolean
  title: string
  subtitle: string
  disabled?: boolean
  busy?: boolean
  onClick: () => void
}) {
  return (
    <button
      disabled={disabled || busy}
      onClick={onClick}
      className="btn-glass group flex w-full items-center gap-3.5 rounded-2xl px-3 py-3 text-left disabled:cursor-not-allowed disabled:opacity-40"
    >
      <span className="well relative flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-text">
        {icon}
        {lit && (
          <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-mint shadow-[0_0_8px_var(--color-mint)] ring-2 ring-[rgb(12_14_24)]" />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-semibold text-text">{title}</div>
        <div className="truncate text-xs text-faint">{subtitle}</div>
      </div>
      <Icon
        name="chevronRight"
        size={14}
        strokeWidth={2.2}
        className="text-faint transition-colors group-hover:text-text"
      />
    </button>
  )
}
