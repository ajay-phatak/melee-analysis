//! nojohns-savestate — Training Mode savestate exporter.
//!
//! Turns the moments the engine already located into Training Mode - Community
//! Edition savestates (`.gci` files in a Dolphin memory-card folder), so a
//! missed edgeguard can be *retried* instead of only re-watched.
//!
//! All of the hard work — building the Melee savestate, encoding it into the
//! GCI/memory-card layers, and replaying the opponent's inputs — is done by
//! AlexanderHarrison's `tm_replay`. This binary only does what its stock CLI
//! can't: pick the human port, and process a whole batch of moments in one go
//! without letting one bad frame take down the rest.
//!
//! The contract mirrors the Python engine's: arg-driven, stateless, and one
//! JSON event per line on stdout under `--ndjson`.
//!
//!     nojohns-savestate --jobs <jobs.json> [--ndjson]

use serde::Deserialize;
use serde_json::json;
use std::io::Write;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use tm_replay::{
    construct_tm_replay_from_replay_buffer, construct_tm_replay_from_slp, dolphin_gci_filename,
    read_replay_buffer, replay_flags, valid_filename_char, CpuRecordingMode, HmnRecordingMode,
    HumanPort, RecordingTime, ReplayCreationError,
};

const USAGE: &str = "\
Usage: nojohns-savestate --jobs <JOBS_JSON> [--ndjson]

Options:
  -j, --jobs <JOBS_JSON>  Path to the job spec (see below)
      --ndjson            Emit one JSON event per line on stdout
  -h, --help              Print help

Job spec:
  {
    \"out_dir\": \"<Dolphin memory card folder>\",
    \"jobs\": [
      { \"id\": \"m1\", \"slp\": \"<path.slp>\", \"human\": \"low\"|\"high\",
        \"start_frame\": 4144, \"num_frames\": 600, \"name\": \"Sheik-Falco EG g2\",
        \"swap_sheik_zelda\": false }
    ]
  }

start_frame is an index into the replay's frame array (Melee frame + 123).
";

#[derive(Deserialize)]
struct Spec {
    out_dir: String,
    #[serde(default)]
    jobs: Vec<Job>,
}

#[derive(Deserialize)]
struct Job {
    /// Opaque to us — echoed back so the app can match results to moments.
    id: String,
    slp: String,
    /// "low" | "high": which of the two used ports the player is on.
    human: String,
    start_frame: usize,
    num_frames: usize,
    /// Display name inside Training Mode. Sanitized here, not by the caller.
    name: String,
    #[serde(default)]
    swap_sheik_zelda: bool,
}

/// (stable snake_case code, human-readable message).
type JobError = (&'static str, String);

fn main() {
    if let Err(e) = run() {
        eprint!("{}\n\n{}", e, USAGE);
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    let mut jobs_path: Option<String> = None;
    let mut ndjson = false;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "-j" | "--jobs" => {
                jobs_path = Some(
                    args.get(i + 1)
                        .cloned()
                        .ok_or_else(|| format!("Error: '{}' requires an argument", args[i]))?,
                );
                i += 2;
            }
            "--ndjson" => {
                ndjson = true;
                i += 1;
            }
            "-h" | "--help" => {
                print!("{}", USAGE);
                return Ok(());
            }
            other => return Err(format!("Error: unknown argument '{}'", other)),
        }
    }

    let jobs_path = jobs_path.ok_or("Error: '--jobs' argument is required")?;
    let raw = std::fs::read_to_string(&jobs_path)
        .map_err(|e| format!("Error: could not read job spec '{}': {}", jobs_path, e))?;
    let spec: Spec =
        serde_json::from_str(&raw).map_err(|e| format!("Error: malformed job spec: {}", e))?;

    let out_dir = PathBuf::from(&spec.out_dir);
    std::fs::create_dir_all(&out_dir).map_err(|e| {
        format!(
            "Error: could not create output folder '{}': {}",
            out_dir.display(),
            e
        )
    })?;

    // Upstream indexes frame arrays unguarded in a few places. We bounds-check
    // before calling, but a panic must never take down the rest of the batch —
    // so keep the hook quiet and let catch_unwind report it as a job failure.
    std::panic::set_hook(Box::new(|_| {}));

    // A set's moments come from a handful of replays, so parse each file once.
    // Sorting by path groups them; the app matches results by id, not order.
    let mut order: Vec<usize> = (0..spec.jobs.len()).collect();
    order.sort_by(|&a, &b| {
        spec.jobs[a]
            .slp
            .cmp(&spec.jobs[b].slp)
            .then(spec.jobs[a].start_frame.cmp(&spec.jobs[b].start_frame))
    });

    let mut cached: Option<(String, slp_parser::Game)> = None;
    let mut written = 0usize;
    let mut failed = 0usize;

    for idx in order {
        let job = &spec.jobs[idx];

        if cached.as_ref().map(|(p, _)| p != &job.slp).unwrap_or(true) {
            cached = match slp_parser::read_game(Path::new(&job.slp)) {
                Ok(game) => Some((job.slp.clone(), game)),
                Err(e) => {
                    failed += 1;
                    emit_error(ndjson, &job.id, "read_failed", &format!("{:?}", e));
                    continue;
                }
            };
        }
        let game = &cached.as_ref().unwrap().1;

        match export(game, &out_dir, job) {
            Ok(path) => {
                written += 1;
                emit(
                    ndjson,
                    json!({
                        "event": "result",
                        "id": job.id,
                        "ok": true,
                        "file": path.to_string_lossy(),
                    }),
                    &format!("Savestate '{}' created", path.display()),
                );
            }
            Err((code, msg)) => {
                failed += 1;
                emit_error(ndjson, &job.id, code, &msg);
            }
        }
    }

    emit(
        ndjson,
        json!({ "event": "result", "written": written, "failed": failed }),
        &format!("{} savestate(s) written, {} skipped", written, failed),
    );
    Ok(())
}

