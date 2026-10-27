#!/usr/bin/env python3
"""
把 javsubs.lua 安装到 mpv-lazy（或任意 portable mpv），并把入口挂到右键菜单。

改动范围（全部是新增，不修改任何既有行）：
  1. <config>/scripts/javsubs.lua               新增
  2. <config>/script-opts/javsubs.conf          新增
  3. <config>/input_uosc.conf                   插入 1 行右键菜单项（带 .bak 备份）

用法：
  python install_to_mpv.py install   [--mpv-dir DIR]
  python install_to_mpv.py uninstall [--mpv-dir DIR]
  python install_to_mpv.py verify    [--mpv-dir DIR]
"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.normpath(os.path.join(HERE, os.pardir))
DEFAULT_MPV = r"D:\tools\音视频处理\MPV1\mpv-lazy"

MENU_KEY = "Ctrl+Alt+s"
MENU_CMD = "script-message javsubs-search"
MENU_TITLE = "工具 > 在线中文字幕搜索（SubtitleCat/迅雷）"
MARK = "javsubs"


def find_config(mpv_dir):
    """portable mpv 用 portable_config，否则用 %APPDATA%/mpv。"""
    p = os.path.join(mpv_dir, "portable_config")
    if os.path.isdir(p):
        return p
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "mpv")


def read(p):
    with open(p, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(s)


def menu_line(col_cmd, col_title):
    """复刻该配置文件的排版：key + 命令 + #! 菜单标题。"""
    line = MENU_KEY.ljust(col_cmd) + MENU_CMD
    return line.ljust(col_title) + "#! " + MENU_TITLE


def detect_columns(lines):
    """从已有的 工具 分组行里推断命令列与 #! 列。"""
    col_cmd, col_title = 19, 72
    for ln in lines:
        if "#!" in ln and "工具 >" in ln:
            i = ln.index("#!")
            col_title = i
            m = re.match(r"^(\S+)(\s+)", ln)
            if m:
                col_cmd = len(m.group(1)) + len(m.group(2))
    return col_cmd, col_title


def insert_menu(conf_path):
    text = read(conf_path)
    lines = text.split("\n")

    col_cmd, col_title = detect_columns(lines)
    newline = menu_line(col_cmd, col_title)

    # 已存在则原地更新（换了快捷键时要能覆盖旧行）
    for i, ln in enumerate(lines):
        if MARK in ln and "#!" in ln:
            if ln.rstrip() == newline.rstrip():
                return "exists", None
            backup = conf_path + ".bak." + time.strftime("%Y%m%d%H%M%S")
            shutil.copy2(conf_path, backup)
            old = ln
            lines[i] = newline
            write(conf_path, "\n".join(lines))
            return "updated", (newline, backup, i + 1, old)

    # 插到最后一个 “工具 >” 条目之后，保持分组完整
    idx = None
    for i, ln in enumerate(lines):
        if "#!" in ln and "工具 >" in ln:
            idx = i
    if idx is None:
        # 没有 工具 分组就插在第一个分组之前
        idx = -1
        for i, ln in enumerate(lines):
            if "#!" in ln:
                idx = i - 1
                break
        if idx < 0:
            idx = len(lines) - 1
    lines.insert(idx + 1, newline)

    backup = conf_path + ".bak." + time.strftime("%Y%m%d%H%M%S")
    shutil.copy2(conf_path, backup)
    write(conf_path, "\n".join(lines))
    return "inserted", (newline, backup, idx + 1)


# ---------------------------------------------------------------- 菜单结构比对
def parse_menu(lines):
    """复刻 uosc lib/menus.lua 的 get_menu_items() 解析逻辑。"""
    root = {"items": [], "by_cmd": {}}
    by_id = {}

    for raw in lines:
        m = re.match(r"\s*([\S]+)\s+([^#]*)\s*(.*?)\s*$", raw)
        if not m:
            continue
        key, command, comment = m.group(1), m.group(2).strip(), m.group(3)
        is_commented = key.startswith("#")
        if is_commented and len(key) > 1:
            continue

        if comment.startswith("#"):
            comment = comment[1:]
        if not (comment.startswith("!") or comment.startswith("menu:")):
            continue

        t = re.match(r"^!\s*(.*?)\s*$", comment) or re.match(r"^menu:\s*(.*?)\s*$", comment)
        title = t.group(1) if t else ""
        if not title:
            continue

        parts = [p.strip() for p in re.split(r"\s*>\s*", title)]
        target = root
        sid = ""
        for n, part in enumerate(parts, 1):
            if n < len(parts):
                sid += part
                if sid not in by_id:
                    items = []
                    by_id[sid] = {"items": items, "by_cmd": {}}
                    target["items"].append({"title": part, "items": items})
                target = by_id[sid]
            else:
                if part.startswith("---"):
                    if target["items"]:
                        target["items"][-1]["sep"] = True
                elif command != "ignore":
                    target["items"].append({"title": part, "cmd": command,
                                            "key": "" if is_commented else key})
    return root


def flatten(menu, prefix=""):
    out = []
    for it in menu["items"]:
        path = prefix + it["title"]
        if "items" in it:
            out.append(("SUB", path, ""))
            out.extend(flatten(it, path + " > "))
        else:
            out.append(("ITEM", path, it.get("cmd", ""), it.get("key", "")))
    return out


