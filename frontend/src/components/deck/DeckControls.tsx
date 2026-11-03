import { Icon } from '../../lib/icons'
import { GridEditStrip, TempoControls, TempoFold } from '../GridControls'
import { HotcueBar } from '../HotcueBar'
import { LoopControls } from '../LoopControls'
import { MemoryCueStrip } from '../MemoryCueStrip'
import { ModeSwitch } from '../ModeSwitch'
import type { Deck } from './useDeck'

const DIVIDER = <span aria-hidden className="h-7 w-px shrink-0 bg-line" />

/**
 * The deck's controls row: transport · loop/jump · pads or grid · tempo.
 *
 * A container, so the row gives way in stages as it narrows instead of the
 * pads sliding under the tempo group: the pads drop their names first (each pad
 * queries its own width, see HotcueBar), then below 77rem the tempo group folds
 * into one BPM button. That is the row's measured width just before the pad
 * bank would hit its minimum (with a lockable grid's extra Lock button, and the
 * Hot / Mem toggle's ~3rem with its gap), so move it with the controls.
 *
 * `stacked` (beside the mixer, in performance mode) puts the pads — or the grid
 * or memory strip — on a second row with the two mode switches, and folds the
 * tempo group below 44rem (measured in the app: transport + loops + the tempo
 * group need ~42rem).
 */
