//! The proxy, inside the session: the program talks to it as if it were virgl_test_server. It keeps one
//! link to the keeper and multiplexes every program connection over it; `detach` drains and closes the
//! link before a freeze, `attach` opens it again.
//!
//! So that a frozen session can be restored later, the proxy keeps a copy of each shared-memory fd the
//! server sent (until the keeper says it is freed), and on detach the keeper's saved state of every
//! connection. A keeper that does not know these connections (a new one, after a restore) gets both
//! back on attach and rebuilds them.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use crate::wire::{self, Frame, LinkReader, LinkWriter};

pub struct Proxy {
    keeper: PathBuf,
    clients: Mutex<HashMap<u32, Arc<UnixStream>>>,
    next_conn: AtomicU32,
    link: Mutex<Option<Arc<LinkWriter>>>,
    attached: Mutex<bool>,
    attached_cv: Condvar,
    detached: Mutex<bool>,
    detached_cv: Condvar,
    moved: Mutex<bool>,
    moved_cv: Condvar,
    kept: Mutex<HashMap<u32, (u32, HashMap<u32, OwnedFd>)>>,  // connection -> (fds so far, index -> fd)
    saved: Mutex<HashMap<u32, Vec<u8>>>,                      // connection -> its state, from the last detach
    state: Mutex<()>,  // held while detaching/attaching
    gate: Mutex<()>,   // held while a frame goes out; DETACH takes it last
}

fn set(flag: &Mutex<bool>, cv: &Condvar, value: bool) {
    *flag.lock().unwrap() = value;
    cv.notify_all();
}

impl Proxy {
    pub fn new(keeper: PathBuf) -> Arc<Proxy> {
        Arc::new(Proxy {
            keeper,
            clients: Mutex::new(HashMap::new()),
            next_conn: AtomicU32::new(1),
            link: Mutex::new(None),
            attached: Mutex::new(false),
            attached_cv: Condvar::new(),
            detached: Mutex::new(false),
            detached_cv: Condvar::new(),
            moved: Mutex::new(false),
            moved_cv: Condvar::new(),
            kept: Mutex::new(HashMap::new()),
            saved: Mutex::new(HashMap::new()),
            state: Mutex::new(()),
            gate: Mutex::new(()),
        })
    }

    fn connect(self: &Arc<Self>) -> Result<(), String> {
        let s = UnixStream::connect(&self.keeper).map_err(|e| format!("cannot reach the GPU keeper: {e}"))?;
        let (mut reader, writer) = wire::link(s).map_err(|e| e.to_string())?;
        writer.send(wire::ATTACH, 0, &[], &[]).map_err(|e| e.to_string())?;
        let held = loop {
            match reader.recv() {
                None => return Err("the GPU keeper closed the link".into()),
                Some(f) if f.kind == wire::ATTACHED => {
                    break (0..f.data.len() / 4).map(|i| u32::from_le_bytes(f.data[4 * i..4 * i + 4].try_into().unwrap()))
                        .collect::<std::collections::HashSet<u32>>();
                }
                Some(f) if f.kind == wire::REFUSED => {
                    return Err("the GPU keeper refused this session (its GPU state belongs to another copy)".into())
                }
                Some(f) => self.deliver(f),
            }
        };
        // a keeper that does not hold our connections (a restored session) rebuilds them; before any
        // request of the program, which waits until we are attached
        let mut restoring = 0;
        for (conn, saved) in std::mem::take(&mut *self.saved.lock().unwrap()) {
            if held.contains(&conn) || !self.clients.lock().unwrap().contains_key(&conn) {
                continue;
            }
            let kept = self.kept.lock().unwrap();
            if let Some((_, fds)) = kept.get(&conn) {
                let fds: Vec<(&u32, &OwnedFd)> = fds.iter().collect();
                for part in fds.chunks(wire::MAX_FDS) {
                    let index: Vec<u8> = part.iter().flat_map(|(i, _)| i.to_le_bytes()).collect();
                    let copies: Vec<OwnedFd> = part.iter().filter_map(|(_, f)| f.try_clone().ok()).collect();
                    if copies.len() == part.len() {
                        writer.send(wire::FDS, conn, &index, &copies).map_err(|e| e.to_string())?;
                    }
                }
            }
            drop(kept);
            writer.send(wire::RESTORE, conn, &saved, &[]).map_err(|e| e.to_string())?;
            restoring += 1;
        }
        while restoring > 0 {
            match reader.recv() {
                None => return Err("the GPU keeper closed the link".into()),
                Some(f) if f.kind == wire::RESTORED => restoring -= 1,
                Some(f) => self.deliver(f),
            }
        }
        *self.link.lock().unwrap() = Some(writer);
        let me = self.clone();
        std::thread::spawn(move || me.read_link(reader));
        set(&self.attached, &self.attached_cv, true);
        Ok(())
    }

    fn deliver(&self, f: Frame) {
        let client = self.clients.lock().unwrap().get(&f.conn).cloned();
        let Some(c) = client else { return };  // its fds are closed when f is dropped
        match f.kind {
            wire::DATA => {
                if !f.fds.is_empty() {
                    let mut kept = self.kept.lock().unwrap();
                    let (next, fds) = kept.entry(f.conn).or_default();
                    for fd in &f.fds {
                        if let Ok(copy) = fd.try_clone() {
                            fds.insert(*next, copy);
                        }
                        *next += 1;
                    }
                }
                let _ = wire::relay(c.as_raw_fd(), &f.data, &f.fds);
            }
            wire::FORGET if f.data.len() == 4 => {
                if let Some((_, fds)) = self.kept.lock().unwrap().get_mut(&f.conn) {
                    fds.remove(&u32::from_le_bytes(f.data[..4].try_into().unwrap()));
                }
            }
            wire::STATE => {
                self.saved.lock().unwrap().insert(f.conn, f.data);
            }
            wire::CLOSE => {
                self.forget(f.conn);
                let _ = c.shutdown(std::net::Shutdown::Write);
            }
            _ => {}
        }
    }

