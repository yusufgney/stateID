//! What a GPU-session program has set up on its virgl_test_server, kept small enough to rebuild later.
//!
//! The keeper feeds every byte a program sends on one vtest connection to a Connection, and tells it
//! when the server sent file descriptors back. The Connection keeps only what is still in effect: the
//! protocol setup, the live resources, and per virgl sub-context the live objects (shaders, states,
//! surfaces, sampler views...) and the last value of every setting. Draws, clears, blits and transfers
//! are dropped: they change resource contents, which are read back separately.
//!
//! Facts this relies on (virglrenderer 1.2, vtest protocol 3):
//! - The server forks one process per connection, so resource IDs are per connection. It gives them
//!   out itself: the most recently freed ID first (a LIFO free list), else the next new one. So they
//!   can be followed from the requests alone.
//! - A RESOURCE_CREATE2 with a data size, and a RESOURCE_CREATE_BLOB, answer with one shared-memory fd.
//! - Object handles come from the program; objects belong to a sub-context, which has its own handles.
//! - Objects keep the resources they use: a surface, sampler view, query or streamout target works on
//!   after its resource is freed, even once the ID is given to another resource. So each such object
//!   remembers the resource it was made on; a freed resource's create command is also kept until its
//!   ID is given out again, for settings that still name it.
//! - A long shader is sent in parts: later parts carry VIRGL_OBJ_SHADER_OFFSET_CONT.
//! - Every answer starts with (length, command); the length counts dwords, except for GET_CAPS(2)
//!   answers (bytes plus one). A file descriptor comes with a byte of its own, outside the answers.
//! - Mesa gives a texture's shared memory exactly the size of its levels laid out one after another,
//!   each row as wide as its blocks (virgl_resource_layout).

#![allow(dead_code)]  // the rebuilding half is used once restore is wired up

use std::collections::{HashMap, HashSet, VecDeque};
use std::hash::Hash;
use std::os::fd::OwnedFd;

// vtest commands
pub const GET_CAPS: u32 = 1;
pub const RES_CREATE: u32 = 2;
pub const RES_UNREF: u32 = 3;
pub const TRANSFER_GET: u32 = 4;
pub const TRANSFER_PUT: u32 = 5;
pub const SUBMIT_CMD: u32 = 6;
pub const BUSY_WAIT: u32 = 7;
pub const CREATE_RENDERER: u32 = 8;
pub const GET_CAPS2: u32 = 9;
pub const PING_PROTOCOL_VERSION: u32 = 10;
pub const PROTOCOL_VERSION: u32 = 11;
pub const RES_CREATE2: u32 = 12;
pub const TRANSFER_GET2: u32 = 13;
pub const TRANSFER_PUT2: u32 = 14;
pub const GET_PARAM: u32 = 15;
pub const GET_CAPSET: u32 = 16;
pub const CONTEXT_INIT: u32 = 17;
pub const RES_CREATE_BLOB: u32 = 18;

// virgl context commands (virgl_protocol.h)
pub const CREATE_OBJECT: u8 = 1;
pub const BIND_OBJECT: u8 = 2;
pub const DESTROY_OBJECT: u8 = 3;
pub const BEGIN_QUERY: u8 = 19;
pub const END_QUERY: u8 = 20;
pub const SET_SUB_CTX: u8 = 28;
pub const CREATE_SUB_CTX: u8 = 29;
pub const DESTROY_SUB_CTX: u8 = 30;
pub const BIND_SHADER: u8 = 31;
pub const OBJECT_SHADER: u8 = 4;
pub const SHADER_OFFSET_CONT: u32 = 1 << 31;

/// Commands that only act (their results live in resources): not kept.
fn is_action(c: u8) -> bool {
    matches!(c, 0 | 7 | 8 | 9 | 16 | 17 | 21 | 36 | 37 | 39 | 42 | 43 | 44 | 45 | 47 | 50 | 51 | 62)
}

/// Settings: how many leading argument dwords tell one slot from another (the last value per slot is
/// kept), or None if the command is not a setting.
fn setting_slots(c: u8) -> Option<usize> {
    Some(match c {
        4 => 1,                            // SET_VIEWPORT_STATE: start slot
        5 | 6 | 11 | 13 | 14 => 0,         // framebuffer, vertex buffers, index buffer, stencil ref, blend color
        10 | 12 | 18 | 27 | 34 | 35 => 2,  // sampler views, constant/uniform buffers, samplers, buffers, images
        15 | 40 | 46 | 48 | 49 => 1,       // scissor, atomic buffers (start slot), tweak id, pipe resource
        22..=26 | 32 | 33 | 38 | 41 => 0,  // stipple, clip, sample mask, streamout, render condition, ...
        52 => 6,                           // LINK_SHADER: the shader handles
        _ => return None,
    })
}

fn video(c: u8) -> bool {
    (53..62).contains(&c)
}

fn dword(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes(b[4 * i..4 * i + 4].try_into().unwrap())
}

