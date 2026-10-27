--=============================================================================
-- javsubs.lua  ——  mpv 在线中文字幕搜索 / 下载脚本
--
-- 来源 / 致谢：
--   PotPlayer 插件 PotplayerJavChineseSubs_subtitlecat（MIT）
--   https://github.com/736368217/PotplayerJavChineseSubs_subtitlecat
--   Copyright (c) 2024 Exhen & Contributors
--   本文件是同许可证下的重写版本：番号识别、标题清理、两个源站的解析规则
--   来自上游；请求层、选择器、搜索状态机按 mpv 的脚本接口重新实现。
--
-- 数据源：
--   SubtitleCat  https://www.subtitlecat.com   （网页解析，含 zh-CN / zh-TW）
--   迅雷字幕      api-shoulei-ssl.xunlei.com    （公开 JSON 接口）
--
-- 快捷键：Ctrl+Alt+s  搜索并选择字幕
--         Ctrl+Alt+m  用文件名/番号再搜一次
-- 注意：不要用 Ctrl+Shift+字母。mpv 会把它归一化成 Ctrl+大写字母，
--       例如 Ctrl+Shift+s 等价于 Ctrl+S，会和「截屏」冲突。
--
-- 依赖：curl（Windows 10 1803+ 自带，Linux/macOS 一般自带）
-- 许可证：MIT，见仓库根目录 LICENSE
--=============================================================================

local mp    = require 'mp'
local msg   = require 'mp.msg'
local utils = require 'mp.utils'
local opt   = require 'mp.options'

--=============================================================================
-- 配置（script-opts/javsubs.conf）
--=============================================================================

local opts = {
    key_search             = "Ctrl+Alt+s",
    key_manual_search      = "Ctrl+Alt+m",

    auto_search            = false,   -- 打开文件时自动搜索（只提示，不弹菜单）
    auto_load_best         = false,   -- 自动加载“高置信度”字幕（同番号 + 时长吻合 + 中文）
    auto_load_min_score    = 120,     -- auto_load_best 的分数门槛

    sources                = "subtitlecat,xunlei",

    subtitlecat_base       = "https://www.subtitlecat.com",
    xunlei_api             = "https://api-shoulei-ssl.xunlei.com/oracle/subtitle",

    http_timeout           = 25,
    proxy                  = "",      -- 例：http://127.0.0.1:7890，留空表示用系统代理
    ignore_env_proxy       = false,   -- true = 忽略 http_proxy/https_proxy 环境变量
    user_agent             = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",

    prefer_langs           = "zh-CN,zh-TW",
    require_preferred_lang = true,    -- 只显示有中文字幕的条目
    detail_probe_limit     = 8,      -- 最多探测多少个 SubtitleCat 条目（每个要请求一次详情页）
    probe_concurrency      = 4,       -- 详情页并发数
    max_results            = 30,
    grace_seconds          = 1.5,     -- 快源返回后再等慢源多久；超时就先给部分结果
    pick_timeout           = 20,      -- 列表显示多少秒后自动关闭（关闭时一并释放按键）
    search_deadline        = 0,       -- 整轮搜索的兜底时限（秒），0 = 自动（http_timeout x 6，最少 30）

    save_dir               = "same",  -- same = 视频同目录；cache = mpv 配置目录下的 cache
    codepage_fix           = true,    -- 字幕不是 UTF-8 时自动设置 sub-codepage
    codepage_fallback      = "gb18030",
    filename_template      = "{name}.{lang}.{ext}",

    debug                  = false,
}

opt.read_options(opts, "javsubs")

local function is_true(v) return v == true end

--=============================================================================
-- 小工具
--=============================================================================

local function log(...)
    if is_true(opts.debug) then msg.info(...) end
end

local function osd(text, dur)
    mp.osd_message(text, dur or 3)
end

local function trim(s) return (s:gsub("^%s+", ""):gsub("%s+$", "")) end

local function urlencode(s)
    return (tostring(s):gsub("[^%w%-%_%.%~]", function(c)
        return string.format("%%%02X", string.byte(c))
    end))
end

local function utf8_char(cp)
    if cp < 0x80 then
        return string.char(cp)
    elseif cp < 0x800 then
        return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
    elseif cp < 0x10000 then
        return string.char(0xE0 + math.floor(cp / 0x1000),
                           0x80 + math.floor(cp / 0x40) % 0x40,
                           0x80 + cp % 0x40)
    else
        return string.char(0xF0 + math.floor(cp / 0x40000),
                           0x80 + math.floor(cp / 0x1000) % 0x40,
                           0x80 + math.floor(cp / 0x40) % 0x40,
                           0x80 + cp % 0x40)
    end
end

local HTML_ENTITIES = {
    ["amp"] = "&", ["quot"] = '"', ["lt"] = "<", ["gt"] = ">", ["apos"] = "'",
    ["nbsp"] = " ", ["rsquo"] = "'", ["lsquo"] = "'", ["ldquo"] = '"',
    ["rdquo"] = '"', ["hellip"] = "…", ["mdash"] = "—", ["ndash"] = "–",
}

local function html_unescape(s)
    if not s then return "" end
    -- 十六进制实体
    s = s:gsub("&#[xX](%x+);", function(h)
        local n = tonumber(h, 16)
        if n and n > 0 and n < 0x110000 then return utf8_char(n) end
        return ""
    end)
    -- 十进制实体
    s = s:gsub("&#(%d+);", function(d)
        local n = tonumber(d)
        if n and n > 0 and n < 0x110000 then return utf8_char(n) end
        return ""
    end)
    -- 命名实体
    s = s:gsub("&(%a+);", function(e) return HTML_ENTITIES[e] or ("&" .. e .. ";") end)
    return s
end

local function strip_tags(s)
    if not s then return "" end
    s = s:gsub("<[^>]->", "")
    s = s:gsub("%s+", " ")
    return trim(s)
end

-- 读文件
-- 注意：Windows 下 Lua 的 io.open 用的是 ANSI 代码页，遇到中文路径会直接失败，
-- 所以优先走 mpv 自带的 utils.read_file（内部按 UTF-8 转换，支持中文路径）。
local function read_file(path)
    if utils.read_file then
        local ok, data = pcall(utils.read_file, path)
        if ok and data then return data end
    end
    local f = io.open(path, "rb")
    if not f then return nil end
    local data = f:read("*a")
    f:close()
    return data
end