def verify(mpv_dir):
    conf = find_config(mpv_dir)
    inp = os.path.join(conf, "input_uosc.conf")
    script = os.path.join(conf, "scripts", "javsubs.lua")
    opt = os.path.join(conf, "script-opts", "javsubs.conf")

    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            ok = False

    chk("脚本已就位", os.path.isfile(script), script)
    chk("配置已就位", os.path.isfile(opt), opt)

    lines = read(inp).split("\n")
    hits = [i for i, l in enumerate(lines) if MARK in l and "#!" in l]
    chk("右键菜单项存在且只有一条", len(hits) == 1, f"行号 {[h + 1 for h in hits]}")

    menu = parse_menu(lines)
    flat = flatten(menu)
    mine = [x for x in flat if MARK in x[2] or "在线中文字幕搜索" in x[1]]
    chk("菜单里能解析出该项", len(mine) == 1, mine)
    if mine:
        print(f"     菜单位置: {mine[0][1]}")
        print(f"     菜单位命令: {mine[0][2]}")

    tools = [x for x in flat if x[1].startswith("工具 >")]
    print(f"     工具 分组条目数: {len(tools)}")

    # 与备份对比：只允许多 1 条
    baks = sorted([f for f in os.listdir(conf)
                   if f.startswith("input_uosc.conf.bak.")])
    if baks:
        base = baks[0]          # 最早的那份 = 安装前状态
        before = flatten(parse_menu(read(os.path.join(conf, base)).split("\n")))
        added = [x for x in flat if x not in before]
        removed = [x for x in before if x not in flat]
        chk("原有菜单条目无缺失", not removed, removed[:3])
        chk("只新增了 1 条", len(added) == 1, added)
        print(f"     基线备份: {base}（共 {len(baks)} 份备份）")
    else:
        print("     ! 没有找到备份文件，跳过差异对比")

    return 0 if ok else 1


def install(mpv_dir):
    conf = find_config(mpv_dir)
    print(f"mpv 配置目录: {conf}")
    if not os.path.isdir(conf):
        print("找不到配置目录，请用 --mpv-dir 指定 mpv 根目录")
        return 1

    os.makedirs(os.path.join(conf, "scripts"), exist_ok=True)
    os.makedirs(os.path.join(conf, "script-opts"), exist_ok=True)

    src_script = os.path.join(PROJ, "scripts", "javsubs.lua")
    src_conf = os.path.join(PROJ, "script-opts", "javsubs.conf")

    dst_script = os.path.join(conf, "scripts", "javsubs.lua")
    shutil.copy2(src_script, dst_script)
    print(f"[OK] {dst_script}")

    dst_conf = os.path.join(conf, "script-opts", "javsubs.conf")
    if os.path.isfile(dst_conf):
        print(f"[SKIP] 已存在，保留你的设置: {dst_conf}")
    else:
        shutil.copy2(src_conf, dst_conf)
        print(f"[OK] {dst_conf}")

    inp = os.path.join(conf, "input_uosc.conf")
    if not os.path.isfile(inp):
        print(f"! 没有 input_uosc.conf，跳过右键菜单（uosc 未安装？）")
        return verify(mpv_dir)

    state, info = insert_menu(inp)
    if state == "exists":
        print("[SKIP] 右键菜单项已存在且内容一致")
    elif state == "updated":
        line, backup, lineno, old = info
        print(f"[OK] 已更新第 {lineno} 行的菜单项")
        print(f"     - {old.strip()}")
        print(f"     + {line.strip()}")
        print(f"     备份: {backup}")
    else:
        line, backup, lineno = info
        print(f"[OK] 已在第 {lineno} 行插入菜单项")
        print(f"     + {line.strip()}")
        print(f"     备份: {backup}")

    print()
    return verify(mpv_dir)


def uninstall(mpv_dir):
    conf = find_config(mpv_dir)
    print(f"mpv 配置目录: {conf}")

    for p in (os.path.join(conf, "scripts", "javsubs.lua"),
              os.path.join(conf, "script-opts", "javsubs.conf")):
        if os.path.isfile(p):
            os.remove(p)
            print(f"[OK] 已删除 {p}")

    inp = os.path.join(conf, "input_uosc.conf")
    if os.path.isfile(inp):
        lines = read(inp).split("\n")
        keep = [l for l in lines if not (MARK in l and "#!" in l)]
        if len(keep) != len(lines):
            write(inp, "\n".join(keep))
            print(f"[OK] 已移除右键菜单项（{len(lines) - len(keep)} 行）")
        else:
            print("[SKIP] 没有需要移除的菜单项")
    print("\n注：input_uosc.conf.bak.* 备份文件保留，可自行删除。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["install", "uninstall", "verify"])
    ap.add_argument("--mpv-dir", default=DEFAULT_MPV)
    a = ap.parse_args()

    if a.action == "install":
        return install(a.mpv_dir)
    if a.action == "uninstall":
        return uninstall(a.mpv_dir)
    return verify(a.mpv_dir)


if __name__ == "__main__":
    sys.exit(main())
