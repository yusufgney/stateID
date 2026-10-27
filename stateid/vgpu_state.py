"""What a GPU-session program has set up on its virgl_test_server, kept small enough to rebuild later.

The keeper (stateid.vgpu) feeds every byte a program sends on one vtest connection to a Connection,
and tells it when the server sent file descriptors back. The Connection keeps only what is still in
effect: the protocol setup, the live resources, and per virgl sub-context the live objects (shaders,
states, surfaces, sampler views...) and the last value of every setting. Draws, clears, blits and
transfers are dropped: they change resource contents, which are read back separately.

Facts this relies on (virglrenderer 1.2, vtest protocol 3):
- The server forks one process per connection, so resource IDs are per connection. It gives them
  out itself: the most recently freed ID first (a LIFO free list), else the next new one. So they
  can be followed from the requests alone.
- A RESOURCE_CREATE2 with a data size, and a RESOURCE_CREATE_BLOB, answer with one shared-memory fd.
- Object handles come from the program; objects belong to a sub-context, which has its own handles.
- A long shader is sent in parts: later parts carry VIRGL_OBJ_SHADER_OFFSET_CONT.
"""
import struct
import threading
from collections import OrderedDict

# vtest commands
GET_CAPS, RES_CREATE, RES_UNREF, TRANSFER_GET, TRANSFER_PUT, SUBMIT_CMD, BUSY_WAIT, CREATE_RENDERER = range(1, 9)
GET_CAPS2, PING_PROTOCOL_VERSION, PROTOCOL_VERSION, RES_CREATE2, TRANSFER_GET2, TRANSFER_PUT2 = range(9, 15)
GET_PARAM, GET_CAPSET, CONTEXT_INIT, RES_CREATE_BLOB = range(15, 19)
SETUP = (CREATE_RENDERER, PROTOCOL_VERSION, CONTEXT_INIT)
NO_STATE = (GET_CAPS, GET_CAPS2, PING_PROTOCOL_VERSION, TRANSFER_GET2, TRANSFER_PUT2, BUSY_WAIT, GET_PARAM,
            GET_CAPSET)

# virgl context commands (virgl_protocol.h)
CREATE_OBJECT, BIND_OBJECT, DESTROY_OBJECT = 1, 2, 3
SET_SUB_CTX, CREATE_SUB_CTX, DESTROY_SUB_CTX = 28, 29, 30
BEGIN_QUERY, END_QUERY = 19, 20
BIND_SHADER = 31
OBJECT_SHADER = 4
SHADER_OFFSET_CONT = 1 << 31
# commands that only act (their results live in resources): not kept
ACTIONS = {0, 7, 8, 9, 16, 17, 21, 36, 37, 39, 42, 43, 44, 45, 47, 50, 51, 62}
VIDEO = set(range(53, 62))
# settings: which leading argument dwords tell one slot from another; the last value per slot is kept
SETTINGS = {
    4: 1,   # SET_VIEWPORT_STATE: start slot
    5: 0,   # SET_FRAMEBUFFER_STATE
    6: 0,   # SET_VERTEX_BUFFERS
    10: 2,  # SET_SAMPLER_VIEWS: shader, start slot
    11: 0,  # SET_INDEX_BUFFER
    12: 2,  # SET_CONSTANT_BUFFER: shader, index
    13: 0, 14: 0,  # SET_STENCIL_REF, SET_BLEND_COLOR
    15: 1,  # SET_SCISSOR_STATE: start slot
    18: 2,  # BIND_SAMPLER_STATES: shader, start slot
    22: 0, 23: 0, 24: 0, 25: 0, 26: 0,  # stipple, clip, sample mask, streamout targets, render condition
    27: 2,  # SET_UNIFORM_BUFFER: shader, index
    32: 0, 33: 0,  # SET_TESS_STATE, SET_MIN_SAMPLES
    34: 2, 35: 2,  # SET_SHADER_BUFFERS, SET_SHADER_IMAGES: shader, start slot
    38: 0,  # SET_FRAMEBUFFER_STATE_NO_ATTACH
    40: 1,  # SET_ATOMIC_BUFFERS: start slot
    41: 0,  # SET_DEBUG_FLAGS
    46: 1,  # SET_TWEAKS: tweak id
    48: 1, 49: 1,  # PIPE_RESOURCE_CREATE / SET_TYPE: resource
    52: 6,  # LINK_SHADER: the shader handles
}


