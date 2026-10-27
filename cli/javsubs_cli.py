#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
javsubs-cli —— 批量下载文件夹内视频对应的中文字幕（srt）

单文件、只用标准库，不依赖 mpv / ffmpeg。
字幕源：迅雷字幕公开接口 + SubtitleCat 网页解析（与 mpv 脚本同一套逻辑）。

来源 / 致谢：
    PotPlayer 插件 PotplayerJavChineseSubs_subtitlecat（MIT）
    https://github.com/736368217/PotplayerJavChineseSubs_subtitlecat
    Copyright (c) 2024 Exhen & Contributors
    番号识别 / 标题清理 / 源站解析规则来自上游；本文件为其 CLI 重写版。

用法示例：
    python javsubs_cli.py "D:\\videos"
    python javsubs_cli.py "D:\\videos" -r --overwrite --jobs 6
    python javsubs_cli.py "D:\\videos" --dry-run -v
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
VERSION = "1.0"

XUNLEI_API = "https://api-shoulei-ssl.xunlei.com/oracle/subtitle"
SUBCAT_BASE = "https://www.subtitlecat.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".mov", ".ts", ".m2ts",
    ".rmvb", ".mpg", ".mpeg", ".m4v", ".vob", ".asf", ".ogv", ".divx", ".3gp",
    ".rm", ".f4v", ".mts",
}

QUALITY_TAGS = [
    "_4k", "_2k", "_8k", "_2160p", "_1080p", "_720p", "_480p", "_360p",
    "_hd", "_fhd", "_uhd", "_sd", "_hq", "_60fps",
    "-4k", "-2k", "-8k", "-2160p", "-1080p", "-720p", "-480p", "-360p",
    "-hd", "-fhd", "-uhd", "-sd", "-hq",
]

# 打分权重（与 mpv 脚本保持一致）
W_CODE, W_ZH_CN, W_ZH_TW, W_OTHER = 60, 40, 32, 12
W_LANG_EXPLICIT, W_DUR_EXACT, W_DUR_NEAR, W_DUR_OK, W_ASS, PEN_DUR_FAR = 6, 70, 45, 15, 3, -25


def _init_console():
    """
    固定输出编码，避免打包成 exe 后中文乱码：
      - 直接在控制台里跑：跟随控制台的代码页（Windows 中文系统是 cp936）
      - 输出被重定向/管道：用 UTF-8（终端、编辑器、管道工具通常按 UTF-8 解析）
    """
    enc = "utf-8"
    if os.name == "nt":
        try:
            import ctypes
            if sys.stdout.isatty():
                cp = ctypes.windll.kernel32.GetConsoleOutputCP()
                if cp and cp != 65001:
                    enc = f"cp{cp}"
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:
            pass


def log(msg, level=0, opt=None):
    """level: 0=总是, 1=详细, 2=调试"""
    if opt is not None and level > opt.verbose:
        return
    print(msg, flush=True)


# ----------------------------------------------------------------------------
# 标题清理 / 番号提取（与 mpv 脚本同一套规则）
# ----------------------------------------------------------------------------
def strip_brackets(s: str) -> str:
    for a, b in (("[", "]"), ("【", "】"), ("(", ")"), ("（", "）")):
        guard = 0
        while guard < 50:
            guard += 1
            i = s.find(a)
            if i < 0:
                break
            j = s.find(b, i + 1)
            if j < 0:
                break
            s = s[:i] + " " + s[j + len(b):]
    return s


def strip_quality(s: str) -> str:
    changed = True
    while changed:
        changed = False
        low = s.lower()
        for t in QUALITY_TAGS:
            if len(s) > len(t) and low.endswith(t):
                s = s[:-len(t)]
                changed = True
                break
    return s


