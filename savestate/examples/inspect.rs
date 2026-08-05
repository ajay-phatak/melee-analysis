//! Dev tool: dump a savestate's Training Mode menu settings.
//!
//!     cargo run --example inspect -- <file.gci>
//!
//! Exists because the menu bytes are the one part of an export you can't check
//! by eye — they decide whether loading the state hands you the controller
//! (hmn Off) or replays your own inputs at you (hmn Playback).

use tm_replay::read_replay_buffer;

fn main() {
    let path = match std::env::args().nth(1) {
        Some(p) => p,
        None => {
            eprintln!("Usage: inspect <file.gci>");
            std::process::exit(1);
        }
    };

    let mut gci = std::fs::read(&path).expect("could not read file");
    let buf = read_replay_buffer(&mut gci).expect("not a valid TM replay gci");
    let offset = u32::from_be_bytes(buf[64..68].try_into().unwrap()) as usize;
    let m = &buf[offset..][..6];

    let hmn = ["Off", "Record", "Playback"];
    let cpu = ["Off", "Control", "Record", "Playback"];
    println!("name      : {}", String::from_utf8_lossy(&gci[0x60..0x60 + 31]).trim_end_matches('\0'));
    println!("hmn_mode  : {} ({})", m[0], hmn.get(m[0] as usize).unwrap_or(&"?"));
    println!("hmn_slot  : {}", m[1]);
    println!("cpu_mode  : {} ({})", m[2], cpu.get(m[2] as usize).unwrap_or(&"?"));
    println!("cpu_slot  : {}", m[3]);
    println!("loop      : {}", m[4]);
    println!("auto_rest : {}", m[5]);
}
