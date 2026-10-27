"""通过 mpv IPC 查询按键绑定与菜单数据，用于确认右键菜单的真实归属。"""
import json
import os
import subprocess
import sys
import time

MPV = r"D:\tools\音视频处理\MPV1\mpv-lazy\mpv.exe"
PIPE = r"\\.\pipe\mpvprobe"


def ipc_send(f, obj):
    f.write((json.dumps(obj) + "\n").encode("utf-8"))
    f.flush()


def ipc_read(f):
    line = f.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8", "replace"))


def main():
    for _ in range(3):
        try:
            os.remove(PIPE)
        except OSError:
            pass

    args = [
        MPV, "--idle=yes", "--force-window=no", "--vo=null", "--ao=null",
        "--input-ipc-server=" + PIPE, "--msg-level=all=info",
        "--script-opts=contextmenu_plus-load=yes",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    f = None
    for _ in range(60):
        try:
            f = open(PIPE, "r+b", buffering=0)
            break
        except OSError:
            time.sleep(0.2)
    if not f:
        print("连不上 IPC")
        proc.kill()
        return 1

    def query(name, prop):
        ipc_send(f, {"command": ["get_property", prop], "request_id": name})
        while True:
            msg = ipc_read(f)
            if msg is None:
                return None
            if msg.get("request_id") == name:
                return msg.get("data")

    time.sleep(1.5)

    binds = query(1, "input-bindings") or []
    hits = [b for b in binds if str(b.get("key", "")).upper() in ("MBTN_RIGHT", "CTRL+MBTN_RIGHT")]
    print("=== MBTN_RIGHT 相关绑定 ===")
    for b in hits:
        print(f"  key={b.get('key'):15} section={b.get('section'):10} "
              f"owner={b.get('owner') or '-':22} pri={b.get('priority')} cmd={b.get('cmd')}")

    md = query(2, "menu-data")
    print("\n=== menu-data（contextmenu_plus 发布的数据）===")
    if not md:
        print("  空（contextmenu_plus 未提供数据）")
    else:
        print("  顶层条目数:", len(md))
        for it in md[:8]:
            print("   -", it.get("title"), "|", "submenu" if it.get("submenu") else it.get("cmd"))

    ver = query(3, "mpv-version")
    print("\nmpv:", ver)

    ipc_send(f, {"command": ["quit"]})
    time.sleep(0.3)
    proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