def ccmd(cmd, obj, args):
    """One encoded virgl context command."""
    return struct.pack("<I", cmd | obj << 8 | (len(args) // 4) << 16) + args


class SubContext:
    def __init__(self):
        self.objects = OrderedDict()   # handle -> [encoded create commands] (in creation order)
        self.zombies = {}              # destroyed objects a kept setting may still name
        self.settings = OrderedDict()  # slot key -> encoded command (in the order last set)

    def create(self, obj, args, cmd):
        handle = args[:4]
        if obj == OBJECT_SHADER and len(args) >= 12 and struct.unpack_from("<I", args, 8)[0] & SHADER_OFFSET_CONT:
            if handle in self.objects:
                self.objects[handle].append(cmd)
            return
        self.objects.pop(handle, None)
        self.zombies.pop(handle, None)
        self.objects[handle] = [cmd]

    def destroy(self, args):
        cmds = self.objects.pop(args[:4], None)
        if cmds:
            self.zombies[args[:4]] = cmds

    def set(self, key, cmd):
        self.settings.pop(key, None)
        self.settings[key] = cmd
        if len(self.zombies) > 64:
            self.drop_unnamed_zombies()

    def drop_unnamed_zombies(self):
        """Forget destroyed objects no kept setting names (by handle value: may keep a few too many)."""
        named = set()
        for cmd in self.settings.values():
            named.update(cmd[i:i + 4] for i in range(4, len(cmd), 4))
        self.zombies = {h: c for h, c in self.zombies.items() if h in named}

    def rebuild(self):
        """Commands that recreate this sub-context's objects and settings. Destroyed objects a setting
        still names are created first and destroyed again at the end, as they were."""
        self.drop_unnamed_zombies()
        out = [c for cmds in self.zombies.values() for c in cmds]
        out += [c for cmds in self.objects.values() for c in cmds]
        out += self.settings.values()
        for handle, cmds in self.zombies.items():
            obj = (struct.unpack_from("<I", cmds[0])[0] >> 8) & 0xff
            out.append(ccmd(DESTROY_OBJECT, obj, handle))
        return out


class Connection:
    """The state one program connection has built on its server, followed from what it sends."""

    def __init__(self):
        self.buf = b""
        self.setup = []                  # encoded vtest commands to send first
        self.resources = OrderedDict()   # id -> (encoded create command, index of its fd or None)
        self.next_id, self.free_ids = 1, []
        self.fd_waiting = []             # resource ids whose fd the server has yet to send, in order
        self.fds_seen = 0
        self.subs = {0: SubContext()}
        self.sub = 0
        self.problem = None              # why this connection cannot be rebuilt, if it cannot
        self.lock = threading.Lock()     # the keeper feeds requests and server fds from two threads

    # ------------------------------------------------------------------ feeding
    def from_client(self, data):
        with self.lock:
            self._from_client(data)

    def _from_client(self, data):
        if self.problem and self.problem.startswith("lost"):
            return
        self.buf += data
        pos = 0
        while len(self.buf) - pos >= 8:
            length, cmd = struct.unpack_from("<II", self.buf, pos)
            size = length if cmd == CREATE_RENDERER else 4 * length
            if cmd in (TRANSFER_GET, TRANSFER_PUT):  # protocol 1: data follows outside the length
                self.problem = "lost: protocol 1 transfers are not followed"
                self.buf = b""
                return
            if len(self.buf) - pos < 8 + size:
                break
            self.command(cmd, self.buf[pos:pos + 8 + size])
            pos += 8 + size
        self.buf = self.buf[pos:]

    def from_server_fds(self, n):
        """The server sent n file descriptors (they come in the order they were asked for)."""
        with self.lock:
            self._fds(n)

    def _fds(self, n):
        for _ in range(n):
            index = self.fds_seen
            self.fds_seen += 1
            if not self.fd_waiting:
                self.problem = self.problem or "the server sent a file descriptor no resource asked for"
                continue
            res = self.fd_waiting.pop(0)
            if res in self.resources:
                self.resources[res] = (self.resources[res][0], index)

    def command(self, cmd, raw):
        body = raw[8:]
        if cmd in SETUP:
            self.setup.append(raw)
        elif cmd in (RES_CREATE, RES_CREATE2, RES_CREATE_BLOB):
            handle = struct.unpack_from("<I", body)[0] if cmd != RES_CREATE_BLOB else 0
            if handle:
                res = handle  # protocol < 3: the program chose it
            elif self.free_ids:
                res = self.free_ids.pop()
            else:
                res, self.next_id = self.next_id, self.next_id + 1
            self.resources[res] = (raw, None)
            if cmd == RES_CREATE2 and struct.unpack_from("<I", body, 40)[0] or cmd == RES_CREATE_BLOB:
                self.fd_waiting.append(res)
            if cmd != RES_CREATE2:
                self.problem = self.problem or f"vtest command {cmd} (resource kind) is not rebuilt yet"
        elif cmd == RES_UNREF:
            res = struct.unpack_from("<I", body)[0]
            if self.resources.pop(res, None) is not None:
                self.free_ids.append(res)
        elif cmd == SUBMIT_CMD:
            self.submit(body)
        elif cmd not in NO_STATE:
            self.problem = self.problem or f"vtest command {cmd} is not followed"

    def submit(self, body):
        q = 0
        while q + 4 <= len(body):
            header = struct.unpack_from("<I", body, q)[0]
            c, obj, n = header & 0xff, (header >> 8) & 0xff, header >> 16
            raw, args = body[q:q + 4 + 4 * n], body[q + 4:q + 4 + 4 * n]
            q += 4 + 4 * n
            sub = self.subs.get(self.sub)
            if c == CREATE_SUB_CTX:
                self.subs[struct.unpack_from("<I", args)[0]] = SubContext()
            elif c == DESTROY_SUB_CTX:
                self.subs.pop(struct.unpack_from("<I", args)[0], None)
            elif c == SET_SUB_CTX:
                self.sub = struct.unpack_from("<I", args)[0]
            elif sub is None or c in ACTIONS:
                continue
            elif c == CREATE_OBJECT:
                sub.create(obj, args, raw)
            elif c == DESTROY_OBJECT:
                sub.destroy(args)
            elif c == BIND_OBJECT:
                sub.set((c, obj), raw)
            elif c == BIND_SHADER:
                sub.set((c, args[4:8]), raw)  # by shader type
            elif c == BEGIN_QUERY:
                sub.set((c, args[:4]), raw)
            elif c == END_QUERY:
                sub.settings.pop((BEGIN_QUERY, args[:4]), None)
            elif c in SETTINGS:
                sub.set((c, args[:4 * SETTINGS[c]]), raw)
            elif c in VIDEO:
                self.problem = self.problem or "video commands are not rebuilt"
            else:
                self.problem = self.problem or f"virgl command {c} is not followed"

    # ------------------------------------------------------------------ rebuilding
    def rebuild_commands(self):
        """The virgl command buffer that recreates every sub-context (resources must exist first)."""
        out = []
        for sid, sub in self.subs.items():
            if sid:
                out.append(ccmd(CREATE_SUB_CTX, 0, struct.pack("<I", sid)))
            out.append(ccmd(SET_SUB_CTX, 0, struct.pack("<I", sid)))
            out += sub.rebuild()
        out.append(ccmd(SET_SUB_CTX, 0, struct.pack("<I", self.sub)))
        return b"".join(out)

    def size(self):
        """Bytes kept (to check it stays small)."""
        n = sum(map(len, self.setup)) + sum(len(r) for r, _ in self.resources.values())
        for sub in self.subs.values():
            n += sum(len(c) for cmds in list(sub.objects.values()) + list(sub.zombies.values()) for c in cmds)
            n += sum(map(len, sub.settings.values()))
        return n

