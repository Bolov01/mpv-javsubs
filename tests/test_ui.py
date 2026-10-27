"""模拟用户路径：按快捷键 -> 搜索 -> 弹列表 -> 选第 1 条 -> 字幕挂上。"""
import os
import shutil
import subprocess
import sys
import tempfile

from lupa import LuaRuntime, LuaError

HERE = os.path.dirname(os.path.abspath(__file__))
LUA_PATH = os.path.normpath(os.path.join(HERE, os.pardir, "scripts", "javsubs.lua"))
_BASE = tempfile.mkdtemp(prefix="javsubs_ui_")
WORKDIR = os.path.join(_BASE, "中文字幕")
os.makedirs(WORKDIR, exist_ok=True)
VIDEO = os.path.join(WORKDIR, "SSNI-497.mp4")
ENV = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}

STUB = r"""
local M = {}
local function newmod()
    local t = {}
    for _, n in ipairs{'info','warn','error','verbose','debug','trace','fatal'} do
        t[n] = function(...) end
    end
    return t
end
package.preload['mp'] = function() return M end
package.preload['mp.msg'] = function() return newmod() end
package.preload['mp.utils'] = function() return _G.__utils end
package.preload['mp.options'] = function()
    local o = {}
    function o.read_options(t, n) return t end
    return o
end
M.add_key_binding = function() end
M.register_script_message = function() end
M.register_event = function() end
M.add_timeout = function() return { kill = function() end } end
M.osd_message = function(_, t) end
M.set_property = function() end
M.commandv = function() end
_G.JAVSUBS_TEST = true
_G.mp = M
"""


def main():
    L = LuaRuntime(unpack_returned_tuples=True)
    L.execute(STUB)
    g = L.globals()

    calls = []
    osd_msgs = []

    def command_native_async(cmd, cb):
        args = cmd["args"]
        argv, i = [], 1
        while True:
            v = args[i]
            if v is None:
                break
            argv.append(str(v))
            i += 1
        r = subprocess.run(argv, capture_output=True, env=ENV, timeout=90)
        res = L.table()
        res["status"] = r.returncode
        res["stdout"] = r.stdout.decode("utf-8", "replace")
        res["stderr"] = r.stderr.decode("utf-8", "replace")
        cb(True, res, None)

    ut = L.table()

    def _read_file(p):
        p = str(p)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def _file_info(p):
        p = str(p)
        if not os.path.exists(p):
            return None
        t = L.table()
        t["is_dir"] = os.path.isdir(p)
        t["is_file"] = os.path.isfile(p)
        return t

    ut["read_file"] = _read_file
    ut["file_info"] = _file_info
    g["__utils"] = ut

    g.mp.command_native_async = command_native_async
    g.mp.get_property = lambda p: {"path": VIDEO, "filename/no-ext": "SSNI-497",
                                   "filename": "SSNI-497.mp4"}.get(str(p), "")
    g.mp.get_property_number = lambda p, d=0: 10389.32
    g.mp.command_native = lambda a: WORKDIR
    g.mp.commandv = lambda name, *a: calls.append([str(name)] + [str(x) for x in a])
    g.mp.set_property = lambda name, *a: calls.append(
        ["set_property", str(name)] + [str(x) for x in a])

    # --- 关键：模拟 mp.input.select，用户选第 1 项 ---
    picked = {}

    def fake_select(prompt, items, opts):
        n = len(items)
        lst = [str(items[i]) for i in range(1, n + 1)]
        picked["prompt"] = str(prompt)
        picked["items"] = lst
        submit = opts["submit"]
        submit(items[1], 1)  # 选第一项

    def fake_get(prompt, opts):
        picked["get"] = str(prompt)
        opts["submit"](opts["default_text"])

    inp = L.table()
    inp["select"] = fake_select
    inp["get"] = fake_get
    g.mp.input = inp

    L.execute(open(LUA_PATH, encoding="utf-8").read())
    I = g.javsubs_internal

    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            ok = False

    # ---- 按 Ctrl+Shift+s 的等价调用 ----
    try:
        I.do_search(False)
    except LuaError as e:
        print("!! do_search 抛错:", e)
        return 1

    check("弹出了选择列表", "items" in picked, picked.get("prompt"))
    print("     标题:", picked.get("prompt"))
    if "items" in picked:
        for i, s in enumerate(picked["items"][:4], 1):
            print(f"       {i}. {s}")
        check("列表里有字幕条目", len(picked["items"]) >= 2, len(picked["items"]))
        check("列表末项是手动搜索入口", "手动输入" in picked["items"][-1], picked["items"][-1])

    check("选中后调用了 sub-add", any(c[0] == "sub-add" for c in calls), calls)
    saved = [f for f in os.listdir(WORKDIR) if f.split(".")[-1] in ("srt", "ass")]
    check("字幕已保存到视频同目录", len(saved) == 1, saved)
    if saved:
        p = os.path.join(WORKDIR, saved[0])
        print(f"     {saved[0]}  {os.path.getsize(p)} bytes")
        check("文件名带语言标签（便于 mpv 下次自动加载）",
              ".zh-CN." in saved[0] or ".zh-TW." in saved[0], saved[0])

    shutil.rmtree(_BASE, ignore_errors=True)
    print("\n结论:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
