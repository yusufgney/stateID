//! The keeper, outside the session: holds the connections to virgl_test_server for one session's proxy,
//! follows what each has set up there, and can rebuild that on a new server connection.

use std::collections::{HashMap, HashSet};
use std::io::{self, Read};
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use crate::state::{self, Connection};
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
    upstreams: Mutex<HashMap<u32, Arc<UnixStream>>>,      // connection -> socket to the server
    states: Mutex<HashMap<u32, Arc<Mutex<Connection>>>>,  // what each connection set up there
    pumps: Mutex<HashMap<u32, JoinHandle<()>>>,
    moving: Mutex<HashSet<u32>>,                          // their old server connection is closing
    shm_offer: Mutex<HashMap<u32, OwnedFd>>,              // resource id -> shared memory, while rebuilding
}

fn dw(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes(b[4 * i..4 * i + 4].try_into().unwrap())
}

fn vtest(cmd: u32, args: &[u32]) -> Vec<u8> {
    let mut out = (args.len() as u32).to_le_bytes().to_vec();
    out.extend(cmd.to_le_bytes());
    args.iter().for_each(|a| out.extend(a.to_le_bytes()));
    out
}

/// Read exactly n bytes, keeping any fds that come with them.
fn read_reply(s: &UnixStream, n: usize) -> io::Result<(Vec<u8>, Vec<OwnedFd>)> {
    let (mut data, mut fds) = (vec![0u8; n], Vec::new());
    let mut got = 0;
    while got < n {
        let r = wire::recv(s.as_raw_fd(), &mut data[got..], &mut fds)?;
        if r == 0 {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "the GPU server closed the connection"));
        }
        got += r;
    }
    Ok((data, fds))
}

impl Keeper {
    pub fn new(server_sock: PathBuf) -> Arc<Keeper> {
        Arc::new(Keeper {
            server_sock,
            shared: Mutex::new(Shared { link: None, pending: Vec::new(), ended: false, detached_since: Instant::now() }),
            upstreams: Mutex::new(HashMap::new()),
            states: Mutex::new(HashMap::new()),
            pumps: Mutex::new(HashMap::new()),
            moving: Mutex::new(HashSet::new()),
            shm_offer: Mutex::new(HashMap::new()),
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
                if !self.moving.lock().unwrap().contains(&conn) {
                    self.to_proxy(Frame::new(wire::CLOSE, conn));
                }
                return;
            }
            if !fds.is_empty() {
                if let Some(st) = self.state(conn) {
                    st.lock().unwrap().from_server_fds(&fds);
                }
            }
            self.to_proxy(Frame { kind: wire::DATA, conn, data: buf[..n].to_vec(), fds });
        }
    }

    fn start_pump(self: &Arc<Self>, conn: u32, s: UnixStream) -> io::Result<()> {
        self.upstreams.lock().unwrap().insert(conn, Arc::new(s.try_clone()?));
        let me = self.clone();
        let pump = std::thread::spawn(move || me.pump(conn, s));
        self.pumps.lock().unwrap().insert(conn, pump);
        Ok(())
    }

    fn open(self: &Arc<Self>, conn: u32) {
        self.states.lock().unwrap().insert(conn, Arc::new(Mutex::new(Connection::new())));
        if let Err(e) = UnixStream::connect(&self.server_sock).and_then(|s| self.start_pump(conn, s)) {
            eprintln!("stateid-vgpu: cannot reach the GPU server: {e}");
            self.states.lock().unwrap().remove(&conn);
            self.to_proxy(Frame::new(wire::CLOSE, conn));
        }
    }

    fn close(&self, conn: u32) {
        if let Some(up) = self.upstreams.lock().unwrap().remove(&conn) {
            let _ = up.shutdown(std::net::Shutdown::Write);
        }
        self.pumps.lock().unwrap().remove(&conn);
        if let Some(st) = self.states.lock().unwrap().remove(&conn) {
            let st = st.lock().unwrap();
            eprintln!("stateid-vgpu: connection {conn} closed; kept {} bytes of state, {} resources; could rebuild: {}",
                      st.size(), st.resources.len(), st.problem.as_deref().unwrap_or("yes"));
        }
    }