def extract_code(s: str):
    """从标题里抽番号：SSNI-497 / ssni497 / SSNI-497-C → SSNI-497"""
    up = s.upper()
    m = re.search(r"(?<![A-Z0-9])([A-Z]{2,5})-?(\d{2,5})(?![A-Z0-9])", up)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"([A-Z]{2,5})-?(\d{2,5})", up)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def clean_title(name: str) -> str:
    """
    复刻 mpv 脚本的 CleanTitle：
    去扩展名 → 去括号内容 → 去站点前缀(xxx.com@) → 去画质标签 → 抽番号
    顺序很重要：必须先去掉站点前缀，否则 hhd800.com@ABP-123 会被当成 HHD-800
    """
    s = name
    stem, ext = os.path.splitext(s)
    if ext.lower() in VIDEO_EXTS:
        s = stem
    s = strip_brackets(s)
    if "@" in s:
        s = s.split("@", 1)[1]
    s = strip_quality(s)
    s = re.sub(r"[_\-.]+$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return extract_code(s) or s


def norm_code(code: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (code or "").upper())


# ----------------------------------------------------------------------------
# 时长探测（MP4 / MKV / AVI），用于判断字幕与视频是否对得上
# ----------------------------------------------------------------------------
def _mp4_duration(f):
    size = os.fstat(f.fileno()).st_size

    def boxes(start, end):
        pos = start
        while pos + 8 <= end:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                return
            bsize = int.from_bytes(hdr[:4], "big")
            typ = hdr[4:8]
            hlen = 8
            if bsize == 1:
                bsize = int.from_bytes(f.read(8), "big")
                hlen = 16
            elif bsize == 0:
                bsize = end - pos
            if bsize < hlen:
                return
            yield typ, pos + hlen, pos + bsize
            pos += bsize

    for typ, s, e in boxes(0, size):
        if typ == b"moov":
            for t2, s2, e2 in boxes(s, e):
                if t2 == b"mvhd":
                    f.seek(s2)
                    ver = f.read(1)[0]
                    f.read(3)
                    if ver == 1:
                        f.read(16)
                        ts, dur = struct.unpack(">IQ", f.read(12))
                    else:
                        f.read(8)
                        ts, dur = struct.unpack(">II", f.read(8))
                    if ts:
                        return dur / ts
            return None
    return None


def _ebml_id(buf, p):
    """EBML 元素 ID 是变长（1~4 字节），带长度标记位。"""
    if p >= len(buf):
        return None, p
    b = buf[p]
    for n, mask in ((1, 0x80), (2, 0x40), (3, 0x20), (4, 0x10)):
        if b & mask:
            return buf[p:p + n], p + n
    return None, p


def _ebml_size(buf, p):
    """EBML 数据长度是变长整数，首字节的高位标记 1 的位置决定字节数。"""
    if p >= len(buf):
        return None, p
    b = buf[p]
    mask, n = 0x80, 1
    while n <= 8 and not (b & mask):
        mask >>= 1
        n += 1
    if n > 8:
        return None, p
    val = b & (mask - 1)
    for i in range(1, n):
        if p + i >= len(buf):
            return None, p
        val = (val << 8) | buf[p + i]
    return val, p + n


def _parse_mkv_info(data: bytes, i: int):
    """尝试把偏移 i 处的 Info 元素解析出时长，失败返回 None。"""
    eid, p = _ebml_id(data, i)
    if eid != b"\x15\x49\xa9\x66":
        return None
    size, p = _ebml_size(data, p)
    if size is None or size <= 0 or p + size > len(data):
        return None
    end = p + size

    scale, duration = None, None
    while p < end:
        eid, p2 = _ebml_id(data, p)
        if not eid:
            return None
        esize, p3 = _ebml_size(data, p2)
        if esize is None or p3 + esize > end:
            return None
        val = data[p3:p3 + esize]
        if eid == b"\x2a\xd7\xb1":       # TimecodeScale（纳秒）
            scale = int.from_bytes(val, "big")
        elif eid == b"\x44\x89":          # Duration（浮点，单位 scale）
            if esize == 4:
                duration = struct.unpack(">f", val)[0]
            elif esize == 8:
                duration = struct.unpack(">d", val)[0]
        p = p3 + esize

    if duration and scale:
        return duration * scale / 1e9
    return None


def _mkv_duration(f):
    """
    找 Segment > Info 读时长。
    注意：SeekHead 里也存在同样的 15 49 A9 66 字节序列（作为索引目标），
    所以不能只取第一个命中，要逐个尝试直到解析成功。
    """
    data = f.read()
    start = 0
    while True:
        i = data.find(b"\x15\x49\xa9\x66", start)
        if i < 0:
            return None
        start = i + 4
        dur = _parse_mkv_info(data, i)
        if dur:
            return dur


def _avi_duration(f):
    f.seek(0)
    head = f.read(12)
    if b"AVI" not in head:
        return None
    f.seek(0)
    data = f.read(4096)
    i = data.find(b"avih")
    if i < 0:
        return None
    try:
        usec, = struct.unpack("<I", data[i + 8:i + 12])
        frames, = struct.unpack("<I", data[i + 24:i + 28])
        if usec and frames:
            return usec * frames / 1e6
    except Exception:
        pass
    return None


def probe_duration(path: str):
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            if ext in (".mp4", ".mov", ".m4v", ".3gp", ".f4v"):
                return _mp4_duration(f)
            if ext in (".mkv", ".webm", ".mka"):
                return _mkv_duration(f)
            if ext in (".avi", ".divx"):
                return _avi_duration(f)
    except Exception:
        return None
    return None


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Http:
    def __init__(self, timeout=20, proxy=None):
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if not os.environ.get("HTTP_PROXY") and not os.environ.get("HTTPS_PROXY") \
                and not os.environ.get("http_proxy") and not os.environ.get("https_proxy"):
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.timeout = timeout

    def get(self, url: str, referer=None):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "*/*",
        })
        if referer:
            req.add_header("Referer", referer)
        with self.opener.open(req, timeout=self.timeout) as r:
            return r.read()


