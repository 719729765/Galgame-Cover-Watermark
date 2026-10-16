# -*- coding: utf-8 -*-
"""Galgame cover title watcher.

Qt-native Windows system tray via QSystemTrayIcon. Monitors new/changed files
named 0.<supported extension>, asks for a title per image unless auto mode is
enabled, creates a WebP sibling, remembers source/output hashes, then deletes
an original only after the output and memory record have been safely written.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

# Import and register extra AVIF/HEIC/HEIF handlers when pillow-heif is installed.
try:
    Image.init()
except Exception:
    pass
try:
    from pillow_heif import register_heif_opener, register_avif_opener
    register_heif_opener()
    if "AVIF" not in Image.OPEN:
        register_avif_opener()
except Exception:
    pass

from PySide6.QtCore import Qt, QTimer, QEvent, QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit, QSlider, QCheckBox, QGroupBox, QTextEdit,
    QFileDialog, QSystemTrayIcon, QMenu, QMessageBox, QStyle, QScrollArea,
    QListWidget, QListWidgetItem, QColorDialog, QSizePolicy, QSplitter,
)

APP_NAME = "Galgame 封面标题监控器"
APP_VERSION = "2.0-QtTray"
GITHUB_REPO_URL = "https://github.com/719729765/Galgame-Cover-Watermark"
GITHUB_RELEASES_URL = "https://github.com/719729765/Galgame-Cover-Watermark/releases"
POLL_SECONDS = 1.5
STABLE_SECONDS = 2.0
DEFAULT_WEBP_QUALITY = 90
SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".webp", ".avif",
    ".heic", ".heics", ".heif", ".heifs", ".hif", ".bmp", ".dib", ".gif",
    ".tif", ".tiff", ".ico", ".icns", ".jp2", ".j2k", ".j2c", ".jpc",
    ".jpf", ".jpx", ".pcx", ".tga", ".ppm", ".pgm", ".pbm", ".pnm",
    ".sgi", ".rgb", ".rgba", ".xbm", ".qoi", ".dds", ".blp", ".im",
    ".msp", ".palm",
}


def resource_path(name: str) -> Path:
    """Find a bundled asset (such as 0.png) in source and PyInstaller builds."""
    candidates = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir) / name)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / name)
    candidates.append(Path(__file__).resolve().parent / name)
    # Also support starting the script from a working directory containing the asset.
    candidates.append(Path.cwd() / name)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return candidates[0] if candidates else Path(name)


@dataclass(frozen=True)
class FileSignature:
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int


def file_signature(path: Path) -> FileSignature:
    st = path.stat()
    return FileSignature(
        size=st.st_size,
        mtime_ns=getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)),
        ctime_ns=getattr(st, "st_ctime_ns", int(st.st_ctime * 1_000_000_000)),
        inode=getattr(st, "st_ino", 0),
    )


def is_target_image(path: Path) -> bool:
    try:
        return path.is_file() and path.stem == "0" and path.suffix.lower() in SUPPORTED_EXTENSIONS
    except OSError:
        return False


@lru_cache(maxsize=256)
def find_font(size: int) -> ImageFont.ImageFont:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    fonts_dir = Path(windir) / "Fonts"
    candidates = [
        fonts_dir / "msyhbd.ttc", fonts_dir / "msyh.ttc", fonts_dir / "simhei.ttf",
        fonts_dir / "simsun.ttc", fonts_dir / "meiryo.ttc", fonts_dir / "arial.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                return ImageFont.truetype(str(candidate), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def load_source_rgba(source: Path) -> Image.Image:
    try:
        with Image.open(source) as opened:
            try:
                opened.seek(0)  # Animated GIF/APNG/WebP: use the first frame.
            except (EOFError, AttributeError):
                pass
            return ImageOps.exif_transpose(opened).convert("RGBA")
    except Exception as exc:
        if source.suffix.lower() in {".avif", ".heic", ".heics", ".heif", ".heifs", ".hif"}:
            raise RuntimeError(
                f"无法解码 {source.suffix.upper()}。请在当前 Python 环境安装/更新：\n"
                "python -m pip install --upgrade pillow pillow-heif"
            ) from exc
        raise


def render_titled_image(
    source_image: Image.Image,
    title: str,
    max_width_ratio: float = 0.33,
    text_color: str = "#FFFFFF",
    shadow_strength: int = 75,
    outline_strength: int = 90,
) -> Tuple[Image.Image, int]:
    title = title.strip()
    if not title:
        raise ValueError("请先填写游戏名称。")
    image = source_image.copy().convert("RGBA")
    width, height = image.size
    if width < 1 or height < 1:
        raise ValueError("图片尺寸无效。")
    ratio = min(0.80, max(0.15, float(max_width_ratio)))
    shadow_strength = min(100, max(0, int(shadow_strength)))
    outline_strength = min(100, max(0, int(outline_strength)))
    max_text_width = max(1, int(width * ratio))
    font_size = max(12, min(int(width * 0.13), int(height * 0.24)))
    min_size = max(8, int(width * 0.012))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    font = find_font(font_size)
    stroke_probe = max(0, int(font_size * 0.06))
    bbox = probe.textbbox((0, 0), title, font=font, stroke_width=stroke_probe)
    while bbox[2] - bbox[0] > max_text_width and font_size > min_size:
        font_size -= max(1, font_size // 30)
        font = find_font(font_size)
        bbox = probe.textbbox((0, 0), title, font=font, stroke_width=max(0, int(font_size * 0.06)))

    shadow_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    blur = max(1, int(font_size * 0.12))
    offset = max(2, int(font_size * 0.045))
    shadow_alpha = int(235 * shadow_strength / 100)
    if shadow_alpha:
        shadow_draw.text(
            (width // 2 + offset, height // 2 + offset), title,
            font=font, anchor="mm", fill=(0, 0, 0, shadow_alpha),
            stroke_width=max(1, int(font_size * 0.025)),
            stroke_fill=(0, 0, 0, shadow_alpha),
        )
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur))
        image = Image.alpha_composite(image, shadow_layer)

    outline_alpha = int(245 * outline_strength / 100)
    stroke_width = max(1, int(font_size * 0.055)) if outline_strength else 0
    ImageDraw.Draw(image).text(
        (width // 2, height // 2), title, font=font, anchor="mm", fill=text_color,
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, outline_alpha) if outline_strength else None,
    )
    return image, font_size


def make_titled_image(source: Path, destination: Path, options: dict) -> Tuple[int, int, int]:
    source_image = load_source_rgba(source)
    width, height = source_image.size
    result, font_size = render_titled_image(
        source_image, options["title"], options["max_width_ratio"], options["text_color"],
        options["shadow_strength"], options["outline_strength"],
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    result.save(destination, format="WEBP", quality=int(options["webp_quality"]), method=6)
    return width, height, font_size


def unique_watermarked_path(source: Path) -> Path:
    candidate = source.with_name(f"{source.stem}_已加水印.webp")
    index = 2
    while candidate.exists():
        candidate = source.with_name(f"{source.stem}_已加水印_{index}.webp")
        index += 1
    return candidate


def unique_skipped_path(source: Path) -> Path:
    ext = source.suffix.lower()
    candidate = source.with_name(f"{source.stem}_已处理过_跳过{ext}")
    index = 2
    while candidate.exists():
        candidate = source.with_name(f"{source.stem}_已处理过_跳过_{index}{ext}")
        index += 1
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        directory = Path(base) / "GalgameCoverTitleWatcher"
    elif os.name == "nt":
        directory = Path.home() / "AppData" / "Local" / "GalgameCoverTitleWatcher"
    else:
        directory = Path.home() / ".local" / "share" / "galgame-cover-title-watcher"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class ProcessedImageMemory:
    """Persistent exact-hash memory shared by source and EXE runs."""
    def __init__(self):
        self.path = app_data_dir() / "watermark_memory.json"
        self.lock = threading.RLock()
        self.data = {"version": 1, "hashes": {}}
        self.load_error: Optional[str] = None
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("hashes"), dict):
                    self.data = {"version": 1, "hashes": loaded["hashes"]}
        except Exception as exc:
            self.load_error = str(exc)

    def lookup(self, digest: str) -> Optional[dict]:
        with self.lock:
            entry = self.data["hashes"].get(digest)
            return dict(entry) if isinstance(entry, dict) else None

    def record_count(self) -> int:
        with self.lock:
            return len({entry.get("record_id") for entry in self.data["hashes"].values()
                        if isinstance(entry, dict) and entry.get("record_id")})

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def register(self, source_hash: str, output_hash: str, title: str, output_name: str) -> str:
        record_id = hashlib.sha256(f"{source_hash}:{output_hash}:{time.time_ns()}".encode()).hexdigest()[:24]
        record = {
            "record_id": record_id, "title": title, "output_name": output_name,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.lock:
            old_source = self.data["hashes"].get(source_hash)
            old_output = self.data["hashes"].get(output_hash)
            self.data["hashes"][source_hash] = {**record, "kind": "source"}
            self.data["hashes"][output_hash] = {**record, "kind": "watermarked_output"}
            try:
                self._save_locked()
            except Exception:
                if old_source is None:
                    self.data["hashes"].pop(source_hash, None)
                else:
                    self.data["hashes"][source_hash] = old_source
                if old_output is None:
                    self.data["hashes"].pop(output_hash, None)
                else:
                    self.data["hashes"][output_hash] = old_output
                raise
        return record_id

    def forget_record(self, record_id: str):
        with self.lock:
            self.data["hashes"] = {
                key: value for key, value in self.data["hashes"].items()
                if not isinstance(value, dict) or value.get("record_id") != record_id
            }
            self._save_locked()

    def clear_all(self) -> int:
        with self.lock:
            count = self.record_count()
            old = self.data
            self.data = {"version": 1, "hashes": {}}
            try:
                self._save_locked()
            except Exception:
                self.data = old
                raise
            return count


class GalgameWatcherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setWindowIcon(self._make_app_icon())
        self.resize(980, 820)
        self.setMinimumSize(700, 560)

        self._loading_settings = False
        self.events: queue.Queue = queue.Queue()
        self.monitor_thread: Optional[threading.Thread] = None
        self.stop_event: Optional[threading.Event] = None
        self.monitoring = False
        self.monitor_stopping = False
        self.processing = False
        self.processing_auto = False
        self.processing_path: Optional[Path] = None
        self.exit_when_idle = False
        self.allow_close = False
        self.shutting_down = False
        self.preview_pixmap: Optional[QPixmap] = None
        self.text_color = "#FFFFFF"
        self.confirmation_queue: Dict[Path, FileSignature] = {}
        self.auto_queue: Dict[Path, FileSignature] = {}
        self.active_confirmation: Optional[Path] = None
        self.auto_title_wait_notified = False
        self.memory = ProcessedImageMemory()
        self.tray: Optional[QSystemTrayIcon] = None
        self.tray_menu: Optional[QMenu] = None

        self._build_ui()
        self._load_settings()
        self._create_tray()
        self._refresh_memory_count()
        self._refresh_queue_ui()

        self.event_timer = QTimer(self)
        self.event_timer.setInterval(150)
        self.event_timer.timeout.connect(self._drain_events)
        self.event_timer.start()

        if self.memory.load_error:
            self.log(f"警告：读取防重复记忆失败，旧记忆文件已保留：{self.memory.load_error}", "error")
        self.log("已启动。请先在界面顶部填写游戏名称；空标题不能启动监控。托盘由 Qt 原生 QSystemTrayIcon 提供。")
        self.log(f"防重复记忆：{self.memory.record_count()} 条；记忆文件：{self.memory.path}")

    # ----- UI setup -----
    def _make_app_icon(self) -> QIcon:
        """Use the user's 0.png for the window, taskbar and system tray icon.

        PyInstaller embeds 0.png with --add-data. The generated EXE's own file
        icon is set separately with --icon 0.ico after prepare_icon.py converts it.
        If 0.png is absent, retain a built-in fallback icon so the app still starts.
        """
        icon_path = resource_path("0.png")
        try:
            if icon_path.is_file():
                icon = QIcon(str(icon_path))
                if not icon.isNull():
                    return icon
        except Exception:
            pass

        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor("#171826"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#9480E8"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(3, 3, 58, 58, 14, 14)
        painter.setPen(QColor("white"))
        font = QFont("Microsoft YaHei UI", 34, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "G")
        painter.end()
        return QIcon(pixmap)

    def _build_ui(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#141520; color:#F2F1FA; font-family:'Microsoft YaHei UI','Segoe UI'; font-size:10pt; }
            QGroupBox { background:#1D2030; border:1px solid #303247; border-radius:9px; margin-top:10px; padding:10px; font-weight:600; }
            QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#E2D9FF; }
            QLineEdit, QListWidget { background:#10111A; color:#F2F1FA; border:1px solid #39364F; border-radius:5px; padding:6px; }
            QGroupBox#titleRequiredBox { background:#24213A; border:2px solid #7662CD; border-radius:11px; margin-top:12px; padding:12px; }
            QGroupBox#titleRequiredBox::title { subcontrol-origin:margin; left:14px; padding:0 7px; color:#D8CCFF; font-size:11pt; font-weight:800; background:#24213A; }
            QLineEdit#gameTitle { background:#10111A; color:#FFFFFF; border:2px solid #9480E8; border-radius:8px; padding:10px 13px; font-size:16pt; font-weight:700; selection-background-color:#7662CD; }
            QLineEdit#gameTitle:focus { border:3px solid #C0B1FF; background:#171629; }
            QLineEdit#gameTitle[titleMissing="true"] { border:2px solid #E3A34B; }
            QPushButton { background:#34364A; color:#F2F1FA; border:0; border-radius:6px; padding:8px 11px; }
            QPushButton:hover { background:#454761; }
            QPushButton:disabled { color:#85879A; background:#282A39; }
            QPushButton#primary { background:#7662CD; color:white; font-weight:600; }
            QPushButton#primary:hover { background:#8B78E0; }
            /* Persistent visual feedback: green while monitoring, amber while stopping. */
            QPushButton[monitorState="running"], QPushButton[monitorState="running"]:disabled {
                background:#168653; color:#FFFFFF; font-weight:700; border:1px solid #31B77C;
            }
            QPushButton[monitorState="running"]:hover { background:#1B9A60; }
            QPushButton[monitorState="stopping"], QPushButton[monitorState="stopping"]:disabled {
                background:#A86B23; color:#FFFFFF; font-weight:700; border:1px solid #D89A43;
            }
            QPushButton#stopActive { background:#A43F50; color:#FFFFFF; font-weight:700; border:1px solid #D96778; }
            QPushButton#stopActive:hover { background:#BE4B5E; }
            QPushButton#stopWaiting, QPushButton#stopWaiting:disabled { background:#A86B23; color:#FFFFFF; font-weight:700; }
            QCheckBox { spacing:8px; }
            QSlider::groove:horizontal { height:5px; background:#39364F; border-radius:2px; }
            QSlider::handle:horizontal { background:#AA98F5; width:15px; margin:-5px 0; border-radius:7px; }
            QTextEdit { background:#10111A; color:#E3E1F5; border:0; border-radius:5px; padding:7px; font-family:Consolas,'Microsoft YaHei UI'; font-size:9pt; }
            QScrollArea { border:0; }
        """)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.setCentralWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        heading = QLabel("GALGAME COVER TOOL  ·  封面标题自动监控器")
        heading.setStyleSheet("font-size:18pt;font-weight:700;color:#E2D9FF;padding:3px;")
        root.addWidget(heading)
        desc = QLabel("监控新出现或变化的 0.* 图片；所有成品统一输出 WebP。每张图可手动确认标题，也可开启自动处理。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#B7BAD0;")
        root.addWidget(desc)

        # The game title is required and intentionally placed before all action buttons.
        # Users should immediately see which text will be burned into the next image.
        title_box = QGroupBox("① 游戏名称（必填：先填写，再开始监控/加水印）")
        title_box.setObjectName("titleRequiredBox")
        title_layout = QVBoxLayout(title_box)
        title_layout.setContentsMargins(12, 14, 12, 12)
        title_layout.setSpacing(7)
        title_hint = QLabel("水印文字完全取自这里。每张图片可以使用不同游戏名；留空时不允许启动监控或生成水印。")
        title_hint.setWordWrap(True)
        title_hint.setStyleSheet("color:#C9C2E8;font-size:9pt;")
        title_layout.addWidget(title_hint)
        self.title_edit = QLineEdit()
        self.title_edit.setObjectName("gameTitle")
        self.title_edit.setMinimumHeight(52)
        self.title_edit.setPlaceholderText("必填，例如：Summer Pockets")
        self.title_edit.setClearButtonEnabled(True)
        self.title_edit.setToolTip("必填：这里填写的游戏名称会绘制到图片中央。")
        self.title_edit.textChanged.connect(self._on_title_text_changed)
        title_layout.addWidget(self.title_edit)
        self.title_status_label = QLabel("⚠ 尚未填写游戏名称：当前不能开始监控或添加水印。")
        self.title_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_status_label.setWordWrap(True)
        self.title_status_label.setStyleSheet("color:#F0B45E;font-weight:700;")
        title_layout.addWidget(self.title_status_label)
        root.addWidget(title_box)

        # Prominent action row at the very top, immediately below the required title.
        #
        action_box = QGroupBox("快捷操作")
        action_v = QVBoxLayout(action_box)
        action_row = QHBoxLayout()
        self.confirm_button = QPushButton("✓ 确认加水印并替换原图")
        self.confirm_button.setObjectName("primary")
        self.confirm_button.clicked.connect(self.confirm_current_image)
        self.confirm_button.setEnabled(False)
        action_row.addWidget(self.confirm_button)
        self.start_button = QPushButton("▶ 开始监控")
        self.start_button.setObjectName("primary")
        self.start_button.setProperty("monitorState", "idle")
        self.start_button.clicked.connect(self.start_monitoring)
        action_row.addWidget(self.start_button)
        self.stop_button = QPushButton("■ 停止监控")
        self.stop_button.clicked.connect(self.stop_monitoring)
        self.stop_button.setEnabled(False)
        action_row.addWidget(self.stop_button)
        self.hide_button = QPushButton("↘ 隐藏到托盘")
        self.hide_button.clicked.connect(self.hide_to_tray)
        action_row.addWidget(self.hide_button)
        action_v.addLayout(action_row)

        mode_row = QHBoxLayout()
        self.auto_checkbox = QCheckBox("自动添加水印（使用当前标题，无需逐张点击确认）")
        self.auto_checkbox.setChecked(False)
        self.auto_checkbox.toggled.connect(self._auto_mode_changed)
        mode_row.addWidget(self.auto_checkbox)
        mode_row.addStretch()
        self.status_label = QLabel("尚未开始监控")
        self.status_label.setStyleSheet("color:#B7BAD0;font-weight:600;")
        mode_row.addWidget(self.status_label)
        action_v.addLayout(mode_row)
        self.pending_label = QLabel("尚未发现待处理图片")
        self.pending_label.setWordWrap(True)
        self.pending_label.setStyleSheet("color:#B7BAD0;")
        action_v.addWidget(self.pending_label)
        tip = QLabel("自动模式下，每张图片使用处理开始时输入框中的标题。不同游戏请及时更换标题；关闭自动模式后，新图需手动确认。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#9C9FB5;font-size:9pt;")
        action_v.addWidget(tip)
        memory_row = QHBoxLayout()
        self.clear_memory_button = QPushButton("清空防重复记忆")
        self.clear_memory_button.clicked.connect(self.clear_processed_memory)
        memory_row.addWidget(self.clear_memory_button)
        self.memory_count_label = QLabel("防重复记忆：0 条")
        self.memory_count_label.setStyleSheet("color:#B7BAD0;")
        memory_row.addWidget(self.memory_count_label)
        memory_row.addStretch()
        action_v.addLayout(memory_row)
        root.addWidget(action_box)

        folder_box = QGroupBox("监控文件夹")
        folder_layout = QHBoxLayout(folder_box)
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("选择要监控的文件夹")
        folder_layout.addWidget(self.folder_edit, 1)
        self.folder_button = QPushButton("选择文件夹…")
        self.folder_button.clicked.connect(self.choose_folder)
        folder_layout.addWidget(self.folder_button)
        root.addWidget(folder_box)

        settings_box = QGroupBox("水印样式与输出设置")
        grid = QGridLayout(settings_box)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(7)
        grid.addWidget(QLabel("标题最大宽度"), 0, 0)
        self.ratio_slider = QSlider(Qt.Orientation.Horizontal)
        self.ratio_slider.setRange(15, 80)
        self.ratio_slider.setValue(33)
        grid.addWidget(self.ratio_slider, 0, 1)
        self.ratio_label = QLabel("33%")
        grid.addWidget(self.ratio_label, 0, 2)

        grid.addWidget(QLabel("阴影强度"), 1, 0)
        self.shadow_slider = QSlider(Qt.Orientation.Horizontal)
        self.shadow_slider.setRange(0, 100)
        self.shadow_slider.setSingleStep(5)
        self.shadow_slider.setValue(75)
        grid.addWidget(self.shadow_slider, 1, 1)
        self.shadow_label = QLabel("75")
        grid.addWidget(self.shadow_label, 1, 2)

        grid.addWidget(QLabel("描边强度"), 2, 0)
        self.outline_slider = QSlider(Qt.Orientation.Horizontal)
        self.outline_slider.setRange(0, 100)
        self.outline_slider.setSingleStep(5)
        self.outline_slider.setValue(90)
        grid.addWidget(self.outline_slider, 2, 1)
        self.outline_label = QLabel("90")
        grid.addWidget(self.outline_label, 2, 2)

        grid.addWidget(QLabel("输出 WebP 质量"), 3, 0)
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(50, 100)
        self.quality_slider.setSingleStep(5)
        self.quality_slider.setValue(DEFAULT_WEBP_QUALITY)
        grid.addWidget(self.quality_slider, 3, 1)
        self.quality_label = QLabel(str(DEFAULT_WEBP_QUALITY))
        grid.addWidget(self.quality_label, 3, 2)

        settings_buttons = QHBoxLayout()
        self.color_button = QPushButton("选择标题颜色…")
        self.color_button.clicked.connect(self.choose_text_color)
        settings_buttons.addWidget(self.color_button)
        self.color_chip = QLabel("白色标题")
        self.color_chip.setStyleSheet("background:#FFFFFF;color:#111111;padding:5px 10px;border-radius:4px;")
        settings_buttons.addWidget(self.color_chip)
        self.preview_button = QPushButton("预览图片效果…")
        self.preview_button.clicked.connect(self.choose_preview)
        settings_buttons.addWidget(self.preview_button)
        settings_buttons.addStretch()
        grid.addLayout(settings_buttons, 4, 1, 1, 2)
        root.addWidget(settings_box)

        queue_box = QGroupBox("待处理图片（选择其中一张，再确认标题）")
        queue_layout = QVBoxLayout(queue_box)
        self.queue_list = QListWidget()
        self.queue_list.setMinimumHeight(95)
        self.queue_list.currentItemChanged.connect(self._queue_selection_changed)
        queue_layout.addWidget(self.queue_list)
        root.addWidget(queue_box)

        preview_log = QSplitter(Qt.Orientation.Horizontal)
        preview_panel = QGroupBox("预览")
        preview_layout = QVBoxLayout(preview_panel)
        self.preview_label = QLabel("选择预览图片后在此显示")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(220, 180)
        self.preview_label.setStyleSheet("background:#10111A;color:#8E91AA;border-radius:5px;")
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        preview_layout.addWidget(self.preview_label)
        log_panel = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_panel)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMinimumHeight(180)
        log_layout.addWidget(self.log_edit)
        preview_log.addWidget(preview_panel)
        preview_log.addWidget(log_panel)
        preview_log.setStretchFactor(0, 1)
        preview_log.setStretchFactor(1, 2)
        root.addWidget(preview_log)

        foot = QLabel("输入支持 JPG / PNG / WebP / AVIF / HEIC / HEIF / BMP / GIF / TIFF / ICO 等常见栅格格式；动画只取首帧。成品均为 WebP，成功后删除原图。")
        foot.setWordWrap(True)
        foot.setStyleSheet("color:#8E91AA;font-size:9pt;")
        root.addWidget(foot)

        author_row = QHBoxLayout()
        author_row.addStretch()
        author_label = QLabel(
            '开发者：<b>码铃薯</b>　|　'
            '官网：<a href="https://www.tudoucode.cn">www.tudoucode.cn</a>　|　'
            f'GitHub：<a href="{GITHUB_REPO_URL}">项目主页</a>　'
            f'<a href="{GITHUB_RELEASES_URL}">发布下载（Releases）</a>'
        )
        author_label.setTextFormat(Qt.TextFormat.RichText)
        author_label.setOpenExternalLinks(True)
        author_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        author_label.setStyleSheet("color:#A8A4C2;font-size:9pt;padding-top:4px;padding-bottom:2px;")
        author_row.addWidget(author_label)
        root.addLayout(author_row)

        self.ratio_slider.valueChanged.connect(lambda v: (self.ratio_label.setText(f"{v}%"), self._save_settings()))
        self.shadow_slider.valueChanged.connect(lambda v: (self.shadow_label.setText(str(v)), self._save_settings()))
        self.outline_slider.valueChanged.connect(lambda v: (self.outline_label.setText(str(v)), self._save_settings()))
        self.quality_slider.valueChanged.connect(lambda v: (self.quality_label.setText(str(v)), self._save_settings()))

    # ----- Native Qt system tray -----
    def _create_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.log("Windows 当前未报告可用系统托盘；请检查任务栏通知区域设置。关闭窗口将保留主窗口，不会静默退出。", "error")
            self.hide_button.setEnabled(False)
            return
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self._make_app_icon())
        self.tray.setToolTip(f"{APP_NAME} v{APP_VERSION}")
        self.tray_menu = QMenu(self)
        self.show_action = QAction("显示主界面", self)
        self.show_action.triggered.connect(self.restore_window)
        self.tray_menu.addAction(self.show_action)
        self.tray_menu.addSeparator()
        self.monitor_action = QAction("开始监控", self)
        self.monitor_action.triggered.connect(self._tray_toggle_monitor)
        self.tray_menu.addAction(self.monitor_action)
        self.clear_memory_action = QAction("清空防重复记忆", self)
        self.clear_memory_action.triggered.connect(self.clear_processed_memory)
        self.tray_menu.addAction(self.clear_memory_action)
        self.tray_menu.addSeparator()
        self.exit_action = QAction("彻底退出后台程序", self)
        self.exit_action.triggered.connect(self.request_exit)
        self.tray_menu.addAction(self.exit_action)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        QTimer.singleShot(700, self._verify_tray)

    def _verify_tray(self):
        if self.tray is not None and self.tray.isVisible():
            self.log("Qt 原生系统托盘已注册。若看不到图标，请检查任务栏右侧的 ^ 隐藏图标区域。", "success")
        elif self.tray is not None:
            self.log("托盘组件已创建，但 Windows 未确认图标可见；检查通知区域设置或重启 Windows 资源管理器。", "error")

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore_window()

    def _tray_toggle_monitor(self):
        if self.monitoring:
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def _update_tray_menu(self):
        if self.tray is not None and self.monitor_action is not None:
            self.monitor_action.setText("停止监控" if self.monitoring or self.monitor_stopping else "开始监控")

    def hide_to_tray(self):
        if self.tray is None or not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.warning(self, APP_NAME, "系统托盘不可用，当前不能安全隐藏到托盘。请检查 Windows 通知区域设置。")
            return
        self.hide()
        self.status_label.setText("后台运行中（系统托盘）")
        self.log("主窗口已隐藏到系统托盘，后台监控继续运行。右键托盘图标可显示主界面或彻底退出。")

    def restore_window(self):
        if self.shutting_down:
            return
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(120, self._hide_if_minimized)

    def _hide_if_minimized(self):
        if self.isMinimized() and not self.shutting_down:
            if self.tray is not None and QSystemTrayIcon.isSystemTrayAvailable():
                self.hide_to_tray()

    def closeEvent(self, event):
        if self.allow_close or self.shutting_down:
            event.accept()
            return
        if self.tray is not None and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide_to_tray()
            if self.tray:
                self.tray.showMessage(APP_NAME, "程序已转入后台运行。右键托盘图标可彻底退出。", QSystemTrayIcon.MessageIcon.Information, 1800)
        else:
            event.ignore()
            QMessageBox.warning(self, APP_NAME, "系统托盘不可用，未关闭程序。请先解决托盘问题，再通过托盘菜单退出。")

    def request_exit(self):
        if self.shutting_down:
            return
        if self.processing:
            self.exit_when_idle = True
            self.auto_queue.clear()
            self.auto_checkbox.setChecked(False)
            if self.stop_event:
                self.stop_event.set()
            self.log("已请求彻底退出：先停止监控，当前图片安全处理结束后退出。")
            self.status_label.setText("当前图片完成后退出…")
            return
        self._finish_exit()

    def _finish_exit(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        if self.stop_event:
            self.stop_event.set()
        if self.tray is not None:
            self.tray.hide()
        self.allow_close = True
        self.close()
        QApplication.quit()

    # ----- Required game title / settings / folder -----
    def _on_title_text_changed(self, *_):
        """Reflect the required title field immediately and persist the setting."""
        title = self.title_edit.text().strip()
        missing = not bool(title)
        self.title_edit.setProperty("titleMissing", "true" if missing else "false")
        self.title_edit.style().unpolish(self.title_edit)
        self.title_edit.style().polish(self.title_edit)
        self.title_edit.update()
        if missing:
            self.title_status_label.setText("⚠ 尚未填写游戏名称：当前不能开始监控或添加水印。")
            self.title_status_label.setStyleSheet("color:#F0B45E;font-weight:700;")
        else:
            shown = title if len(title) <= 60 else title[:57] + "..."
            self.title_status_label.setText(f"✓ 当前水印标题：{shown}")
            self.title_status_label.setStyleSheet("color:#3CD39B;font-weight:700;")
        self._save_settings()

    def _settings_path(self) -> Path:
        return app_data_dir() / "settings.json"

    def _load_settings(self):
        self._loading_settings = True
        try:
            path = self._settings_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            folder = data.get("folder", "")
            if folder and Path(folder).exists():
                self.folder_edit.setText(folder)
            self.title_edit.setText(data.get("title", ""))
            self.ratio_slider.setValue(int(data.get("ratio", 33)))
            self.shadow_slider.setValue(int(data.get("shadow", 75)))
            self.outline_slider.setValue(int(data.get("outline", 90)))
            self.quality_slider.setValue(int(data.get("webp_quality", 90)))
            self.auto_checkbox.setChecked(bool(data.get("auto", False)))
            self.text_color = data.get("text_color", "#FFFFFF")
            self._update_color_chip()
        except Exception as exc:
            self.log(f"读取设置失败：{exc}", "error")
        finally:
            self._loading_settings = False
            self._on_title_text_changed()
            self._save_settings()

    def _save_settings(self, *_):
        if self._loading_settings or not hasattr(self, "title_edit"):
            return
        data = {
            "folder": self.folder_edit.text().strip(), "title": self.title_edit.text(),
            "ratio": self.ratio_slider.value(), "shadow": self.shadow_slider.value(),
            "outline": self.outline_slider.value(), "webp_quality": self.quality_slider.value(),
            "auto": self.auto_checkbox.isChecked(), "text_color": self.text_color,
        }
        try:
            path = self._settings_path()
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            pass

    def choose_folder(self):
        initial = self.folder_edit.text().strip() or str(Path.home() / "Desktop")
        chosen = QFileDialog.getExistingDirectory(self, "选择要监控的文件夹", initial)
        if chosen:
            self.folder_edit.setText(chosen)
            self.confirmation_queue.clear()
            self.auto_queue.clear()
            self.active_confirmation = None
            self._refresh_queue_ui()
            self._save_settings()
            self.log(f"已选择监控文件夹：{chosen}")

    def choose_text_color(self):
        color = QColorDialog.getColor(QColor(self.text_color), self, "选择标题颜色")
        if color.isValid():
            self.text_color = color.name()
            self._update_color_chip()
            self._save_settings()

    def _update_color_chip(self):
        self.color_chip.setText(self.text_color)
        luminance = QColor(self.text_color).lightness()
        fg = "#111111" if luminance > 170 else "#FFFFFF"
        self.color_chip.setStyleSheet(f"background:{self.text_color};color:{fg};padding:5px 10px;border-radius:4px;")

    def _options_for_title(self, title: str) -> dict:
        return {
            "title": title,
            "max_width_ratio": self.ratio_slider.value() / 100.0,
            "text_color": self.text_color,
            "shadow_strength": self.shadow_slider.value(),
            "outline_strength": self.outline_slider.value(),
            "webp_quality": self.quality_slider.value(),
        }

    # ----- Preview -----
    def choose_preview(self):
        filters = " ".join(f"*{x}" for x in sorted(SUPPORTED_EXTENSIONS))
        path, _ = QFileDialog.getOpenFileName(self, "选择一张图片进行预览", self.folder_edit.text(), f"支持的图片 ({filters});;所有文件 (*.*)")
        if not path:
            return
        self._show_preview(Path(path))

    def _show_preview(self, path: Path):
        title = self.title_edit.text().strip()
        if not title:
            QMessageBox.information(self, APP_NAME, "请先填写游戏名称，再预览效果。")
            return
        try:
            original = load_source_rgba(path)
            rendered, _font_size = render_titled_image(
                original, title, self.ratio_slider.value() / 100.0, self.text_color,
                self.shadow_slider.value(), self.outline_slider.value(),
            )
            rendered.thumbnail((420, 330), Image.Resampling.LANCZOS)
            from io import BytesIO
            stream = BytesIO()
            rendered.save(stream, format="PNG")
            pixmap = QPixmap()
            pixmap.loadFromData(QByteArray(stream.getvalue()), "PNG")
            self.preview_pixmap = pixmap
            self._fit_preview()
            self.log(f"预览：{path.name}")
        except Exception as exc:
            QMessageBox.critical(self, "预览失败", str(exc))
            self.log(f"预览失败：{path.name}；{exc}", "error")

    def _fit_preview(self):
        if self.preview_pixmap is not None:
            size = self.preview_label.size()
            self.preview_label.setPixmap(self.preview_pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_preview()

    # ----- Monitor lifecycle -----
    def _initial_scan(self, folder: Path) -> Dict[Path, FileSignature]:
        existing: Dict[Path, FileSignature] = {}
        for entry in folder.iterdir():
            if is_target_image(entry):
                try:
                    existing[entry] = file_signature(entry)
                except OSError:
                    pass
        return existing

    def _set_monitor_button_state(self, state: str):
        """Give immediate, persistent color/text feedback for monitor state."""
        if state == "running":
            self.start_button.setText("● 监控已开启")
            self.start_button.setEnabled(False)
            self.start_button.setProperty("monitorState", "running")
            self.start_button.setObjectName("")
            self.stop_button.setText("■ 停止监控")
            self.stop_button.setObjectName("stopActive")
            self.stop_button.setEnabled(True)
        elif state == "stopping":
            self.start_button.setText("⏳ 正在停止监控…")
            self.start_button.setEnabled(False)
            self.start_button.setProperty("monitorState", "stopping")
            self.start_button.setObjectName("")
            self.stop_button.setText("⏳ 正在停止…")
            self.stop_button.setObjectName("stopWaiting")
            self.stop_button.setEnabled(False)
        else:
            self.start_button.setText("▶ 开始监控")
            self.start_button.setEnabled(True)
            self.start_button.setProperty("monitorState", "idle")
            self.start_button.setObjectName("primary")
            self.stop_button.setText("■ 停止监控")
            self.stop_button.setObjectName("")
            self.stop_button.setEnabled(False)

        # Qt stylesheets may cache selector results after objectName/property changes.
        for button in (self.start_button, self.stop_button):
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def start_monitoring(self):
        if self.monitoring or self.monitor_stopping:
            return
        if not self.title_edit.text().strip():
            QMessageBox.warning(
                self, "游戏名称必填",
                "请先在窗口顶部醒目的‘游戏名称（必填）’区域输入当前游戏名称。\n\n"
                "所有水印文字都来自这个输入框；没有标题就无法生成有意义的水印。",
            )
            self.title_edit.setFocus()
            return
        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            QMessageBox.warning(self, APP_NAME, "请先选择要监控的文件夹。")
            self.choose_folder()
            folder_text = self.folder_edit.text().strip()
            if not folder_text:
                return
        folder = Path(folder_text).expanduser()
        if not folder.exists() or not folder.is_dir():
            QMessageBox.critical(self, APP_NAME, "所选路径不存在或不是文件夹。")
            return
        try:
            baseline = self._initial_scan(folder)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"无法读取监控文件夹：\n{exc}")
            return
        self.stop_event = threading.Event()
        self.monitoring = True
        self.monitor_stopping = False
        self._set_monitor_button_state("running")
        self.status_label.setStyleSheet("color:#3CD39B;font-weight:700;")
        self.folder_edit.setEnabled(False)
        self.folder_button.setEnabled(False)
        self.status_label.setText("监控中…")
        self.log(f"开始监控：{folder}")
        self.log(f"已记录 {len(baseline)} 张现存目标图片；启动时已有图片不会自动处理。")
        self.log("自动模式已开启。" if self.auto_checkbox.isChecked() else "手动模式已开启；新图片会等待顶部确认按钮。")
        self.monitor_thread = threading.Thread(target=self._monitor_loop, args=(folder, baseline, self.stop_event), daemon=True)
        self.monitor_thread.start()
        self._update_tray_menu()

    def stop_monitoring(self):
        if not self.monitoring or self.monitor_stopping:
            return
        self.monitor_stopping = True
        self.status_label.setText("正在停止监控…")
        self.status_label.setStyleSheet("color:#E8AE52;font-weight:700;")
        self._set_monitor_button_state("stopping")
        if self.stop_event:
            self.stop_event.set()
        self.log("已请求停止监控。")
        self._update_tray_menu()

    def _monitor_loop(self, folder: Path, known: Dict[Path, FileSignature], stop_event: threading.Event):
        pending: Dict[Path, Tuple[FileSignature, float]] = {}
        announced: Dict[Path, FileSignature] = {}
        while not stop_event.is_set():
            now = time.monotonic()
            current: Dict[Path, FileSignature] = {}
            try:
                for entry in folder.iterdir():
                    if is_target_image(entry):
                        try:
                            current[entry] = file_signature(entry)
                        except (OSError, PermissionError):
                            pass
            except (OSError, PermissionError) as exc:
                self.events.put(("log", f"读取文件夹失败：{exc}"))
                stop_event.wait(2.0)
                continue

            for old_path in list(known):
                if old_path not in current:
                    known.pop(old_path, None)
            for old_path in list(pending):
                if old_path not in current:
                    pending.pop(old_path, None)
                    announced.pop(old_path, None)
                    self.events.put(("removed", old_path))

            for path, sig in current.items():
                if path not in known:
                    if path not in pending or pending[path][0] != sig:
                        pending[path] = (sig, now)
                        self.events.put(("log", f"发现新图片：{path.name}，等待文件写入完成…"))
                elif known[path] != sig:
                    if path not in pending or pending[path][0] != sig:
                        pending[path] = (sig, now)
                        announced.pop(path, None)
                        self.events.put(("log", f"检测到图片变化：{path.name}，等待文件稳定…"))

            for path, (candidate_sig, since) in list(pending.items()):
                if path not in current:
                    continue
                if current[path] != candidate_sig:
                    pending[path] = (current[path], now)
                    announced.pop(path, None)
                    continue
                if now - since < STABLE_SECONDS or announced.get(path) == candidate_sig:
                    continue
                try:
                    if file_signature(path) != candidate_sig:
                        changed_sig = file_signature(path)
                        pending[path] = (changed_sig, now)
                        announced.pop(path, None)
                        continue
                    digest = sha256_file(path)
                    if file_signature(path) != candidate_sig:
                        changed_sig = file_signature(path)
                        pending[path] = (changed_sig, now)
                        announced.pop(path, None)
                        continue
                except (OSError, PermissionError) as exc:
                    self.events.put(("log", f"暂时无法读取 {path.name}：{exc}"))
                    pending[path] = (candidate_sig, now + 1.0)
                    continue

                previous = self.memory.lookup(digest)
                if previous:
                    try:
                        if file_signature(path) != candidate_sig:
                            changed_sig = file_signature(path)
                            pending[path] = (changed_sig, time.monotonic())
                            announced.pop(path, None)
                            continue
                        moved_to = unique_skipped_path(path)
                        os.replace(path, moved_to)
                        known.pop(path, None)
                        pending.pop(path, None)
                        announced.pop(path, None)
                        self.events.put(("duplicate_skipped", {"source": path, "destination": moved_to, "record": previous}))
                    except OSError as exc:
                        known[path] = candidate_sig
                        pending.pop(path, None)
                        announced[path] = candidate_sig
                        self.events.put(("log", f"检测到历史重复图，但无法改名跳过：{path.name}；{exc}。原图保留。"))
                    continue
                announced[path] = candidate_sig
                known[path] = candidate_sig
                self.events.put(("ready", (path, candidate_sig)))
            stop_event.wait(POLL_SECONDS)
        self.events.put(("stopped", None))

    # ----- Queue UI / auto mode -----
    def _auto_mode_changed(self, enabled: bool):
        self._save_settings()
        self.auto_title_wait_notified = False
        if enabled:
            self.log("自动加水印已开启：当前标题将用于待处理的新图片。请在不同游戏图片到来前及时修改标题。")
            for path, sig in self.confirmation_queue.items():
                self.auto_queue[path] = sig
            self._pump_auto_queue()
        else:
            self.auto_queue.clear()
            self.log("自动加水印已关闭：未开始处理的图片将改为手动确认。")
        self._refresh_queue_ui()

    def _refresh_queue_ui(self):
        selected_path = self._selected_queue_path()
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        for path, signature in self.confirmation_queue.items():
            suffix = "（已排入自动队列）" if path in self.auto_queue else ""
            item = QListWidgetItem(f"{path.name}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.queue_list.addItem(item)
            if selected_path == path:
                self.queue_list.setCurrentItem(item)
        if self.queue_list.currentRow() < 0 and self.queue_list.count() > 0:
            self.queue_list.setCurrentRow(0)
        self.queue_list.blockSignals(False)
        self._update_pending_label()

    def _selected_queue_path(self) -> Optional[Path]:
        item = self.queue_list.currentItem() if hasattr(self, "queue_list") else None
        if item is not None:
            value = item.data(Qt.ItemDataRole.UserRole)
            if value:
                return Path(value)
        return self.active_confirmation

    def _queue_selection_changed(self, current, _previous=None):
        if current is not None:
            raw = current.data(Qt.ItemDataRole.UserRole)
            if raw:
                self.active_confirmation = Path(raw)
                self._update_pending_label()

    def _update_pending_label(self):
        if self.processing:
            mode = "自动处理" if self.processing_auto else "正在处理"
            self.pending_label.setText(f"{mode}：{self.processing_path.name if self.processing_path else ''}")
            self.confirm_button.setEnabled(False)
            return
        path = self._selected_queue_path()
        if path not in self.confirmation_queue:
            path = next(iter(self.confirmation_queue), None)
            self.active_confirmation = path
        if path is None:
            self.pending_label.setText("尚未发现待处理图片")
            self.confirm_button.setEnabled(False)
            return
        n = len(self.confirmation_queue)
        suffix = f"；另有 {n - 1} 张待处理" if n > 1 else ""
        how = "自动队列" if path in self.auto_queue else "请填写本张游戏名后点击确认"
        self.pending_label.setText(f"当前：{path.name}；{how}{suffix}")
        self.confirm_button.setEnabled(not self.auto_checkbox.isChecked())

    def confirm_current_image(self):
        if self.processing:
            return
        path = self._selected_queue_path()
        if path not in self.confirmation_queue:
            QMessageBox.information(self, APP_NAME, "暂无待处理图片。启动监控并放入新的目标图片后即可确认。")
            return
        self._start_processing(path, self.confirmation_queue[path], auto=False)

    def _pump_auto_queue(self):
        if not self.auto_checkbox.isChecked() or self.processing or not self.auto_queue or self.exit_when_idle:
            self._update_pending_label()
            return
        while self.auto_queue:
            path, sig = next(iter(self.auto_queue.items()))
            if path not in self.confirmation_queue or not path.exists():
                self.auto_queue.pop(path, None)
                continue
            title = self.title_edit.text().strip()
            if not title:
                if not self.auto_title_wait_notified:
                    self.log("自动模式正在等待游戏名称。填写标题后会继续处理。", "error")
                    self.auto_title_wait_notified = True
                self.status_label.setText("自动模式等待游戏名称")
                self._update_pending_label()
                return
            self.auto_queue.pop(path, None)
            self._start_processing(path, sig, auto=True)
            return
        self._update_pending_label()

    def _start_processing(self, source: Path, signature: FileSignature, auto: bool):
        if self.processing:
            return
        title = self.title_edit.text().strip()
        if not title:
            if auto:
                self.status_label.setText("自动模式等待游戏名称")
                return
            QMessageBox.warning(self, APP_NAME, "请先填写当前这张图片对应的游戏名称。")
            self.title_edit.setFocus()
            return
        if not source.exists():
            self.confirmation_queue.pop(source, None)
            self.auto_queue.pop(source, None)
            self._refresh_queue_ui()
            return
        options = self._options_for_title(title)
        self.processing = True
        self.processing_auto = auto
        self.processing_path = source
        self.status_label.setText("自动加水印中…" if auto else "正在生成 WebP 并替换原图…")
        self.log(f"{'自动处理' if auto else '手动确认处理'}：{source.name}；标题“{title}”。")
        self._update_pending_label()
        threading.Thread(target=self._process_image, args=(source, signature, options), daemon=True).start()

    # ----- Watermark worker -----
    def _process_image(self, source: Path, expected_signature: FileSignature, options: dict):
        temp_path: Optional[Path] = None
        destination: Optional[Path] = None
        output_created = False
        memory_record_id: Optional[str] = None
        source_deleted = False
        try:
            if not source.exists():
                raise RuntimeError("原图已不存在；请等待监控更新队列。")
            if file_signature(source) != expected_signature:
                raise RuntimeError("处理前原图发生变化；请等待它稳定后重新处理。")
            source_hash = sha256_file(source)
            if file_signature(source) != expected_signature:
                raise RuntimeError("读取原图期间文件变化；本次未处理。")
            previous = self.memory.lookup(source_hash)
            if previous:
                skipped_to = unique_skipped_path(source)
                os.replace(source, skipped_to)
                self.events.put(("duplicate_skipped", {"source": source, "destination": skipped_to, "record": previous, "processing": True}))
                return

            destination = unique_watermarked_path(source)
            temp_path = destination.with_name(f".{destination.stem}.{os.getpid()}-{threading.get_ident()}.tmp.webp")
            width, height, font_size = make_titled_image(source, temp_path, options)
            if file_signature(source) != expected_signature:
                raise RuntimeError("加水印期间原图发生变化；原图不会删除。")
            os.replace(temp_path, destination)
            temp_path = None
            output_created = True
            if file_signature(source) != expected_signature:
                raise RuntimeError("生成输出后发现原图发生变化；原图保留。")
            output_hash = sha256_file(destination)
            if file_signature(source) != expected_signature:
                raise RuntimeError("记录输出期间原图发生变化；原图保留。")
            memory_record_id = self.memory.register(source_hash, output_hash, options["title"], destination.name)
            if file_signature(source) != expected_signature:
                self.memory.forget_record(memory_record_id)
                memory_record_id = None
                raise RuntimeError("保存防重复记忆期间原图发生变化；原图保留。")
            try:
                source.unlink()
                source_deleted = True
            except OSError as exc:
                try:
                    self.memory.forget_record(memory_record_id)
                    memory_record_id = None
                except Exception:
                    pass
                raise RuntimeError(f"已生成 WebP 但无法删除原图（可能被其他程序占用）：{exc}。原图保留。") from exc
            self.events.put(("process_done", {
                "source": source, "destination": destination, "success": True,
                "width": width, "height": height, "font_size": font_size,
                "quality": options["webp_quality"], "title": options["title"],
            }))
        except Exception as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            if output_created and destination is not None and not source_deleted:
                try:
                    destination.unlink()
                except OSError:
                    pass
            if memory_record_id and not source_deleted:
                try:
                    self.memory.forget_record(memory_record_id)
                except Exception:
                    pass
            self.events.put(("process_done", {"source": source, "destination": destination, "success": False, "error": str(exc), "title": options.get("title", "")}))

    # ----- Memory and event queue -----
    def clear_processed_memory(self):
        if self.processing:
            QMessageBox.warning(self, APP_NAME, "当前图片正在处理，请完成后再清空防重复记忆。")
            return
        count = self.memory.record_count()
        if count <= 0:
            QMessageBox.information(self, APP_NAME, "当前没有需要清理的防重复记忆。")
            self._refresh_memory_count()
            return
        answer = QMessageBox.warning(
            self, "清空防重复记忆",
            f"确定清空全部 {count} 条记录吗？\n\n只清除记忆索引，不会删除现有 WebP 成品。请使用干净原图重新处理，避免叠加标题。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self.memory.clear_all()
            self._refresh_memory_count()
            self.log(f"已清空防重复记忆：{removed} 条；现有 WebP 文件未删除。", "success")
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"清理失败：\n{exc}")

    def _refresh_memory_count(self):
        self.memory_count_label.setText(f"防重复记忆：{self.memory.record_count()} 条")

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload, "error" if "失败" in payload or "警告" in payload else "")
                elif kind == "ready":
                    path, signature = payload
                    self.confirmation_queue[path] = signature
                    if self.active_confirmation not in self.confirmation_queue:
                        self.active_confirmation = path
                    if self.auto_checkbox.isChecked():
                        self.auto_queue[path] = signature
                        self.log(f"图片稳定并进入自动队列：{path.name}")
                    else:
                        self.log(f"图片已就绪，等待手动确认：{path.name}")
                    self._refresh_queue_ui()
                    self._pump_auto_queue()
                elif kind == "removed":
                    path = payload
                    self.confirmation_queue.pop(path, None)
                    self.auto_queue.pop(path, None)
                    if self.active_confirmation == path:
                        self.active_confirmation = None
                    self._refresh_queue_ui()
                elif kind == "duplicate_skipped":
                    result = payload
                    source = result["source"]
                    self.confirmation_queue.pop(source, None)
                    self.auto_queue.pop(source, None)
                    if result.get("processing"):
                        self.processing = False
                        self.processing_auto = False
                        self.processing_path = None
                    if self.active_confirmation == source:
                        self.active_confirmation = None
                    title = (result.get("record") or {}).get("title", "未知标题")
                    self.log(f"防重复记忆命中：{source.name}；未重复加水印，已改名保留为 {result['destination'].name}（历史标题：{title}）。")
                    self.status_label.setText("已跳过历史重复图片")
                    self._refresh_queue_ui()
                elif kind == "process_done":
                    result = payload
                    self.processing = False
                    self.processing_auto = False
                    self.processing_path = None
                    source = result["source"]
                    self.auto_queue.pop(source, None)
                    if result["success"]:
                        self.confirmation_queue.pop(source, None)
                        if self.active_confirmation == source:
                            self.active_confirmation = None
                        self.log(
                            f"完成：{result['destination'].name}（{result['width']}×{result['height']}，字号 {result['font_size']}px，"
                            f"WebP 质量 {result['quality']}）；原图 {source.name} 已删除，记忆已保存。", "success"
                        )
                        self.status_label.setText("自动处理完成，继续监控…" if self.auto_checkbox.isChecked() else ("监控中…" if self.monitoring else "处理完成"))
                    else:
                        self.log(f"处理失败：{source.name}；{result['error']}", "error")
                        self.status_label.setText("处理失败，请查看日志；原图应已保留")
                    self._refresh_queue_ui()
                    self._refresh_memory_count()
                    self._pump_auto_queue()
                elif kind == "stopped":
                    self.monitoring = False
                    self.monitor_stopping = False
                    self._set_monitor_button_state("idle")
                    self.status_label.setStyleSheet("color:#B7BAD0;font-weight:600;")
                    self.folder_edit.setEnabled(True)
                    self.folder_button.setEnabled(True)
                    if not self.processing:
                        self.status_label.setText("尚未开始监控")
                    self.log("监控已停止。")
                    self._update_tray_menu()
        except queue.Empty:
            pass
        if self.exit_when_idle and not self.processing:
            self._finish_exit()
            return

    def log(self, text: str, kind: str = ""):
        color = "#FF8C8C" if kind == "error" else ("#75D9AF" if kind == "success" else "#E3E1F5")
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.log_edit.append(f'<span style="color:#85879A">[{timestamp}]</span> <span style="color:{color}">{safe}</span>')


# Backwards-friendly name for PyInstaller entry point.
GalgameWatcherApp = GalgameWatcherWindow


def main():
    app = QApplication([])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    window = GalgameWatcherWindow()
    # Apply the same user-provided 0.png at QApplication level (taskbar fallback).
    app.setWindowIcon(window._make_app_icon())
    window.show()
    try:
        app.exec()
    except Exception:
        QMessageBox.critical(window, APP_NAME, traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
