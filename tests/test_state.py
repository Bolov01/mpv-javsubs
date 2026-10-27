"""
搜索/选择状态机测试（离线，用 lupa 真实执行 Lua 脚本，stub 掉 mpv 接口）

覆盖「超时未选择后无法再次进入选择」这个问题的四种场景：
  1) 列表超时未选 -> 数字键/Esc 必须被释放（否则 Esc 被永久劫持）
  2) 按数字键 -> 正常回调，且按键同样被释放
  3) 已有结果时再按键 -> 直接重开列表，不再联网（秒回）
  4) 上一轮搜索卡住（源不返回）-> 再按键能作废旧轮、重新发起搜索；
     且兜底闹钟到点后 searching 一定回落到 false
"""
import json
import os
import sys

from lupa import LuaRuntime

HERE = os.path.dirname(os.path.abspath(__file__))
LUA_PATH = os.path.normpath(os.path.join(HERE, os.pardir, "scripts", "javsubs.lua"))
FIX = os.path.join(HERE, "fixtures")

ok_all = True


def check(label, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(("PASS " if cond else "FAIL ") + label + (f"   [{detail}]" if detail and not cond else ""))


XUNLEI_JSON = open(os.path.join(FIX, "xunlei.json"), "rb").read()
SC_HTML = open(os.path.join(FIX, "sc.html"), "rb").read()


def build():
    L = LuaRuntime(unpack_returned_tuples=True)
    g = L.globals()

    # ---- 可观测的 mpv 状态 ----
    forced = {}          # name -> {key, fn}
    removed = []         # 被移除的绑定名
    timers = []          # {sec, fn, alive}
    osd_log = []
    curl_calls = []
    clock = [1000.0]     # mp.get_time()

    def add_forced(key, name=None, fn=None, opts=None):
        if callable(name) and fn is None:      # 省略 name
            name, fn = key, name
        if name is None:
            name = "anon-" + str(len(forced))
        forced[name] = {"key": str(key), "fn": fn}

    def remove_key(name):
        if name in forced:
            del forced[name]
        removed.append(str(name))

    def add_timeout(sec, fn):
        t = {"sec": float(sec), "fn": fn, "alive": True}
        t["kill"] = lambda *a: t.update(alive=False)
        timers.append(t)
        return t

    M = L.table()
    M["get_time"] = lambda: clock[0]
    M["add_timeout"] = add_timeout
    M["add_key_binding"] = lambda *a, **k: None
    M["add_forced_key_binding"] = add_forced
    M["remove_key_binding"] = remove_key
    M["register_script_message"] = lambda *a: None
    M["register_event"] = lambda *a: None
    M["osd_message"] = lambda text, dur=None: osd_log.append(str(text))
    M["set_property"] = lambda *a: None
    M["commandv"] = lambda *a: None
    M["get_property"] = lambda name, default=None: {
        "filename": "SSNI-497.mp4",
        "filename/no-ext": "SSNI-497",
        "path": r"C:\x\SSNI-497.mp4",
        "working-directory": r"C:\x",
    }.get(name, default or "")
    M["get_property_number"] = lambda name, default=0: 10389.0 if name == "duration" else default
    M["input"] = None            # 这台 mpv 就是 nil

    def command_native_async(cmd, cb):
        args = [str(x) for x in cmd["args"].values()] if not isinstance(cmd["args"], list) else cmd["args"]
        joined = " ".join(str(a) for a in args)
        if "--version" in joined:
            cb(True, {"status": 0, "stdout": "curl 8.0", "stderr": ""}, None)
            return
        curl_calls.append(joined)
        if HANG_MODE[0]:
            return            # 模拟源卡死：永不回调
        # 把 fixture 内容写到 -o 指定的临时文件
        out = None
        for i, a in enumerate(args):
            if str(a) == "-o":
                out = str(args[i + 1])
        body = XUNLEI_JSON if "xunlei" in joined else SC_HTML
        if out:
            with open(out, "wb") as f:
                f.write(body)
        code = "200" if "xunlei" in joined else "500"   # SubtitleCat 直接失败，省掉详情页探测
        cb(True, {"status": 0, "stdout": code, "stderr": ""}, None)

    M["command_native_async"] = command_native_async

    HANG_MODE = [False]

    def msgmod():
        t = L.table()
        for n in ("info", "warn", "error", "verbose", "debug"):
            t[n] = lambda *a, **k: None
        return t

    ut = L.table()

    def read_file(p):
        p = str(p)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def file_info(p):
        p = str(p)
        if not os.path.exists(p):
            return None
        t = L.table()
        t["is_dir"] = os.path.isdir(p)
        t["is_file"] = os.path.isfile(p)
        t["size"] = os.path.getsize(p)
        return t

    ut["read_file"] = read_file
    ut["file_info"] = file_info
    ut["getcwd"] = lambda: r"C:\x"

    g["mp"] = M
    g["__utils"] = ut
    g["__msg"] = msgmod()
    g["JAVSUBS_TEST"] = True

    L.execute("""
package.preload['mp'] = function() return mp end
package.preload['mp.msg'] = function() return __msg end
package.preload['mp.utils'] = function() return __utils end
package.preload['mp.options'] = function() return { read_options = function(t) return t end } end
""")

    src = open(LUA_PATH, encoding="utf-8").read()
    L.execute(src)

    return L, g, {
        "forced": forced, "removed": removed, "timers": timers,
        "osd": osd_log, "curl": curl_calls, "clock": clock, "HANG": HANG_MODE,
    }


def fire_timer(state, pred):
    """触发第一个满足条件的定时器（模拟时间到）"""
    for t in state["timers"]:
        if t["alive"] and pred(t):
            t["alive"] = False
            t["fn"]()
            return True
    return False


def fire_all(state, pred):
    """触发所有满足条件的定时器（含回调里新注册的）"""
    total = 0
    for _ in range(20):
        hit = [t for t in state["timers"] if t["alive"] and pred(t)]
        if not hit:
            break
        for t in hit:
            t["alive"] = False
            t["fn"]()
            total += 1
    return total


def main():
    # ---------- 场景 1：列表超时未选，按键要释放 ----------
    L, g, st = build()
    I = g.javsubs_internal
    picked = []
    I.pick_numbered("测试列表", L.table("A", "B", "C"), lambda i: picked.append(int(i)))
    keys = {v["key"] for v in st["forced"].values()}
    check("1 列表打开后绑定 1~3 + Esc", {"1", "2", "3", "Esc"} <= keys, keys)

    fired = fire_timer(st, lambda t: t["sec"] > 5)      # pick_timeout=20 -> life+0.5
    check("1 超时定时器被设置", fired)
    check("1 超时后按键全部释放（Esc 不再被劫持）", len(st["forced"]) == 0, list(st["forced"]))
    check("1 超时给出重新打开的提示", any("重新打开" in o for o in st["osd"]), st["osd"][-1:])
    check("1 超时不应触发选择回调", picked == [], picked)

    # ---------- 场景 2：按数字键 ----------
    L, g, st = build()
    I = g.javsubs_internal
    picked = []
    I.pick_numbered("测试列表", L.table("A", "B", "C"), lambda i: picked.append(int(i)))
    st["forced"]["javsubs-pick-2"]["fn"]()
    check("2 数字键触发回调", picked == [2], picked)
    check("2 选完后按键释放", len(st["forced"]) == 0, list(st["forced"]))

    # ---------- 场景 3：已有结果时再按键 -> 直接重开列表，不再联网 ----------
    L, g, st = build()
    I = g.javsubs_internal
    I.run_search("SSNI-497", True)
    # 让 grace 定时器立刻触发（1.5s）
    fire_timer(st, lambda t: 1.0 < t["sec"] < 2.0)
    st0 = I.state()
    check("3 首轮搜索拿到结果", int(st0["cached"]) > 0, st0["cached"])
    n_curl = len(st["curl"])

    keys_before = len(st["forced"])
    I.run_search("SSNI-497")           # 用户再按一次（不强制刷新）
    check("3 缓存命中：没有发起新的网络请求", len(st["curl"]) == n_curl,
          f"{n_curl} -> {len(st['curl'])}")
    check("3 缓存命中：列表重新打开", len(st["forced"]) > 0, keys_before)
    last_osd = st["osd"][-1] if st["osd"] else ""
    check("3 列表里带「重新搜索」入口", "重新搜索" in last_osd, last_osd[:80])

    # ---------- 场景 4：搜索卡死 ----------
    L, g, st = build()
    I = g.javsubs_internal
    st["HANG"][0] = True               # 两个源都不返回
    I.run_search("SSNI-497", True)
    s1 = I.state()
    check("4 卡住时处于搜索中", bool(s1["searching"]), s1)
    n_before = len(st["curl"])

    st["clock"][0] += 5.0              # 过了 5 秒，用户又按一次
    I.run_search("SSNI-497", True)
    s2 = I.state()
    check("4 再按键后重新发起了搜索", len(st["curl"]) > n_before, f"{n_before} -> {len(st['curl'])}")
    check("4 重新发起后仍在搜索中", bool(s2["searching"]), s2)
    check("4 轮次号递增（旧轮回调作废）", int(s2["seq"]) > int(s1["seq"]), (s1["seq"], s2["seq"]))

    # 兜底闹钟：把最大时长的定时器点掉，searching 必须回落
    fired = fire_all(st, lambda t: t["sec"] >= 30)
    check("4 兜底闹钟存在并被触发", fired > 0, fired)
    s3 = I.state()
    check("4 兜底触发后 searching=false（不会永久锁死）", not bool(s3["searching"]), s3)

    # 秒内连按两次不应重复发请求（防手抖）
    L, g, st = build()
    I = g.javsubs_internal
    st["HANG"][0] = True
    I.run_search("SSNI-497", True)
    n = len(st["curl"])
    st["clock"][0] += 0.3
    I.run_search("SSNI-497", True)
    check("4 1 秒内连按不重复请求", len(st["curl"]) == n, f"{n} -> {len(st['curl'])}")

    print("\n结论:", "全部通过" if ok_all else "有失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