# ----------------------------------------------------------------------------
# 字幕源
# ----------------------------------------------------------------------------
def xunlei_search(http: Http, query: str):
    url = f"{XUNLEI_API}?gcid=&cid=&name={urllib.parse.quote(query)}"
    try:
        data = json.loads(http.get(url).decode("utf-8", "replace"))
    except Exception as e:
        return [], f"迅雷请求失败: {e}"
    items, seen = [], set()
    for it in data.get("data") or []:
        u = it.get("url")
        if not u or not u.startswith("http"):
            continue
        key = it.get("cid") or u
        if key in seen:
            continue
        seen.add(key)
        ext = (it.get("ext") or "srt").lower()
        if ext == "ssa":
            ext = "ass"
        if ext not in ("srt", "ass"):
            continue
        langs = it.get("languages") or []
        langnote = "/".join(langs) if isinstance(langs, list) else str(langs)
        dur = it.get("duration") or 0
        items.append({
            "source": "迅雷",
            "name": (it.get("name") or query).strip() or query,
            "lang": "zh-TW" if "繁" in langnote else "zh-CN",
            "lang_explicit": ("简" in langnote or "繁" in langnote),
            "ext": ext,
            "url": u,
            "duration": dur / 1000.0 if dur else None,
        })
    return items, None


def _subcat_href_lang(href: str):
    stem = href.rsplit("/", 1)[-1]
    stem = re.sub(r"\.[a-z]+$", "", stem)
    m = re.search(r"-([a-z]{2}-[A-Z]{2})$", stem) or re.search(r"-([a-z]{2,3})$", stem)
    return m.group(1) if m else None


