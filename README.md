# mpv-javsubs

给 [mpv](https://mpv.io/) 用的在线中文字幕搜索脚本，附带一个可批量给整个文件夹配字幕的独立程序。

字幕源是 **SubtitleCat** 与 **迅雷字幕公开接口**，无需注册、无需 API Key。

> 移植自 PotPlayer 插件 [PotplayerJavChineseSubs_subtitlecat](https://github.com/736368217/PotplayerJavChineseSubs_subtitlecat)（MIT，作者 Exhen & Contributors）。
> 上游用 PotPlayer 的 ActionScript 宿主接口发请求，mpv 没有这套 API，所以请求层、选择器、状态机都按 mpv 的脚本接口重写了，只复用它的番号识别、标题清理和源站解析规则。详见[来源与致谢](#来源与致谢)。

---

## 目录

- [特性](#特性)
- [安装（mpv 脚本）](#安装mpv-脚本)
- [使用](#使用)
  - [选择器行为](#选择器行为)
  - [配置项](#配置项)
- [独立程序 javsubs-cli](#独立程序-javsubs-cli)
- [实现要点](#实现要点)
- [修复记录：超时未选择后无法再次进入选择](#修复记录超时未选择后无法再次进入选择)
- [右键菜单是怎么注册的](#右键菜单是怎么注册的)
- [验证方式（本机实测）](#验证方式本机实测)
- [测试](#测试)
- [已知限制](#已知限制)
- [来源与致谢](#来源与致谢)
- [许可证](#许可证)

---

## 特性

- **一个键出列表**：`Ctrl+Alt+s`，结果按「番号匹配 + 中文 + 时长吻合」排序，选中即下载并挂上。
- **两个源并发、慢源不挡路**：迅雷通常 0.2~1 秒返回，SubtitleCat 要抓详情页、常要十几秒。快源回来先给结果，慢源回来再补提示。
- **按时长挑字幕**：用迅雷返回的 `duration` 和视频实际时长比对，差 1 秒内标「时长吻合」，基本不用调轴。
- **坏字幕会被拦下**：源站上存在「文件名和时长都对、正文整篇是乱码」的文件，下载后会校验乱码占比与假名/汉字比例。
- **编码自动修正**：迅雷的字幕多是 GBK，非 UTF-8 时自动把 `sub-codepage` 指向 `gb18030`。
- **中文路径可用**：不用 `io.open`（Windows 下走 ANSI 代码页，中文路径必失败），统一用 `mp.utils.read_file`。
- **附带 CLI**：`javsubs-cli` 可递归扫描文件夹，批量下载 `<视频同名>.srt`。

---

## 安装（mpv 脚本）

mpv 只会从配置目录的 `scripts/` 自动加载 `*.lua`，**不需要改 `mpv.conf`**：

```
portable_config\
├── scripts\
│   └── javsubs.lua          ← 脚本本体
└── script-opts\
    └── javsubs.conf         ← 配置（文件名必须与脚本同名）
```

- 便携版：`<mpv 安装目录>\portable_config\...`
- 普通安装：`%APPDATA%\mpv\...`

三种装法：

```bat
:: 1) 简单复制（Windows / Linux）
install.bat
install.sh

:: 2) 装到指定 mpv（自动备份 input_uosc.conf、幂等、可回退）
python tools\install_to_mpv.py install
python tools\install_to_mpv.py verify
python tools\install_to_mpv.py uninstall

:: 3) 手动：把 scripts/javsubs.lua 和 script-opts/javsubs.conf 拷进对应目录
```

`install_to_mpv.py` 会做三件事：拷脚本、拷配置（已存在则保留你的改动）、往右键菜单插 1 行（插前备份）。

---

## 使用

| 操作 | 说明 |
|---|---|
| 右键 → 工具 → 在线中文字幕搜索 | 搜索当前视频 |
| `Ctrl+Alt+s` | 同上 |
| `Ctrl+Alt+m` | 用文件名/番号再搜一次（文件名是 `00000.m2ts` 时手动改关键词的入口） |
| 列表里 `1`~`9` | 选择第 N 条 |
| `Esc` | 取消选择 |

### 选择器行为

这一版 mpv（`mpv-lazy` v0.41.0）的 **Lua 侧没有 `mp.input`**（实测 `mp.input == nil`），所以选择器不是弹窗，而是 **OSD 列表 + 数字键**：

```
找到 20 条字幕（SSNI-497），列出前 7 条
1) [迅雷] · SSNI-497.ass · ASS · 简中 · 时长吻合
2) [迅雷] · SSNI-497.srt · SRT · 简中 · 时长吻合
...
8) —— 重新搜索（联网刷新）——
9) —— 手动输入关键词重新搜索 ——
数字键选择 · Esc 取消 · Ctrl+Alt+s 重新打开
```

几个约定：

- **只有 9 个键位**，所以结果最多列前 7 条，最后两格留给「重新搜索」「手动输入」。排序把最可能对的放前面，一般前 7 条就够。
- 列表 **`pick_timeout`（默认 20 秒）后自动关闭**；关闭时会把 `1~9` / `Esc` 的绑定一起释放，`Esc` 归还给 mpv 本身。
- 超时/取消后 **再按 `Ctrl+Alt+s` 立刻重开列表**，不会重新联网（结果已缓存）。想要新结果就选第 8 项。
- 换片（`file-loaded`）会清空缓存，不会把上一部的列表端出来。

### 配置项

`script-opts\javsubs.conf`（全部带中文注释）：

```ini
key_search=Ctrl+Alt+s        ; 别用 Ctrl+Shift+字母，会被 mpv 归一化成 Ctrl+字母（Ctrl+Shift+s == Ctrl+S）
key_manual_search=Ctrl+Alt+m
sources=xunlei,subtitlecat   ; 国内 SubtitleCat 常超时，只要迅雷就写 xunlei
auto_load_best=no            ; yes = 高置信度匹配时自动挂字幕
auto_load_min_score=90
http_timeout=25              ; 单请求超时（秒）
grace_seconds=1.5            ; 快源返回后再等慢源多久，超时先给部分结果
pick_timeout=20              ; 列表显示多少秒后自动关闭（关闭时释放按键）
search_deadline=0            ; 整轮搜索兜底时限，0 = 自动（http_timeout x 6，最少 30）
max_results=30               ; 结果条数上限（数字键模式下实际只列 7 条）
proxy=                       ; 例 http://127.0.0.1:7890
ignore_env_proxy=no
codepage_fix=yes             ; 非 UTF-8 自动设 sub-codepage
codepage_fallback=gb18030
save_dir=same                ; same = 视频同目录；cache = mpv 配置目录下的 cache
require_preferred_lang=yes   ; 只显示中文
```

---

## 独立程序 javsubs-cli

不依赖 mpv / ffmpeg / Python，单文件 exe，扫描文件夹批量下载对应字幕。

| | |
|---|---|
| 源码 | `cli/javsubs_cli.py`（纯标准库，装了 Python 可直接跑） |
| 打包 | `cli/build.bat`（PyInstaller onefile，产物约 9.2 MB） |
| 成品 | Releases 页下载，或自行 `cli\build.bat` |

> exe 不进仓库（9 MB 二进制会拖累 clone），需要时从 Releases 取或本地打包。

### 输入参数

```
javsubs-cli.exe <路径...> [选项]
```

`<路径...>` 可以是文件夹，也可以是单个视频，可传多个。

| 参数 | 默认 | 说明 |
|---|---|---|
| `-r, --recursive` | 关 | 递归子目录 |
| `-o, --out-dir DIR` | 视频同目录 | 字幕输出目录 |
| `-f, --overwrite` | 关 | 覆盖已存在的 srt（默认跳过，避免盖掉你手动调过轴的版本） |
| `-j, --jobs N` | 4 | 并发处理的视频数 |
| `--source SRC` | `xunlei` | 字幕源，逗号分隔：`xunlei` / `xunlei,subtitlecat` |
| `--prefer-langs` | `zh-CN,zh-TW` | 语言优先顺序 |
| `--tries N` | 3 | 每个视频最多试几个候选（前面的坏了自动换下一个） |
| `--min-score N` | 0 | 低于该分数视为无匹配 |
| `--tag-lang` | 关 | 文件名带语言：`<视频>.zh-CN.srt` |
| `--bom` | 关 | 输出带 UTF-8 BOM（部分老播放器需要） |
| `--probe-limit N` | 6 | SubtitleCat 详情页探测条数 |
| `--no-duration` | 关 | 不解析视频时长（更快，但排序少一个依据） |
| `--timeout N` | 20 | 单请求超时秒数 |
| `--proxy URL` | 空 | 代理，如 `http://127.0.0.1:7890` |
| `--dry-run` | 关 | 只显示匹配结果，不下载 |
| `--json PATH` | 关 | 结果写入 JSON 报告 |
| `--version` | | 打印版本 |
| `-v / -vv` | 0 | 详细 / 调试输出 |

### 输出结果

**字幕文件**：默认 `<视频同名>.srt`，与视频同目录（播放器打开视频会自动加载）。

- 源是 ASS/SSA 时自动转 SRT（去特效标签、去矢量绘图、`\N` 转真实换行）
- 时间戳统一成标准 SRT（`00:00:13,266`），重排并补全序号——迅雷上不少文件没有序号、还用点号分隔
- 编码统一转 UTF-8（源可能是 GBK / UTF-16 / 带 BOM）

**控制台**：

```
javsubs-cli 1.0  共 4 个视频  源=xunlei  并发=4
------------------------------------------------------------------------------
[1/4] ✔ SSNI-497.mp4 时长=8.0s
         关键词: SSNI-497
         迅雷 SRT zh-CN score=100  6424汉字 1364条 源编码=utf-8-sig
[2/4] · [ThZu.Cc]ssni-497-C_1080p.mp4
         已存在 [ThZu.Cc]ssni-497-C_1080p.srt
[3/4] ✘ ABP-123.mkv 时长=5.0s
         关键词: ABP-123
         未找到字幕
------------------------------------------------------------------------------
完成: 成功 1 / 跳过 2 / 失败 1  用时 0.6s
```

标记：`✔` 成功、`·` 跳过（已存在）、`✘` 失败、`?` 空跑。

**退出码**：`0` 全部成功或跳过；`1` 有失败项；`2` 路径下没有视频。
**JSON 报告**：`{"version","elapsed","counts":{...},"items":[{file,query,status,detail,out,vdur}]}`

---

## 实现要点

**为什么重写而不是移植。** 上游是 PotPlayer 的 ActionScript，靠宿主的 `HostUrlGetString()` 发请求。mpv 没有这套 API，所以请求层改成 `mp.command_native_async` 起 curl 子进程（异步，不阻塞播放）。

**两源并发、慢源不挡路。** 第一个源回来后只再等 `grace_seconds`（1.5s）就给一次「部分结果」，另一个源回来再补一条 OSD；慢源不会把菜单卡住。

**详情页并发探测。** SubtitleCat 要逐条进详情页才知道有哪些语言。这里并发探测，并按搜索结果页里的「语言数 / 下载量」优先挑最可能带中文的条目。

**排序。** 番号匹配 60 + 中文 40 + 时长吻合 70 + ASS 3；源站明确标了「简/繁」再 +6。

**时长从哪来。** 脚本用迅雷返回的 `duration`；CLI 自己解析 MP4（`mvhd`）/ MKV（EBML `Info > Duration`）/ AVI（`avih`），不依赖 ffprobe。

**内容校验。** 下载后检查 `U+FFFD` 占比与假名/汉字比例。自动匹配到坏文件时**不挂载并删掉落盘文件**（否则下次会被 `sub-auto` 又加载）；CLI 则自动换下一个候选。

**中文路径。** Windows 下 Lua 的 `io.open` 走 ANSI 代码页，中文路径直接失败——于是「下载成功却挂不上字幕」。统一改用 `mp.utils.read_file` / `file_info`。

---

## 修复记录：超时未选择后无法再次进入选择

**现象**：搜出列表后没选（等它自己消失），再按 `Ctrl+Alt+s` 毫无反应，只能关掉视频重开。

**定位过程**：用 IPC 发 `keypress` 复现，日志里第二次、第三次按键连一条 `SubtitleCat 搜索` 都没有 —— 说明请求根本没发出去。读代码发现两个独立问题：

1. `searching` 是个全局开关，只在「最终结果」回来时才置回 `false`。而 SubtitleCat 的搜索页 + 8 个详情页、每个请求 25 秒超时还要换域名重试一次，整轮可能跑 1~3 分钟。这段时间里按键只会弹一句 2 秒的「正在搜索中，请稍候…」，列表再也打不开。**如果某个源的回调意外丢失（子进程被杀、curl 卡死），`searching` 会永远为 true。**
2. 列表超时消失后，注册的 `1~9` / `Esc` 强制绑定没有撤销 —— `Esc` 被脚本永久占着，之后按 `Esc` 只弹「已取消」，退出全屏之类的功能全废。

**修法**：

| 改动 | 作用 |
|---|---|
| 结果缓存 + 秒开 | 再按键时若已有结果，直接重开列表（日志 `复用上次结果，直接重新打开列表`），不再等 1~3 分钟；列表末尾新增「重新搜索（联网刷新）」用于强制刷新 |
| 轮次号 `search_seq` | 每轮搜索自增，回调里比对，过期结果直接丢弃；用户再按键 = 明确要求重搜，旧轮作废 |
| 兜底闹钟 `search_deadline` | 到点强制收尾（`0` = `max(30, http_timeout×6)`），保证 `searching` 一定会回落，不会永久锁死 |
| 1 秒防抖 | 手抖连按不会重复发请求（按 `active_query` 判断，而不是已被清空的 `last_query`） |
| 选择器生命周期 | 列表 `pick_timeout` 秒后自动关闭并**释放所有按键**；选完/取消/换片都走同一条清理路径 |
| 键位预算 | 数字键只有 9 个，结果条数因此压到 7 条，给「重新搜索」「手动输入」留出位置——否则结果一多，这两项被挤到 9 名之后永远按不到 |

**验证结果**（`tests/test_state.py` + 真机）：

```
PASS 1 超时后按键全部释放（Esc 不再被劫持）
PASS 2 数字键触发回调 / 选完后按键释放
PASS 3 缓存命中：没有发起新的网络请求 / 列表重新打开
PASS 4 再按键后重新发起了搜索 / 轮次号递增 / 兜底触发后 searching=false
PASS 4 1 秒内连按不重复请求
```

真机（`tests/test_real_picker.py`，IPC 发按键，查 `input-bindings`）：

```
t=0s   按键         → 选择器绑定 10 个（1~9 + Esc）
t=8s   再按键       → 绑定 10 个，日志「复用上次结果，直接重新打开列表」，无新网络请求
t=37s  列表已超时   → 绑定 0 个（按键已释放）
t=40s  再按键       → 绑定 10 个（列表能重新打开）
```

---

## 右键菜单是怎么注册的

先在 mpv-lazy 上用 IPC 查 `input-bindings`，弄清楚「右键」到底归谁：

```
MBTN_RIGHT        → uosc/menu      (优先级 19，实际生效)
Ctrl+MBTN_RIGHT   → context-menu   (mpv 原生菜单，数据来自 contextmenu_plus)
```

uosc 的菜单是**从 `input_uosc.conf` 里带 `#!` 注释的行动态生成**的（见 `uosc/lib/menus.lua` 的 `get_menu_items()`），所以注册方式就是在该文件的「工具」分组末尾插一行：

```
Ctrl+Alt+s       script-message javsubs-search   #! 工具 > 在线中文字幕搜索（SubtitleCat/迅雷）
```

**不影响原有配置**：与安装前备份做 `diff` 只多 1 行（`93a94`）；按 uosc 规则复算菜单树 89 → 90 条，原有条目零缺失；`Ctrl+右键` 的原生菜单 32 条不变。`install_to_mpv.py uninstall` 可完全回退。

> **踩过的坑**：第一版用 `Ctrl+Shift+s`，装完发现用户的 `Ctrl+S`（窗口截图）被顶掉了。mpv 会把 `Ctrl+Shift+字母` 归一化成 `Ctrl+大写字母`（`Ctrl+Shift+s` ≡ `Ctrl+S`），这一行直接覆盖了截屏绑定。查 `input-bindings` 才定位到，已改为 `Ctrl+Alt+s`。**加菜单项必须验证按键是否冲突**，而不只是看菜单里有没有出现。

---

## 验证方式（本机实测）

环境：`mpv-lazy` v0.41.0-173（`portable_config`，自带 uosc + contextmenu_plus）。

**1) mpv 可运行**

```
$ mpv.exe --version
mpv v0.41.0-173-g3847a8157
FFmpeg version: N-122695-g38230db7B
```

**2) 真能解码播放**（lavfi 合成片源，不依赖素材）

```
● Video --vid=1 (320x240 25fps)
PLAY_OK 320x240          退出码 0
```

**3) 走的是 portable_config + 脚本加载成功**

```
Reading config file ...\portable_config\mpv.conf
[javsubs] Opened config file script-opts/javsubs.conf.
[javsubs] javsubs 已加载：搜索键 = Ctrl+Alt+s
[javsubs] curl 可用: curl
```

**4) 真按键 → 真搜索 → 真挂载**

```
[javsubs] SubtitleCat 搜索: https://www.subtitlecat.com/index.php?search=SSNI-497
[javsubs] 迅雷搜索: https://api-shoulei-ssl.xunlei.com/oracle/subtitle?...name=SSNI-497
[javsubs] 迅雷命中 20 条（去重后）
[javsubs] 字幕已加载: ...\SSNI-497.zh-CN.srt
（Lua error 计数 = 0）
```

**5) 菜单项进右键菜单**：89 → 90 条，只 +1，位置「工具 > 在线中文字幕搜索（SubtitleCat/迅雷）」。

---

## 测试

```sh
pip install lupa            # 用 Python 加载 Lua 脚本做回归

python tests/test_parse.py  # 离线解析（fixtures 是抓下来的真实网页 / JSON）
python tests/test_load.py   # 下载→保存→加载（含中文路径、GBK 检测）
python tests/test_ui.py     # 按键→弹列表→选第 1 条→挂字幕
python tests/test_state.py  # 搜索/选择状态机（超时释放、缓存秒开、兜底闹钟）
python tests/test_e2e.py    # 全链路 + 排序 + 慢源容错（真实联网，20~90 秒）
```

以下两个需要本机装 mpv（默认找 mpv-lazy，可用 `MPV_BIN` 指定），会真的起播放器、发按键、查绑定：

```sh
set MPV_BIN=D:\path\to\mpv.com
python tests/test_real_picker.py   # 未选择→再按键能否重开列表；超时后按键是否释放
python tests/test_real_load.py     # 按数字键选中→下载→sub-add
python tools/install_to_mpv.py verify   # 校验 mpv 侧安装与菜单结构
```

---

## 已知限制

- **SubtitleCat 在国内经常连不上或极慢**，脚本会超时跳过，迅雷结果不受影响。它还有个小坑：`/subs/` 详情页会 301 到 `www.` 域名，请求失败时会在两个域名间重试一次。
- **数字键只有 9 个键位**，列表最多列 7 条（+2 个操作项）。想要长列表需要 mpv 提供 `mp.input`（本机这版没有）。
- 只挑**中文**字幕；`require_preferred_lang=no` 才显示其他语言。
- 字幕不同步用 mpv 自带的 `z` / `x`（或 `Shift+←/→`）调轴，脚本不负责调轴。
- CLI 对 `ts` / `m2ts` 拿不到时长，只按番号和语言排序。
- 字幕源是第三方站点，可用性不由本工具保证。

---

## 来源与致谢

| 项目 | 说明 |
|---|---|
| [PotplayerJavChineseSubs_subtitlecat](https://github.com/736368217/PotplayerJavChineseSubs_subtitlecat) | 上游 PotPlayer 插件（MIT，Copyright (c) 2024 Exhen & Contributors）。番号识别、标题清理（去站点前缀/画质标签/扩展名）、SubtitleCat 与迅雷两个源的解析规则来自这里 |
| [SubtitleCat](https://www.subtitlecat.com) | 字幕数据源之一 |
| 迅雷字幕公开接口 | 字幕数据源之一（`api-shoulei-ssl.xunlei.com`） |
| [mpv](https://mpv.io/) / [uosc](https://github.com/tomasklaen/uosc) / [mpv-lazy](https://github.com/hooke007/mpv_PlayKit) | 运行环境、右键菜单与配置框架 |

本项目的 Lua 实现、Python CLI、测试与文档为重新编写，与上游是**同许可证下的衍生作品**而非代码逐行移植。

### 免责声明

- 本项目只做「按番号检索字幕」的工具能力，**不托管、不分发任何字幕文件**；字幕内容的版权归原上传者，使用时请自行确认合规。
- 仅供个人学习与技术研究，请遵守所在地区法律法规及字幕源站的服务条款。
- 字幕源可能随时变更接口或失效，本项目不保证可用性。

---

## 许可证

[MIT](LICENSE) © 2024 Exhen & Contributors（上游）+ 2026 mpv-javsubs contributors