local function file_exists(path)
    if utils.file_info then
        local ok, info = pcall(utils.file_info, path)
        if ok then return info ~= nil end
    end
    local f = io.open(path, "rb")
    if f then f:close() return true end
    return false
end

local function remove_file(path)
    if not path or path == "" then return false end
    if os.remove(path) then return true end
    -- io 层删不掉（中文路径）时，借 mpv 起进程删，避免临时文件堆积
    if file_exists(path) and package.config:sub(1, 1) == "\\" then
        mp.command_native({
            name = "subprocess",
            args = { "cmd", "/c", "del", "/q", path },
            playback_only = false,
            capture_stdout = true,
            capture_stderr = true,
        })
    end
    return false
end

local tmp_seq = 0
local function tmpfile(tag)
    tmp_seq = tmp_seq + 1
    local dir = os.getenv("TEMP") or os.getenv("TMPDIR") or "/tmp"
    dir = dir:gsub("\\", "/"):gsub("/+$", "")
    return string.format("%s/javsubs_%s_%d_%d.tmp", dir, tag, os.time(), tmp_seq)
end

local function ensure_dir(path)
    if not path or path == "" then return false end
    if file_exists(path) then return true end
    if package.config:sub(1, 1) == "\\" then
        mp.command_native({
            name = "subprocess",
            args = { "cmd", "/c", "mkdir", path },
            playback_only = false,
            capture_stdout = true,
            capture_stderr = true,
        })
    else
        os.execute('mkdir -p "' .. path .. '"')
    end
    return file_exists(path)
end

local function dirname(path)
    local d = path:match("^(.*)[/\\][^/\\]*$")
    return d
end

local function expand_path(p)
    local r = mp.command_native({ "expand-path", p })
    return r or p
end

-- 通用并发池：items 列表，最多 limit 个 worker 同时跑
local function pool(items, limit, worker, on_done)
    local total = #items
    if total == 0 then on_done() return end
    limit = math.max(1, limit or 4)
    local idx, running, finished = 0, 0, 0

    local launch
    launch = function()
        while running < limit and idx < total do
            idx = idx + 1
            running = running + 1
            local item = items[idx]
            local called = false
            worker(item, function()
                if called then return end
                called = true
                running = running - 1
                finished = finished + 1
                if finished >= total then
                    on_done()
                else
                    launch()
                end
            end)
        end
    end
    launch()
end

--=============================================================================
-- 极简 JSON 解析（够用于迅雷返回体）
--=============================================================================

local json = {}