def subtitlecat_search(http: Http, query: str, prefer_langs, probe_limit=6):
    """返回 (items, err)。抓搜索结果页 → 并发探测详情页拿中文链接。"""
    try:
        html = http.get(f"{SUBCAT_BASE}/index.php?search={urllib.parse.quote(query)}").decode(
            "utf-8", "replace")
    except Exception as e:
        return [], f"SubtitleCat 请求失败: {e}"

    entries, seen = [], set()
    for m in re.finditer(r'href="([^"]*subs/[^"]*)"', html):
        href = re.sub(r"^https?://[^/]+/", "", m.group(1)).lstrip("/")
        if not href.endswith(".html") or href in seen:
            continue
        seen.add(href)
        gt = html.find(">", m.end())
        close = html.find("</a>", gt) if gt >= 0 else -1
        if close < 0:
            continue
        title = re.sub(r"<[^>]*>", "", html[gt + 1:close]).strip()
        if not title:
            continue
        row = html[close:html.find("</tr>", close) if html.find("</tr>", close) > 0 else close + 1500]
        lm = re.search(r'Languages</span><span class="sub-table__metric-value">(\d+)', row)
        entries.append({"path": href, "title": title,
                        "langs": int(lm.group(1)) if lm else 0})

    want = norm_code(extract_code(query) or query)
    entries.sort(key=lambda e: (want not in norm_code(e["title"]), -e["langs"]))

    items = []
    for e in entries[:probe_limit]:
        try:
            detail = http.get(f"{SUBCAT_BASE}/{e['path']}").decode("utf-8", "replace")
        except Exception:
            continue
        found = {}
        for m in re.finditer(r'href="(/subs/[^"]+\.(?:srt|ass|ssa))"', detail):
            href = m.group(1)
            lang = _subcat_href_lang(href) or "und"
            found.setdefault(lang, href)
        pick = next((l for l in prefer_langs if l in found), None)
        if not pick:
            continue
        items.append({
            "source": "SubtitleCat",
            "name": e["title"],
            "lang": pick,
            "lang_explicit": True,
            "ext": found[pick].rsplit(".", 1)[-1].lower(),
            "url": SUBCAT_BASE + found[pick],
            "duration": None,
        })
    return items, None


def score_items(items, query, code, vdur):
    want = norm_code(code or query)
    for it in items:
        s = 0
        if want and want in norm_code(it["name"]):
            s += W_CODE
        s += {"zh-CN": W_ZH_CN, "zh-TW": W_ZH_TW}.get(it["lang"], W_OTHER)
        if it.get("lang_explicit"):
            s += W_LANG_EXPLICIT
        it["dur_match"] = ""
        if vdur and it.get("duration"):
            d = abs(vdur - it["duration"])
            if d <= 1.0:
                s += W_DUR_EXACT
                it["dur_match"] = "时长吻合"
            elif d <= 3:
                s += W_DUR_NEAR
                it["dur_match"] = f"±{d:.0f}s"
            elif d <= 20:
                s += W_DUR_OK
                it["dur_match"] = f"±{d:.0f}s"
            elif d > 180:
                s += PEN_DUR_FAR
                it["dur_match"] = "时长不符"
            else:
                it["dur_match"] = f"±{d / 60:.0f}m"
        if it["ext"] == "ass":
            s += W_ASS
        it["score"] = s
    items.sort(key=lambda x: -x["score"])
    return items


# ----------------------------------------------------------------------------
# 字幕处理
# ----------------------------------------------------------------------------
def decode_text(raw: bytes):
    """返回 (text, encoding)。字幕源常见 UTF-8 / GBK。"""
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    for enc in ("gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace"), "utf-8(replace)"


_ASS_TIME = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[.,](\d{1,3})")


def _ass_ts_to_srt(ts: str):
    m = _ASS_TIME.match(ts.strip())
    if not m:
        return None
    h, mi, s, cs = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    ms = int(cs.ljust(3, "0")[:3])
    return f"{h:02d}:{mi:02d}:{s:02d},{ms:03d}"


