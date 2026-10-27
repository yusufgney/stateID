//! Unix stream sockets with SCM_RIGHTS file descriptors, and the framed link between proxy and keeper.

use std::collections::VecDeque;
use std::io;
use std::mem::size_of;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::raw::{c_int, c_void};
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};

#[repr(C)]
struct IoVec {
    base: *const c_void,
    len: usize,
}

#[repr(C)]
struct MsgHdr {
    name: *mut c_void,
    namelen: u32,
    iov: *const IoVec,
    iovlen: usize,
    control: *mut c_void,
    controllen: usize,
    flags: c_int,
}

#[repr(C)]
struct CmsgHdr {
    len: usize,
    level: c_int,
    kind: c_int,
}

extern "C" {
    fn sendmsg(fd: c_int, msg: *const MsgHdr, flags: c_int) -> isize;
    fn recvmsg(fd: c_int, msg: *mut MsgHdr, flags: c_int) -> isize;
}

const SOL_SOCKET: c_int = 1;
const SCM_RIGHTS: c_int = 1;
const MSG_NOSIGNAL: c_int = 0x4000;
const MSG_CMSG_CLOEXEC: c_int = 0x4000_0000;
pub const MAX_FDS: usize = 32;
const CMSG_HDR: usize = size_of::<CmsgHdr>();
const CONTROL_WORDS: usize = (CMSG_HDR + 4 * MAX_FDS) / 8 + 1;
pub const CHUNK: usize = 1 << 16;

fn align(n: usize) -> usize {
    (n + 7) & !7
}

/// Send `parts` as one stream of bytes with `fds` attached to its first byte (the caller's fds are
/// only borrowed; dropping them afterwards closes our copies).
pub fn send(fd: RawFd, parts: &[&[u8]], fds: &[OwnedFd]) -> io::Result<()> {
    assert!(fds.len() <= MAX_FDS);
    let total: usize = parts.iter().map(|p| p.len()).sum();
    if total == 0 && fds.is_empty() {
        return Ok(());
    }
    let mut control = [0u64; CONTROL_WORDS];
    let mut sent = 0;
    let mut first = true;
    while first || sent < total {
        let mut skip = sent;
        let mut iov = Vec::with_capacity(parts.len());
        for p in parts {
            if skip >= p.len() {
                skip -= p.len();
                continue;
            }
            iov.push(IoVec { base: p[skip..].as_ptr() as *const c_void, len: p.len() - skip });
            skip = 0;
        }
        let mut msg = MsgHdr {
            name: std::ptr::null_mut(),
            namelen: 0,
            iov: iov.as_ptr(),
            iovlen: iov.len(),
            control: std::ptr::null_mut(),
            controllen: 0,
            flags: 0,
        };
        if first && !fds.is_empty() {
            let len = CMSG_HDR + 4 * fds.len();
            let base = control.as_mut_ptr() as *mut u8;
            unsafe {
                std::ptr::write(base as *mut CmsgHdr, CmsgHdr { len, level: SOL_SOCKET, kind: SCM_RIGHTS });
                let data = base.add(CMSG_HDR) as *mut i32;
                for (i, f) in fds.iter().enumerate() {
                    std::ptr::write_unaligned(data.add(i), f.as_raw_fd());
                }
            }
            msg.control = base as *mut c_void;
            msg.controllen = align(len);
        }
        let r = unsafe { sendmsg(fd, &msg, MSG_NOSIGNAL) };
        if r < 0 {
            let e = io::Error::last_os_error();
            if e.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(e);
        }
        first = false;
        sent += r as usize;
    }
    Ok(())
}

/// Read what is there (up to buf.len() bytes) and any fds that came with it. 0 bytes = end of stream.
pub fn recv(fd: RawFd, buf: &mut [u8], fds: &mut Vec<OwnedFd>) -> io::Result<usize> {
    let mut control = [0u64; CONTROL_WORDS];
    let iov = IoVec { base: buf.as_mut_ptr() as *const c_void, len: buf.len() };
    let mut msg = MsgHdr {
        name: std::ptr::null_mut(),
        namelen: 0,
        iov: &iov,
        iovlen: 1,
        control: control.as_mut_ptr() as *mut c_void,
        controllen: CONTROL_WORDS * 8,
        flags: 0,
    };
    let r = loop {
        let r = unsafe { recvmsg(fd, &mut msg, MSG_CMSG_CLOEXEC) };
        if r >= 0 {
            break r as usize;
        }
        let e = io::Error::last_os_error();
        if e.kind() != io::ErrorKind::Interrupted {
            return Err(e);
        }
    };
    let base = control.as_ptr() as *const u8;
    let mut pos = 0;
    while pos + CMSG_HDR <= msg.controllen {
        let h = unsafe { std::ptr::read_unaligned(base.add(pos) as *const CmsgHdr) };
        if h.len < CMSG_HDR {
            break;
        }
        if h.level == SOL_SOCKET && h.kind == SCM_RIGHTS {
            for i in 0..(h.len - CMSG_HDR) / 4 {
                let raw = unsafe { std::ptr::read_unaligned(base.add(pos + CMSG_HDR + 4 * i) as *const i32) };
                fds.push(unsafe { OwnedFd::from_raw_fd(raw) });
            }
        }
        pos += align(h.len);
    }
    Ok(r)
}