/// One encoded virgl context command.
pub fn ccmd(cmd: u8, obj: u8, args: &[u32]) -> Vec<u8> {
    let mut out = (cmd as u32 | (obj as u32) << 8 | (args.len() as u32) << 16).to_le_bytes().to_vec();
    for a in args {
        out.extend_from_slice(&a.to_le_bytes());
    }
    out
}

/// A map that remembers the order its keys were last inserted in.
struct Ordered<K, V> {
    map: HashMap<K, (u64, V)>,
    seq: u64,
}

impl<K: Hash + Eq + Clone, V> Ordered<K, V> {
    fn new() -> Self {
        Ordered { map: HashMap::new(), seq: 0 }
    }
    fn put_last(&mut self, k: K, v: V) {
        self.seq += 1;
        self.map.insert(k, (self.seq, v));
    }
    fn remove(&mut self, k: &K) -> Option<V> {
        self.map.remove(k).map(|(_, v)| v)
    }
    fn get_mut(&mut self, k: &K) -> Option<&mut V> {
        self.map.get_mut(k).map(|(_, v)| v)
    }
    fn len(&self) -> usize {
        self.map.len()
    }
    fn in_order(&self) -> Vec<(&K, &V)> {
        let mut items: Vec<_> = self.map.iter().map(|(k, (s, v))| (*s, k, v)).collect();
        items.sort_unstable_by_key(|(s, _, _)| *s);
        items.into_iter().map(|(_, k, v)| (k, v)).collect()
    }
}

type Handle = [u8; 4];

/// The argument holding the resource an object is made on, for objects made on one.
fn resource_arg(obj: u8) -> Option<usize> {
    match obj {
        6 | 8 | 10 => Some(1),  // sampler view, surface, streamout target
        9 => Some(3),           // query
        _ => None,
    }
}

/// The resource an object was made on: its ID, which creation of it (Resources::generation), and how
/// it was created.
pub type Made = (u32, u64, Vec<u8>);

pub struct SubContext {
    objects: Ordered<Handle, Vec<Vec<u8>>>,  // handle -> encoded create commands (in creation order)
    zombies: Ordered<Handle, Vec<Vec<u8>>>,  // destroyed objects a kept setting may still name
    settings: Ordered<Vec<u8>, Vec<u8>>,     // slot key -> encoded command (in the order last set)
    made_on: HashMap<Handle, Made>,          // object -> the resource it was made on
}

impl SubContext {
    fn new() -> Self {
        SubContext { objects: Ordered::new(), zombies: Ordered::new(), settings: Ordered::new(), made_on: HashMap::new() }
    }

    fn create(&mut self, obj: u8, args: &[u8], cmd: &[u8], made_on: Option<Made>) {
        if args.len() < 4 {
            return;
        }
        let handle: Handle = args[..4].try_into().unwrap();
        if obj == OBJECT_SHADER && args.len() >= 12 && dword(args, 2) & SHADER_OFFSET_CONT != 0 {
            if let Some(parts) = self.objects.get_mut(&handle) {
                parts.push(cmd.to_vec());
            }
            return;
        }
        self.zombies.remove(&handle);
        self.objects.put_last(handle, vec![cmd.to_vec()]);
        match made_on {
            Some(m) => self.made_on.insert(handle, m),
            None => self.made_on.remove(&handle),
        };
    }

    fn destroy(&mut self, args: &[u8]) {
        if args.len() < 4 {
            return;
        }
        let handle: Handle = args[..4].try_into().unwrap();
        if let Some(cmds) = self.objects.remove(&handle) {
            self.zombies.put_last(handle, cmds);
        }
    }

    fn set(&mut self, key: Vec<u8>, cmd: &[u8]) {
        self.settings.put_last(key, cmd.to_vec());
        if self.zombies.len() > 64 {
            self.drop_unnamed_zombies();
        }
    }

    /// Forget destroyed objects no kept setting names (by handle value: may keep a few too many).
    fn drop_unnamed_zombies(&mut self) {
        let mut named: HashSet<Handle> = HashSet::new();
        for (_, cmd) in self.settings.map.values() {
            for i in (4..cmd.len().saturating_sub(3)).step_by(4) {
                named.insert(cmd[i..i + 4].try_into().unwrap());
            }
        }
        self.zombies.map.retain(|h, _| named.contains(h));
        let (objects, zombies) = (&self.objects.map, &self.zombies.map);
        self.made_on.retain(|h, _| objects.contains_key(h) || zombies.contains_key(h));
    }