function json.decode(str)
    if type(str) ~= "string" then return nil end
    local pos = 1
    local len = #str

    local function skip_ws()
        local _, e = str:find("^[ \t\r\n]*", pos)
        pos = (e or pos - 1) + 1
    end

    local parse_value

    local function parse_string()
        pos = pos + 1 -- 跳过开引号
        local buf = {}
        while pos <= len do
            local c = str:sub(pos, pos)
            if c == '"' then
                pos = pos + 1
                return table.concat(buf)
            elseif c == "\\" then
                local n = str:sub(pos + 1, pos + 1)
                if n == "u" then
                    local hex = str:sub(pos + 2, pos + 5)
                    local cp = tonumber(hex, 16) or 0
                    pos = pos + 6
                    if cp >= 0xD800 and cp <= 0xDBFF then
                        -- 代理对
                        local hex2 = str:sub(pos + 2, pos + 5)
                        local lo = tonumber(hex2, 16)
                        if lo and lo >= 0xDC00 and lo <= 0xDFFF and str:sub(pos, pos + 1) == "\\u" then
                            cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00)
                            pos = pos + 6
                        end
                    end
                    buf[#buf + 1] = utf8_char(cp)
                else
                    local map = { n = "\n", t = "\t", r = "\r", b = "\b", f = "\f",
                                  ['"'] = '"', ["\\"] = "\\", ["/"] = "/" }
                    buf[#buf + 1] = map[n] or n
                    pos = pos + 2
                end
            else
                buf[#buf + 1] = c
                pos = pos + 1
            end
        end
        return nil
    end

    local function parse_number()
        local s, e = str:find("^-?%d+%.?%d*[eE]?[-+]?%d*", pos)
        if not s then return nil end
        local num = tonumber(str:sub(s, e))
        pos = e + 1
        return num
    end

    parse_value = function()
        skip_ws()
        if pos > len then return nil end
        local c = str:sub(pos, pos)
        if c == "{" then
            pos = pos + 1
            local obj = {}
            skip_ws()
            if str:sub(pos, pos) == "}" then pos = pos + 1 return obj end
            while pos <= len do
                skip_ws()
                if str:sub(pos, pos) ~= '"' then break end
                local key = parse_string()
                skip_ws()
                if str:sub(pos, pos) == ":" then pos = pos + 1 end
                obj[key] = parse_value()
                skip_ws()
                local d = str:sub(pos, pos)
                if d == "," then pos = pos + 1
                elseif d == "}" then pos = pos + 1 break
                else break end
            end
            return obj
        elseif c == "[" then
            pos = pos + 1
            local arr = {}
            skip_ws()
            if str:sub(pos, pos) == "]" then pos = pos + 1 return arr end
            while pos <= len do
                arr[#arr + 1] = parse_value()
                skip_ws()
                local d = str:sub(pos, pos)
                if d == "," then pos = pos + 1
                elseif d == "]" then pos = pos + 1 break
                else break end
            end
            return arr
        elseif c == '"' then
            return parse_string()
        elseif str:sub(pos, pos + 3) == "true" then
            pos = pos + 4 return true
        elseif str:sub(pos, pos + 4) == "false" then
            pos = pos + 5 return false
        elseif str:sub(pos, pos + 3) == "null" then
            pos = pos + 4 return nil
        else
            return parse_number()
        end
    end

    local ok, res = pcall(parse_value)
    if not ok then
        msg.warn("JSON 解析失败: " .. tostring(res))
        return nil
    end
    return res
end

--=============================================================================
-- HTTP（curl 子进程，异步，不阻塞播放）
--=============================================================================

local http = { probed = false, bin = nil, probing = false, waiters = {} }

local function probe_http(cb)
    local cands = { "curl", "curl.exe", "/usr/bin/curl", "/usr/local/bin/curl" }
    local i = 0
    local function next_one()
        i = i + 1
        if i > #cands then return cb(nil) end
        local bin = cands[i]
        mp.command_native_async({
            name = "subprocess",
            args = { bin, "--version" },
            capture_stdout = true,
            capture_stderr = true,
            playback_only = false,
        }, function(success, res)
            if success and res and res.status == 0 then
                log("curl 可用: " .. bin)
                cb(bin)
            else
                next_one()
            end
        end)
    end
    next_one()
end

local function with_http(cb)
    if http.probed then return cb(http.bin) end
    http.waiters[#http.waiters + 1] = cb
    if http.probing then return end
    http.probing = true
    probe_http(function(bin)
        http.probed, http.bin, http.probing = true, bin, false
        local waiters = http.waiters
        http.waiters = {}
        for _, fn in ipairs(waiters) do fn(bin) end
    end)
end

local function curl_base_args(bin, url)
    local args = {
        bin, "-sS", "-L",
        "--connect-timeout", "10",
        "--max-time", tostring(opts.http_timeout),
        "-A", opts.user_agent,
        "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
    }
    if opts.proxy and opts.proxy ~= "" then
        args[#args + 1] = "-x"
        args[#args + 1] = opts.proxy
    elseif is_true(opts.ignore_env_proxy) then
        args[#args + 1] = "--noproxy"
        args[#args + 1] = "*"
    end
    args[#args + 1] = url
    return args
end

-- www.subtitlecat.com <-> subtitlecat.com 互换
local function swap_www(url)
    local hosts = { "www.subtitlecat.com", "subtitlecat.com" }
    for _, h in ipairs(hosts) do
        if url:find("//" .. h .. "/", 1, true) then
            local other = (h == hosts[1]) and hosts[2] or hosts[1]
            return (url:gsub("//" .. h:gsub("%.", "%%.") .. "/", "//" .. other .. "/", 1))
        end
    end
    return nil
end

-- GET 到内存：cb(ok, code, body)
local function http_get(url, cb, _retried)
    with_http(function(bin)
        if not bin then
            return cb(false, "no-curl", nil)
        end
        local out = tmpfile("get")
        local args = curl_base_args(bin, url)
        args[#args + 1] = "-o"
        args[#args + 1] = out
        args[#args + 1] = "-w"
        args[#args + 1] = "%{http_code}"

        mp.command_native_async({
            name = "subprocess",
            args = args,
            capture_stdout = true,
            capture_stderr = true,
            playback_only = false,
        }, function(success, res)
            local body = read_file(out)
            remove_file(out)

            local code = 0
            if success and res and res.stdout then
                code = tonumber(res.stdout:match("%d+")) or 0
            end

            local ok = success and res and res.status == 0 and code >= 200 and code < 400

            if not ok and not _retried then
                local alt = swap_www(url)
                if alt then
                    log(string.format("请求失败(http=%s curl=%s)，换域名重试: %s",
                        tostring(code), tostring((res and res.status) or "?"), alt))
                    return http_get(alt, cb, true)
                end
            end

            if not ok then
                local st = (res and res.status) or "?"
                log(string.format("HTTP 失败 http=%s curl=%s %s", tostring(code), tostring(st), url))
                return cb(false, code, nil, st)
            end
            cb(true, code, body)
        end)
    end)
end

-- GET 到文件：cb(ok, code, path)
local function http_download(url, path, cb, _retried)
    with_http(function(bin)
        if not bin then return cb(false, "no-curl", nil) end
        local args = curl_base_args(bin, url)
        args[#args + 1] = "-o"
        args[#args + 1] = path

        mp.command_native_async({
            name = "subprocess",
            args = args,
            capture_stdout = true,
            capture_stderr = true,
            playback_only = false,
        }, function(success, res)
            local code = 0
            if success and res and res.stdout then
                code = tonumber(res.stdout:match("%d+")) or 0
            end
            local ok = success and res and res.status == 0 and file_exists(path)
            if ok then
                local data = read_file(path)
                local head = data and data:sub(1, 64) or ""
                -- 服务器返回 200 但内容是空/HTML 错误页时视为失败
                if head == "" or head:find("<html", 1, true) or head:find("<!DOCTYPE", 1, true) then
                    ok = false
                end
            end
            if not ok and not _retried then
                local alt = swap_www(url)
                if alt then return http_download(alt, path, cb, true) end
            end
            if not ok then return cb(false, code, nil) end
            cb(true, 200, path)
        end)
    end)
end

--=============================================================================
-- 标题清理 / 番号提取
--=============================================================================

local VIDEO_EXTS = {}
for ext in ([[mp4 mkv avi wmv flv webm mov ts m2ts rmvb mpg mpeg divx 3gp vob asf m4v rm ogm ogv mpls]]):gmatch("%S+") do
    VIDEO_EXTS[ext] = true
end

local QUALITY_TAGS = {
    "_4k", "_2k", "_8k", "_2160p", "_1080p", "_720p", "_480p", "_360p",
    "_hd", "_fhd", "_uhd", "_sd", "_hq", "_60fps",
    "-4k", "-2k", "-8k", "-2160p", "-1080p", "-720p", "-480p", "-360p",
    "-hd", "-fhd", "-uhd", "-sd", "-hq",
}

local function strip_quality_tags(s)
    local changed = true
    while changed do
        changed = false
        local low = s:lower()
        for _, tag in ipairs(QUALITY_TAGS) do
            if #s > #tag and low:sub(-#tag) == tag then
                s = s:sub(1, #s - #tag)
                changed = true
                break
            end
        end
    end
    return s
end

local function strip_brackets(s)
    local patterns = { { "%[", "%]" }, { "【", "】" }, { "(", ")" }, { "（", "）" } }
    for _, p in ipairs(patterns) do
        local guard = 0
        while true do
            guard = guard + 1
            if guard > 50 then break end
            local a = s:find(p[1], 1, true)
            if not a then break end
            local b = s:find(p[2], a + 1, true)
            if not b then break end
            s = s:sub(1, a - 1) .. " " .. s:sub(b + #p[2])
        end
    end
    return s
end

-- 从标题里抽番号，如 abp-123 / SSNI497 / IPX-001-C
local function extract_jav_code(s)
    local up = s:upper()
    local pos = 1
    local fallback = nil
    while pos <= #up do
        local a, b, letters, digits = up:find("(%u%u%u?%u?%u?)%-?(%d%d%d?%d?%d?)", pos)
        if not a then break end
        local prev = (a > 1) and up:sub(a - 1, a - 1) or ""
        local nxt = up:sub(b + 1, b + 1)
        local code = letters .. "-" .. digits
        if not prev:match("%w") and not nxt:match("%w") then
            return code
        end
        if not fallback then fallback = code end
        pos = a + 1
    end
    return fallback
end

-- 复刻原插件的 CleanTitle()
local function clean_title(raw)
    local s = raw or ""
    s = html_unescape(s)
    -- 去掉视频扩展名（注意：gsub 回调收到的是完整匹配，这里用捕获组只取扩展名本体）
    s = s:gsub("%.([^%.\\/]+)$", function(ext)
        if VIDEO_EXTS[ext:lower()] then return "" end
        return "." .. ext
    end)
    s = strip_brackets(s)
    -- 去掉 xxx.com@ 这类站点前缀
    local at = s:match("^.-@(.+)$")
    if at then s = at end
    s = strip_quality_tags(s)
    s = s:gsub("[_%-%.]+$", "")
    s = s:gsub("%s+", " ")
    s = trim(s)
    local code = extract_jav_code(s)
    if code then return code end
    return s
end

local function build_query()
    local name = mp.get_property("filename/no-ext") or mp.get_property("filename") or ""
    name = name:gsub("%?.*$", "")
    -- 先清理（去站点前缀 / 去标签）再抽番号，否则 hhd800.com@ABP-123 会被识别成 HHD-800
    return clean_title(name), name
end

-- 归一化番号，用于模糊比对：SSNI-497 → SSNI497
local function norm_code(code)
    return (code or ""):upper():gsub("[^%w]", "")
end

--=============================================================================
-- 数据源：SubtitleCat
--=============================================================================

local LANG_SET = {}
for code in ([[af sq am ar hy az eu be bn bs bg my ca ceb zh-CN zh-TW co hr cs da nl en eo et fi fr fy gl ka de el gu ht ha haw he hi hmn hu is ig id ga it ja jv kn kk km rw ko ku ky lo la lv lt lb mk mg ms ml mt mi mr mn ne no ny or ps fa pl pt pa ro ru sm st sn sd si sk sl so es su sw sv tl tg ta tt te th tr tk uk ur ug uz vi cy xh yi yo zu und]]):gmatch("%S+") do
    LANG_SET[code] = true
end

local function lang_from_href(href)
    local stem = href:match("([^/]+)$") or href
    stem = stem:gsub("%.%a+$", "")
    -- 先试两段式（zh-CN / zh-TW），再试单段（en / af / ceb）
    local two = stem:match("%-(%a%a%-%a%a)$")
    if two and LANG_SET[two] then return two end
    local one = stem:match("%-(%a%a%a?)$")
    if one and LANG_SET[one] then return one end
    return nil
end

-- 把 href 统一成 "161/SSNI-497.html" 这种相对路径
local function normalize_subs_href(href)
    href = href:gsub("^https?://[^/]+/", "")
    href = href:gsub("^/", "")
    return href
end

-- 解析搜索结果页
-- 返回 { path = "subs/161/SSNI-497.html", title = "SSNI-497",
--        langs = 18, downloads = 5 }
local function parse_sc_search(html)
    local out, seen = {}, {}
    local pos = 1
    while true do
        local s, e, href = html:find('href="([^"]-subs/[^"]-)"', pos)
        if not s then break end
        pos = e + 1
        href = normalize_subs_href(href)
        -- 只要条目页（.html），不要单个语言的字幕文件
        if href:match("%.html$") and not seen[href] then
            local gt = html:find(">", e, true)
            if gt then
                local close = html:find("</a>", gt, true)
                if close then
                    local text = strip_tags(html:sub(gt + 1, close - 1))
                    if text ~= "" then
                        seen[href] = true
                        -- 同一行里的语言数 / 下载数，用于决定优先探测哪条
                        local row_end = html:find("</tr>", close, true) or (close + 1500)
                        local row = html:sub(close, row_end)
                        local langs = tonumber(row:match(
                            'Languages</span><span class="sub%-table__metric%-value">(%d+)')) or 0
                        local dls = tonumber(row:match(
                            'Downloads</span><span class="sub%-table__metric%-value">(%d+)')) or 0
                        out[#out + 1] = {
                            path = href, title = text, langs = langs, downloads = dls,
                        }
                    end
                end
            end
        end
    end
    return out
end

-- 解析详情页，得到可用语言 { ["zh-CN"] = { href = "/subs/169/x-zh-CN.srt", ext = "srt" } }
local function parse_sc_detail(html)
    local langs = {}
    local pos = 1
    while true do
        local s, e, href = html:find('href="(/subs/[^"]-%.%a%a%a?)"', pos)
        if not s then break end
        pos = e + 1
        local real_ext = (href:match("%.([%a]+)$") or "srt"):lower()
        local lang = lang_from_href(href) or "und"
        if not langs[lang] then
            langs[lang] = { href = href, ext = real_ext }
        end
    end
    return langs
end

local function split_list(s)
    local out = {}
    for item in (s or ""):gmatch("[^,]+") do
        out[#out + 1] = trim(item)
    end
    return out
end

local function choose_lang(langs)
    for _, want in ipairs(split_list(opts.prefer_langs)) do
        if langs[want] then return want, langs[want] end
    end
    if not is_true(opts.require_preferred_lang) then
        for lang, info in pairs(langs) do
            if lang ~= "und" then return lang, info end
        end
    end
    return nil
end

local function subtitlecat_search(query, cb)
    local base = (opts.subtitlecat_base or ""):gsub("/+$", "")
    local url = base .. "/index.php?search=" .. urlencode(query)
    log("SubtitleCat 搜索: " .. url)

    http_get(url, function(ok, code, body)
        if not ok or not body or #body == 0 then
            msg.warn("SubtitleCat 搜索请求失败: " .. tostring(code))
            return cb({})
        end

        local entries = parse_sc_search(body)
        log(string.format("SubtitleCat 命中 %d 个条目", #entries))
        if #entries == 0 then return cb({}) end

        -- 排序后再探测：标题带番号的优先，其次语言数多（更可能含中文）、下载量高的
        local want = norm_code(extract_jav_code(query) or query)
        table.sort(entries, function(a, b)
            local ha = norm_code(a.title):find(want, 1, true) and 1 or 0
            local hb = norm_code(b.title):find(want, 1, true) and 1 or 0
            if ha ~= hb then return ha > hb end
            if (a.langs or 0) ~= (b.langs or 0) then return (a.langs or 0) > (b.langs or 0) end
            return (a.downloads or 0) > (b.downloads or 0)
        end)

        -- 截断后并发探测详情页（原插件是串行请求，这里并发）
        local todo = {}
        for i = 1, math.min(#entries, opts.detail_probe_limit) do
            todo[#todo + 1] = entries[i]
        end

        local results = {}
        pool(todo, opts.probe_concurrency, function(entry, done)
            local detail_url = base .. "/" .. entry.path
            http_get(detail_url, function(dok, _, dbody)
                if dok and dbody then
                    local langs = parse_sc_detail(dbody)
                    local lang, info = choose_lang(langs)
                    if lang then
                        results[#results + 1] = {
                            source   = "subtitlecat",
                            title    = entry.title,
                            lang     = lang,
                            ext      = info.ext,
                            url      = base .. info.href,
                            duration = nil,
                            note     = "SubtitleCat",
                        }
                    end
                end
                done()
            end)
        end, function()
            cb(results)
        end)
    end)
end

--=============================================================================
-- 数据源：迅雷字幕
--=============================================================================

local function xunlei_search(query, cb)
    local url = opts.xunlei_api .. "?gcid=&cid=&name=" .. urlencode(query)
    log("迅雷搜索: " .. url)

    http_get(url, function(ok, code, body)
        if not ok or not body then
            msg.warn("迅雷字幕请求失败: " .. tostring(code))
            return cb({})
        end

        local data = json.decode(body)
        if type(data) ~= "table" or type(data.data) ~= "table" then
            msg.warn("迅雷返回体无法解析")
            return cb({})
        end

        local results, seen = {}, {}
        for _, item in ipairs(data.data) do
            if type(item) == "table" and type(item.url) == "string" and item.url ~= "" then
                local key = item.cid or item.url
                if not seen[key] then
                    seen[key] = true
                    local ext = (item.ext or "srt"):lower()
                    if ext == "ssa" then ext = "ass" end
                    local name = item.name or query
                    name = trim(name:gsub("%s+", " "))
                    local langnote = ""
                    if type(item.languages) == "table" then
                        langnote = table.concat(item.languages, "/")
                    end
                    results[#results + 1] = {
                        source   = "xunlei",
                        title    = name,
                        lang     = (langnote:find("繁") and "zh-TW") or "zh-CN",
                        lang_explicit = (langnote:find("简") ~= nil or langnote:find("繁") ~= nil),
                        ext      = ext,
                        url      = item.url,
                        duration = item.duration and item.duration > 0 and (item.duration / 1000) or nil,
                        note     = "迅雷" .. (langnote ~= "" and ("·" .. langnote) or ""),
                    }
                end
            end
        end
        log(string.format("迅雷命中 %d 条（去重后）", #results))
        cb(results)
    end)
end

--=============================================================================
-- 排序 / 展示
--=============================================================================

local function video_duration()
    local d = mp.get_property_number("duration", 0)
    if d and d > 0 then return d end
    return nil
end

local function score_results(results, query, code)
    local vdur = video_duration()
    local want = norm_code(code or query)

    for _, r in ipairs(results) do
        local s = 0

        -- 番号匹配
        local rt = norm_code(r.title)
        if want ~= "" and rt:find(want, 1, true) then
            s = s + 60
        end

        -- 语言
        local langrank = { ["zh-CN"] = 40, ["zh-TW"] = 32 }
        s = s + (langrank[r.lang] or 12)
        -- 源站明确标了简体/繁体的，比"默认"更可信
        if r.lang_explicit then s = s + 6 end

        -- 时长吻合（视频时长 vs 字幕对应时长）
        r.duration_match = nil
        if vdur and r.duration then
            local diff = math.abs(vdur - r.duration)
            if diff <= 1.0 then
                s = s + 70
                r.duration_match = "吻合"
            elseif diff <= 3 then
                s = s + 45
                r.duration_match = string.format("±%ds", math.floor(diff + 0.5))
            elseif diff <= 20 then
                s = s + 15
                r.duration_match = string.format("±%ds", math.floor(diff + 0.5))
            elseif diff > 180 then
                s = s - 25
                r.duration_match = "不匹配"
            else
                r.duration_match = string.format("±%dm", math.floor(diff / 60 + 0.5))
            end
        end

        -- ass 略优（样式更完整）
        if r.ext == "ass" then s = s + 3 end

        r.score = s
    end

    table.sort(results, function(a, b)
        if a.score ~= b.score then return a.score > b.score end
        return (a.title or "") < (b.title or "")
    end)

    return results
end

local function label_of(r)
    local parts = {}
    local icon = (r.source == "xunlei") and "迅雷" or "猫"
    parts[#parts + 1] = "[" .. icon .. "]"
    parts[#parts + 1] = (r.title or "?"):sub(1, 46)
    parts[#parts + 1] = r.ext:upper()
    if r.lang == "zh-CN" then parts[#parts + 1] = "简中"
    elseif r.lang == "zh-TW" then parts[#parts + 1] = "繁中"
    elseif r.lang ~= "und" then parts[#parts + 1] = r.lang end
    if r.duration_match then parts[#parts + 1] = "时长" .. r.duration_match end
    return table.concat(parts, " · ")
end

--=============================================================================
-- 选择器
--=============================================================================

-- 这台 mpv 的 Lua 侧**没有** mp.input（实测 mp.input == nil），
-- 所以实际生效的是"OSD 列表 + 数字键"。它有两个必须自己兜住的问题：
--   1) 用户不选、列表超时消失后，绑过的 1~9 / Esc 必须一起撤掉，
--      否则 Esc 会被脚本永久占着（按 Esc 只弹"已取消"，退出全屏等功能全废）
--   2) 再次按键必须能重新打开列表，而不是毫无反应
local numbered = { names = {}, timer = nil }

local function clear_numbered()
    for _, name in ipairs(numbered.names) do
        pcall(function() mp.remove_key_binding(name) end)
    end
    numbered.names = {}
    if numbered.timer then
        pcall(function() numbered.timer:kill() end)
        numbered.timer = nil
    end
end

local function pick_numbered(prompt, items, on_select)
    clear_numbered()

    local lines = { prompt or "选择字幕：" }
    for i = 1, math.min(#items, 9) do
        lines[#lines + 1] = string.format("%d) %s", i, items[i])
    end
    if #items > 9 then
        lines[#lines + 1] = string.format("（仅显示前 9 条，共 %d 条）", #items)
    end
    lines[#lines + 1] = string.format("数字键选择 · Esc 取消 · %s 重新打开", opts.key_search)

    local life = math.max(6, tonumber(opts.pick_timeout) or 20)
    osd(table.concat(lines, "\n"), life)

    local function finish(index, note)
        clear_numbered()
        if note then osd(note, 3) end
        if index then on_select(index) end
    end

    local function bind(key, name, fn)
        numbered.names[#numbered.names + 1] = name
        mp.add_forced_key_binding(key, name, fn, { repeatable = false })
    end

    for i = 1, math.min(#items, 9) do
        bind(tostring(i), "javsubs-pick-" .. i, function() finish(i) end)
    end

    numbered.names[#numbered.names + 1] = "javsubs-pick-cancel"
    mp.add_forced_key_binding("Esc", "javsubs-pick-cancel", function()
        finish(nil, "已取消选择")
    end, { repeatable = false })

    -- OSD 消失后把按键还回去。用 life + 0.5 让"列表已关闭"的提示不被立刻覆盖。
    numbered.timer = mp.add_timeout(life + 0.5, function()
        numbered.timer = nil
        clear_numbered()
        osd(string.format("列表已超时关闭（未选择）\n按 %s 可重新打开", opts.key_search), 4)
    end)
end

local function pick(prompt, items, on_select)
    if #items == 0 then return end

    if mp.input and mp.input.select then
        -- 部分 mpv 构建提供 mp.input（原生菜单）；参数不被支持时会抛错，退回数字键
        local ok = pcall(mp.input.select, prompt, items, {
            default_item = 1,
            submit = function(_, index)
                if index and items[index] then on_select(index) end
            end,
        })
        if ok then return end
        msg.warn("mp.input.select 调用失败，退回数字键选择")
    end

    pick_numbered(prompt, items, on_select)
end

--=============================================================================
-- 下载与加载
--=============================================================================

-- 简单 UTF-8 合法性校验：用来判断下载到的字幕是不是非 UTF-8（多为 GBK）
local function is_valid_utf8(data)
    if not data then return false end
    local i, n = 1, #data
    while i <= n do
        local c = data:byte(i)
        if c < 0x80 then
            i = i + 1
        elseif c >= 0xC2 and c <= 0xDF then
            local c2 = data:byte(i + 1)
            if not c2 or c2 < 0x80 or c2 > 0xBF then return false end
            i = i + 2
        elseif c >= 0xE0 and c <= 0xEF then
            local c2, c3 = data:byte(i + 1), data:byte(i + 2)
            if not c2 or not c3 or c2 < 0x80 or c2 > 0xBF or c3 < 0x80 or c3 > 0xBF then
                return false
            end
            i = i + 3
        elseif c >= 0xF0 and c <= 0xF4 then
            local c2, c3, c4 = data:byte(i + 1), data:byte(i + 2), data:byte(i + 3)
            if not c2 or not c3 or not c4 then return false end
            if c2 < 0x80 or c2 > 0xBF or c3 < 0x80 or c3 > 0xBF
                or c4 < 0x80 or c4 > 0xBF then
                return false
            end
            i = i + 4
        else
            return false
        end
    end
    return true
end

-- 统计字节串出现次数
local function count_bytes(data, needle)
    local n, i, len = 0, 1, #needle
    while true do
        local s = data:find(needle, i, true)
        if not s then break end
        n = n + 1
        i = s + len
    end
    return n
end

-- 粗略统计汉字 / 假名的个数（按 UTF-8 首字节判断）
local function script_stats(data)
    local han, kana, i, n = 0, 0, 1, #data
    while i <= n do
        local c = data:byte(i)
        if c == 0xE3 then
            local c2 = data:byte(i + 1)
            if c2 == 0x81 or c2 == 0x82 then kana = kana + 1 end
            i = i + 3
        elseif c and c >= 0xE4 and c <= 0xE9 then
            han = han + 1
            i = i + 3
        else
            i = i + 1
        end
    end
    return han, kana
end

-- 检测字幕内容是否可用；正常返回 nil，异常返回原因
-- 迅雷上有不少源头就损坏的文件（整篇 U+FFFD），仅靠文件名/时长是看不出来的
local function subtitle_problem(data)
    if not data or #data == 0 then return "空文件" end
    local bad = count_bytes(data, "\239\191\189")
    if bad > 20 and bad * 3 > #data * 0.03 then
        return string.format("内容损坏（%d 处乱码替换符）", bad)
    end
    local han, kana = script_stats(data)
    if kana > 200 and kana > han then
        return string.format("疑似日文而非中文（假名 %d / 汉字 %d）", kana, han)
    end
    return nil
end

local function pick_save_path(movie_path, base_name, lang, ext)
    local dir    local mode = (opts.save_dir or "same"):lower()

    if mode == "same" and movie_path and not movie_path:match("^%a+://") then
        dir = dirname(movie_path)
    end
    if not dir or dir == "" then
        dir = expand_path("~~/cache/javsubs")
        ensure_dir(dir)
    end

    local fname = (opts.filename_template or "{name}.{lang}.{ext}")
        :gsub("{name}", base_name)
        :gsub("{lang}", lang or "und")
        :gsub("{ext}", ext)
    -- 文件名里不能出现路径分隔符
    fname = fname:gsub("[/\\]", "_")

    local path = dir .. "/" .. fname
    if file_exists(path) then
        path = dir .. "/" .. fname:gsub("%.%a+$", "") .. "_" .. os.time() .. "." .. ext
    end
    return path
end

local function load_subtitle(r)
    local movie_path = mp.get_property("path") or ""
    local base_name = mp.get_property("filename/no-ext") or "subtitle"
    base_name = base_name:gsub("[\\/:*?\"<>|]", "_")

    osd("正在下载字幕…", 10)

    local save_path = pick_save_path(movie_path, base_name, r.lang, r.ext)

    local function finish(ok, path)
        if not ok then
            osd("字幕下载失败", 4)
            return
        end

        local data = read_file(path)

        -- 迅雷的字幕很多是 GBK，mpv 默认按系统代码页解码，非中文系统会乱码。
        -- 这里探测一次：不是合法 UTF-8 就临时把 sub-codepage 指到 GB18030（GBK 的超集）。
        if is_true(opts.codepage_fix) and data and #data > 0 and not is_valid_utf8(data) then
            local cp = opts.codepage_fallback
            mp.set_property("sub-codepage", cp)
            msg.info("字幕不是 UTF-8，已设置 sub-codepage=" .. cp)
        end

        -- 内容校验：损坏的文件宁可不用，也别把满屏乱码挂上去
        local problem = subtitle_problem(data)
        if problem then
            msg.warn("字幕内容异常: " .. problem)
            if r.auto then
                -- 自动匹配的不加载，同时把文件删掉，
                -- 否则下次播放会被 mpv 的 sub-auto 当成正常字幕又挂上来
                remove_file(path)
                osd("自动匹配的字幕不可用：" .. problem .. "\n按 " .. opts.key_search .. " 手动选择", 6)
                return
            end
            osd("注意：" .. problem, 5)
        end

        mp.commandv("sub-add", path, "select")
        mp.set_property("sub-visibility", "yes")
        osd("已加载字幕：" .. (path:match("([^/\\]+)$") or path), 4)
        msg.info("字幕已加载: " .. path)
    end

    http_download(r.url, save_path, function(ok, code, path)
        if ok then return finish(true, path) end

        -- 没有 curl 时，mpv 自己也能直接加载 URL
        if code == "no-curl" and r.url:match("^https?://") then
            mp.commandv("sub-add", r.url, "select")
            osd("已直接加载网络字幕（未能保存到本地）", 4)
            return
        end
        finish(false)
    end)
end

--=============================================================================
-- 主流程
--=============================================================================

local last_results, last_query = {}, nil
local searching, search_started_at = false, 0
local active_query = nil      -- 当前正在搜的关键词（last_query 在搜索开始时会被清空）
local search_seq = 0          -- 每轮搜索自增；回调里比对，丢弃过期结果

-- 前置声明：这些函数互相调用
local manual_search, do_search, run_search, show_results

local function now_seconds()
    if mp.get_time then
        local t = mp.get_time()
        if t and t > 0 then return t end
    end
    return os.time()
end

-- 并发查询所有字幕源。
-- 迅雷通常 0.2~1 秒就回，SubtitleCat 要探测详情页，可能十几秒甚至更久。
-- 所以不等所有源：第一个源返回后只再等 grace_seconds 就给一次“部分结果”，
-- 剩下那个源回来时再给一次“最终结果”。慢源不会拖住菜单。
local function gather(query, on_partial, on_final)
    local wanted = {}
    for name in (opts.sources or ""):gmatch("[^,]+") do
        wanted[trim(name):lower()] = true
    end

    local all = {}
    local launched = {}
    if wanted.subtitlecat then
        launched[#launched + 1] = { label = "SubtitleCat", fn = subtitlecat_search }
    end
    if wanted.xunlei then
        launched[#launched + 1] = { label = "迅雷", fn = xunlei_search }
    end

    local pending = #launched
    local partial_done, final_done, grace, watchdog = false, false, nil, nil

    local function score()
        return score_results(all, query, extract_jav_code(query))
    end

    local function kill_timer(t)
        if t and t.kill then pcall(function() t:kill() end) end
    end

    local function finish(timed_out)
        if final_done then return end
        final_done = true
        kill_timer(grace) grace = nil
        kill_timer(watchdog) watchdog = nil
        on_final(score(), timed_out and true or false)
    end

    if pending == 0 then return finish(false) end

    -- 兜底闹钟：万一某个源的异步回调丢了（子进程被 mpv 杀掉、curl 卡死不返回等），
    -- 不能让 searching 永远为 true —— 那样用户再按键就毫无反应，只能重启播放器。
    local wait = tonumber(opts.search_deadline) or 0
    if wait <= 0 then
        wait = math.max(30, (tonumber(opts.http_timeout) or 25) * 6)
    end
    watchdog = mp.add_timeout(wait, function()
        watchdog = nil
        if final_done then return end
        msg.warn(string.format("搜索超过 %.0f 秒未完成，放弃等待剩余字幕源", wait))
        finish(true)
    end)

    -- 先把所有源都计入 pending 再启动：否则某个源若同步返回（例如没装 curl），
    -- pending 会提前归零，排序会在结果没集齐时跑完，后到的结果拿不到 score。
    for _, src in ipairs(launched) do
        src.fn(query, function(results)
            if final_done then return end
            for _, r in ipairs(results or {}) do all[#all + 1] = r end
            pending = pending - 1

            if pending <= 0 then
                return finish(false)
            end

            if not partial_done and not grace then
                grace = mp.add_timeout(math.max(0.3, tonumber(opts.grace_seconds) or 1.5), function()
                    grace = nil
                    if partial_done or final_done then return end
                    partial_done = true
                    if #all > 0 then on_partial(score()) end
                end)
            end
        end)
    end
end

-- 主流程（搜索与展示）

-- 数字键选择器一共只有 9 个键位（1~9），所以列表末尾的两个操作项必须算进去，
-- 否则结果一多，"重新搜索"就会被挤到 9 名之后、永远按不到。
local picker_is_numbered = not (mp.input and mp.input.select)

show_results = function(query, results, is_final, from_cache)
    last_results, last_query = results, query

    if #results == 0 then
        if not is_final then return end
        osd("未找到字幕：" .. query .. "\n试试 " .. opts.key_manual_search .. " 手动输入关键词", 5)
        return
    end

    local cap = opts.max_results
    if picker_is_numbered then cap = math.min(cap, 7) end   -- 7 条结果 + 2 个操作项 = 9 键位
    local limit = math.min(#results, cap)

    local shown = {}
    for i = 1, limit do shown[i] = label_of(results[i]) end
    shown[#shown + 1] = "—— 重新搜索（联网刷新）——"
    shown[#shown + 1] = "—— 手动输入关键词重新搜索 ——"

    local title = string.format("找到 %d 条字幕（%s）", #results, query)
    if limit < #results then
        title = title .. string.format("，列出前 %d 条", limit)
    end
    if not is_final then
        title = title .. " · 其他源仍在搜索"
    elseif from_cache then
        title = title .. " · 上次结果"
    end

    pick(title, shown, function(index)
        if index == limit + 1 then
            run_search(query, true)      -- 强制联网刷新
        elseif index == limit + 2 then
            manual_search()
        else
            load_subtitle(results[index])
        end
    end)
end

-- 统一的搜索流程：快源先给菜单，慢源回来只补一条提示，不打断用户
run_search = function(query, force)
    -- 已经有这个视频的结果就不要再联网跑一遍（慢源动辄 30~60 秒）。
    -- 这是"超时没选、再按一次却毫无反应"的修法：直接重开列表，秒回。
    if not force and last_query == query and #last_results > 0 then
        msg.verbose("复用上次结果，直接重新打开列表")
        show_results(query, last_results, true, true)
        return
    end

    if searching then
        local elapsed = now_seconds() - search_started_at
        if elapsed < 1.0 and active_query == query then
            osd("正在搜索中，请稍候…", 2)
            return
        end
        -- 用户又按了一次 = 明确要求重搜，旧的那一轮回调全部作废
        msg.info(string.format("上一轮搜索已进行 %.1f 秒，作废后重新开始", elapsed))
        search_seq = search_seq + 1
        searching = false
    end

    search_seq = search_seq + 1
    local seq = search_seq
    searching = true
    search_started_at = now_seconds()
    active_query = query
    last_results, last_query = {}, nil

    local shown = false

    local function present(results, is_final, timed_out)
        if seq ~= search_seq then return end     -- 过期回调，丢弃
        if is_final then
            searching = false
            active_query = nil
        end

        if #results == 0 then
            if is_final then
                show_results(query, results, true)
                if timed_out then osd("搜索超时，未拿到任何结果", 4) end
            end
            return
        end

        if shown then
            -- 列表已经开过，只更新数据并提示，不打断用户选择
            last_results, last_query = results, query
            if is_final then
                osd(string.format("搜索完成，共 %d 条，按 %s 可重新打开列表",
                    #results, opts.key_search), 4)
            end
            return
        end

        shown = true
        show_results(query, results, is_final)
        if timed_out then
            osd("搜索超时，仅显示已拿到的结果", 3)
        end
    end

    gather(query,
        function(results) present(results, false, false) end,
        function(results, timed_out) present(results, true, timed_out) end)
end

function manual_search()
    local default_text = (build_query())

    local function go(text)
        text = trim(text or "")
        if text == "" then return end
        osd("正在搜索：" .. text, 6)
        run_search(text, true)
    end

    if mp.input and mp.input.get then
        local ok = pcall(mp.input.get, "输入片名 / 番号后回车搜索", {
            default_text = default_text,
            submit = function(text) go(text) end,
        })
        if ok then return end
    end

    -- 没有 mp.input 时（本机 mpv 就是这样）退回用文件名再搜一次
    go(default_text)
end

function do_search(manual)
    if manual then return manual_search() end

    local query = build_query()
    if query == nil or query == "" then
        osd("拿不到文件名，无法搜索", 3)
        return
    end

    osd("正在搜索字幕：" .. query, 10)
    run_search(query)
end

-- 自动模式：不弹菜单，只在找到高置信度匹配时直接加载
local auto_loaded = false

local function auto_search_flow()
    local query = build_query()
    if not query or query == "" then return end
    auto_loaded = false

    local function try_auto(results)
        if auto_loaded or searching or #results == 0 then return false end
        local best = results[1]
        if is_true(opts.auto_load_best)
            and best.score and best.score >= tonumber(opts.auto_load_min_score) then
            auto_loaded = true
            best.auto = true
            msg.info(string.format("自动加载字幕：%s (score=%d)", label_of(best), best.score))
            load_subtitle(best)
            return true
        end
        return false
    end

    gather(query,
        -- 部分结果里就已经有高置信度匹配，直接用了，不必等慢源
        function(results) try_auto(results) end,
        function(results)
            if try_auto(results) then return end
            if #results == 0 then
                osd("自动搜索：未找到「" .. query .. "」的字幕", 3)
            else
                osd(string.format("找到 %d 条字幕，按 %s 选择", #results, opts.key_search), 4)
            end
        end)
end


--=============================================================================
-- 绑定
--=============================================================================

mp.add_key_binding(opts.key_search, "javsubs-search", function() do_search(false) end)
mp.add_key_binding(opts.key_manual_search, "javsubs-manual", function() do_search(true) end)

mp.register_script_message("javsubs-search", function() do_search(false) end)
mp.register_script_message("javsubs-manual", function() do_search(true) end)
mp.register_script_message("javsubs-reselect", function()
    if #last_results > 0 then
        local query = build_query()
        show_results(query, last_results, true, true)
    else
        do_search(false)
    end
end)

-- 预热 curl 探测，避免第一次搜索等太久
with_http(function(bin)
    if not bin then
        msg.warn("未找到 curl，请先安装 curl（Windows 10 1803+ 自带）")
    end
end)

-- 换片时清掉上一部的缓存结果，避免把别的视频的字幕列表端出来
mp.register_event("file-loaded", function()
    last_results, last_query = {}, nil
    searching, active_query = false, nil
    search_seq = search_seq + 1      -- 作废上一部影片还在路上的回调
    clear_numbered()
    if is_true(opts.auto_search) or is_true(opts.auto_load_best) then
        mp.add_timeout(1.0, auto_search_flow)
    end
end)

msg.verbose("javsubs 已加载：搜索键 = " .. opts.key_search)

--=============================================================================
-- 测试钩子：仅在设置了全局 JAVSUBS_TEST 时导出内部函数（正常使用无副作用）
--=============================================================================

if _G.JAVSUBS_TEST then
    _G.javsubs_internal = {
        clean_title        = clean_title,
        extract_jav_code   = extract_jav_code,
        norm_code          = norm_code,
        html_unescape      = html_unescape,
        strip_tags         = strip_tags,
        urlencode          = urlencode,
        parse_sc_search    = parse_sc_search,
        parse_sc_detail    = parse_sc_detail,
        lang_from_href     = lang_from_href,
        choose_lang        = choose_lang,
        json_decode        = json.decode,
        label_of           = label_of,
        score_results      = score_results,
        split_list         = split_list,
        is_valid_utf8      = is_valid_utf8,
        subtitle_problem   = subtitle_problem,
        script_stats       = script_stats,
        swap_www           = swap_www,
        -- 端到端测试用
        gather             = gather,
        build_query        = build_query,
        load_subtitle      = load_subtitle,
        subtitlecat_search = subtitlecat_search,
        xunlei_search      = xunlei_search,
        do_search          = do_search,
        manual_search      = manual_search,
        run_search         = run_search,
        pick               = pick,
        pick_numbered      = pick_numbered,
        clear_numbered     = clear_numbered,
        show_results       = show_results,
        score_results      = score_results,
        -- 供状态测试读取
        state              = function()
            return {
                searching = searching,
                seq       = search_seq,
                cached    = #last_results,
                query     = last_query,
                numbered  = #numbered.names,
            }
        end,
    }
end
