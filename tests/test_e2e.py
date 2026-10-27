"""端到端测试：真实执行 curl，走完 搜索 -> 解析 -> 排序 -> 下载 -> 加载 全流程。"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

from lupa import LuaRuntime, LuaError

HERE = os.path.dirname(os.path.abspath(__file__))
LUA_PATH = os.path.normpath(os.path.join(HERE, os.pardir, "scripts", "javsubs.lua"))
# 故意用中文目录，复现并验证 Windows 下 ANSI 代码页打不开中文路径的问题
_BASE = tempfile.mkdtemp(prefix="javsubs_test_")
WORKDIR = os.path.join(_BASE, "中文字幕目录")
os.makedirs(WORKDIR, exist_ok=True)
VIDEO = os.path.join(WORKDIR, "SSNI-497.mp4")

# 去掉环境里的代理变量，模拟普通用户网络
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
M._calls = {}
M.commandv = function(...)
    local t = {...}
    table.insert(M._calls, t)
end
_G.JAVSUBS_TEST = true
_G.mp = M
"""


def main():
    L = LuaRuntime(unpack_returned_tuples=True)
    L.execute(STUB)
    g = L.globals()

    # ---- 用真实 subprocess 替换 mpv 的异步子进程接口 ----
    def command_native_async(cmd, cb):
        args = cmd["args"]
        argv = []
        i = 1
        while True:
            v = args[i]
            if v is None:
                break
            argv.append(str(v))
            i += 1
        if os.environ.get("DBG_ARGV"):
            print("ARGV:", argv)
        try:
            r = subprocess.run(argv, capture_output=True, env=ENV, timeout=60)
            status, out, err = r.returncode, r.stdout, r.stderr
            if argv[0].endswith("curl") or "curl" in argv[0]:
                pass
        except FileNotFoundError:
            return cb(False, None, "no such binary")
        except subprocess.TimeoutExpired:
            return cb(False, None, "timeout")

        res = L.table()
        res["status"] = status
        res["stdout"] = out.decode("utf-8", "replace")
        res["stderr"] = err.decode("utf-8", "replace")
        res["error_string"] = ""
        if os.environ.get("DBG_ARGV"):
            print("     -> status=", status, "stdout=", res["stdout"], "stderr=", res["stderr"][:120])
        cb(True, res, None)

    g.mp.command_native_async = command_native_async
    g.mp.get_property = lambda p: {  # noqa: E731
        "path": VIDEO,
        "filename/no-ext": "SSNI-497",
        "filename": "SSNI-497.mp4",
    }.get(str(p), "")
    g.mp.get_property_number = lambda p, d=0: 10389.32
    g.mp.command_native = lambda a: WORKDIR

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

    src = open(LUA_PATH, encoding="utf-8").read()
    try:
        L.execute(src)
    except LuaError as e:
        print("!! 加载失败:", e)
        return 1

    I = g.javsubs_internal
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            ok = False

    # ---- 1. 标题解析 ----
    q = I.build_query()
    q0 = q[0] if isinstance(q, tuple) else q
    print(f"     文件名 -> 查询词: {q0}")
    check("build_query 得到番号", str(q0) == "SSNI-497", q0)

    # ---- 2. 端到端搜索（真实联网）----
    if os.environ.get("GRACE_IMMEDIATE"):
        def _add_timeout(sec, fn):
            fn()
            t = L.table()
            t["kill"] = lambda *a: None
            return t
        g.mp.add_timeout = _add_timeout

    t0 = time.time()
    box = {}
    I.gather("SSNI-497",
             lambda r: box.setdefault("partial_n", len(r)),
             lambda r: box.setdefault("r", r))
    results = box.get("r")
    dt = time.time() - t0

    if "partial_n" in box:
        pn = int(box["partial_n"])
        print(f"     部分结果(回调时的条数): {pn} / 最终 {len(results) if results else 0}")
        check("部分结果 <= 最终结果", pn <= (len(results) if results else 0))
    if results is None:
        print("!! 没有收到最终结果")
        return 1
    check("搜索返回结果", results is not None and len(results) > 0,
          f"{len(results) if results else 0} 条 / {dt:.1f}s")

    if not results:
        return 1

    srcs = {}
    for i in range(1, len(results) + 1):
        r = results[i]
        srcs[str(r["source"])] = srcs.get(str(r["source"]), 0) + 1
    print("     来源统计:", srcs)
    if srcs.get("subtitlecat", 0) == 0:
        print("     ! SubtitleCat 本次无结果（该站国内不稳定/超时，属预期容错情况）")
    check("至少一个源有结果", sum(srcs.values()) > 0, srcs)

    print("     排序后 top5:")
    for i in range(1, min(len(results), 5) + 1):
        print(f"       {i}. {I.label_of(results[i])}   score={results[i]['score']}")

    top = results[1]
    check("Top1 是中文", str(top["lang"]) in ("zh-CN", "zh-TW"), top["lang"])
    dm = top["duration_match"]
    check("Top1 时长吻合", str(dm or "") == "吻合", dm)
    check("Top1 分数最高", all(
        results[i]["score"] <= top["score"] for i in range(1, len(results) + 1)))

    # ---- 3. 下载 + 加载 ----
    pycalls = []
    def py_commandv(name, *a):
        pycalls.append([str(name)] + [str(x) for x in a if x is not None])
        print("        >>> mpv:", pycalls[-1])
    g.mp.commandv = py_commandv
    before = len(g.mp._calls)
    I.load_subtitle(top)
    calls = pycalls
    print("     mpv 调用:", calls)
    check("调用了 sub-add select", any(c[:2] == ["sub-add", VIDEO.replace("SSNI-497.mp4", "")] or
                                       c[0] == "sub-add" and c[2] == "select" for c in calls), calls)

    saved = None
    for f in os.listdir(WORKDIR):
        if f.startswith("SSNI-497.") and f.split(".")[-1] in ("srt", "ass"):
            saved = os.path.join(WORKDIR, f)
    check("字幕文件已落盘", saved is not None, saved)

    if saved:
        raw = open(saved, "rb").read()
        check("文件非空", len(raw) > 1000, f"{len(raw)} bytes")
        try:
            text = raw.decode("utf-8")
            check("内容为 UTF-8", True)
        except UnicodeDecodeError:
            text = raw.decode("gbk", "replace")
            check("内容为 UTF-8", False, "实际是 GBK")
        han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        check("字幕含中文", han > 50, f"{han} 个汉字")
        print(f"     落盘: {os.path.basename(saved)}  {len(raw)} bytes  {han} 汉字")
        print("     首行:", text.strip().splitlines()[:1][0][:40] if text.strip() else "-")
        os.remove(saved)

    shutil.rmtree(_BASE, ignore_errors=True)
    print("\n结论:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