def ass_to_srt(text: str) -> str:
    """把 ASS/SSA 转成 SRT：去特效标签、去绘图指令、合并换行。"""
    lines = text.splitlines()
    in_events = False
    fields = []
    out = []
    idx = 0
    for ln in lines:
        s = ln.strip()
        if not in_events:
            if s.lower().startswith("[events]"):
                in_events = True
            continue
        if not s:
            continue
        low = s.lower()
        if low.startswith("format:"):
            fields = [f.strip().lower() for f in s[7:].split(",")]
            continue
        if not low.startswith("dialogue:"):
            continue
        body = s[len("dialogue:"):].strip()
        parts = body.split(",", len(fields) - 1) if fields else body.split(",", 9)
        if not fields:
            fields = ["layer", "start", "end", "style", "name",
                      "marginl", "marginr", "marginv", "effect", "text"]
        try:
            d = dict(zip(fields, parts))
        except Exception:
            continue
        start = _ass_ts_to_srt(d.get("start", ""))
        end = _ass_ts_to_srt(d.get("end", ""))
        if not start or not end:
            continue
        txt = d.get("text", "")
        # 绘图指令（矢量图形）不是台词，直接丢弃
        if re.search(r"\{[^}]*\\p\d", txt):
            continue
        txt = re.sub(r"\{[^}]*\}", "", txt)
        txt = txt.replace("\\N", "\n").replace("\\n", "\n")
        txt = txt.replace("\\h", " ")
        txt = re.sub(r"[ \t]+", " ", txt).strip()
        if not txt:
            continue
        idx += 1
        out.append(f"{idx}\n{start} --> {end}\n{txt}\n")
    return "\n".join(out)


_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")


def _srt_ts(h, m, s, ms):
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms.ljust(3, '0')[:3]}"


def normalize_srt(text: str):
    """
    把不规范的字幕重排成标准 SRT：
      - 补/重排序号（迅雷上有不少文件干脆没有序号）
      - 时间戳分隔符统一成逗号（有的用点号，部分播放器不认）
      - 去掉首尾空行
    返回 (新文本, 有效条目数)。
    """
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", text)
    out = []
    for b in blocks:
        lines = b.split("\n")
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        m = _SRT_TIME.search(lines[ti])
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
        timing = f"{_srt_ts(h1, m1, s1, ms1)} --> {_srt_ts(h2, m2, s2, ms2)}"
        body = "\n".join(lines[ti + 1:]).strip("\n")
        if not body.strip():
            continue
        out.append(f"{len(out) + 1}\n{timing}\n{body}\n")
    return "\n".join(out), len(out)


def quality_problem(text: str):
    """内容可用性检查：损坏的乱码 / 误判成中文的日文字幕。返回原因或 None。"""
    if not text or not text.strip():
        return "空文件"
    bad = text.count("\ufffd")
    if bad > 20 and bad / max(len(text), 1) > 0.01:
        return f"内容损坏（{bad} 处乱码）"
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    kana = sum(1 for c in text if "\u3040" <= c <= "\u30ff")
    if kana > 200 and kana > han:
        return f"疑似日文（假名 {kana} / 汉字 {han}）"
    return None


def to_srt(raw: bytes, src_ext: str):
    """统一下载内容为 SRT 文本。"""
    text, enc = decode_text(raw)
    if src_ext == "srt":
        # 有些站点的 srt 行尾不规范，顺手统一成 \n
        return text.replace("\r\n", "\n").replace("\r", "\n"), enc
    return ass_to_srt(text), enc


# ----------------------------------------------------------------------------
# 单个视频处理
# ----------------------------------------------------------------------------
def find_existing(video_path, out_dir, tag_lang):
    stem = os.path.splitext(os.path.basename(video_path))[0]
    d = out_dir or os.path.dirname(video_path)
    if tag_lang:
        return [p for p in (os.path.join(d, f"{stem}.zh-CN.srt"),
                            os.path.join(d, f"{stem}.zh-TW.srt")) if os.path.isfile(p)]
    p = os.path.join(d, f"{stem}.srt")
    return [p] if os.path.isfile(p) else []