    /// Commands that recreate this sub-context's objects and settings. Destroyed objects a setting
    /// still names are created first and destroyed again at the end, as they were. An object whose
    /// resource is gone is made on the stand-in `gone` gives for it.
    fn rebuild(&mut self, out: &mut Vec<u8>, gone: &mut dyn FnMut(&Made) -> Option<u32>) {
        self.drop_unnamed_zombies();
        let zombies = self.zombies.in_order();
        let objects = self.objects.in_order();
        for (handle, cmds) in zombies.iter().chain(&objects) {
            let stand_in = self.made_on.get(*handle).and_then(&mut *gone);
            for c in cmds.iter() {
                match (stand_in, resource_arg((dword(c, 0) >> 8) as u8)) {
                    (Some(id), Some(arg)) if c.len() >= 8 + 4 * arg => {
                        let mut c = c.clone();
                        c[4 + 4 * arg..8 + 4 * arg].copy_from_slice(&id.to_le_bytes());
                        out.extend(c);
                    }
                    _ => out.extend_from_slice(c),
                }
            }
        }
        for (_, cmd) in self.settings.in_order() {
            out.extend_from_slice(cmd);
        }
        for (handle, cmds) in &zombies {
            let obj = (dword(&cmds[0], 0) >> 8) as u8;
            out.extend(ccmd(DESTROY_OBJECT, obj, &[u32::from_le_bytes(**handle)]));
        }
    }

    fn size(&self) -> usize {
        let objects: usize = self.objects.map.values().chain(self.zombies.map.values())
            .map(|(_, cmds)| cmds.iter().map(|c| c.len()).sum::<usize>()).sum();
        objects + self.settings.map.values().map(|(_, c)| c.len()).sum::<usize>()
    }
}

/// The state one program connection has built on its server, followed from what it sends.
pub struct Connection {
    buf: Vec<u8>,
    pub setup: Vec<Vec<u8>>,                            // encoded vtest commands to send first
    pub resources: Resources,                            // id -> create command, index of its fd
    pub released: HashMap<u32, Vec<u8>>,  // freed id -> its create command (an object may still use it)
    pub next_id: u32,
    pub free_ids: Vec<u32>,
    fd_waiting: VecDeque<u32>,  // resource ids whose fd the server has yet to send, in order
    pub shm: HashMap<u32, OwnedFd>,  // our copy of each live resource's shared memory
    fds_seen: u32,
    subs: HashMap<u32, SubContext>,
    sub_order: Vec<u32>,
    pub sub: u32,
    pub problem: Option<String>,  // why this connection cannot be rebuilt, if it cannot
    lost: bool,
    pub pings: u32,               // the program's PING_PROTOCOL_VERSIONs not answered yet
    pub replies: Replies,         // what the server sends, split into answers
    pub contents: HashMap<u32, Vec<Vec<u8>>>,  // resource id -> what the GPU held, per level (read back)
}

/// Live resources in creation order.
pub struct Resources {
    inner: Ordered<u32, (Vec<u8>, Option<u32>)>,
}

impl Resources {
    pub fn len(&self) -> usize {
        self.inner.len()
    }
    pub fn ids(&self) -> Vec<u32> {
        self.inner.in_order().into_iter().map(|(k, _)| *k).collect()
    }
    pub fn fd_index(&self, id: u32) -> Option<u32> {
        self.inner.map.get(&id).and_then(|(_, (_, fd))| *fd)
    }
    /// Which creation of a resource with this ID is live (a new one each time the ID is given out).
    pub fn generation(&self, id: u32) -> Option<u64> {
        self.inner.map.get(&id).map(|(seq, _)| *seq)
    }
}

impl Default for Connection {
    fn default() -> Self {
        Self::new()
    }
}

impl Connection {
    pub fn new() -> Self {
        let mut subs = HashMap::new();
        subs.insert(0, SubContext::new());
        Connection {
            buf: Vec::new(),
            setup: Vec::new(),
            resources: Resources { inner: Ordered::new() },
            released: HashMap::new(),
            next_id: 1,
            free_ids: Vec::new(),
            fd_waiting: VecDeque::new(),
            shm: HashMap::new(),
            fds_seen: 0,
            subs,
            sub_order: vec![0],
            sub: 0,
            problem: None,
            lost: false,
            pings: 0,
            replies: Replies::default(),
            contents: HashMap::new(),
        }
    }

    fn trouble(&mut self, why: String) {
        if self.problem.is_none() {
            self.problem = Some(why);
        }
    }

    // ------------------------------------------------------------------ feeding
    pub fn from_client(&mut self, data: &[u8]) {
        if self.lost {
            return;
        }
        self.buf.extend_from_slice(data);
        let buf = std::mem::take(&mut self.buf);
        let mut pos = 0;
        while buf.len() - pos >= 8 {
            let length = dword(&buf[pos..], 0) as usize;
            let cmd = dword(&buf[pos..], 1);
            let size = if cmd == CREATE_RENDERER { length } else { 4 * length };
            if cmd == TRANSFER_GET || cmd == TRANSFER_PUT {  // protocol 1: data follows outside the length
                self.lost = true;
                self.trouble("lost: protocol 1 transfers are not followed".into());
                return;
            }
            if buf.len() - pos < 8 + size {
                break;
            }
            self.command(cmd, &buf[pos..pos + 8 + size]);
            pos += 8 + size;
        }
        self.buf = buf;
        self.buf.drain(..pos);
    }

