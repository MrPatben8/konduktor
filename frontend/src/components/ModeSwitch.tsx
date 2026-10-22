/**
 * The deck's two-way mode switch: two small stacked tabs in a well, the chosen
 * one lit. BEAT / MAN (loops) and HOT / MEM (the cue bank) are both this.
 * With exactly two options a click anywhere on it flips to the other one;
 * with more, each tab selects itself.
 */
export function ModeSwitch<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  /** Names the group for assistive tech ("Loop mode", "Cue bank"). */
  label: string
  value: T
  options: { value: T; text: string; title: string }[]
  onChange: (value: T) => void
}) {
  const other = options.length === 2 ? options.find((o) => o.value !== value) : undefined
  return (
    <div
      role="group"
      aria-label={label}
      onClick={other ? () => onChange(other.value) : undefined}
      className={`well ${other ? 'cursor-pointer ' : ''}flex h-10 shrink-0 flex-col justify-center gap-0.5 rounded-[10px] p-0.5`}
    >
      {options.map((o) => (
        <button
          key={o.value}
          onClick={other ? undefined : () => onChange(o.value)}
          title={o.title}
          aria-pressed={value === o.value}
          className={`h-[17px] rounded-[7px] px-[7px] text-[9px] font-bold tracking-[0.08em] transition-colors ${
            value === o.value ? 'btn-primary' : 'text-faint hover:text-text'
          }`}
        >
          {o.text}
        </button>
      ))}
    </div>
  )
}
