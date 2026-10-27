//! The keeper, outside the session: holds the connections to virgl_test_server for one session's proxy.

use std::collections::HashMap;
use std::os::fd::AsRawFd;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::state::Connection;
use crate::wire::{self, Frame, LinkReader, LinkWriter};

/// Without a proxy for this long the keeper gives up (a freeze takes seconds).
const MAX_DETACHED: Duration = Duration::from_secs(600);

struct Shared {
    link: Option<Arc<LinkWriter>>,
    pending: Vec<Frame>,  // frames for the proxy while it is detached
    ended: bool,          // the proxy left without detaching: the session is over
    detached_since: Instant,
}

pub struct Keeper {
    server_sock: PathBuf,
    shared: Mutex<Shared>,
    upstreams: Mutex<HashMap<u32, Arc<UnixStream>>>,         // connection -> socket to the server
    states: Mutex<HashMap<u32, Arc<Mutex<Connection>>>>,     // what each connection set up there
}

impl Keeper {
    pub fn new(server_sock: PathBuf) -> Arc<Keeper> {
        Arc::new(Keeper {
            server_sock,
            shared: Mutex::new(Shared { link: None, pending: Vec::new(), ended: false, detached_since: Instant::now() }),
            upstreams: Mutex::new(HashMap::new()),
            states: Mutex::new(HashMap::new()),
        })
    }

    /// One lock for every frame to the proxy: nothing can slip in after DETACH_ACK.
    fn to_proxy(&self, mut frame: Frame) {
        let mut s = self.shared.lock().unwrap();
        if let Some(link) = s.link.clone() {
            if link.frame(&frame).is_ok() {
                return;
            }
            s.link = None;  // the proxy went away: keep it for the next attach (its fds are gone)
            frame.fds.clear();
        }
        s.pending.push(frame);
    }

    fn state(&self, conn: u32) -> Option<Arc<Mutex<Connection>>> {
        self.states.lock().unwrap().get(&conn).cloned()
    }

    fn pump(&self, conn: u32, sock: UnixStream) {
        let mut buf = vec![0u8; wire::CHUNK];
        loop {
            let mut fds = Vec::new();
            let n = wire::recv(sock.as_raw_fd(), &mut buf, &mut fds).unwrap_or(0);
            if n == 0 && fds.is_empty() {
                self.to_proxy(Frame::new(wire::CLOSE, conn));
                return;
            }
            if !fds.is_empty() {
                if let Some(st) = self.state(conn) {
                    st.lock().unwrap().from_server_fds(fds.len());
                }
            }
            self.to_proxy(Frame { kind: wire::DATA, conn, data: buf[..n].to_vec(), fds });
        }
    }

    fn open(self: &Arc<Self>, conn: u32) {
        match UnixStream::connect(&self.server_sock).and_then(|s| Ok((s.try_clone()?, s))) {
            Ok((up, s)) => {
                self.upstreams.lock().unwrap().insert(conn, Arc::new(up));
                self.states.lock().unwrap().insert(conn, Arc::new(Mutex::new(Connection::new())));
                let me = self.clone();
                std::thread::spawn(move || me.pump(conn, s));
            }
            Err(e) => {
                eprintln!("stateid-vgpu: cannot reach the GPU server: {e}");
                self.to_proxy(Frame::new(wire::CLOSE, conn));
            }
        }
    }

    fn close(&self, conn: u32) {
        if let Some(up) = self.upstreams.lock().unwrap().remove(&conn) {
            let _ = up.shutdown(std::net::Shutdown::Write);
        }
        if let Some(st) = self.states.lock().unwrap().remove(&conn) {
            let st = st.lock().unwrap();
            eprintln!("stateid-vgpu: connection {conn} closed; kept {} bytes of state, {} resources; could rebuild: {}",
                      st.size(), st.resources.len(), st.problem.as_deref().unwrap_or("yes"));
        }
    }

    fn serve_link(self: Arc<Self>, mut reader: LinkReader, writer: Arc<LinkWriter>) {
        while let Some(f) = reader.recv() {
            match f.kind {
                wire::OPEN => self.open(f.conn),
                wire::DATA => {
                    let up = self.upstreams.lock().unwrap().get(&f.conn).cloned();
                    if let Some(up) = up {
                        if let Some(st) = self.state(f.conn) {
                            st.lock().unwrap().from_client(&f.data);  // before the server can answer it
                        }
                        let _ = wire::relay(up.as_raw_fd(), &f.data, &f.fds);
                    }
                }
                wire::CLOSE => self.close(f.conn),
                wire::DETACH => {
                    let mut s = self.shared.lock().unwrap();  // nothing more goes to the proxy until it attaches
                    s.link = None;
                    s.detached_since = Instant::now();
                    let _ = writer.send(wire::DETACH_ACK, 0, &[], &[]);
                    return;
                }
                wire::ATTACH => {
                    let mut s = self.shared.lock().unwrap();
                    if s.link.is_some() {  // e.g. a restored copy while the original still runs
                        let _ = writer.send(wire::REFUSED, 0, &[], &[]);
                        return;
                    }
                    for frame in std::mem::take(&mut s.pending) {
                        let _ = writer.frame(&frame);
                    }
                    s.link = Some(writer.clone());
                    let _ = writer.send(wire::ATTACHED, 0, &[], &[]);
                }
                _ => {}
            }
        }
        let mut s = self.shared.lock().unwrap();
        if s.link.as_ref().is_some_and(|l| Arc::ptr_eq(l, &writer)) {  // the proxy (the session) ended
            s.link = None;
            s.ended = true;
        }
    }

    /// Serve proxies on `path` until the session ends; `done` is called then (it does not return).
    pub fn run(self: Arc<Self>, path: PathBuf, done: fn() -> !) -> std::io::Result<()> {
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path)?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        let me = self.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(200));
            let s = me.shared.lock().unwrap();
            if s.ended || (s.link.is_none() && s.detached_since.elapsed() > MAX_DETACHED) {
                done();
            }
        });
        for c in listener.incoming() {
            let (reader, writer) = wire::link(c?)?;
            let me = self.clone();
            std::thread::spawn(move || me.serve_link(reader, writer));
        }
        Ok(())
    }
}