    /// The resource whose fd the server sends next, if one is expected.
    pub fn next_fd_for(&self) -> Option<u32> {
        self.fd_waiting.front().copied()
    }

    /// The server sent file descriptors (they come in the order they were asked for); a copy of each
    /// is kept while its resource lives.
    pub fn from_server_fds(&mut self, fds: &[OwnedFd]) {
        for f in fds {
            let index = self.fds_seen;
            self.fds_seen += 1;
            match self.fd_waiting.pop_front() {
                None => self.trouble("the server sent a file descriptor no resource asked for".into()),
                Some(res) => {
                    if let Some((_, fd)) = self.resources.inner.get_mut(&res) {
                        *fd = Some(index);
                        match f.try_clone() {
                            Ok(copy) => {
                                self.shm.insert(res, copy);
                            }
                            Err(e) => self.trouble(format!("cannot keep a shared memory fd: {e}")),
                        }
                    }
                }
            }
        }
    }

    fn command(&mut self, cmd: u32, raw: &[u8]) {
        let body = &raw[8..];
        match cmd {
            CREATE_RENDERER | PROTOCOL_VERSION | CONTEXT_INIT => self.setup.push(raw.to_vec()),
            RES_CREATE | RES_CREATE2 | RES_CREATE_BLOB => {
                let handle = if cmd != RES_CREATE_BLOB && body.len() >= 4 { dword(body, 0) } else { 0 };
                let res = if handle != 0 {
                    handle  // protocol < 3: the program chose it
                } else if let Some(id) = self.free_ids.pop() {
                    id
                } else {
                    self.next_id += 1;
                    self.next_id - 1
                };
                self.released.remove(&res);
                self.resources.inner.put_last(res, (raw.to_vec(), None));
                if (cmd == RES_CREATE2 && body.len() >= 44 && dword(body, 10) != 0) || cmd == RES_CREATE_BLOB {
                    self.fd_waiting.push_back(res);
                }
                if cmd != RES_CREATE2 {
                    self.trouble(format!("vtest command {cmd} (resource kind) is not rebuilt yet"));
                }
            }
            RES_UNREF => {
                if body.len() >= 4 {
                    let res = dword(body, 0);
                    if let Some((create, _)) = self.resources.inner.remove(&res) {
                        self.released.insert(res, create);
                        self.free_ids.push(res);
                        self.shm.remove(&res);
                        self.contents.remove(&res);
                    }
                }
            }
            SUBMIT_CMD => self.submit(body),
            PING_PROTOCOL_VERSION => self.pings += 1,
            GET_CAPS | GET_CAPS2 | TRANSFER_GET2 | TRANSFER_PUT2 | BUSY_WAIT | GET_PARAM
            | GET_CAPSET => {}
            _ => self.trouble(format!("vtest command {cmd} is not followed")),
        }
    }

    fn submit(&mut self, body: &[u8]) {
        let mut q = 0;
        while q + 4 <= body.len() {
            let header = dword(&body[q..], 0);
            let (c, obj, n) = (header as u8, (header >> 8) as u8, (header >> 16) as usize);
            let end = (q + 4 + 4 * n).min(body.len());
            let raw = &body[q..end];
            let args = &body[q + 4..end];
            q += 4 + 4 * n;
            let first = |a: &[u8]| if a.len() >= 4 { dword(a, 0) } else { 0 };
            match c {
                CREATE_SUB_CTX => {
                    let id = first(args);
                    self.subs.insert(id, SubContext::new());
                    self.sub_order.retain(|s| *s != id);
                    self.sub_order.push(id);
                    continue;
                }
                DESTROY_SUB_CTX => {
                    let id = first(args);
                    self.subs.remove(&id);
                    self.sub_order.retain(|s| *s != id);
                    continue;
                }
                SET_SUB_CTX => {
                    self.sub = first(args);
                    continue;
                }
                _ => {}
            }
            if is_action(c) {
                continue;
            }
            if video(c) {
                self.trouble("video commands are not rebuilt".into());
                continue;
            }
            let Some(sub) = self.subs.get_mut(&self.sub) else { continue };
            match c {
                CREATE_OBJECT => {
                    let res = resource_arg(obj).filter(|a| args.len() >= 4 * a + 4).map(|a| dword(args, a));
                    let made_on = res.and_then(|r| {
                        let (seq, (create, _)) = self.resources.inner.map.get(&r)?;
                        Some((r, *seq, create.clone()))
                    });
                    sub.create(obj, args, raw, made_on)
                }
                DESTROY_OBJECT => sub.destroy(args),
                BIND_OBJECT => sub.set(vec![c, obj], raw),
                BIND_SHADER => sub.set([&[c][..], args.get(4..8).unwrap_or(&[])].concat(), raw),  // by shader type
                BEGIN_QUERY => sub.set([&[c][..], args.get(..4).unwrap_or(&[])].concat(), raw),
                END_QUERY => {
                    sub.settings.remove(&[&[BEGIN_QUERY][..], args.get(..4).unwrap_or(&[])].concat());
                }
                _ => match setting_slots(c) {
                    Some(n) => sub.set([&[c][..], &args[..(4 * n).min(args.len())]].concat(), raw),
                    None => self.trouble(format!("virgl command {c} is not followed")),
                },
            }
        }
    }