    // ------------------------------------------------------------------ rebuilding
    /// Recreate what `st` describes on a new connection to the server: the setup, the resources with
    /// the same IDs and the same shared memory, buffer contents from that memory, then every object
    /// and setting. Textures and render targets come back empty.
    fn rebuild(&self, st: &mut Connection) -> io::Result<UnixStream> {
        if let Some(p) = &st.problem {
            return Err(io::Error::other(format!("cannot be rebuilt: {p}")));
        }
        let s = UnixStream::connect(&self.server_sock)?;
        let send = |b: &[u8]| wire::send(s.as_raw_fd(), &[b], &[]);
        for cmd in &st.setup {
            send(cmd)?;
            if dw(cmd, 1) == state::PROTOCOL_VERSION {
                read_reply(&s, 12)?;
            }
        }
        // the server numbers resources itself: create them in order, with stand-ins for the gaps
        let creates = st.resource_creates();
        if let Some((id, _)) = creates.iter().find(|(id, cmd)| dw(cmd, 12) != 0 && !st.shm.contains_key(id)) {
            return Err(io::Error::other(format!("the shared memory of resource {id} is not known")));
        }
        let last = creates.last().map_or(0, |(id, _)| *id);
        let live: HashMap<u32, Vec<u8>> = creates.into_iter().collect();
        {
            let mut offer = self.shm_offer.lock().unwrap();
            for (id, fd) in &st.shm {
                offer.insert(*id, fd.try_clone()?);
            }
        }
        let stand_in = vtest(state::RES_CREATE2, &[0, 0, 64, 0x10, 1, 1, 1, 1, 0, 0, 0]);  // a 1-byte buffer
        let mut gaps = Vec::new();
        let result = (|| -> io::Result<()> {
            for id in 1..=last {
                let cmd = live.get(&id).unwrap_or(&stand_in);
                send(cmd)?;
                let with_shm = dw(cmd, 12) != 0;
                let (reply, _fds) = read_reply(&s, if with_shm { 13 } else { 12 })?;  // the fd is ours already
                if dw(&reply, 2) != id {
                    return Err(io::Error::other(format!("the server gave resource {} instead of {id}", dw(&reply, 2))));
                }
                if !live.contains_key(&id) {
                    gaps.push(id);
                }
            }
            Ok(())
        })();
        let untaken = std::mem::take(&mut *self.shm_offer.lock().unwrap()).len();
        result?;
        if untaken > 0 {
            return Err(io::Error::other(format!("the server did not take the shared memory of {untaken} resources \
                                                 (is vgpu_preload loaded?)")));
        }
        for id in &gaps {
            send(&vtest(state::RES_UNREF, &[*id]))?;
        }
        st.renumbered(last + 1, gaps);
        // buffers hold what the program last put in their shared memory
        for (id, cmd) in &live {
            if dw(cmd, 3) == 0 && dw(cmd, 12) != 0 {  // a buffer with shared memory
                send(&vtest(state::TRANSFER_PUT2, &[*id, 0, 0, 0, 0, dw(cmd, 6), 1, 1, 0, 0]))?;
            }
        }
        let body = st.rebuild_commands();
        let mut submit = ((body.len() / 4) as u32).to_le_bytes().to_vec();
        submit.extend(state::SUBMIT_CMD.to_le_bytes());
        submit.extend(body);
        send(&submit)?;
        // the server works through a connection in order: once it answers this, all of the above is
        // done, so the program may change its shared memory again
        send(&vtest(state::BUSY_WAIT, &[0, 1]))?;
        read_reply(&s, 12)?;
        Ok(s)
    }

    /// Move every connection to a new server connection, rebuilt from what was followed. The old
    /// connection is closed first, so the server answers everything already sent on it.
    fn move_all(self: &Arc<Self>) {
        let mut conns: Vec<u32> = self.upstreams.lock().unwrap().keys().copied().collect();
        conns.sort_unstable();
        for conn in conns {
            let started = Instant::now();
            self.moving.lock().unwrap().insert(conn);
            if let Some(up) = self.upstreams.lock().unwrap().remove(&conn) {
                let _ = up.shutdown(std::net::Shutdown::Write);
            }
            if let Some(pump) = self.pumps.lock().unwrap().remove(&conn) {
                let _ = pump.join();
            }
            self.moving.lock().unwrap().remove(&conn);
            let Some(st) = self.state(conn) else { continue };
            let mut st = st.lock().unwrap();
            match self.rebuild(&mut st).and_then(|s| self.start_pump(conn, s)) {
                Ok(()) => eprintln!("stateid-vgpu: connection {conn} rebuilt in {:.0?}: {} resources, {} bytes of state",
                                    started.elapsed(), st.resources.len(), st.size()),
                Err(e) => {
                    eprintln!("stateid-vgpu: connection {conn} could not be rebuilt: {e}");
                    self.to_proxy(Frame::new(wire::CLOSE, conn));
                }
            }
        }
    }

    /// Answer the server's preloaded library: a resource id -> its shared memory, while rebuilding.
    fn serve_shm(self: Arc<Self>, listener: UnixListener) {
        for c in listener.incoming() {
            let Ok(mut c) = c else { continue };
            let me = self.clone();
            std::thread::spawn(move || {
                let mut id = [0u8; 4];
                while c.read_exact(&mut id).is_ok() {
                    let fd = me.shm_offer.lock().unwrap().remove(&u32::from_le_bytes(id));
                    let r = match fd {
                        Some(fd) => wire::send(c.as_raw_fd(), &[b"y"], &[fd]),
                        None => wire::send(c.as_raw_fd(), &[b"n"], &[]),
                    };
                    if r.is_err() {
                        return;
                    }
                }
            });
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
                wire::MOVE => {
                    self.move_all();
                    let _ = writer.send(wire::MOVED, 0, &[], &[]);
                }
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

    /// Serve proxies on `path` and the server's library on `shm_path` until the session ends; `done`
    /// is called then (it does not return).
    pub fn run(self: Arc<Self>, path: PathBuf, shm_path: &Path, done: fn() -> !) -> io::Result<()> {
        let bind = |p: &Path| -> io::Result<UnixListener> {
            let _ = std::fs::remove_file(p);
            let l = UnixListener::bind(p)?;
            std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o600))?;
            Ok(l)
        };
        let shm = bind(shm_path)?;
        let me = self.clone();
        std::thread::spawn(move || me.serve_shm(shm));
        let listener = bind(&path)?;
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
