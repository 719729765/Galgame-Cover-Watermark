# Galgame Cover Watermark

https://github.com/719729765/Galgame-Cover-Watermark/blob/main/logo1.png

**Galgame 封面标题监控器**是一款面向 Windows 的桌面工具，用于给 Galgame 封面图片添加居中的游戏标题，并将成品统一保存为 WebP。程序采用 PySide6 原生系统托盘，支持后台运行、逐张手动确认或自动加水印，并可记忆已处理图片，避免重复叠加标题。

- **开发者：** 码铃薯
- **官网：** [www.tudoucode.cn](https://www.tudoucode.cn)
- **GitHub 项目：** [Galgame-Cover-Watermark](https://github.com/719729765/Galgame-Cover-Watermark)
- **Windows 版本下载：** [GitHub Releases](https://github.com/719729765/Galgame-Cover-Watermark/releases)
- **技术栈：** Python、PySide6、Pillow、pillow-heif

## ✨ 功能特性

- **游戏名称必填：** 游戏名称放在主界面前列；标题为空时不能启动监控或处理图片。
- **手动 / 自动模式：** 默认逐张手动确认；启用自动模式后，会使用处理开始时输入框中的当前标题。不同游戏请及时修改标题。
- **监控新图与替换图：** 监控选定文件夹第一层中主文件名恰好为 `0` 的图片，例如 `0.jpg`、`0.png`、`0.avif`。启动监控时已存在的目标图片只作为基线，不会立即处理。
- **常见栅格图片输入：** 支持 JPG、JPEG、PNG、WebP、AVIF、HEIC/HEIF、BMP、GIF、TIFF、ICO 等 Pillow 可解码格式。AVIF、HEIC/HEIF 的解码取决于 Pillow 版本及 `pillow-heif` 插件是否正常工作。
- **统一输出 WebP：** 标题水平和垂直居中，带阴影与描边；可调节标题最大宽度、文字颜色、阴影、描边及 WebP 图片质量。
- **防重复加水印：** 根据文件内容哈希记录原图和成品。识别到完全相同的历史图片时会跳过并保留该文件，不再重复叠加标题。
- **记忆管理：** 可清空防重复记忆以允许重新处理。清空记忆不会删除已生成的 WebP，也不能恢复已删除的原图。
- **Windows 系统托盘：** 使用 PySide6 的 `QSystemTrayIcon`。最小化或关闭主窗口时隐藏到托盘；托盘菜单可恢复主界面、开始/停止监控、清空记忆或彻底退出。
- **小屏幕适配：** 主界面支持滚动，便于查看全部设置、待处理队列、预览和日志。
- **记住设置：** 保存游戏名称、监控目录及标题样式，方便下次继续使用。
- **自定义程序图标：** 将自己的 `0.png` 放在源码同目录，即可用于程序窗口和系统托盘；打包前可自动生成多尺寸 `0.ico`，并将图标嵌入 EXE 文件属性中。

## ⚠️ 重要说明

> **成功加水印后，程序会删除原始图片。** 只有在 WebP 成功生成、防重复记录成功保存且原图未发生变化后，程序才会删除原图。请先用可恢复的测试图片验证流程，并提前备份重要文件。

- 被监控图片的主文件名必须恰好为 `0`，例如 `0.jpg`；`cover0.jpg` 不会被监控。
- 仅监控所选文件夹第一层，不递归扫描子文件夹。
- 动画 GIF、APNG、WebP 等默认只读取第一帧。
- 自动模式使用图片开始处理时输入框中的当前标题。如果每张图片都需要不同的游戏名称，建议使用手动确认模式，或者在下一张图片开始处理前及时修改标题。
- 成品保存为同目录下的 `0_已加水印.webp`；若重名，会自动编号，避免覆盖已有成品。
- 清空防重复记忆不会恢复原图，也不会删除成品。再次处理时请使用干净原图，避免标题重复叠加。

## 💻 运行环境

- Windows 10 / Windows 11
- Python 3.10 或更高版本（从源码运行或自行打包时需要）
- 依赖：PySide6、Pillow、pillow-heif

## 🚀 从源码运行

在项目目录打开命令提示符，执行：

```cmd
python -m pip install --upgrade PySide6 Pillow pillow-heif
python galgame_cover_title_watcher.py
```

如果遇到 AVIF、HEIC/HEIF 解码失败，请先更新 Pillow 与 `pillow-heif`，并确认当前环境已正确安装相应依赖。没有自定义图标时，程序仍会尝试使用内置的备用图标启动。

## 🖼️ 设置自定义图标

1. 将你想使用的图标图片命名为 **`0.png`**。
2. 把 `0.png` 放在 `galgame_cover_title_watcher.py` 和 `prepare_icon.py` 所在的项目根目录。
3. 打包前运行 `prepare_icon.py`，自动生成多尺寸 Windows 图标 `0.ico`。

```cmd
python -m pip install --upgrade Pillow
python prepare_icon.py
```

生成的 `0.ico` 包含 16、24、32、48、64、128、256 像素等常用尺寸。如果 `0.png` 不是正方形，脚本会保留比例并以透明边缘补齐，不会直接拉伸或裁切。`0.png` 用于程序窗口和系统托盘；`0.ico` 通过 PyInstaller 的 `--icon` 参数用于 EXE 文件图标。

> `0.png` 是使用者自行提供的图标素材，因此不会包含在源码压缩包中。打包前请确认该文件存在于项目根目录。

## 📦 打包为 Windows EXE

请在 Windows 环境下打包 Windows 程序。在项目目录打开命令提示符，安装完整依赖：

```cmd
python -m pip install --upgrade PySide6 Pillow pillow-heif PyInstaller
```

确认 `0.png` 已放在项目根目录后，先生成 `0.ico`：

```cmd
python prepare_icon.py
```

然后执行打包命令：

```cmd
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "Galgame封面标题监控器" --icon "0.ico" --add-data "0.png;." --collect-all PIL --collect-all pillow_heif galgame_cover_title_watcher.py
```

打包完成后，EXE 位于：

```text
dist\Galgame封面标题监控器.exe
```

如果只想运行源码，不需要生成 `0.ico`；但希望 EXE 文件本身显示自定义图标，就必须在打包前生成 `0.ico` 并使用 `--icon "0.ico"`。`--add-data "0.png;."` 会把 PNG 一并打包，供运行中的程序窗口和托盘加载。

首次运行时若右下角看不到托盘图标，请检查任务栏的 `^` 隐藏图标区域及 Windows 通知区域设置。Windows 资源管理器可能缓存旧的 EXE 图标；重新打包后仍显示旧图标时，可先确认打开的是 `dist` 目录里的新 EXE，再刷新图标缓存。

## 📝 使用步骤

1. 启动程序，在界面顶部填写游戏名称。
2. 选择要监控的文件夹。
3. 调整标题宽度、文字颜色、阴影、描边和 WebP 质量。
4. 点击「开始监控」。启动时已有的目标图片不会立即处理。
5. 将新图片放入监控目录，并按目标命名为 `0.jpg`、`0.png`、`0.webp`、`0.avif` 等。
6. **手动模式：**检查当前标题后点击「确认加水印并替换原图」。**自动模式：**程序使用当前标题自动处理。
7. 成品统一保存为带有“已加水印”后缀的 WebP 文件。
8. 点击窗口最小化或关闭时，程序会隐藏到系统托盘。右键托盘图标可以恢复主界面或彻底退出后台程序。

## 📥 发布下载

Windows 预编译版会发布在 [GitHub Releases](https://github.com/719729765/Galgame-Cover-Watermark/releases)。如果 Releases 页面暂时没有版本，请稍后查看，或从本仓库下载源码自行打包。

## 👨‍💻 开发者

**码铃薯**  
官网：[www.tudoucode.cn](https://www.tudoucode.cn)  
GitHub：[Galgame-Cover-Watermark](https://github.com/719729765/Galgame-Cover-Watermark)

## 🐛 反馈与问题排查

反馈问题时，请尽量提供：

- Windows 版本和 Python 版本（若从源码运行）
- 程序运行日志中的完整错误信息
- 出错图片的格式与扩展名
- 执行过的完整打包命令（若是打包问题）

请勿上传包含个人隐私或不便公开的图片。
