import { api } from '../../api'
import { CapabilitiesContext } from '../../lib/capabilities'
import { AutoCueDialog } from '../AutoCueDialog'
import { ContextMenu } from '../ContextMenu'
import { TrackInfoDialog } from '../TrackInfoDialog'
import type { Deck } from './useDeck'

/** The deck's menus and dialogs (all portalled, so where this renders does not
 *  matter). Under the deck's OWN capabilities: the track-info dialog reads
 *  them from context. */
export function DeckOverlays({ deck }: { deck: Deck }) {
  const {
    caps,
    track,
    origin,
    onError,
    onEditTags,
    memoryMenu,
    setMemoryMenu,
    memoryMenuItems,
    padMenu,
    setPadMenu,
    padMenuItems,
    infoOpen,
    setInfoOpen,
    autoOpen,
    setAutoOpen,
    slotCount,
    slotLabel,
    existingHotcues,
    autoHotcuesDone,
  } = deck
  return (
    <CapabilitiesContext.Provider value={caps}>
      {memoryMenu && (
        <ContextMenu
          key={`m:${memoryMenu.cue.id}:${memoryMenu.x}:${memoryMenu.y}`}
          x={memoryMenu.x}
          y={memoryMenu.y}
          items={memoryMenuItems(memoryMenu.cue)}
          onClose={() => setMemoryMenu(null)}
        />
      )}
      {padMenu && (
        <ContextMenu
          key={`${padMenu.slot}:${padMenu.x}:${padMenu.y}`}
          x={padMenu.x}
          y={padMenu.y}
          items={padMenuItems(padMenu.slot)}
          onClose={() => setPadMenu(null)}
        />
      )}
      {infoOpen && track && (
        <TrackInfoDialog
          track={track}
          artUrl={origin === 'collection' ? api.artUrl(track.id) : null}
          onClose={() => setInfoOpen(false)}
          onEditTags={
            onEditTags
              ? () => {
                  setInfoOpen(false)
                  onEditTags(track)
                }
              : undefined
          }
        />
      )}
      {autoOpen && track && (
        <AutoCueDialog
          track={track}
          slotCount={slotCount}
          slotLabel={slotLabel}
          existing={existingHotcues}
          onClose={() => setAutoOpen(false)}
          onDone={autoHotcuesDone}
          onError={(msg) => onError?.(msg)}
        />
      )}
    </CapabilitiesContext.Provider>
  )
}
