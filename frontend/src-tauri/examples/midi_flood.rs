// MIDI transport spike (macOS): a virtual CoreMIDI source named
// "Konduktor Flood" that sends a jog-like CC stream at a fixed rate, so the
// shell's MIDI path can be measured without hardware.
//
//   cargo run --example midi_flood -- <rate Hz> <count> <start delay s>
//
// Each message is CC 33 on channel 1 with a 7-bit counter as its value, so
// the receiver can check for drops and reordering.

#[cfg(target_os = "macos")]
fn main() {
    use midir::os::unix::VirtualOutput;
    use midir::MidiOutput;
    use std::time::{Duration, Instant};

    let args: Vec<String> = std::env::args().collect();
    let rate: f64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(1000.0);
    let count: u32 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(5000);
    let delay: f64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(3.0);

    let out = MidiOutput::new("Konduktor Flood").expect("MIDI client");
    let mut conn = out.create_virtual("Konduktor Flood").expect("virtual source");
    std::thread::sleep(Duration::from_secs_f64(delay));

    let start = Instant::now();
    let step = Duration::from_secs_f64(1.0 / rate);
    for i in 0..count {
        let due = start + step * i;
        while Instant::now() < due {
            let left = due - Instant::now();
            if left > Duration::from_micros(300) {
                std::thread::sleep(left - Duration::from_micros(200));
            }
        }
        conn.send(&[0xB0, 33, (i % 128) as u8]).expect("send");
    }
    eprintln!("sent {count} at {rate} Hz in {:.3} s", start.elapsed().as_secs_f64());
    std::thread::sleep(Duration::from_secs(1));
}

#[cfg(not(target_os = "macos"))]
fn main() {
    eprintln!("midi_flood needs CoreMIDI virtual ports (macOS).");
}
