// The DJ platforms' marks, for the collection picker and anywhere else a
// library's platform is named. Redrawn as geometry from each platform's app icon
// (not traced), single-colour via `currentColor` so they take the theme's text
// colour; the square app-icon backgrounds are left to whatever tile holds them.
//
// These are the platforms' trademarks, used to say which platform is which.
//
// A lookup of display ASSETS keyed by platform id — not behaviour branching on
// the platform, which components must never do. An unknown id falls back to a
// generic mark, so a new adapter shows up without touching this file.
import { useId } from 'react'
import { Icon } from './icons'

type Mark = (props: { mask: string }) => React.ReactElement

const MARKS: Record<string, Mark> = {
  // An outer ring around a disk cut by an X, with a hole at the centre.
  traktor: ({ mask }) => (
    <>
      <mask id={mask}>
        <rect width="100" height="100" fill="#fff" />
        <path d="M82.2 26.05L14.36 68.44L17.8 73.95L85.64 31.56Z" fill="#000" />
        <path d="M85.64 68.44L17.8 26.05L14.36 31.56L82.2 73.95Z" fill="#000" />
        <path d="M38 50a12 12 0 1 0 24 0a12 12 0 1 0 -24 0Z" fill="#000" />
      </mask>
      <path d="M5 50a45 45 0 1 0 90 0a45 45 0 1 0 -90 0ZM13 50a37 37 0 1 0 74 0a37 37 0 1 0 -74 0Z" fillRule="evenodd" />
      <path d="M19.5 50a30.5 30.5 0 1 0 61 0a30.5 30.5 0 1 0 -61 0Z" mask={`url(#${mask})`} />
    </>
  ),
  // A cube split into three faces by a Y of spokes that meet in a ring.
  rekordbox: ({ mask }) => (
    <>
      <mask id={mask}>
        <rect width="100" height="100" fill="#fff" />
        <path d="M51.88 46.75L10.31 22.75L6.56 29.25L48.12 53.25Z" fill="#000" />
        <path d="M51.88 53.25L93.44 29.25L89.69 22.75L48.12 46.75Z" fill="#000" />
        <path d="M46.25 50L46.25 98L53.75 98L53.75 50Z" fill="#000" />
        <path d="M29 50a21 21 0 1 0 42 0a21 21 0 1 0 -42 0Z" fill="#000" />
        <path d="M38 50a12 12 0 1 0 24 0a12 12 0 1 0 -24 0Z" fill="#fff" />
      </mask>
      <path
        d="M50 9L85.51 29.5L85.51 70.5L50 91L14.49 70.5L14.49 29.5Z"
        stroke="currentColor"
        strokeWidth="5"
        strokeLinejoin="round"
        mask={`url(#${mask})`}
      />
    </>
  ),
  // A "C": a solid arc breaking into blades that thin toward three o'clock.
  // Fewer, wider blades than the original, so they stay distinct at 16 px.
  onelibrary: () => <path d="M44.29 9.4A41 41 0 1 0 58.52 90.1L55.41 75.43A26 26 0 1 1 46.38 24.25ZM61.58 89.33A41 41 0 0 0 66.41 87.57L61.23 75.71A28.05 28.05 0 0 1 57.92 76.91ZM70.13 85.72A41 41 0 0 0 74.45 82.92L67.45 73.5A29.27 29.27 0 0 1 64.37 75.5ZM77.65 80.28A41 41 0 0 0 81.22 76.57L73.09 69.65A30.32 30.32 0 0 1 70.44 72.39ZM83.75 73.28A41 41 0 0 0 86.4 68.87L77.65 64.33A31.14 31.14 0 0 1 75.63 67.68ZM88.12 65.09A41 41 0 0 0 89.71 60.2L80.71 57.89A31.71 31.71 0 0 1 79.48 61.67ZM90.54 56.13A41 41 0 0 0 90.99 51L81.97 50.78A31.98 31.98 0 0 1 81.62 54.78ZM90.88 46.85A41 41 0 0 0 90.16 41.76L81.29 43.58A31.94 31.94 0 0 1 81.85 47.55ZM89.12 37.74A41 41 0 0 0 87.28 32.93L78.73 36.84A31.6 31.6 0 0 1 80.16 40.55ZM85.36 29.25A41 41 0 0 0 82.48 24.98L74.54 31.1A30.97 30.97 0 0 1 76.72 34.33ZM79.79 21.83A41 41 0 0 0 76.02 18.32L69.1 26.75A30.09 30.09 0 0 1 71.86 29.32ZM72.69 15.85A41 41 0 0 0 68.23 13.28L62.89 24.02A29 29 0 0 1 66.05 25.85ZM64.43 11.62A41 41 0 0 0 59.5 10.12L56.43 23A27.75 27.75 0 0 1 59.77 24.02ZM55.42 9.36A41 41 0 0 0 50.29 9L50.18 23.58A26.42 26.42 0 0 1 53.49 23.81Z" />,
}

export function PlatformIcon({
  platform,
  size = 20,
  className,
}: {
  platform: string
  size?: number
  className?: string
}) {
  // Masks are addressed by id, and one page can show the same mark twice.
  const mask = `pm-${useId().replace(/:/g, '')}`
  const Mark = MARKS[platform]
  if (!Mark) return <Icon name="music" size={size} className={className} />
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      fill="currentColor"
      aria-hidden
      className={className}
    >
      <Mark mask={mask} />
    </svg>
  )
}
