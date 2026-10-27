//! stateid-vgpu: GPU for stateid's freezable sessions. Programs render on the real GPU through virgl,
//! while the GPU state lives outside the session, so the session can still be frozen.
//!
//!     program --vtest--> proxy (in the session) ==link==> keeper (outside) --vtest--> virgl_test_server
//!
//! CRIU cannot freeze a socket whose other end is outside the session, so before a freeze the proxy
//! drains and closes the link (`detach`), and opens it again afterwards (`attach`); meanwhile the keeper
//! holds the server connections, so the GPU state survives, and buffers what the server sends. Bytes and
//! SCM_RIGHTS fds are relayed unchanged; the keeper also follows what each connection has set up on the
//! server (state.rs), so that it can be rebuilt.
//!
//! usage:
//!     stateid-vgpu keeper KEEPER_SOCK SERVER_SOCK [-- SERVER_COMMAND...]
//!     stateid-vgpu proxy KEEPER_SOCK -- PROGRAM [ARGS...]     (in the session; serves $XDG_RUNTIME_DIR/gpu.*)
//!     stateid-vgpu check-log LOG     (development: follow a recorded session, check resource IDs)

mod keeper;
mod proxy;
mod state;
mod wire;

use std::ffi::CString;
use std::os::raw::{c_char, c_int};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicI32, AtomicPtr, Ordering};
use std::time::Duration;

extern "C" {
    fn signal(sig: c_int, handler: usize) -> usize;
    fn kill(pid: c_int, sig: c_int) -> c_int;
    fn unlink(path: *const c_char) -> c_int;
    fn umask(mask: u32) -> u32;
    fn _exit(code: c_int) -> !;
}
const SIGTERM: c_int = 15;

// what the keeper cleans up when it ends, also from a signal handler
static SERVER_PID: AtomicI32 = AtomicI32::new(0);
static SOCKETS: [AtomicPtr<c_char>; 2] = [AtomicPtr::new(std::ptr::null_mut()), AtomicPtr::new(std::ptr::null_mut())];

extern "C" fn end_keeper(_: c_int) {
    unsafe {
        let pid = SERVER_PID.load(Ordering::SeqCst);
        if pid > 0 {
            kill(pid, SIGTERM);
        }
        for s in &SOCKETS {
            let p = s.load(Ordering::SeqCst);
            if !p.is_null() {
                unlink(p);
            }
        }
        _exit(0);
    }
}

fn keeper_done() -> ! {
    end_keeper(0);
    unreachable!()
}

fn remember_socket(i: usize, path: &Path) {
    let c = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
    SOCKETS[i].store(c.into_raw(), Ordering::SeqCst);
}

