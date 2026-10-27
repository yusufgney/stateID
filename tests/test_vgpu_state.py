"""Following a vtest connection's state (stateid.vgpu_state) from hand-made command streams."""
import struct

from stateid import vgpu_state as vs


def vt(cmd, *dwords):
    return struct.pack(f"<II{len(dwords)}I", len(dwords), cmd, *dwords)


def create2(size=4096):
    # handle 0 (the server picks), target, format, bind, w, h, depth, array size, last level, samples, size
    return vt(vs.RES_CREATE2, 0, 2, 1, 0, 64, 64, 1, 1, 0, 0, size)


def unref(res):
    return vt(vs.RES_UNREF, res)


def cc(cmd, obj, *dwords):
    return vs.ccmd(cmd, obj, struct.pack(f"<{len(dwords)}I", *dwords))


def submit(*cmds):
    body = b"".join(cmds)
    return struct.pack("<II", len(body) // 4, vs.SUBMIT_CMD) + body


def commands(buf):
    """(cmd, obj, args) of each command in a virgl command buffer."""
    out, q = [], 0
    while q < len(buf):
        h = struct.unpack_from("<I", buf, q)[0]
        n = h >> 16
        out.append((h & 0xff, (h >> 8) & 0xff, struct.unpack_from(f"<{n}I", buf, q + 4)))
        q += 4 + 4 * n
    return out


def test_resource_ids_follow_the_servers_free_list():
    c = vs.Connection()
    c.from_client(create2() + create2() + create2() + unref(2))
    c.from_client(create2())  # the freed 2 comes back first
    assert list(c.resources) == [1, 3, 2]
    c.from_client(unref(1) + unref(3) + create2() + create2() + create2())
    assert sorted(c.resources) == [1, 2, 3, 4]
    assert list(c.resources)[-3:] == [3, 1, 4]  # last freed first, then a new one


def test_shared_memory_fds_belong_to_the_resources_that_asked():
    c = vs.Connection()
    c.from_client(create2(size=0) + create2() + create2())
    c.from_server_fds(1)
    c.from_server_fds(1)
    assert [fd for _, fd in c.resources.values()] == [None, 0, 1]
    assert c.problem is None
    c.from_server_fds(1)
    assert "no resource asked" in c.problem


def test_split_input_gives_the_same_state():
    stream = (vt(vs.CREATE_RENDERER) + create2() + submit(cc(vs.CREATE_OBJECT, 1, 7, 0), cc(vs.BIND_OBJECT, 1, 7))
              + create2() + unref(1))
    whole, parts = vs.Connection(), vs.Connection()
    whole.from_client(stream)
    for i in range(len(stream)):
        parts.from_client(stream[i:i + 1])
    assert list(whole.resources) == list(parts.resources) == [2]
    assert whole.rebuild_commands() == parts.rebuild_commands()


def test_settings_keep_their_last_value_and_objects_only_while_alive():
    c = vs.Connection()
    c.from_client(submit(
        cc(vs.CREATE_SUB_CTX, 0, 1), cc(vs.SET_SUB_CTX, 0, 1),
        cc(vs.CREATE_OBJECT, 1, 10, 0xb1),   # blend state 10
        cc(vs.CREATE_OBJECT, 1, 11, 0xb2),   # blend state 11
        cc(vs.BIND_OBJECT, 1, 10), cc(vs.BIND_OBJECT, 1, 11),
        cc(14, 0, 1, 2, 3, 4), cc(14, 0, 5, 6, 7, 8),  # SET_BLEND_COLOR twice
        cc(vs.CREATE_OBJECT, 8, 12, 3), cc(vs.DESTROY_OBJECT, 8, 12),  # a surface made and gone
        cc(8, 0, *range(12)),                # a draw: not kept
    ))
    cmds = commands(c.rebuild_commands())
    assert cmds.index((vs.CREATE_SUB_CTX, 0, (1,))) < cmds.index((vs.SET_SUB_CTX, 0, (1,))) < 3
    assert (vs.CREATE_OBJECT, 1, (10, 0xb1)) in cmds and (vs.CREATE_OBJECT, 1, (11, 0xb2)) in cmds
    assert [a for k, _, a in cmds if k == vs.BIND_OBJECT] == [(11,)]
    assert [a for k, _, a in cmds if k == 14] == [(5, 6, 7, 8)]
    assert not [a for k, _, a in cmds if k in (8, vs.DESTROY_OBJECT) or a[:1] == (12,)]
    assert cmds[-1] == (vs.SET_SUB_CTX, 0, (1,))


def test_a_destroyed_object_a_setting_still_names_is_recreated_then_destroyed():
    c = vs.Connection()
    c.from_client(submit(
        cc(vs.CREATE_OBJECT, 6, 20, 1), cc(vs.CREATE_OBJECT, 6, 21, 1),  # two sampler views
        cc(10, 0, 1, 0, 20, 21),   # SET_SAMPLER_VIEWS fragment, from slot 0: 20, 21
        cc(10, 0, 1, 1, 22),       # later slot 1 only: 21 stays named by the first one
        cc(vs.DESTROY_OBJECT, 6, 21),
    ))
    cmds = commands(c.rebuild_commands())
    made = cmds.index((vs.CREATE_OBJECT, 6, (21, 1)))
    used = cmds.index((10, 0, (1, 0, 20, 21)))
    gone = cmds.index((vs.DESTROY_OBJECT, 6, (21,)))
    assert made < used < gone


def test_a_long_shader_keeps_all_its_parts():
    c = vs.Connection()
    c.from_client(submit(
        cc(vs.CREATE_OBJECT, vs.OBJECT_SHADER, 30, 1, 100, 25, 0xaaaa),
        cc(vs.CREATE_OBJECT, vs.OBJECT_SHADER, 30, 1, vs.SHADER_OFFSET_CONT | 60, 25, 0xbbbb),
    ))
    parts = [a for k, _, a in commands(c.rebuild_commands()) if k == vs.CREATE_OBJECT]
    assert [p[-1] for p in parts] == [0xaaaa, 0xbbbb]


def test_what_is_not_followed_is_reported():
    c = vs.Connection()
    c.from_client(submit(cc(55, 0, 1)))  # a video command
    assert "video" in c.problem
    c = vs.Connection()
    c.from_client(vt(vs.TRANSFER_PUT, *range(11)) + b"data")
    assert c.problem.startswith("lost")