fn export(game: &slp_parser::Game, out_dir: &Path, job: &Job) -> Result<PathBuf, JobError> {
    let human = match job.human.as_str() {
        "low" => HumanPort::HumanLowPort,
        "high" => HumanPort::HumanHighPort,
        other => {
            return Err((
                "bad_human_port",
                format!("expected \"low\" or \"high\", got {:?}", other),
            ))
        }
    };

    let (lo, hi) = game.info.low_high_ports().ok_or((
        "not_1v1",
        "replay is not a two-player game".to_string(),
    ))?;
    let frame_count = port_len(game, lo).min(port_len(game, hi));
    if job.start_frame >= frame_count {
        return Err((
            "out_of_bounds",
            format!(
                "frame {} is past the end of the replay ({} frames)",
                job.start_frame, frame_count
            ),
        ));
    }

    let name = sanitize_name(&job.name);
    let flags = if job.swap_sheik_zelda {
        replay_flags::SWAP_SHEIK_ZELDA
    } else {
        0
    };

    let gci = catch_unwind(AssertUnwindSafe(|| {
        let gci =
            construct_tm_replay_from_slp(game, human, job.start_frame, job.num_frames, &name, flags)?;
        Ok(set_practice_menu(gci, game, &name))
    }))
    .map_err(|_| {
        (
            "panicked",
            "savestate builder panicked on this frame".to_string(),
        )
    })?
    .map_err(describe)?
    .ok_or((
        "menu_patch_failed",
        "could not set Training Mode's menu defaults".to_string(),
    ))?;

    let path = unique_gci_path(out_dir, game.info.start_time.fields());
    std::fs::write(&path, &gci)
        .map_err(|e| ("write_failed", format!("could not write '{}': {}", path.display(), e)))?;
    Ok(path)
}

fn port_len(game: &slp_parser::Game, port: usize) -> usize {
    game.frames[port].as_ref().map(|f| f.len()).unwrap_or(0)
}

/// Make the export a *practice* state rather than a *watch* state.
///
/// `construct_tm_replay_from_slp` hardcodes `hmn_mode: Playback`, so Training
/// Mode replays your own inputs back at you and the controller does nothing —
/// right for reviewing, useless for retrying. There's no parameter for it, so
/// decode the replay buffer, flip the two mode bytes, and re-encode.
///
/// Layout (see tm_replay's Readme): the buffer header holds the menu-settings
/// offset as a big-endian u32 at [64..68], and the settings are six bytes:
/// hmn_mode, hmn_slot, cpu_mode, cpu_slot, loop_inputs, auto_restore.
fn set_practice_menu(mut gci: Vec<u8>, game: &slp_parser::Game, name: &str) -> Option<Vec<u8>> {
    let mut buf = read_replay_buffer(&mut gci)?;
    let offset = u32::from_be_bytes(buf.get(64..68)?.try_into().ok()?) as usize;
    if buf.len() < offset + 6 {
        return None;
    }
    buf[offset] = HmnRecordingMode::Off as u8; // you drive your own character
    buf[offset + 2] = CpuRecordingMode::Playback as u8; // they replay what they really did
    buf[offset + 5] = 1; // auto_restore: snap back to the situation for the next rep

    let t = game.info.start_time.fields();
    let mut filename = [0u8; 31];
    filename[..name.len()].copy_from_slice(name.as_bytes());
    construct_tm_replay_from_replay_buffer(
        RecordingTime {
            year: t.year,
            month: t.month,
            day: t.day,
            hour: t.hour,
            minute: t.minute,
            second: t.second,
        },
        &filename,
        &buf,
    )
    .ok()
}

