import { MainWaveform } from '../MainWaveform'
import { OverviewWaveform } from '../OverviewWaveform'
import type { Deck } from './useDeck'

/** The main view's zoom and raw/bars toggle — shared by every deck's lane
 *  (so beats line up between them), owned by the deck area. */
export interface DeckView {
  secPerView: number
  setSecPerView: (sec: number) => void
  rawWave: boolean
  setRawWave: (raw: boolean) => void
}

/** The scrolling main waveform, in its well — or what stands in for it: no
 *  track, a load error, the analysis running. */
export function DeckMainWave({ deck, view, empty = 'Load a track to prep it' }: { deck: Deck; view: DeckView; empty?: string }) {
  const {
    track,
    waveStatus,
    showWaves,
    cols,
    raw,
    current,
    duration,
    cueData,
    cuePoint,
    grid,
    activeMarkerIndex,
    gridMode,
    activeLoop,
    loopActive,
    loopRegion,
    loopInPoint,
    seekManual,
    onScratchStart,
    onScratchMove,
    onScratchEnd,
    lanes,
    toggleStem,
  } = deck
  if (!track) {
    return (
      <div className="well flex min-h-0 flex-1 items-center justify-center rounded-2xl text-xs text-faint">
        {empty}
      </div>
    )
  }
  if (waveStatus === 'error') {
    return (
      <div className="well flex min-h-0 flex-1 items-center justify-center rounded-2xl text-xs text-pink">
        Could not load audio — file may be missing or an unsupported format.
      </div>
    )
  }
  return (
    <div className="well relative min-h-0 flex-1 overflow-hidden rounded-2xl">
      {showWaves && cols ? (
        <MainWaveform
          cols={cols}
          raw={raw}
          rawMode={view.rawWave}
          onRawModeChange={view.setRawWave}
          currentTime={current}
          duration={duration}
          cues={cueData?.cues ?? []}
          cuePoint={cuePoint}
          grid={grid}
          activeMarker={activeMarkerIndex}
          emphasizeGrid={gridMode}
          loop={activeLoop}
          idleLoop={!loopActive ? loopRegion : null}
          loopIn={loopInPoint}
          secPerView={view.secPerView}
          onZoomChange={view.setSecPerView}
          onSeek={seekManual}
          onScratchStart={onScratchStart}
          onScratchMove={onScratchMove}
          onScratchEnd={onScratchEnd}
          lanes={lanes}
          onStemToggle={toggleStem}
        />
      ) : (
        <div className="flex h-full items-center justify-center text-xs text-faint">
          Analysing waveform…
        </div>
      )}
    </div>
  )
}

/** The whole-track overview, framing the main view's range. Nothing without a
 *  playable track (the main well says why). */
export function DeckOverview({ deck, view }: { deck: Deck; view: DeckView }) {
  const { track, waveStatus, showWaves, cols, current, duration, cueData, cuePoint, activeLoop, seekManual } = deck
  if (!track || waveStatus === 'error') return null
  return (
    <div className="well h-7 shrink-0 overflow-hidden rounded-[9px]">
      {showWaves && cols && (
        <OverviewWaveform
          cols={cols}
          currentTime={current}
          duration={duration}
          cues={cueData?.cues ?? []}
          cuePoint={cuePoint}
          loop={activeLoop}
          secPerView={view.secPerView}
          onSeek={seekManual}
        />
      )}
    </div>
  )
}
