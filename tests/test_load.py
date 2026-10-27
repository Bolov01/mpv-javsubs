"""单独验证 load_subtitle（下载 -> 保存 -> sub-add）这一段。"""
import os
import shutil
import subprocess
import sys
import tempfile

from lupa import LuaRuntime, LuaError

HERE = os.path.dirname(os.path.abspath(__file__))
LUA_PATH = os.path.normpath(os.path.join(HERE, os.pardir, "scripts", "javsubs.lua"))
# 故意用中文目录，复现并验证 Windows 下 ANSI 代码页打不开中文路径的问题
_BASE = tempfile.mkdtemp(prefix="javsubs_test_")
WORKDIR = os.path.join(_BASE, "中文字幕目录")
os.makedirs(WORKDIR, exist_ok=True)
VIDEO = os.path.join(WORKDIR, "SSNI-497.mp4")
URL = "https://subtitle.v.geilijiasu.com/42/42/4242F8F6B36FC764329CE827E560CB8F3D83560C.srt"
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
M.add_timeout = function() end
M.osd_message = function() end
M.set_property = function() end
M.input = nil
M.commandv = function() end
_G.JAVSUBS_TEST = true
_G.mp = M
"""


def main():
    L = LuaRuntime(unpack_returned_tuples=True)
    L.execute(STUB)
    g = L.globals()

    calls = []

    def command_native_async(cmd, cb):
        args = cmd["args"]
        argv, i = [], 1
        while True:
            v = args[i]
            if v is None:
                break
            argv.append(str(v))
            i += 1
        r = subprocess.run(argv, capture_output=True, env=ENV, timeout=60)
        res = L.table()
        res["status"] = r.returncode
        res["stdout"] = r.stdout.decode("utf-8", "replace")
        res["stderr"] = r.stderr.decode("utf-8", "replace")
        cb(True, res, None)

    g.mp.command_native_async = command_native_async
    g.mp.get_property = lambda p: {"path": VIDEO, "filename/no-ext": "SSNI-497",
                                   "filename": "SSNI-497.mp4"}.get(str(p), "")
    g.mp.get_property_number = lambda p, d=0: 10389.32
    g.mp.command_native = lambda a: WORKDIR
    g.mp.commandv = lambda name, *a: calls.append([str(name)] + [str(x) for x in a])
    g.mp.set_property = lambda *a: calls.append(["set_property"] + [str(x) for x in a])

    # 模拟 mpv 的 utils.read_file / file_info（支持中文路径）
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
        t["size"] = os.path.getsize(p)
        return t
    ut["read_file"] = _read_file
    ut["file_info"] = _file_info
    g["__utils"] = ut

    L.execute(open(LUA_PATH, encoding="utf-8").read())
    I = g.javsubs_internal

    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            ok = False

    # 构造一条迅雷结果
    item = L.table()
    item["source"], item["title"], item["lang"] = "xunlei", "SSNI-497.srt", "zh-CN"
    item["ext"], item["url"] = "srt", URL

    for f in os.listdir(WORKDIR):
        if f.startswith("SSNI-497.") and f.split(".")[-1] in ("srt", "ass"):
            os.remove(os.path.join(WORKDIR, f))

    try:
        I.load_subtitle(item)
    except LuaError as e:
        print("!! load_subtitle 抛错:", e)
        return 1

    print("     mpv 调用:", calls)
    check("调用了 sub-add 且带 select",
          any(c[0] == "sub-add" and c[-1] == "select" for c in calls), calls)

    saved = [f for f in os.listdir(WORKDIR)
             if f.startswith("SSNI-497.") and f.split(".")[-1] in ("srt", "ass")]
    check("文件落盘", len(saved) == 1, saved)
    if saved:
        p = os.path.join(WORKDIR, saved[0])
        raw = open(p, "rb").read()
        utf8 = True
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            utf8 = False
            text = raw.decode("gb18030", "replace")
        han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        print(f"     {saved[0]}  {len(raw)} bytes  {han} 汉字  UTF-8={utf8}")
        check("内容正常", len(raw) > 1000 and han > 50)
        check("非 UTF-8 时已切换 sub-codepage", utf8 or any(
            c[0] == "set_property" and c[1] == "sub-codepage" for c in calls), calls)
        check("sub-add 路径与落盘一致",
              any(c[0] == "sub-add" and c[1].endswith(saved[0]) for c in calls), calls)
        os.remove(p)

    shutil.rmtree(_BASE, ignore_errors=True)
    print("\n结论:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
