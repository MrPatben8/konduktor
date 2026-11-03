// MIDI INPUT for controller mapping (decided 2026-11-03,
// .claude/discussions/discuss-midi-controllers-2026-11-03.md).
//
// WKWebView has no Web MIDI, so the shell reads the ports itself with midir
// (CoreMIDI on macOS, WinMM on Windows) and streams each port's messages to the
// page over a `Channel` — the frontend does all parsing, mapping and learning
// (lib/midi/). Input only: nothing is ever sent to a controller.
//
// Batching without a timer: the driver's callback queues each message on an
// mpsc; one sender thread per port blocks on it, then drains whatever else has
// queued and sends that as ONE batch. Idle, a message goes out the moment it
// arrives; under a jog wheel's flood, messages coalesce instead of queueing up
// one IPC call each. System messages (SysEx, MIDI clock, active sensing) are
// ignored at the driver: a 24-ppqn clock from a mixer would flood the page.
//
// `wall_us` is the wall clock (µs since the Unix epoch) when the driver handed
// the message over, so the page can measure transport latency against its own
// `Date.now()`.

use std::collections::HashMap;
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use midir::{Ignore, MidiInput, MidiInputConnection, MidiInputPort};
use serde::Serialize;
use tauri::ipc::Channel;
use tauri::State;

#[derive(Clone, Serialize)]
pub struct MidiPacket {
    pub wall_us: u64,
    pub bytes: Vec<u8>,
}

/// Open connections by port name. Dropping one closes the port and ends its
/// sender thread (the callback's mpsc sender goes with it).
#[derive(Default)]
pub struct MidiInputs(Mutex<HashMap<String, MidiInputConnection<()>>>);

fn client() -> Result<MidiInput, String> {
    let mut input = MidiInput::new("Konduktor").map_err(|e| e.to_string())?;
    input.ignore(Ignore::All);
    Ok(input)
}

/// Every input port with a unique name: two identical controllers are
/// "X" and "X #2", in the order the OS lists them.
fn named_ports(input: &MidiInput) -> Vec<(String, MidiInputPort)> {
    let mut seen: HashMap<String, usize> = HashMap::new();
    input
        .ports()
        .into_iter()
        .filter_map(|p| {
            let base = input.port_name(&p).ok()?;
            let n = seen.entry(base.clone()).or_insert(0);
            *n += 1;
            let name = if *n == 1 { base } else { format!("{base} #{n}") };
            Some((name, p))
        })
        .collect()
}

fn now_us() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_micros() as u64).unwrap_or(0)
}

#[tauri::command]
pub fn midi_ports() -> Result<Vec<String>, String> {
    Ok(named_ports(&client()?).into_iter().map(|(name, _)| name).collect())
}

/// Open `port` and stream its messages to `on_message`. Opening a port that is
/// already open replaces the old connection (a page reload re-opens).
#[tauri::command]
pub fn midi_open(
    inputs: State<'_, MidiInputs>,
    port: String,
    on_message: Channel<Vec<MidiPacket>>,
) -> Result<(), String> {
    let input = client()?;
    let found = named_ports(&input)
        .into_iter()
        .find(|(name, _)| *name == port)
        .map(|(_, p)| p)
        .ok_or_else(|| format!("MIDI port not found: {port}"))?;

    let (tx, rx) = mpsc::channel::<MidiPacket>();
    std::thread::Builder::new()
        .name(format!("midi:{port}"))
        .spawn(move || {
            while let Ok(first) = rx.recv() {
                let mut batch = vec![first];
                while let Ok(more) = rx.try_recv() {
                    batch.push(more);
                }
                if on_message.send(batch).is_err() {
                    break; // the page went away
                }
            }
        })
        .map_err(|e| e.to_string())?;

    let conn = input
        .connect(
            &found,
            "Konduktor input",
            move |_stamp, bytes, _| {
                let _ = tx.send(MidiPacket { wall_us: now_us(), bytes: bytes.to_vec() });
            },
            (),
        )
        .map_err(|e| e.to_string())?;
    inputs.0.lock().unwrap().insert(port, conn);
    Ok(())
}

#[tauri::command]
pub fn midi_close(inputs: State<'_, MidiInputs>, port: String) {
    if let Some(conn) = inputs.0.lock().unwrap().remove(&port) {
        conn.close();
    }
}

