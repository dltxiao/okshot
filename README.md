# macshot — 仿 macOS 的 GNOME/Wayland 截图工具

在 Ubuntu 26.04 + GNOME 50 (Wayland) 上复刻 macOS 的 ⇧⌘3 / ⇧⌘4 / ⇧⌘5 截图体验：
按快捷键 → 冻结画面 → 框选/选窗口 → 落盘到桌面或进剪贴板。

```
Alt+Shift+3   全屏截图            Ctrl+Alt+Shift+3   全屏 → 只进剪贴板
Alt+Shift+4   区域截图            Ctrl+Alt+Shift+4   区域 → 只进剪贴板
Alt+Shift+5   窗口截图            Ctrl+Alt+Shift+5   窗口 → 只进剪贴板
```

（macOS 的 ⌘ 对应 Linux 的 Super 键；这里按你的选择用了 Alt+Shift，要换组合改
`macshot/hotkeys.py` 里的 `BINDINGS` 后重跑 `./install.sh --keys` 即可。）

## 和 macOS 的对照

| macOS 行为 | macshot | 备注 |
| --- | --- | --- |
| ⇧⌘3 全屏截图，存到桌面 | `Alt+Shift+3` | 全部显示器合成为一张图 |
| ⇧⌘4 拖动框选 | `Alt+Shift+4` | 冻结画面 + 十字光标 + 实时尺寸提示 |
| 松手后还能拖动手柄微调、方向键微调 | ✅ | 方向键 1px，`Shift`+方向键 10px |
| `Enter` 保存、`Esc` 取消 | ✅ | 选区内部单击也可保存，双击亦可 |
| ⇧⌘4 后按空格选窗口 | `Alt+Shift+4` 后按空格，或直接 `Alt+Shift+5` | 见下方「窗口模式」说明 |
| ⌃⇧⌘3/4 只复制到剪贴板、不存盘 | `Ctrl+Alt+Shift+3/4/5` | 剪贴板里是真正的 PNG 图像 |
| 存到桌面，文件名 `Screenshot 2026-02-14 at 15.04.05.png` | ✅ | 模板可配置 |
| 快门音效 | ✅ | 走系统 `canberra-gtk-play` 音效 |
| 快门白闪 | ✅（区域模式） | 全屏/窗口模式没有（见限制说明） |

## 环境要求

* Ubuntu 26.04 / GNOME 50（Wayland）——实际依赖的是 XDG Desktop Portal，
  GNOME 45+ 的 Wayland 会话都能用。
* `python3-gi`、`gir1.2-gtk-4.0`（Ubuntu 桌面版自带）。
* 剪贴板功能需要 XWayland（Ubuntu 默认开启）。没有 XWayland 时会退回 Wayland
  原生剪贴板，此时只有在有焦点的窗口里才能占据剪贴板。

## 安装

```bash
cd ~/AICoding/screenshot
./install.sh          # 安装启动器、图标、桌面项和快捷键
./install.sh --keys   # 只重装快捷键
```

安装脚本会：

1. 把 `macshot.py` 软链到 `~/.local/bin/macshot`；
2. 装图标和 `~/.local/share/applications/macshot.desktop`；
3. 通过 `gsettings` 在 `org.gnome.settings-daemon.plugins.media-keys`
   里追加 6 条自定义快捷键——**不会动你已有的其它自定义快捷键**，并在安装前
   检查这些组合是否已被系统占用。

卸载：`./uninstall.sh`

## 用法

```bash
macshot full                 # 全屏（Alt+Shift+3）
macshot region               # 区域（Alt+Shift+4）
macshot window               # 窗口（Alt+Shift+5）
macshot region -c            # 区域截图并只放进剪贴板
macshot full -d 3            # 3 秒后截图（拍菜单、拍下拉框时有用）
macshot region -s ~/图片     # 保存到别的目录
macshot region --notify      # 存盘后发一条桌面通知
macshot full --no-sound --no-flash
macshot --config ~/my.json
```

| 参数 | 说明 |
| --- | --- |
| `-c, --clipboard` | 只写剪贴板，不落盘（macOS 的 ⌃ 变体） |
| `-d, --delay N` | 截图前等待 N 秒 |
| `-s, --save-dir DIR` | 保存目录（默认 `~/Desktop`，不存在时退回 XDG 桌面目录） |
| `--no-sound` / `--no-flash` / `--no-hints` | 关掉声音 / 白闪 / 屏幕提示 |
| `--notify` | 存盘后弹通知 |
| `--image FILE` | 用现成 PNG 代替实时抓屏（调试用） |
| `--select X,Y,W,H` | 跳过交互，直接按逻辑坐标裁剪（脚本/测试用） |
| `-v, --verbose` | 同时输出日志到终端 |