/// Stable codes for every upstream failure, so the app can explain a skip
/// instead of just counting it. No wildcard arm on purpose: a new upstream
/// variant should break the build rather than silently become "unknown".
fn describe(e: ReplayCreationError) -> JobError {
    match e {
        ReplayCreationError::NotTwoPlayerGame => {
            ("not_1v1", "savestates only work for 1v1 replays".into())
        }
        ReplayCreationError::RecordingOutOfBounds => {
            ("out_of_bounds", "frame range is outside the replay".into())
        }
        ReplayCreationError::DurationTooLong => (
            "too_long",
            "clip exceeds Training Mode's 3600-frame maximum".into(),
        ),
        ReplayCreationError::FilenameTooLong => {
            ("name_too_long", "recording name is over 31 characters".into())
        }
        ReplayCreationError::FilenameNotASCII => (
            "name_not_ascii",
            "recording name has unsupported characters".into(),
        ),
        ReplayCreationError::SpecialActionState => (
            "special_action_state",
            "a character is in a move Training Mode can't restore".into(),
        ),
        ReplayCreationError::NoGoodExportFrame => (
            "no_good_frame",
            "no restorable game state near this frame".into(),
        ),
        ReplayCreationError::ZeldaOnCpu => (
            "zelda_on_cpu",
            "Zelda as the CPU is unsupported by Training Mode".into(),
        ),
        ReplayCreationError::OutdatedReplay => (
            "outdated_replay",
            format!(
                "replay predates slp {}.{} — too old to export",
                tm_replay::MIN_VERSION_MAJOR,
                tm_replay::MIN_VERSION_MINOR
            ),
        ),
    }
}

/// Training Mode names are 31 bytes of a restricted ASCII subset, so anything
/// outside it becomes a space and runs of spaces collapse.
fn sanitize_name(raw: &str) -> String {
    let mut out = String::new();
    let mut pending_space = false;
    for c in raw.chars() {
        let c = if valid_filename_char(c) { c } else { ' ' };
        if c == ' ' {
            pending_space = !out.is_empty();
            continue;
        }
        if pending_space {
            if out.len() + 2 > 31 {
                break;
            }
            out.push(' ');
            pending_space = false;
        }
        if out.len() + 1 > 31 {
            break;
        }
        out.push(c);
    }
    if out.is_empty() {
        out.push_str("nojohns");
    }
    out
}

/// Dolphin's own GCI naming convention, keyed off the match's start time. A set
/// contributes several moments from one replay, so those collide by design —
/// suffix until free. (The identity Dolphin actually reads lives in the file
/// header, which tm_replay already randomizes.)
fn unique_gci_path(dir: &Path, t: slp_parser::TimeFields) -> PathBuf {
    let base = dolphin_gci_filename(RecordingTime {
        year: t.year,
        month: t.month,
        day: t.day,
        hour: t.hour,
        minute: t.minute,
        second: t.second,
    });
    let stem = base.strip_suffix(".gci").unwrap_or(base.as_str()).to_string();
    let mut path = dir.join(&base);
    let mut n = 2;
    while path.exists() {
        path = dir.join(format!("{}-{}.gci", stem, n));
        n += 1;
    }
    path
}

fn emit(ndjson: bool, value: serde_json::Value, human: &str) {
    if ndjson {
        println!("{}", value);
    } else {
        println!("{}", human);
    }
    let _ = std::io::stdout().flush();
}

fn emit_error(ndjson: bool, id: &str, code: &str, msg: &str) {
    emit(
        ndjson,
        json!({ "event": "error", "id": id, "ok": false, "code": code, "msg": msg }),
        &format!("Skipped {}: {} ({})", id, msg, code),
    );
}