    fn forget(&self, conn: u32) {
        self.kept.lock().unwrap().remove(&conn);
        self.saved.lock().unwrap().remove(&conn);
    }

    fn read_link(&self, mut reader: LinkReader) {
        while let Some(f) = reader.recv() {
            if f.kind == wire::DETACH_ACK {
                drop(reader);  // nothing of the link may stay open once detached
                set(&self.detached, &self.detached_cv, true);
                return;
            }
            if f.kind == wire::MOVED {
                set(&self.moved, &self.moved_cv, true);
                continue;
            }
            self.deliver(f);
        }
    }

    fn send(&self, kind: u8, conn: u32, data: &[u8], fds: &[OwnedFd]) {
        loop {
            {
                let mut a = self.attached.lock().unwrap();
                while !*a {
                    a = self.attached_cv.wait(a).unwrap();
                }
            }
            let _gate = self.gate.lock().unwrap();
            if *self.attached.lock().unwrap() {
                if let Some(link) = self.link.lock().unwrap().clone() {
                    let _ = link.send(kind, conn, data, fds);
                }
                return;
            }
        }
    }

    fn client(&self, conn: u32, sock: Arc<UnixStream>) {
        self.send(wire::OPEN, conn, &[], &[]);
        let mut buf = vec![0u8; wire::CHUNK];
        loop {
            let mut fds = Vec::new();
            let n = wire::recv(sock.as_raw_fd(), &mut buf, &mut fds).unwrap_or(0);
            if n == 0 && fds.is_empty() {
                self.send(wire::CLOSE, conn, &[], &[]);
                self.clients.lock().unwrap().remove(&conn);
                self.forget(conn);
                return;
            }
            self.send(wire::DATA, conn, &buf[..n], &fds);
        }
    }

    fn detach(&self) -> Result<(), String> {
        let _state = self.state.lock().unwrap();
        if !*self.attached.lock().unwrap() {
            return Ok(());
        }
        set(&self.attached, &self.attached_cv, false);
        *self.detached.lock().unwrap() = false;
        {
            let _gate = self.gate.lock().unwrap();  // waits for a frame being sent; later ones wait for attach
            if let Some(link) = self.link.lock().unwrap().clone() {
                link.send(wire::DETACH, 0, &[], &[]).map_err(|e| e.to_string())?;
            }
        }
        let done = self.detached.lock().unwrap();
        let (done, _) = self.detached_cv.wait_timeout_while(done, Duration::from_secs(10), |d| !*d).unwrap();
        if !*done {
            return Err("the GPU keeper did not answer".into());
        }
        if let Some(link) = self.link.lock().unwrap().take() {
            link.shutdown();
        }
        Ok(())
    }

    /// Have the keeper rebuild every connection on a new server connection; the program's requests
    /// wait meanwhile.
    fn move_connections(&self) -> Result<(), String> {
        let _state = self.state.lock().unwrap();
        if !*self.attached.lock().unwrap() {
            return Err("not attached".into());
        }
        *self.moved.lock().unwrap() = false;
        let _gate = self.gate.lock().unwrap();
        if let Some(link) = self.link.lock().unwrap().clone() {
            link.send(wire::MOVE, 0, &[], &[]).map_err(|e| e.to_string())?;
        }
        let done = self.moved.lock().unwrap();
        let (done, _) = self.moved_cv.wait_timeout_while(done, Duration::from_secs(60), |d| !*d).unwrap();
        if !*done {
            return Err("the GPU keeper did not finish moving".into());
        }
        Ok(())
    }

    fn attach(self: &Arc<Self>) -> Result<(), String> {
        let _state = self.state.lock().unwrap();
        if !*self.attached.lock().unwrap() {
            self.connect()?;
        }
        Ok(())
    }

    /// `detach` / `attach`, one per connection, answered with `ok` or an error line.
    fn serve_control(self: Arc<Self>, listener: UnixListener) {
        for c in listener.incoming() {
            let Ok(mut c) = c else { continue };
            let mut buf = [0u8; 64];
            let n = c.read(&mut buf).unwrap_or(0);
            let result = match String::from_utf8_lossy(&buf[..n]).trim() {
                "detach" => self.detach(),
                "attach" => self.attach(),
                "move" => self.move_connections(),
                other => Err(format!("unknown command {other:?}")),
            };
            let _ = c.write_all(match result {
                Ok(()) => "ok\n".to_string(),
                Err(e) => format!("error: {e}\n"),
            }.as_bytes());
        }
    }

    /// Attach to the keeper and serve the program's connections and the control socket.
    pub fn start(self: &Arc<Self>, listen: &Path, control: &Path) -> Result<(), String> {
        self.connect()?;
        let bind = |p: &Path| -> std::io::Result<UnixListener> {
            let _ = std::fs::remove_file(p);
            let l = UnixListener::bind(p)?;
            std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o600))?;
            Ok(l)
        };
        let programs = bind(listen).map_err(|e| e.to_string())?;
        let controls = bind(control).map_err(|e| e.to_string())?;
        let me = self.clone();
        std::thread::spawn(move || me.serve_control(controls));
        let me = self.clone();
        std::thread::spawn(move || {
            for c in programs.incoming().flatten() {
                let conn = me.next_conn.fetch_add(1, Ordering::SeqCst);
                let c = Arc::new(c);
                me.clients.lock().unwrap().insert(conn, c.clone());
                let me = me.clone();
                std::thread::spawn(move || me.client(conn, c));
            }
        });
        Ok(())
    }
}