/// Pass on a chunk read from a vtest peer. A read returns the bytes before a message carrying fds
/// together with it, and the fds belong to that last message: vtest sends each fd with one byte of
/// its own, which the other side reads with recvmsg. So the fds go with the last byte.
pub fn relay(fd: RawFd, data: &[u8], fds: &[OwnedFd]) -> io::Result<()> {
    if !fds.is_empty() && data.len() > 1 {
        send(fd, &[&data[..data.len() - 1]], &[])?;
        return send(fd, &[&data[data.len() - 1..]], fds);
    }
    send(fd, &[data], fds)
}

// ---------------------------------------------------------------- framed link
pub const DATA: u8 = 1;
pub const OPEN: u8 = 2;
pub const CLOSE: u8 = 3;
pub const DETACH: u8 = 4;
pub const DETACH_ACK: u8 = 5;
pub const ATTACH: u8 = 6;
pub const ATTACHED: u8 = 7;  // with the connections the keeper holds, as u32s
pub const REFUSED: u8 = 8;
pub const MOVE: u8 = 9;    // rebuild every connection on a new server connection (a test of rebuilding)
pub const MOVED: u8 = 10;
pub const STATE: u8 = 11;    // keeper -> proxy on DETACH: a connection's saved state (kept in the frozen session)
pub const FORGET: u8 = 12;   // keeper -> proxy: the resource with this fd index (u32) is freed
pub const FDS: u8 = 13;      // proxy -> keeper: shared memory to restore with, its fd indexes as u32s
pub const RESTORE: u8 = 14;  // proxy -> keeper: rebuild this connection from its saved state
pub const RESTORED: u8 = 15; // keeper -> proxy: done (a CLOSE came first if it failed)
const HEADER: usize = 10; // kind u8, connection u32, payload length u32, number of fds u8

pub struct Frame {
    pub kind: u8,
    pub conn: u32,
    pub data: Vec<u8>,
    pub fds: Vec<OwnedFd>,
}

impl Frame {
    pub fn new(kind: u8, conn: u32) -> Frame {
        Frame { kind, conn, data: Vec::new(), fds: Vec::new() }
    }
}

/// The sending half: one frame at a time, its fds with it.
pub struct LinkWriter {
    sock: Mutex<UnixStream>,
}

impl LinkWriter {
    pub fn send(&self, kind: u8, conn: u32, data: &[u8], fds: &[OwnedFd]) -> io::Result<()> {
        let mut header = [0u8; HEADER];
        header[0] = kind;
        header[1..5].copy_from_slice(&conn.to_le_bytes());
        header[5..9].copy_from_slice(&(data.len() as u32).to_le_bytes());
        header[9] = fds.len() as u8;
        let sock = self.sock.lock().unwrap();
        send(sock.as_raw_fd(), &[&header, data], fds)
    }

    pub fn frame(&self, f: &Frame) -> io::Result<()> {
        self.send(f.kind, f.conn, &f.data, &f.fds)
    }

    pub fn shutdown(&self) {
        let _ = self.sock.lock().unwrap().shutdown(std::net::Shutdown::Both);
    }
}

/// The receiving half; fds are counted per frame.
pub struct LinkReader {
    sock: UnixStream,
    buf: Vec<u8>,
    fds: VecDeque<OwnedFd>,
    scratch: Vec<u8>,
}

impl LinkReader {
    /// The next frame, or None at end of stream.
    pub fn recv(&mut self) -> Option<Frame> {
        loop {
            if self.buf.len() >= HEADER {
                let n = u32::from_le_bytes(self.buf[5..9].try_into().unwrap()) as usize;
                let nfds = self.buf[9] as usize;
                if self.buf.len() >= HEADER + n && self.fds.len() >= nfds {
                    let kind = self.buf[0];
                    let conn = u32::from_le_bytes(self.buf[1..5].try_into().unwrap());
                    let data = self.buf[HEADER..HEADER + n].to_vec();
                    self.buf.drain(..HEADER + n);
                    let fds = self.fds.drain(..nfds).collect();
                    return Some(Frame { kind, conn, data, fds });
                }
            }
            let mut fds = Vec::new();
            let n = recv(self.sock.as_raw_fd(), &mut self.scratch, &mut fds).ok()?;
            if n == 0 && fds.is_empty() {
                return None;
            }
            self.buf.extend_from_slice(&self.scratch[..n]);
            self.fds.extend(fds);
        }
    }
}

pub fn link(sock: UnixStream) -> io::Result<(LinkReader, Arc<LinkWriter>)> {
    let writer = Arc::new(LinkWriter { sock: Mutex::new(sock.try_clone()?) });
    let reader = LinkReader { sock, buf: Vec::new(), fds: VecDeque::new(), scratch: vec![0; CHUNK] };
    Ok((reader, writer))
}