    // ------------------------------------------------------------------ rebuilding
    /// The live resources by id: (id, encoded create command).
    pub fn resource_creates(&self) -> Vec<(u32, Vec<u8>)> {
        let mut out: Vec<_> = self.resources.inner.map.iter().map(|(id, (_, (raw, _)))| (*id, raw.clone())).collect();
        out.sort_unstable_by_key(|(id, _)| *id);
        out
    }

    /// After a rebuild the new server hands out IDs from its own free list.
    pub fn renumbered(&mut self, next_id: u32, free_ids: Vec<u32>) {
        self.next_id = next_id;
        self.free_ids = free_ids;
    }

    /// The virgl command buffer that recreates every sub-context (resources must exist first).
    pub fn rebuild_commands(&mut self) -> Vec<u8> {
        self.rebuild_commands_from(self.next_id).0
    }

    /// The same, with objects whose resource is gone made on stand-ins numbered from `next`, created
    /// as their resources were (returned in order: create them first, free them after).
    pub fn rebuild_commands_from(&mut self, next: u32) -> (Vec<u8>, Vec<Vec<u8>>) {
        let (mut out, mut stand_ins) = (Vec::new(), Vec::new());
        let mut given: HashMap<(u32, u64), u32> = HashMap::new();
        let resources = &self.resources;
        let mut gone = |m: &Made| {
            if resources.generation(m.0) == Some(m.1) {
                return None;
            }
            Some(*given.entry((m.0, m.1)).or_insert_with(|| {
                stand_ins.push(m.2.clone());
                next + stand_ins.len() as u32 - 1
            }))
        };
        for id in self.sub_order.clone() {
            if id != 0 {
                out.extend(ccmd(CREATE_SUB_CTX, 0, &[id]));
            }
            out.extend(ccmd(SET_SUB_CTX, 0, &[id]));
            if let Some(sub) = self.subs.get_mut(&id) {
                sub.rebuild(&mut out, &mut gone);
            }
        }
        out.extend(ccmd(SET_SUB_CTX, 0, &[self.sub]));
        (out, stand_ins)
    }

    /// Bytes kept (to check it stays small).
    pub fn size(&self) -> usize {
        self.setup.iter().map(|s| s.len()).sum::<usize>()
            + self.resources.inner.map.values().map(|(_, (r, _))| r.len()).sum::<usize>()
            + self.subs.values().map(|s| s.size()).sum::<usize>()
    }
}

/// Splits what the server sends on a connection into answers (the bytes carrying fds left out).
#[derive(Default)]
pub struct Replies {
    header: Vec<u8>,
    left: usize,
    cmd: u32,
    start: u64,
    pub pos: u64,  // bytes fed so far
}

impl Replies {
    /// Feed what came next; returns (start, end, command) of each answer completed by it, as positions.
    pub fn feed(&mut self, mut data: &[u8]) -> Vec<(u64, u64, u32)> {
        let mut done = Vec::new();
        loop {
            if self.header.len() < 8 {
                if data.is_empty() {
                    break;
                }
                if self.header.is_empty() {
                    self.start = self.pos;
                }
                let take = (8 - self.header.len()).min(data.len());
                self.header.extend_from_slice(&data[..take]);
                self.pos += take as u64;
                data = &data[take..];
                if self.header.len() < 8 {
                    break;
                }
                let length = dword(&self.header, 0) as usize;
                self.cmd = dword(&self.header, 1);
                // GET_CAPS and GET_CAPS2 (whose answer says 2) count bytes, plus one
                self.left = if self.cmd == GET_CAPS || self.cmd == 2 { length.saturating_sub(1) } else { 4 * length };
            }
            let take = self.left.min(data.len());
            self.left -= take;
            self.pos += take as u64;
            data = &data[take..];
            if self.left > 0 {
                break;
            }
            done.push((self.start, self.pos, self.cmd));
            self.header.clear();
        }
        done
    }
}

/// One mip level of a resource, as a transfer reads or writes it whole.
#[derive(Debug, PartialEq)]
pub struct Level {
    pub level: u32,
    pub width: u32,
    pub height: u32,
    pub depth: u32,  // layers, faces or slices
    pub stride: u32,
    pub layer_stride: u32,
}

impl Level {
    pub fn size(&self) -> usize {
        if self.stride == 0 {
            self.width as usize  // a buffer: bytes
        } else {
            self.layer_stride as usize * self.depth as usize
        }
    }