fn keeper(args: &[String]) -> i32 {
    if args.len() < 2 {
        return usage();
    }
    let (keeper_sock, server_sock) = (PathBuf::from(&args[0]), PathBuf::from(&args[1]));
    unsafe {
        umask(0o077);  // the server socket gives full access to the GPU state: this user only
        signal(SIGTERM, end_keeper as extern "C" fn(c_int) as usize);  // so the server is stopped too
    }
    remember_socket(0, &keeper_sock);
    if let Some(i) = args.iter().position(|a| a == "--") {
        let cmd = &args[i + 1..];
        if cmd.is_empty() {
            return usage();
        }
        let _ = std::fs::remove_file(&server_sock);
        remember_socket(1, &server_sock);
        match Command::new(&cmd[0]).args(&cmd[1..]).stdin(Stdio::null()).spawn() {
            Ok(child) => SERVER_PID.store(child.id() as i32, Ordering::SeqCst),
            Err(e) => {
                eprintln!("stateid-vgpu: cannot start {}: {e}", cmd[0]);
                return 1;
            }
        }
        for _ in 0..100 {
            if server_sock.exists() {
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    if let Err(e) = keeper::Keeper::new(server_sock).run(keeper_sock, keeper_done) {
        eprintln!("stateid-vgpu: keeper: {e}");
    }
    keeper_done()
}

fn proxy(args: &[String]) -> i32 {
    let Some(i) = args.iter().position(|a| a == "--") else { return usage() };
    let program = &args[i + 1..];
    if i != 1 || program.is_empty() {
        return usage();
    }
    let run_dir = PathBuf::from(std::env::var_os("XDG_RUNTIME_DIR").unwrap_or_else(|| "/tmp".into()));
    let listen = run_dir.join("gpu.vtest");
    let p = proxy::Proxy::new(PathBuf::from(&args[0]));
    if let Err(e) = p.start(&listen, &run_dir.join("gpu.ctl")) {
        eprintln!("stateid-vgpu: {e}");
        return 1;
    }
    let status = Command::new(&program[0])
        .args(&program[1..])
        .env("VTEST_SOCKET_NAME", &listen)
        .env("LIBGL_ALWAYS_SOFTWARE", "1")
        .env("GALLIUM_DRIVER", "virpipe")
        .status();
    match status {
        Ok(s) => s.code().unwrap_or_else(|| 128 + s.signal().unwrap_or(0)),
        Err(e) => {
            eprintln!("stateid-vgpu: cannot start {}: {e}", program[0]);
            127
        }
    }
}

/// Follow a recorded vtest session ([conn u32][len u32][nfds u8] bytes per chunk; the top bit of conn
/// marks what the server sent) and check the resource IDs we expect against the ones the server gave.
fn check_log(args: &[String]) -> i32 {
    let Some(data) = args.first().and_then(|p| std::fs::read(p).ok()) else { return usage() };
    let mut conns: std::collections::BTreeMap<u32, state::Connection> = Default::default();
    let (mut pos, mut checked, mut wrong) = (0, 0, 0);
    let dw = |b: &[u8], i: usize| u32::from_le_bytes(b[4 * i..4 * i + 4].try_into().unwrap());
    while pos + 9 <= data.len() {
        let (conn, n, nfds) = (dw(&data[pos..], 0), dw(&data[pos + 4..], 0) as usize, data[pos + 8] as usize);
        let chunk = &data[pos + 9..(pos + 9 + n).min(data.len())];
        pos += 9 + n;
        let c = conns.entry(conn & 0x7fff_ffff).or_default();
        if conn >> 31 == 0 {
            c.from_client(chunk);
            continue;
        }
        if nfds > 0 && chunk.len() >= 13 {
            let reply = &chunk[chunk.len() - 13..];
            if dw(reply, 0) == 1 && dw(reply, 1) == state::RES_CREATE2 {
                checked += 1;
                if Some(dw(reply, 2)) != c.next_fd_for() {
                    wrong += 1;
                    println!("ID MISMATCH: server gave {}, expected {:?}", dw(reply, 2), c.next_fd_for());
                }
            }
        }
        c.from_server_fds(nfds);
    }
    for (id, c) in conns.iter_mut() {
        println!("connection {id}: kept {} bytes, {} resources, rebuild {} bytes, could rebuild: {}", c.size(),
                 c.resources.len(), c.rebuild_commands().len(), c.problem.as_deref().unwrap_or("yes"));
    }
    println!("resource IDs checked against the server: {checked}, mismatches {wrong}");
    (wrong > 0) as i32
}

fn usage() -> i32 {
    eprintln!("usage: stateid-vgpu keeper KEEPER_SOCK SERVER_SOCK [-- SERVER_COMMAND...]\n       \
               stateid-vgpu proxy KEEPER_SOCK -- PROGRAM [ARGS...]");
    2
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match args.first().map(String::as_str) {
        Some("keeper") => keeper(&args[1..]),
        Some("proxy") => proxy(&args[1..]),
        Some("check-log") => check_log(&args[1..]),
        _ => usage(),
    };
    std::process::exit(code);
}