区域模式的交互：拖动框选 → 松手后进入「调整」态（选区外变暗、八个手柄可拖）→
`Enter`/单击选区内部/双击保存，`Esc` 取消，方向键微调，空格切窗口模式。

## 工作原理（为什么这么做）

GNOME 50 的 `org.gnome.Shell.Screenshot` / `org.gnome.Shell.Introspect` 都挂在
`DBusSenderChecker` 白名单后面，只允许 `org.gnome.SettingsDaemon.MediaKeys` 和
`org.freedesktop.impl.portal.desktop.gnome` 调用（见 gnome-shell 的
`js/misc/util.js`）。所以第三方程序只有一条合法路径：**XDG Desktop Portal**。

* **抓屏**：`org.freedesktop.portal.Screenshot`，`interactive=false` 时 GNOME 会
  静默抓取整个桌面并返回一个 PNG 文件（无弹窗、无权限询问，实测 ~0.3s）。
* **选择界面**：先抓屏、再显示覆盖层，所以覆盖层自己不会进画面（和 macOS 一样）。
  每个显示器一个无边框全屏窗口，各自画冻结画面的对应切片，最后从同一张像素
  缓冲里裁剪——这样跨显示器的选区也能正确裁切。绘制用 GTK4 的 Gsk 快照节点，
  不依赖 pycairo。
* **逻辑像素 ↔ 物理像素**：分数缩放（本机 1.25）下 GTK 用逻辑像素、截图是物理
  像素，比例来自 `org.gnome.Mutter.DisplayConfig.GetCurrentState`（这个接口没有
  白名单限制）。若抓到的图与 Mutter 报告的布局对不上，会自动退回按图像尺寸等比
  映射。
* **剪贴板**：Wayland 下只有「有键盘焦点的客户端」才能占据 selection，快捷键
  拉起的进程拿不到输入序列号，所以由一个分离的 helper 走 XWayland 设置剪贴板
  （Mutter 负责 X11↔Wayland 剪贴板桥接），helper 默认持有 180 秒。
* **窗口模式**：`org.gnome.Shell.Introspect` 拿不到窗口几何，无法自己做
  「悬停高亮」，因此窗口选择交给 GNOME 自带的冻结屏 UI（`interactive=true`，
  它支持悬停选窗口），拿到文件后再按 macOS 风格改名移动到桌面。

已知差异：全屏/窗口模式没有白色闪光；窗口模式的选择界面是 GNOME 原生的。

## 配置

`~/.config/macshot/config.json`（首次运行自动生成）：

```json
{
  "save_dir": "~/Desktop",
  "filename_template": "Screenshot {date} at {time}.png",
  "play_sound": true,
  "flash": true,
  "hints": true,
  "clipboard_hold_seconds": 180,
  "window_mode": "gnome"
}
```

* `filename_template` 支持 `{date}`、`{time}`、`{datetime}`；
  中文风格可写成 `"截屏 {date} {time}.png"`。
* `window_mode` 设为 `"off"` 时，区域界面里的空格提示会消失（保留 `Alt+Shift+5`）。

## 故障排查

日志在 `~/.cache/macshot/macshot.log`（每次运行追加，超过 512KB 自动清空）。

| 现象 | 处理 |
| --- | --- |
| 按快捷键没反应 | `python3 -m macshot.hotkeys show` 看绑定是否在；`gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings` |
| 弹出的是 GNOME 自带截图 UI | 只有 `Alt+Shift+5` / 空格是故意的；其它模式若也这样，看日志里是否有 portal 报错 |
| 剪贴板是空的 | 确认 `echo $DISPLAY` 有值（XWayland 在跑）；看日志里有没有 `x11 clipboard helper unavailable` |
| 截图全黑/画面不对 | 多显示器混合缩放时看日志里的 `layout=` 与 `image=`，若尺寸对不上会走等比兜底 |
| 快捷键跟别的软件冲突 | 改 `macshot/hotkeys.py` 的 `BINDINGS`，再 `./install.sh --keys` |

## 开发

```
macshot/portal.py     XDG portal 客户端（Screenshot / Response 信号）
macshot/monitors.py   Mutter DisplayConfig → 显示器布局与缩放映射
macshot/selection.py  纯几何：选区正规化、手柄命中、缩放、移动
macshot/overlay.py    GTK4 冻结屏选择覆盖层（Gsk 绘制）
macshot/save.py       命名/移动/裁剪/剪贴板 helper/音效/通知
macshot/hotkeys.py    gsettings 自定义快捷键增删
macshot/cli.py        参数解析与三种模式流程
tests/                unittest 测试（41 个）
```

```bash
python3 -m unittest discover -s tests -t .   # 全部测试
macshot region --image shot.png --select 100,80,600,400 -s /tmp   # 不走交互的端到端
```