    /// The arguments of a protocol-1 TRANSFER_GET / TRANSFER_PUT of all of it.
    pub fn transfer(&self, res: u32) -> [u32; 11] {
        [res, self.level, self.stride, self.layer_stride, 0, 0, 0, self.width, self.height, self.depth,
         self.size() as u32]
    }
}

/// Block-compressed and multi-plane formats (virgl_hw.h): their layout is not worked out here.
fn not_plain(format: u32) -> bool {
    matches!(format, 14..=15 | 105..=118 | 143..=146 | 163..=167 | 226..=228 | 255..=258 | 269..=306 | 314..=316
                     | 323..=325 | 327 | 348..=367 | 371 | 384 | 394..=396 | 398..=400 | 402 | 420..=426 | 429
                     | 433..=449 | 454..=480)
}

/// How to read and write back a resource's contents, level by level, from its RESOURCE_CREATE2. The
/// bytes per pixel come from the size of its shared memory, laid out as Mesa does.
pub fn layout(create: &[u8]) -> Result<Vec<Level>, String> {
    let a = |i: usize| dword(create, 2 + i);
    let (target, format, width, height, depth, array, last, samples, size) =
        (a(1), a(2), a(4), a(5), a(6), a(7), a(8), a(9), a(10));
    if target == 0 {  // a buffer
        return Ok(vec![Level { level: 0, width, height: 1, depth: 1, stride: 0, layer_stride: 0 }]);
    }
    if samples > 1 {
        return Err("multisampled".into());
    }
    if target == 6 {  // PIPE_TEXTURE_1D_ARRAY: its layers are rows
        return Err("1D array".into());
    }
    if not_plain(format) {
        return Err(format!("format {format} is compressed or has planes"));
    }
    if size == 0 {
        return Err("no shared memory to tell its layout".into());
    }
    let minify = |v: u32, l: u32| (v >> l).max(1);
    let layers = |l: u32| match target {
        3 => minify(depth, l),  // PIPE_TEXTURE_3D
        4 => 6,                 // PIPE_TEXTURE_CUBE
        _ => array.max(1),
    };
    let pixels: u64 = (0..=last).map(|l| minify(width, l) as u64 * minify(height, l) as u64 * layers(l) as u64).sum();
    let bpp = size as u64 / pixels.max(1);
    if pixels == 0 || bpp * pixels != size as u64 || ![1, 2, 3, 4, 6, 8, 12, 16].contains(&bpp) {
        return Err(format!("its {size} bytes do not fit {pixels} pixels"));
    }
    Ok((0..=last).map(|l| {
        let (w, h) = (minify(width, l), minify(height, l));
        let stride = w * bpp as u32;
        Level { level: l, width: w, height: h, depth: layers(l), stride, layer_stride: stride * h }
    }).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vt(cmd: u32, dwords: &[u32]) -> Vec<u8> {
        let mut out = (dwords.len() as u32).to_le_bytes().to_vec();
        out.extend(cmd.to_le_bytes());
        dwords.iter().for_each(|d| out.extend(d.to_le_bytes()));
        out
    }

    fn create2(size: u32) -> Vec<u8> {
        // handle 0 (the server picks), target, format, bind, w, h, depth, array size, last level, samples, size
        vt(RES_CREATE2, &[0, 2, 1, 0, 64, 64, 1, 1, 0, 0, size])
    }

    fn unref(res: u32) -> Vec<u8> {
        vt(RES_UNREF, &[res])
    }

    fn submit(cmds: &[Vec<u8>]) -> Vec<u8> {
        let body = cmds.concat();
        let mut out = ((body.len() / 4) as u32).to_le_bytes().to_vec();
        out.extend(SUBMIT_CMD.to_le_bytes());
        out.extend(body);
        out
    }

    /// (cmd, obj, args) of each command in a virgl command buffer.
    fn commands(buf: &[u8]) -> Vec<(u8, u8, Vec<u32>)> {
        let (mut out, mut q) = (Vec::new(), 0);
        while q < buf.len() {
            let h = dword(&buf[q..], 0);
            let n = (h >> 16) as usize;
            out.push((h as u8, (h >> 8) as u8, (0..n).map(|i| dword(&buf[q + 4..], i)).collect()));
            q += 4 + 4 * n;
        }
        out
    }

    fn fds(n: usize) -> Vec<OwnedFd> {
        (0..n).map(|_| std::fs::File::open("/dev/null").unwrap().into()).collect()
    }

    fn feed(stream: &[Vec<u8>]) -> Connection {
        let mut c = Connection::new();
        c.from_client(&stream.concat());
        c
    }

    #[test]
    fn resource_ids_follow_the_servers_free_list() {
        let mut c = feed(&[create2(4096), create2(4096), create2(4096), unref(2)]);
        c.from_client(&create2(4096));  // the freed 2 comes back first
        assert_eq!(c.resources.ids(), vec![1, 3, 2]);
        c.from_client(&[unref(1), unref(3), create2(1), create2(1), create2(1)].concat());
        let mut ids = c.resources.ids();
        assert_eq!(ids[ids.len() - 3..], [3, 1, 4]);  // last freed first, then a new one
        ids.sort();
        assert_eq!(ids, vec![1, 2, 3, 4]);
    }

    #[test]
    fn shared_memory_fds_belong_to_the_resources_that_asked() {
        let mut c = feed(&[create2(0), create2(4096), create2(4096)]);
        c.from_server_fds(&fds(1));
        c.from_server_fds(&fds(1));
        assert_eq!([1, 2, 3].map(|r| c.resources.fd_index(r)), [None, Some(0), Some(1)]);
        assert_eq!(c.shm.len(), 2);
        assert!(c.problem.is_none());
        c.from_client(&unref(2));
        assert!(!c.shm.contains_key(&2) && c.shm.contains_key(&3));  // the copy goes with the resource
        c.from_server_fds(&fds(1));
        assert!(c.problem.unwrap().contains("no resource asked"));
    }

    #[test]
    fn split_input_gives_the_same_state() {
        let stream = [vt(CREATE_RENDERER, &[]), create2(64),
                      submit(&[ccmd(CREATE_OBJECT, 1, &[7, 0]), ccmd(BIND_OBJECT, 1, &[7])]),
                      create2(64), unref(1)].concat();
        let mut whole = Connection::new();
        whole.from_client(&stream);
        let mut parts = Connection::new();
        stream.iter().for_each(|b| parts.from_client(&[*b]));
        assert_eq!(whole.resources.ids(), vec![2]);
        assert_eq!(parts.resources.ids(), vec![2]);
        assert_eq!(whole.rebuild_commands(), parts.rebuild_commands());
    }

    #[test]
    fn settings_keep_their_last_value_and_objects_only_while_alive() {
        let mut c = feed(&[submit(&[
            ccmd(CREATE_SUB_CTX, 0, &[1]), ccmd(SET_SUB_CTX, 0, &[1]),
            ccmd(CREATE_OBJECT, 1, &[10, 0xb1]),  // blend state 10
            ccmd(CREATE_OBJECT, 1, &[11, 0xb2]),  // blend state 11
            ccmd(BIND_OBJECT, 1, &[10]), ccmd(BIND_OBJECT, 1, &[11]),
            ccmd(14, 0, &[1, 2, 3, 4]), ccmd(14, 0, &[5, 6, 7, 8]),  // SET_BLEND_COLOR twice
            ccmd(CREATE_OBJECT, 8, &[12, 3]), ccmd(DESTROY_OBJECT, 8, &[12]),  // a surface made and gone
            ccmd(8, 0, &(0..12).collect::<Vec<_>>()),  // a draw: not kept
        ])]);
        let cmds = commands(&c.rebuild_commands());
        let at = |x: (u8, u8, Vec<u32>)| cmds.iter().position(|y| *y == x);
        assert!(at((CREATE_SUB_CTX, 0, vec![1])).unwrap() < at((SET_SUB_CTX, 0, vec![1])).unwrap());
        assert!(at((CREATE_OBJECT, 1, vec![10, 0xb1])).is_some() && at((CREATE_OBJECT, 1, vec![11, 0xb2])).is_some());
        let of = |k: u8| cmds.iter().filter(|x| x.0 == k).map(|x| x.2.clone()).collect::<Vec<_>>();
        assert_eq!(of(BIND_OBJECT), vec![vec![11]]);
        assert_eq!(of(14), vec![vec![5, 6, 7, 8]]);
        assert!(of(8).is_empty() && of(DESTROY_OBJECT).is_empty());
        assert!(!cmds.iter().any(|x| x.2.first() == Some(&12)));
        assert_eq!(*cmds.last().unwrap(), (SET_SUB_CTX, 0, vec![1]));
        assert!(c.problem.is_none());
    }

    #[test]
    fn a_destroyed_object_a_setting_still_names_is_recreated_then_destroyed() {
        let mut c = feed(&[submit(&[
            ccmd(CREATE_OBJECT, 6, &[20, 1]), ccmd(CREATE_OBJECT, 6, &[21, 1]),  // two sampler views
            ccmd(10, 0, &[1, 0, 20, 21]),  // SET_SAMPLER_VIEWS fragment, from slot 0: 20, 21
            ccmd(10, 0, &[1, 1, 22]),      // later slot 1 only: 21 stays named by the first one
            ccmd(DESTROY_OBJECT, 6, &[21]),
        ])]);
        let cmds = commands(&c.rebuild_commands());
        let at = |x: (u8, u8, Vec<u32>)| cmds.iter().position(|y| *y == x).unwrap();
        let made = at((CREATE_OBJECT, 6, vec![21, 1]));
        let used = at((10, 0, vec![1, 0, 20, 21]));
        let gone = at((DESTROY_OBJECT, 6, vec![21]));
        assert!(made < used && used < gone);
    }

    #[test]
    fn a_long_shader_keeps_all_its_parts() {
        let mut c = feed(&[submit(&[
            ccmd(CREATE_OBJECT, OBJECT_SHADER, &[30, 1, 100, 25, 0xaaaa]),
            ccmd(CREATE_OBJECT, OBJECT_SHADER, &[30, 1, SHADER_OFFSET_CONT | 60, 25, 0xbbbb]),
        ])]);
        let parts: Vec<u32> = commands(&c.rebuild_commands()).into_iter()
            .filter(|x| x.0 == CREATE_OBJECT).map(|x| *x.2.last().unwrap()).collect();
        assert_eq!(parts, vec![0xaaaa, 0xbbbb]);
    }

    #[test]
    fn what_is_not_followed_is_reported() {
        let c = feed(&[submit(&[ccmd(55, 0, &[1])])]);  // a video command
        assert!(c.problem.unwrap().contains("video"));
        let mut put = vt(TRANSFER_PUT, &(0..11).collect::<Vec<_>>());
        put.extend(b"data");
        let c = feed(&[put]);
        assert!(c.problem.unwrap().starts_with("lost"));
    }

    #[test]
    fn answers_are_found_however_they_arrive() {
        let mut stream = vt(PROTOCOL_VERSION, &[3]);
        stream.extend([0, 0, 0, 0, 1, 0, 0, 0]);  // GET_CAPS: 0 bytes of caps (length 1)
        stream.extend(vt(PING_PROTOCOL_VERSION, &[]));
        stream.extend(vt(RES_CREATE2, &[7]));
        let whole: Vec<_> = Replies::default().feed(&stream);
        assert_eq!(whole, vec![(0, 12, PROTOCOL_VERSION), (12, 20, GET_CAPS), (20, 28, PING_PROTOCOL_VERSION),
                               (28, 40, RES_CREATE2)]);
        let mut r = Replies::default();
        let bytewise: Vec<_> = stream.iter().flat_map(|b| r.feed(&[*b])).collect();
        assert_eq!(bytewise, whole);
    }

    #[test]
    fn contents_are_laid_out_as_mesa_does() {
        // the GTK window: 700x520 BGRA, 4 bytes a pixel
        let window = layout(&create2_of(2, 1, 700, 520, 1, 1, 0, 1456000)).unwrap();
        assert_eq!(window, vec![Level { level: 0, width: 700, height: 520, depth: 1, stride: 2800,
                                        layer_stride: 1456000 }]);
        // a mip chain: 8x4, 4x2, 2x1, 1x1 of 2-byte pixels
        let mips = layout(&create2_of(2, 0, 8, 4, 1, 1, 3, (32 + 8 + 2 + 1) * 2)).unwrap();
        assert_eq!(mips.iter().map(|l| (l.width, l.height, l.stride)).collect::<Vec<_>>(),
                   vec![(8, 4, 16), (4, 2, 8), (2, 1, 4), (1, 1, 2)]);
        let cube = layout(&create2_of(4, 1, 4, 4, 1, 1, 0, 4 * 4 * 4 * 6)).unwrap();
        assert_eq!((cube[0].depth, cube[0].size()), (6, 384));
        assert_eq!(layout(&create2_of(0, 64, 100, 1, 1, 1, 0, 0)).unwrap()[0].size(), 100);  // a buffer
        assert!(layout(&create2_of(2, 108, 16, 16, 1, 1, 0, 256)).is_err());  // DXT5: blocks, not pixels
        assert!(layout(&create2_of(2, 1, 64, 64, 1, 1, 0, 0)).is_err());  // no shared memory
    }

    #[test]
    fn an_object_whose_resource_is_gone_is_made_on_a_stand_in() {
        let tex = vt(RES_CREATE2, &[0, 2, 67, 2, 4, 4, 1, 1, 0, 0, 64]);
        let query_buf = vt(RES_CREATE2, &[0, 0, 64, 1 << 17, 8, 1, 1, 0, 0, 0, 8]);
        let mut c = feed(&[tex.clone(), submit(&[ccmd(CREATE_OBJECT, 8, &[1, 1, 67, 0, 0])]), unref(1),
                           query_buf, unref(1)]);  // the surface's texture is gone, its ID given out again
        let (cmds, stand_ins) = c.rebuild_commands_from(7);
        assert!(commands(&cmds).contains(&(CREATE_OBJECT, 8, vec![1, 7, 67, 0, 0])));
        assert_eq!(stand_ins, vec![tex]);
        let mut c = feed(&[create2(64), submit(&[ccmd(CREATE_OBJECT, 8, &[1, 1, 67, 0, 0])])]);
        assert_eq!(c.rebuild_commands_from(7).1.len(), 0);  // still there: no stand-in
    }

    fn create2_of(target: u32, format: u32, w: u32, h: u32, depth: u32, array: u32, last: u32, size: u32) -> Vec<u8> {
        vt(RES_CREATE2, &[0, target, format, 0, w, h, depth, array, last, 0, size])
    }
}