def process_video(video_path, opt, http):
    name = os.path.basename(video_path)
    stem = os.path.splitext(name)[0]
    res = {"file": name, "query": "", "status": "", "detail": "", "out": ""}

    query = clean_title(name)
    res["query"] = query
    if not query:
        res["status"] = "FAIL"
        res["detail"] = "无法从文件名得到关键词"
        return res

    existing = find_existing(video_path, opt.out_dir, opt.tag_lang)
    if existing and not opt.overwrite:
        res["status"] = "SKIP"
        res["detail"] = "已存在 " + os.path.basename(existing[0])
        return res

    vdur = probe_duration(video_path) if not opt.no_duration else None
    res["vdur"] = round(vdur, 1) if vdur else None

    items, errs = [], []
    srcs = opt.source.lower()
    if "xunlei" in srcs:
        it, e = xunlei_search(http, query)
        items += it
        if e:
            errs.append(e)
    if "subtitlecat" in srcs:
        it, e = subtitlecat_search(http, query, opt.prefer_langs, opt.probe_limit)
        items += it
        if e:
            errs.append(e)

    if not items:
        res["status"] = "FAIL"
        res["detail"] = "未找到字幕" + (f"（{'; '.join(errs)}）" if errs else "")
        return res

    code = extract_code(query)
    items = score_items(items, query, code, vdur)
    if opt.min_score and items[0]["score"] < opt.min_score:
        res["status"] = "FAIL"
        res["detail"] = f"匹配度过低（最高 {items[0]['score']} < {opt.min_score}）"
        return res

    if opt.dry_run:
        best = items[0]
        res["status"] = "DRY"
        res["detail"] = (f"{best['source']} {best['name']} [{best['ext']}/{best['lang']}"
                         f" score={best['score']} {best['dur_match']}] 共 {len(items)} 条候选")
        return res

    # 逐个候选尝试，坏的就换下一个（迅雷上确有源头就损坏的文件）
    tried = []
    for cand in items[:opt.tries]:
        try:
            raw = http.get(cand["url"], referer=SUBCAT_BASE if cand["source"] == "SubtitleCat" else None)
        except Exception as e:
            tried.append(f"{cand['source']} 下载失败: {e}")
            continue
        if not raw:
            tried.append(f"{cand['source']} 内容为空")
            continue
        srt, enc = to_srt(raw, cand["ext"])
        srt, ncues = normalize_srt(srt)
        if ncues == 0:
            tried.append(f"{cand['source']} 解析不出字幕条目")
            continue
        prob = quality_problem(srt)
        if prob:
            tried.append(f"{cand['source']} {prob}")
            continue

        # 写字幕
        out_dir = opt.out_dir or os.path.dirname(video_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        suffix = f".{cand['lang']}" if opt.tag_lang else ""
        out_path = os.path.join(out_dir, f"{stem}{suffix}.srt")
        if opt.bom:
            data = b"\xef\xbb\xbf" + srt.encode("utf-8")
        else:
            data = srt.encode("utf-8")
        with open(out_path, "wb") as f:
            f.write(data)

        han = sum(1 for c in srt if "\u4e00" <= c <= "\u9fff")
        res["status"] = "OK"
        res["out"] = out_path
        res["detail"] = (f"{cand['source']} {cand['ext'].upper()} {cand['lang']} "
                         f"score={cand['score']} {cand['dur_match']} "
                         f"{han}汉字 {ncues}条 源编码={enc}")
        return res

    res["status"] = "FAIL"
    res["detail"] = "候选全部不可用：" + "; ".join(tried) if tried else "下载失败"
    return res


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def collect_videos(paths, recursive):
    out = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            out.append(p)
            continue
        if not os.path.isdir(p):
            print(f"! 路径不存在: {p}", file=sys.stderr)
            continue
        if recursive:
            for root, _dirs, files in os.walk(p):
                for f in files:
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                        out.append(os.path.join(root, f))
        else:
            for f in sorted(os.listdir(p)):
                fp = os.path.join(p, f)
                if os.path.isfile(fp) and os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                    out.append(fp)
    return sorted(out)


def main(argv=None):
    _init_console()
    ap = argparse.ArgumentParser(
        prog="javsubs_cli",
        description="批量下载文件夹内视频对应的中文字幕（输出 srt）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  javsubs_cli.py "D:\\videos"
  javsubs_cli.py "D:\\videos" -r --overwrite -j 6
  javsubs_cli.py "D:\\videos" --dry-run -v
""")
    ap.add_argument("paths", nargs="+", help="文件夹或视频文件（可多个）")
    ap.add_argument("-r", "--recursive", action="store_true", help="递归子目录")
    ap.add_argument("-o", "--out-dir", help="字幕输出目录（默认与视频同目录）")
    ap.add_argument("-f", "--overwrite", action="store_true", help="覆盖已存在的字幕")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="并发处理数（默认 4）")
    ap.add_argument("--source", default="xunlei",
                    help="字幕源，逗号分隔：xunlei / xunlei,subtitlecat（默认只用迅雷，"
                         "SubtitleCat 在国内经常超时，批量时很慢）")
    ap.add_argument("--prefer-langs", default="zh-CN,zh-TW", help="语言优先顺序")
    ap.add_argument("--tries", type=int, default=3, help="每个视频最多尝试几个候选（默认 3）")
    ap.add_argument("--min-score", type=int, default=0, help="低于该分数视为无匹配")
    ap.add_argument("--tag-lang", action="store_true", help="文件名带语言标记：<视频>.zh-CN.srt")
    ap.add_argument("--bom", action="store_true", help="输出带 UTF-8 BOM（部分老播放器需要）")
    ap.add_argument("--probe-limit", type=int, default=6, help="SubtitleCat 详情页探测条数")
    ap.add_argument("--no-duration", action="store_true", help="不解析视频时长（更快）")
    ap.add_argument("--timeout", type=int, default=20, help="单请求超时秒数（默认 20）")
    ap.add_argument("--proxy", default="", help="代理，如 http://127.0.0.1:7890")
    ap.add_argument("--dry-run", action="store_true", help="只显示匹配结果，不下载")
    ap.add_argument("--json", dest="json_path", help="把结果写入 JSON 文件")
    ap.add_argument("-v", "--verbose", action="count", default=0, help="-v 详细 / -vv 调试")
    ap.add_argument("--version", action="version", version=f"javsubs-cli {VERSION}")
    opt = ap.parse_args(argv)

    videos = collect_videos(opt.paths, opt.recursive)
    if not videos:
        print("没有找到视频文件", file=sys.stderr)
        return 2

    print(f"javsubs-cli {VERSION}  共 {len(videos)} 个视频  "
          f"源={opt.source}  并发={opt.jobs}{'  [空跑]' if opt.dry_run else ''}")
    print("-" * 78)

    http = Http(timeout=opt.timeout, proxy=opt.proxy or None)
    results = []
    t0 = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=max(1, opt.jobs)) as pool:
        futs = {pool.submit(process_video, v, opt, http): v for v in videos}
        for fut in as_completed(futs):
            v = futs[fut]
            done += 1
            try:
                r = fut.result()
            except Exception as e:
                r = {"file": os.path.basename(v), "status": "FAIL",
                     "detail": f"异常: {e}", "query": ""}
            results.append(r)
            tag = {"OK": "✔", "SKIP": "·", "FAIL": "✘", "DRY": "?"}.get(r["status"], "?")
            vd = f" 时长={r.get('vdur')}s" if r.get("vdur") else ""
            print(f"[{done}/{len(videos)}] {tag} {r['file']}{vd}")
            if r["query"]:
                print(f"         关键词: {r['query']}")
            if r["detail"]:
                print(f"         {r['detail']}")

    dt = time.time() - t0
    cnt = {}
    for r in results:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    print("-" * 78)
    print(f"完成: 成功 {cnt.get('OK', 0)} / 跳过 {cnt.get('SKIP', 0)} / "
          f"失败 {cnt.get('FAIL', 0)}"
          + (f" / 空跑 {cnt.get('DRY', 0)}" if cnt.get("DRY") else "")
          + f"  用时 {dt:.1f}s")

    if opt.json_path:
        with open(opt.json_path, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "elapsed": round(dt, 1),
                       "counts": cnt, "items": results}, f, ensure_ascii=False, indent=2)
        print(f"报告: {opt.json_path}")

    return 0 if cnt.get("FAIL", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