export function DeckControls({ deck, stacked = false }: { deck: Deck; stacked?: boolean }) {
  const {
    caps,
    track,
    toggle,
    ready,
    previewing,
    playing,
    onCuePress,
    onCueRelease,
    cueHeld,
    loopMode,
    setLoopMode,
    grid,
    beatSize,
    stepSize,
    loopActive,
    activeBeats,
    toggleSizeLoop,
    loopRegion,
    loopInPoint,
    loopIn,
    loopOut,
    toggleLoop,
    moveOrJump,
    gridMode,
    setGridMode,
    bankMode,
    setBankMode,
    setPadMenu,
    setMemoryMenu,
    setRenamingSlot,
    setRenamingMemory,
    activeMarkerIndex,
    markerCount,
    activeMarker,
    atMarker,
    beforeFirstMarker,
    originalGridRef,
    prevMarker,
    nextMarker,
    nudgeGrid,
    nudgeGridBeats,
    addMarkerHere,
    deleteMarkerHere,
    deleteGrid,
    resetGrid,
    showMemory,
    cueData,
    cuePoint,
    callMemoryCue,
    addMemoryCue,
    memoryAddRefusal,
    renamingMemory,
    renameMemoryCue,
    slotCount,
    slotLabel,
    selectedSlot,
    setSelectedSlot,
    onSlotPress,
    onSlotRelease,
    hotcueAt,
    renamingSlot,
    renameSlot,
    tempoProps,
    channel,
  } = deck
  // On-screen MIDI learning (lib/midi): controls carry their action.
  const midi = `deck.${channel}`
  const transport = (
    <>
      <button
        onClick={toggle}
        data-midi-action={`${midi}.play`}
        disabled={!ready}
        className={
          'flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-all active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 ' +
          (previewing
            ? 'bg-gradient-to-b from-white to-gold text-ink-950 shadow-[0_0_0_4px_rgb(255_200_97/0.22),0_8px_24px_-4px_var(--color-gold)]'
            : playing
              ? 'btn-primary shadow-[0_0_0_4px_color-mix(in_oklab,var(--color-accent)_22%,transparent),0_8px_24px_-4px_var(--color-accent)]'
              : 'btn-glass text-text')
        }
        title={playing ? 'Pause (Space)' : 'Play (Space)'}
      >
        <Icon name={playing ? 'pause' : 'play'} size={16} />
      </button>
      <button
        data-midi-action={`${midi}.cue`}
        onPointerDown={(e) => {
          e.preventDefault()
          e.currentTarget.setPointerCapture(e.pointerId)
          onCuePress()
        }}
        onPointerUp={onCueRelease}
        onPointerCancel={onCueRelease}
        disabled={!ready}
        aria-pressed={cueHeld}
        className={`flex h-10 w-14 shrink-0 items-center justify-center rounded-xl font-mono text-[13px] font-semibold tracking-wider transition-[background-color,box-shadow,transform,color] duration-75 disabled:cursor-not-allowed disabled:opacity-40 ${
          cueHeld
            ? // Held: lit like a pressed controller button — brighter fill,
              // solid gold rim, a glow, and a slight press-in.
              'scale-[0.96] bg-gold/35 text-[#fff1cc] shadow-[inset_0_1px_0_rgb(255_255_255/0.3),inset_0_0_0_1px_rgb(255_200_97/0.9),0_0_20px_-2px_rgb(255_200_97/0.75)]'
            : 'bg-gold/10 text-gold shadow-[inset_0_1px_0_rgb(255_255_255/0.12),inset_0_0_0_1px_rgb(255_200_97/0.4)] hover:bg-gold/15'
        }`}
        title="Cue — set the cue point (paused), hold to preview from it, or jump back to it (playing) (C)"
      >
        CUE
      </button>
    </>
  )
  const loopControls = (
    <LoopControls
      midi={midi}
      mode={loopMode}
      onMode={setLoopMode}
      hasGrid={grid != null}
      ready={ready}
      size={beatSize}
      onSmaller={() => stepSize(-1)}
      onBigger={() => stepSize(1)}
      loopActive={loopActive}
      sizeLit={loopActive && activeBeats === beatSize}
      onToggleSize={toggleSizeLoop}
      canToggle={loopRegion != null}
      inArmed={loopInPoint != null}
      manualSet={loopRegion != null && activeBeats == null}
      onLoopIn={loopIn}
      onLoopOut={loopOut}
      onToggleLoop={toggleLoop}
      onMove={moveOrJump}
    />
  )
  const bankSwitch = caps.cues.memory_cues && !gridMode && (
    <ModeSwitch
      label="Cue bank"
      value={bankMode}
      options={[
        { value: 'hot', text: 'HOT', title: 'Hot cues: the pads' },
        { value: 'memory', text: 'MEM', title: 'Memory cues: call one to make it the CUE point' },
      ]}
      onChange={(m) => {
        setBankMode(m)
        setPadMenu(null)
        setMemoryMenu(null)
        setRenamingSlot(null)
        setRenamingMemory(null)
      }}
    />
  )
  const strip = gridMode ? (
    <GridEditStrip
      markerIndex={activeMarkerIndex}
      markerCount={markerCount}
      markerStart={activeMarker?.start ?? null}
      atMarker={atMarker}
      beforeFirst={beforeFirstMarker}
      canReset={originalGridRef.current != null}
      onPrevMarker={prevMarker}
      onNextMarker={nextMarker}
      onNudgeMarker={nudgeGrid}
      onNudgeMarkerBeats={nudgeGridBeats}
      onAddMarker={addMarkerHere}
      onDeleteMarker={deleteMarkerHere}
      onDeleteGrid={deleteGrid}
      onReset={resetGrid}
    />
  ) : showMemory ? (
    <MemoryCueStrip
      cues={cueData?.cues ?? []}
      cuePoint={cuePoint}
      onCall={callMemoryCue}
      onMenu={(cue, x, y) => setMemoryMenu({ cue, x, y })}
      onAdd={() => void addMemoryCue()}
      addRefusal={memoryAddRefusal}
      renamingId={renamingMemory}
      onRenameCommit={renameMemoryCue}
      onRenameCancel={() => setRenamingMemory(null)}
    />
  ) : (
    <HotcueBar
      midi={midi}
      cues={cueData?.cues ?? []}
      slotCount={slotCount}
      slotLabel={slotLabel}
      selectedSlot={selectedSlot}
      onSlotPress={onSlotPress}
      onSlotRelease={onSlotRelease}
      onSlotMenu={(slot, x, y) => {
        setSelectedSlot(slot)
        if (hotcueAt(slot)) setPadMenu({ slot, x, y })
      }}
      renamingSlot={renamingSlot}
      onRenameCommit={renameSlot}
      onRenameCancel={() => setRenamingSlot(null)}
    />
  )
  const modeSwitch = (
    <ModeSwitch
      label="Deck mode"
      value={gridMode ? 'grid' : 'cues'}
      options={[
        { value: 'cues', text: 'CUES', title: 'Cues: the hotcue pads or memory cues' },
        { value: 'grid', text: 'GRID', title: 'Edit the beatgrid: markers, phase, reset' },
      ]}
      disabled={!track}
      onChange={(m) => {
        setGridMode(m === 'grid')
        setPadMenu(null)
        setRenamingSlot(null)
        setMemoryMenu(null)
        setRenamingMemory(null)
      }}
    />
  )

  if (stacked) {
    return (
      <div className="@container flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex h-11 shrink-0 items-center gap-3">
          {transport}
          {DIVIDER}
          {loopControls}
          <span className="flex-1" />
          <TempoControls {...tempoProps} className="@max-[44rem]:hidden" />
          <TempoFold locked={tempoProps.locked} className="hidden @max-[44rem]:flex">
            <TempoControls {...tempoProps} />
          </TempoFold>
        </div>
        <div className="flex h-11 shrink-0 items-center gap-2">
          {modeSwitch}
          {bankSwitch}
          {strip}
        </div>
      </div>
    )
  }
  return (
    <div className="@container flex h-11 shrink-0 items-center gap-3">
      {transport}
      {DIVIDER}
      {loopControls}
      {DIVIDER}
      {bankSwitch}
      {strip}
      {DIVIDER}
      {modeSwitch}
      <TempoControls {...tempoProps} className="@max-[77rem]:hidden" />
      <TempoFold locked={tempoProps.locked} className="hidden @max-[77rem]:flex">
        <TempoControls {...tempoProps} />
      </TempoFold>
    </div>
  )
}
