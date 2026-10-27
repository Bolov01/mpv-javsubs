"""用 lupa 载入 javsubs.lua，对真实抓取的 HTML / JSON 做解析验证。"""
import json as pyjson
import os
import sys
from lupa import LuaRuntime

HERE = os.path.dirname(os.path.abspath(__file__))
LUA_PATH = os.path.normpath(os.path.join(HERE, os.pardir, "scripts", "javsubs.lua"))
SRC = os.path.join(HERE, "fixtures")

STUB = r"""
local M = {}

local function newmod()
    local t = {}
    t.info = function(...) end
    t.warn = function(...) end
    t.error = function(...) end
    t.verbose = function(...) end
    t.debug = function(...) end
    return t
end

package.preload['mp'] = function() return M end
package.preload['mp.msg'] = function() return newmod() end

package.preload['mp.utils'] = function()
    return { join_path = function(a,b) return a..'/'..b end }
end

package.preload['mp.options'] = function()
    local o = {}
    function o.read_options(tbl, name)
        -- 模拟从 conf 读到的默认值：不再改动
        return tbl
    end
    return o
end

M.command_native_async = function(args, cb)
    -- 模拟“curl 不存在”，避免测试时真的联网
    if cb then cb(false, nil, 'stub') end
end
M.command_native = function(a)
    if type(a) == 'table' and a[1] == 'expand-path' then return '/tmp/x' end
    return nil
end
M.add_key_binding = function() end
M.register_script_message = function() end
M.register_event = function() end
M.add_timeout = function() end
M.osd_message = function() end
M.get_property = function(p) return 'SSNI-497.mp4' end
M.get_property_number = function(p, d) return 10389.3 end
M.commandv = function() end
M.set_property = function() end
M.input = nil

_G.JAVSUBS_TEST = true
"""


def main():
    L = LuaRuntime(unpack_returned_tuples=True)
    L.execute(STUB)
    with open(LUA_PATH, encoding="utf-8") as f:
        code = f.read()

    try:
        L.execute(code)
    except Exception as e:
        print("!! 脚本加载失败:", e)
        return 1

    I = L.globals().javsubs_internal
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + ("  " + str(extra) if extra else ""))
        if not cond:
            ok = False

    # ---- 1. 番号提取 / 标题清理 ----
    cases = [
        ("SSNI-497.mp4", "SSNI-497"),
        ("ssni-497.mkv", "SSNI-497"),
        ("[ThZu.Cc]SSNI-497-C.mp4", "SSNI-497"),
        ("hhd800.com@ABP-123_1080p.mp4", "ABP-123"),
        ("IPX001-4K.mkv", "IPX-001"),
        ("SSIS-001-C.mp4", "SSIS-001"),
        ("Game.of.Thrones.S01E02.mkv", "Game.of.Thrones.S01E02"),
    ]
    for fn, want in cases:
        got = I.clean_title(fn)
        check(f"番号/标题 {fn}", got == want, f"got={got!r} want={want!r}")

    # ---- 2. 语言识别 ----
    for href, want in [
        ("/subs/169/SSNI-497-zh-CN.srt", "zh-CN"),
        ("/subs/191/SSNI-497-zh-TW.srt", "zh-TW"),
        ("/subs/161/SSNI-497-en.srt", "en"),
        ("/subs/379/x-af.srt", "af"),
    ]:
        got = I.lang_from_href(href)
        check(f"语言 {href}", got == want, f"got={got!r}")

    # ---- 3. 搜索结果页解析（真实 HTML）----
    html = open(SRC + "/sc.html", encoding="utf-8", errors="ignore").read()
    entries = I.parse_sc_search(html)
    n = len(entries)
    print(f"     搜索页解析出 {n} 个条目")
    for i in range(1, min(n, 5) + 1):
        e = entries[i]
        print(f"       - {e['path']}  |  {e['title'][:50]}")
    check("搜索页条目数 > 0", n > 0, n)
    check("条目 path 均为 subs/xxx.html 且无域名", all(
        str(entries[i]["path"]).endswith(".html")
        and "http" not in str(entries[i]["path"])
        and "subs/" in str(entries[i]["path"])
        for i in range(1, n + 1)))

    # ---- 4. 详情页解析（真实 HTML）----
    dhtml = open(SRC + "/d.html", encoding="utf-8", errors="ignore").read()
    langs = I.parse_sc_detail(dhtml)
    keys = sorted(str(k) for k in langs.keys())
    print(f"     详情页语言数 {len(keys)}: {keys[:6]} ...")
    check("详情页含 zh-CN", "zh-CN" in keys)
    check("详情页含 zh-TW", "zh-TW" in keys)
    if "zh-CN" in keys:
        print("       zh-CN ->", langs["zh-CN"]["href"], langs["zh-CN"]["ext"])

    lang, info = I.choose_lang(langs)
    check("优先语言选中 zh-CN", lang == "zh-CN", lang)

    # ---- 5. 迅雷 JSON 解析（真实返回体）----
    with open(SRC + "/xunlei.json", encoding="utf-8") as f:
        raw = f.read()
    data = I.json_decode(raw)
    check("JSON 解析出 data 数组", data is not None and len(data["data"]) > 0)
    if data is not None:
        first = data["data"][1]
        print(f"       第1条: ext={first['ext']} duration={first['duration']} name={first['name']}")
        check("JSON 字段 url 存在", str(first["url"]).startswith("http"))
        pyjson.loads(raw)  # 交叉验证：Python 也能解析同一份
        print("       交叉验证：Python json 解析同一返回体 OK")

    # ---- 6. 排序打分 ----
    results = L.table()
    mk = L.eval("function(t) return t end")
    items = [
        {"source": "xunlei", "title": "SSNI-497.srt", "lang": "zh-CN", "ext": "srt",
         "url": "u1", "duration": 10389.0},
        {"source": "xunlei", "title": "SSNI-497.ass", "lang": "zh-CN", "ext": "ass",
         "url": "u2", "duration": 15000.0},
        {"source": "subtitlecat", "title": "SSNI-497", "lang": "zh-TW", "ext": "srt",
         "url": "u3", "duration": None},
    ]
    tbl = L.eval("function(list) return list end")(L.table(*[
        L.table(**{k: v for k, v in it.items() if v is not None}) for it in items
    ]))
    scored = I.score_results(tbl, "SSNI-497", "SSNI-497")
    out = []
    for i in range(1, 4):
        r = scored[i]
        out.append((str(r["title"]), int(r["score"]), str(r["lang"])))
        print(f"       {I.label_of(r)}  score={r['score']}")
    check("时长吻合的排第一", out[0][1] == max(x[1] for x in out))

    # ---- 7. HTML 实体 / URL 编码 ----
    check("实体解码", str(I.html_unescape("A &amp; B &#039;c&#039;")) == "A & B 'c'",
          I.html_unescape("A &amp; B &#039;c&#039;"))
    check("URL 编码", str(I.urlencode("SSNI-497 测试")) == "SSNI-497%20%E6%B5%8B%E8%AF%95",
          I.urlencode("SSNI-497 测试"))

    # ---- 8. 域名互换 ----
    a = I.swap_www("https://www.subtitlecat.com/subs/1/x.html")
    b = I.swap_www("https://subtitlecat.com/subs/1/x.html")
    check("www 互换", "//subtitlecat.com/" in str(a) and "//www.subtitlecat.com/" in str(b),
          f"{a} | {b}")

    print("\n结论:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
