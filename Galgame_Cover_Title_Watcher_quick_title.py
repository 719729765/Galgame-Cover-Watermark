# -*- coding: utf-8 -*-
"""Galgame cover title watcher.

Qt-native Windows system tray via QSystemTrayIcon. Monitors new/changed files
named 0.<supported extension>, adds a title at a selectable position, limits output WebP width to 1000px
without upscaling smaller images, remembers hashes, and safely replaces originals.
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
    QListWidget, QListWidgetItem, QColorDialog, QSizePolicy, QSplitter, QComboBox,
)


class DraggableTitleBar(QWidget):
    """Small frameless-window title bar that lets the user drag its owner."""

    def __init__(self, target_window: QWidget, on_drag=None, parent=None):
        super().__init__(parent)
        self._target_window = target_window
        self._on_drag = on_drag
        self._drag_offset = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("按住这里可以拖动快捷输入框")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self._target_window.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._target_window.move(event.globalPosition().toPoint() - self._drag_offset)
            if callable(self._on_drag):
                self._on_drag()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


APP_NAME = "Galgame 封面标题监控器"
APP_VERSION = "3.2.0"
GITHUB_REPO_URL = "https://github.com/719729765/Galgame-Cover-Watermark"
GITHUB_RELEASES_URL = "https://github.com/719729765/Galgame-Cover-Watermark/releases"
POLL_SECONDS = 1.5
STABLE_SECONDS = 2.0
DEFAULT_WEBP_QUALITY = 90
MAX_OUTPUT_WIDTH = 1000  # Limit output WebP width; smaller images remain unchanged.
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
    position: str = "center",
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

    # Place the title at one of nine preset anchor points. Margins scale with
    # the image while remaining usable for small covers.
    margin = max(4, int(min(width, height) * 0.04))
    margin = min(margin, max(1, min(width, height) // 4))
    position_points = {
        "top_left": (margin, margin, "lt"),
        "top": (width // 2, margin, "mt"),
        "top_right": (width - margin, margin, "rt"),
        "left": (margin, height // 2, "lm"),
        "center": (width // 2, height // 2, "mm"),
        "right": (width - margin, height // 2, "rm"),
        "bottom_left": (margin, height - margin, "lb"),
        "bottom": (width // 2, height - margin, "mb"),
        "bottom_right": (width - margin, height - margin, "rb"),
    }
    x, y, anchor = position_points.get(position, position_points["center"])

    shadow_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    blur = max(1, int(font_size * 0.12))
    offset = max(2, int(font_size * 0.045))
    shadow_alpha = int(235 * shadow_strength / 100)
    if shadow_alpha:
        shadow_draw.text(
            (x + offset, y + offset), title,
            font=font, anchor=anchor, fill=(0, 0, 0, shadow_alpha),
            stroke_width=max(1, int(font_size * 0.025)),
            stroke_fill=(0, 0, 0, shadow_alpha),
        )
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur))
        image = Image.alpha_composite(image, shadow_layer)

    outline_alpha = int(245 * outline_strength / 100)
    stroke_width = max(1, int(font_size * 0.055)) if outline_strength else 0
    ImageDraw.Draw(image).text(
        (x, y), title, font=font, anchor=anchor, fill=text_color,
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, outline_alpha) if outline_strength else None,
    )
    return image, font_size


def make_titled_image(source: Path, destination: Path, options: dict) -> Tuple[int, int, int]:
    source_image = load_source_rgba(source)

    # Resize before adding the title so the output dimensions and watermark are
    # designed together. Preserve aspect ratio and never enlarge small images.
    original_width, original_height = source_image.size
    if original_width > MAX_OUTPUT_WIDTH:
        output_height = max(1, int(original_height * MAX_OUTPUT_WIDTH / original_width + 0.5))
        source_image = source_image.resize(
            (MAX_OUTPUT_WIDTH, output_height), Image.Resampling.LANCZOS
        )

    # These are the dimensions that will be written to the output WebP and shown in the log.
    width, height = source_image.size
    result, font_size = render_titled_image(
        source_image, options["title"], options["max_width_ratio"], options["text_color"],
        options["shadow_strength"], options["outline_strength"], options.get("position", "center"),
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

    def title_records(self) -> list[dict]:
        """Return distinct title records for one-time migration to title memory."""
        with self.lock:
            records: dict[str, dict] = {}
            for entry in self.data["hashes"].values():
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("title", "")).strip()
                if not title:
                    continue
                record_id = str(entry.get("record_id") or title)
                records.setdefault(record_id, {
                    "title": title,
                    "output_name": str(entry.get("output_name", "")),
                    "processed_at": str(entry.get("processed_at", "")),
                })
            return list(records.values())

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


class UsedTitleMemory:
    """Persistent one-use memory for watermark titles.

    A normalized title can only be assigned to one successfully processed image.
    Previous titles are imported once from the image hash memory for compatibility
    with installations upgrading from older versions.
    """
    def __init__(self):
        self.path = app_data_dir() / "used_title_memory.json"
        self.lock = threading.RLock()
        self.data = {"version": 1, "titles": {}, "legacy_import_done": False}
        self.load_error: Optional[str] = None
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("titles"), dict):
                    self.data = {
                        "version": 1,
                        "titles": loaded["titles"],
                        "legacy_import_done": bool(loaded.get("legacy_import_done", False)),
                    }
                else:
                    raise ValueError("标题记忆文件格式不正确")
        except Exception as exc:
            self.load_error = str(exc)

    @staticmethod
    def normalize_title(title: str) -> str:
        # Ignore case, full-width/compatibility variants, and accidental extra spaces.
        import unicodedata
        return " ".join(unicodedata.normalize("NFKC", str(title or "")).split()).casefold()

    def is_used(self, title: str) -> bool:
        key = self.normalize_title(title)
        if not key:
            return False
        with self.lock:
            if self.load_error:
                # Fail closed: don't risk reusing a title if history cannot be read.
                return True
            return key in self.data["titles"]

    def count(self) -> int:
        with self.lock:
            return len(self.data["titles"])

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def import_legacy_records(self, records: list[dict]) -> int:
        """Import existing processed titles once; never re-import after the user clears this memory."""
        with self.lock:
            if self.load_error or self.data.get("legacy_import_done", False):
                return 0
            old_titles = dict(self.data["titles"])
            old_done = bool(self.data.get("legacy_import_done", False))
            added = 0
            for record in records:
                title = str(record.get("title", "")).strip()
                key = self.normalize_title(title)
                if not key or key in self.data["titles"]:
                    continue
                self.data["titles"][key] = {
                    "title": title,
                    "output_name": str(record.get("output_name", "")),
                    "processed_at": str(record.get("processed_at", "")),
                    "migrated_from_image_memory": True,
                }
                added += 1
            self.data["legacy_import_done"] = True
            try:
                self._save_locked()
            except Exception:
                self.data["titles"] = old_titles
                self.data["legacy_import_done"] = old_done
                raise
            return added

    def register(self, title: str, source_name: str, output_name: str) -> str:
        key = self.normalize_title(title)
        if not key:
            raise ValueError("水印标题不能为空，无法记录标题记忆。")
        with self.lock:
            if self.load_error:
                raise RuntimeError(f"无法读取已用标题记忆：{self.load_error}。请先清空或修复标题记忆。")
            if key in self.data["titles"]:
                prior = self.data["titles"][key]
                raise RuntimeError(
                    f"标题“{title}”已经使用过（历史成品：{prior.get('output_name', '未知')}），"
                    "为避免标题与图片错配，本次已阻止重复使用。"
                )
            record = {
                "title": title.strip(),
                "source_name": source_name,
                "output_name": output_name,
                "processed_at": datetime.now().isoformat(timespec="seconds"),
            }
            self.data["titles"][key] = record
            try:
                self._save_locked()
            except Exception:
                self.data["titles"].pop(key, None)
                raise
            return key

    def forget(self, key: str):
        with self.lock:
            old = self.data["titles"].pop(key, None)
            if old is None:
                return
            try:
                self._save_locked()
            except Exception:
                self.data["titles"][key] = old
                raise

    def clear_all(self) -> int:
        with self.lock:
            count = len(self.data["titles"])
            old = self.data
            # Mark migration complete so cleared titles aren't immediately imported again.
            self.data = {"version": 1, "titles": {}, "legacy_import_done": True}
            self.load_error = None
            try:
                self._save_locked()
            except Exception:
                self.data = old
                self.load_error = "无法保存已清空的标题记忆文件"
                raise
            return count


class GalgameWatcherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setWindowIcon(self._make_app_icon())
        self.resize(1100, 880)
        self.setMinimumSize(880, 660)

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
        self.auto_title_memory_notified: Optional[str] = None
        self.title_edit_pending = False
        self._last_title_warning_key: Optional[str] = None
        self._title_shake_generation = 0
        self.memory = ProcessedImageMemory()
        self.title_memory = UsedTitleMemory()
        if not self.title_memory.load_error:
            try:
                self.title_memory.import_legacy_records(self.memory.title_records())
            except Exception as exc:
                self.title_memory.load_error = str(exc)
        self.tray: Optional[QSystemTrayIcon] = None
        self.tray_menu: Optional[QMenu] = None
        # Independent always-on-top title editor shown while the main window is minimized.
        self.quick_title_window: Optional[QWidget] = None
        self.quick_title_edit: Optional[QLineEdit] = None
        self.quick_title_status: Optional[QLabel] = None
        self._quick_title_positioned = False
        self._quick_title_user_moved = False

        self._build_ui()
        self._load_settings()
        self._create_quick_title_widget()
        self._create_tray()
        self._refresh_memory_count()
        self._refresh_title_memory_count()
        self._refresh_queue_ui()

        self.event_timer = QTimer(self)
        self.event_timer.setInterval(150)
        self.event_timer.timeout.connect(self._drain_events)
        self.event_timer.start()

        if self.memory.load_error:
            self.log(f"警告：读取防重复记忆失败，旧记忆文件已保留：{self.memory.load_error}", "error")
        if self.title_memory.load_error:
            self.log(f"警告：读取已用标题记忆失败；为避免标题错配，自动与手动处理都会暂停。请检查标题记忆或使用‘清空已用标题记忆’：{self.title_memory.load_error}", "error")
        self.log("已启动。请在主界面顶部或最小化后右下角的快捷输入框填写游戏名称；空标题不能启动监控。托盘由 Qt 原生 QSystemTrayIcon 提供。")
        self.log(f"图片防重复记忆：{self.memory.record_count()} 条；记忆文件：{self.memory.path}")
        self.log(f"已用标题记忆：{self.title_memory.count()} 个；记忆文件：{self.title_memory.path}")

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
        """Build the light, pastel Cover Studio UI while keeping processing logic unchanged."""
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #F3F6FF;
                color: #293654;
                font-family: 'Microsoft YaHei UI', 'Segoe UI';
                font-size: 10pt;
            }
            QScrollArea { background: transparent; border: 0; }
            QGroupBox {
                background: #FFFFFF;
                border: 1px solid #DFE6F7;
                border-radius: 15px;
                margin-top: 12px;
                padding: 13px 12px 12px 12px;
                font-weight: 700;
                color: #5368A9;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 15px;
                padding: 0 7px;
                color: #5368A9;
                background: #F3F6FF;
            }
            QLineEdit, QListWidget, QTextEdit, QComboBox {
                background: #F9FBFF;
                color: #293654;
                border: 1px solid #DCE4F5;
                border-radius: 9px;
                padding: 8px 10px;
                selection-background-color: #879CF4;
                selection-color: white;
            }
            QLineEdit:focus, QListWidget:focus, QTextEdit:focus, QComboBox:focus {
                border: 1px solid #8EA2F4;
                background: #FFFFFF;
            }
            QComboBox::drop-down { border: 0; width: 26px; }
            QComboBox QAbstractItemView {
                background: #FFFFFF; color: #293654; border: 1px solid #DCE4F5;
                selection-background-color: #E7ECFF; selection-color: #354A8A;
            }
            QGroupBox#titleRequiredBox {
                background: #FFFFFF;
                border: 1px solid #C9D5FF;
                border-top: 3px solid #8298F6;
                border-radius: 16px;
                margin-top: 12px;
                padding: 13px;
            }
            QGroupBox#titleRequiredBox::title {
                color: #5268B5;
                background: #F3F6FF;
                font-size: 10pt;
                font-weight: 800;
            }
            QLineEdit#gameTitle {
                background: #F9FAFF;
                color: #293654;
                border: 2px solid #B6C5FF;
                border-radius: 11px;
                padding: 10px 13px;
                font-size: 16pt;
                font-weight: 700;
                selection-background-color: #8298F6;
            }
            QLineEdit#gameTitle:focus { border: 2px solid #8197F4; background: #FFFFFF; }
            QLineEdit#gameTitle[titleMissing="true"] { border: 2px solid #E9B76E; }
            QLineEdit#gameTitle[titleDuplicate="true"] { border: 2px solid #E11D48; background: #FFF1F2; }
            QPushButton {
                background: #EEF2FF;
                color: #455B9B;
                border: 1px solid #DCE4FA;
                border-radius: 10px;
                padding: 8px 12px;
                min-height: 18px;
                font-weight: 600;
            }
            QPushButton:hover { background: #E3E9FF; border-color: #C2D0FF; }
            QPushButton:pressed { background: #D7E0FF; }
            QPushButton:disabled { color: #A7B0C8; background: #F3F5FA; border-color: #E7EBF4; }
            QPushButton#primary {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #648BFA, stop:1 #927BEA);
                color: #000000;
                border: 0;
                font-weight: 800;
            }
            QPushButton#primary:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7599FF, stop:1 #A08AF4);
            }
            QPushButton[monitorState="running"], QPushButton[monitorState="running"]:disabled {
                background: #DDF7EB; color: #16835A; border: 1px solid #B8E9D2; font-weight: 800;
            }
            QPushButton[monitorState="stopping"], QPushButton[monitorState="stopping"]:disabled {
                background: #FFF1D9; color: #A66A17; border: 1px solid #F4D7A4; font-weight: 800;
            }
            QPushButton#stopActive { background: #FFE8ED; color: #BA4965; border: 1px solid #F7C9D4; font-weight: 800; }
            QPushButton#stopActive:hover { background: #FFDAE4; }
            QPushButton#stopWaiting, QPushButton#stopWaiting:disabled { background: #FFF1D9; color: #A66A17; font-weight: 800; }
            QCheckBox { spacing: 8px; color: #455579; font-weight: 600; }
            /* Keep the native indicator so Qt/Windows can draw the checked tick visibly. */
            QSlider::groove:horizontal { height: 6px; background: #E3E9F9; border-radius: 3px; }
            QSlider::handle:horizontal { background: #8498F3; border: 2px solid #FFFFFF; width: 15px; margin: -6px 0; border-radius: 8px; }
            QSlider::sub-page:horizontal { background: #A9B8FF; border-radius: 3px; }
            QTextEdit { font-family: Consolas, 'Microsoft YaHei UI'; font-size: 9pt; }
            QListWidget::item { padding: 7px; border-radius: 6px; }
            QListWidget::item:selected { background: #E7ECFF; color: #354A8A; }
            QSplitter::handle { background: #E5EAF8; width: 5px; }
        """)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.setCentralWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(18, 16, 18, 15)
        root.setSpacing(12)

        # Header banner: a compact app identity area inspired by the reference cover.
        header = QWidget()
        header.setObjectName("heroHeader")
        header.setStyleSheet("""
            QWidget#heroHeader {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6F91F5, stop:0.58 #8396EF, stop:1 #A58CEB);
                border-radius: 18px;
            }
            QLabel { background: transparent; color: white; border: 0; }
        """)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(19, 14, 18, 14)
        header_layout.setSpacing(12)
        brand_column = QVBoxLayout()
        brand_column.setSpacing(3)
        eyebrow = QLabel("✦  COVER STUDIO  ·  WINDOWS DESKTOP TOOL")
        eyebrow.setStyleSheet("font-size:8pt;font-weight:800;color:#F0F3FF;letter-spacing:1px;")
        brand_column.addWidget(eyebrow)
        heading = QLabel("Galgame 封面标题监控器")
        heading.setStyleSheet("font-size:19pt;font-weight:900;color:#FFFFFF;")
        brand_column.addWidget(heading)
        desc = QLabel("让每一张 Galgame 封面，都拥有清晰统一的标题。")
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size:9pt;color:#F4F5FF;")
        brand_column.addWidget(desc)
        header_layout.addLayout(brand_column, 1)
        version_badge = QLabel("WEBP  输出\nWindows 10 / 11")
        version_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version_badge.setStyleSheet("background:rgba(255,255,255,0.20);border:1px solid rgba(255,255,255,0.45);border-radius:12px;padding:10px 13px;font-size:9pt;font-weight:800;color:white;")
        header_layout.addWidget(version_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(header)

        # Two-column workspace: controls on the left, live cover preview on the right.
        workspace = QHBoxLayout()
        workspace.setSpacing(12)
        left_panel = QWidget()
        left_panel.setStyleSheet("background:transparent;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        right_panel = QWidget()
        right_panel.setStyleSheet("background:transparent;")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        # Required title card.
        title_box = QGroupBox("①  游戏名称 · 必填")
        title_box.setObjectName("titleRequiredBox")
        title_layout = QVBoxLayout(title_box)
        title_layout.setContentsMargins(10, 11, 10, 9)
        title_layout.setSpacing(7)
        title_hint = QLabel("水印文字来自这里。每张图都可以单独确认标题。")
        title_hint.setWordWrap(True)
        title_hint.setStyleSheet("color:#7A86A5;font-size:9pt;font-weight:400;")
        title_layout.addWidget(title_hint)
        self.title_edit = QLineEdit()
        self.title_edit.setObjectName("gameTitle")
        self.title_edit.setMinimumHeight(48)
        self.title_edit.setPlaceholderText("例如：Summer Pockets")
        self.title_edit.setClearButtonEnabled(True)
        self.title_edit.setToolTip("必填：这里填写的游戏名称会绘制到图片中央。")
        self.title_edit.textChanged.connect(self._on_title_text_changed)
        self.title_edit.textEdited.connect(self._on_title_user_edited)
        self.title_edit.editingFinished.connect(self._on_title_editing_finished)
        title_layout.addWidget(self.title_edit)
        self.title_status_label = QLabel("⚠ 先填写游戏名称，再开始监控或加水印。")
        self.title_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_status_label.setWordWrap(True)
        self.title_status_label.setMinimumHeight(28)
        self.title_status_label.setContentsMargins(6, 4, 6, 4)
        self.title_status_label.setStyleSheet(
            "QLabel { background:#FFF7E8; color:#9A5B13; border:1px solid #F2D19A; "
            "border-radius:8px; padding:5px 8px; font-weight:700; font-size:9pt; }"
        )
        title_layout.addWidget(self.title_status_label)
        left_layout.addWidget(title_box)

        # Folder selection.
        folder_box = QGroupBox("②  监控文件夹")
        folder_layout = QVBoxLayout(folder_box)
        folder_layout.setSpacing(7)
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("选择放置 0.jpg / 0.png 的文件夹")
        folder_row.addWidget(self.folder_edit, 1)
        self.folder_button = QPushButton("选择文件夹…")
        self.folder_button.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder_button)
        folder_layout.addLayout(folder_row)
        folder_note = QLabel("仅监控当前文件夹第一层中，主文件名恰好为 0 的图片。")
        folder_note.setWordWrap(True)
        folder_note.setStyleSheet("color:#8490AC;font-size:8pt;font-weight:400;")
        folder_layout.addWidget(folder_note)
        left_layout.addWidget(folder_box)

        # Main actions and modes.
        action_box = QGroupBox("③  监控与处理")
        action_v = QVBoxLayout(action_box)
        action_v.setSpacing(9)
        self.auto_checkbox = QCheckBox("自动加水印（使用当前标题）")
        self.auto_checkbox.setChecked(False)
        self.auto_checkbox.toggled.connect(self._auto_mode_changed)
        action_v.addWidget(self.auto_checkbox)
        action_row = QHBoxLayout()
        self.confirm_button = QPushButton("✓  确认加水印")
        self.confirm_button.setObjectName("primary")
        self.confirm_button.clicked.connect(self.confirm_current_image)
        self.confirm_button.setEnabled(False)
        action_row.addWidget(self.confirm_button, 1)
        self.start_button = QPushButton("▶  开始监控")
        self.start_button.setObjectName("primary")
        self.start_button.setProperty("monitorState", "idle")
        self.start_button.clicked.connect(self.start_monitoring)
        action_row.addWidget(self.start_button, 1)
        self.stop_button = QPushButton("■  停止")
        self.stop_button.clicked.connect(self.stop_monitoring)
        self.stop_button.setEnabled(False)
        action_row.addWidget(self.stop_button)
        action_v.addLayout(action_row)
        status_row = QHBoxLayout()
        self.status_label = QLabel("● 尚未开始监控")
        self.status_label.setStyleSheet("color:#6F7FA2;font-weight:700;")
        status_row.addWidget(self.status_label, 1)
        self.hide_button = QPushButton("↘ 隐藏到托盘")
        self.hide_button.clicked.connect(self.hide_to_tray)
        status_row.addWidget(self.hide_button)
        action_v.addLayout(status_row)
        self.pending_label = QLabel("尚未发现待处理图片")
        self.pending_label.setWordWrap(True)
        self.pending_label.setStyleSheet("background:#F5F7FF;color:#7380A2;border:1px solid #E4E9F8;border-radius:8px;padding:8px;font-size:9pt;")
        action_v.addWidget(self.pending_label)
        tip = QLabel("提示：标题成功使用后会记忆，重复标题不会再次处理。请输入新标题继续；需重用时可单独清空已用标题记忆。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8994AF;font-size:8pt;font-weight:400;")
        action_v.addWidget(tip)
        memory_row = QHBoxLayout()
        self.clear_memory_button = QPushButton("清空防重复记忆")
        self.clear_memory_button.clicked.connect(self.clear_processed_memory)
        memory_row.addWidget(self.clear_memory_button)
        self.memory_count_label = QLabel("防重复记忆：0 条")
        self.memory_count_label.setStyleSheet("color:#7886A7;font-size:9pt;")
        memory_row.addWidget(self.memory_count_label)
        memory_row.addStretch()
        action_v.addLayout(memory_row)

        title_memory_row = QHBoxLayout()
        self.clear_title_memory_button = QPushButton("清空已用标题记忆")
        self.clear_title_memory_button.setToolTip("只清除已使用过的水印标题记录，不会清空图片哈希防重复记忆。")
        self.clear_title_memory_button.clicked.connect(self.clear_used_title_memory)
        title_memory_row.addWidget(self.clear_title_memory_button)
        self.title_memory_count_label = QLabel("已用标题：0 个")
        self.title_memory_count_label.setStyleSheet("color:#7886A7;font-size:9pt;")
        title_memory_row.addWidget(self.title_memory_count_label)
        title_memory_row.addStretch()
        action_v.addLayout(title_memory_row)
        left_layout.addWidget(action_box)

        # Styling controls, same settings/functions as the original tool.
        settings_box = QGroupBox("④  标题样式与输出")
        grid = QGridLayout(settings_box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        grid.addWidget(QLabel("水印位置"), 0, 0)
        self.position_combo = QComboBox()
        # Explicitly style both the closed combo box and its popup list.
        # This prevents Windows/native Qt themes from rendering the popup black.
        self.position_combo.setStyleSheet("""
            QComboBox {
                background-color: #F9FBFF;
                color: #293654;
                border: 1px solid #DCE4F5;
                border-radius: 9px;
                padding: 8px 10px;
                min-height: 18px;
            }
            QComboBox:hover { border-color: #C2D0FF; }
            QComboBox:focus { border: 1px solid #8EA2F4; }
            QComboBox::drop-down {
                background-color: #F9FBFF;
                border: 0;
                width: 26px;
            }
            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                color: #293654;
                selection-background-color: #E7ECFF;
                selection-color: #354A8A;
                border: 1px solid #DCE4F5;
                outline: 0;
            }
            QComboBox QAbstractItemView::item {
                background-color: #FFFFFF;
                color: #293654;
                padding: 6px 10px;
                min-height: 26px;
            }
            QComboBox QAbstractItemView::item:selected {
                background-color: #E7ECFF;
                color: #354A8A;
            }
        """)
        self.position_combo.view().setStyleSheet("""
            QAbstractItemView {
                background-color: #FFFFFF;
                color: #293654;
                selection-background-color: #E7ECFF;
                selection-color: #354A8A;
                border: 1px solid #DCE4F5;
                outline: 0;
            }
            QAbstractItemView::item {
                background-color: #FFFFFF;
                color: #293654;
                padding: 6px 10px;
                min-height: 26px;
            }
            QAbstractItemView::item:selected {
                background-color: #E7ECFF;
                color: #354A8A;
            }
        """)
        self.position_combo.setToolTip("选择游戏标题在封面上的放置位置。")
        self.position_combo.addItem("居中", "center")
        self.position_combo.addItem("顶部居中", "top")
        self.position_combo.addItem("底部居中", "bottom")
        self.position_combo.addItem("左侧居中", "left")
        self.position_combo.addItem("右侧居中", "right")
        self.position_combo.addItem("左上角", "top_left")
        self.position_combo.addItem("右上角", "top_right")
        self.position_combo.addItem("左下角", "bottom_left")
        self.position_combo.addItem("右下角", "bottom_right")
        grid.addWidget(self.position_combo, 0, 1, 1, 2)

        grid.addWidget(QLabel("标题最大宽度"), 1, 0)
        self.ratio_slider = QSlider(Qt.Orientation.Horizontal)
        self.ratio_slider.setRange(15, 80)
        self.ratio_slider.setValue(33)
        grid.addWidget(self.ratio_slider, 1, 1)
        self.ratio_label = QLabel("33%")
        self.ratio_label.setMinimumWidth(34)
        grid.addWidget(self.ratio_label, 1, 2)
        grid.addWidget(QLabel("阴影强度"), 2, 0)
        self.shadow_slider = QSlider(Qt.Orientation.Horizontal)
        self.shadow_slider.setRange(0, 100)
        self.shadow_slider.setSingleStep(5)
        self.shadow_slider.setValue(75)
        grid.addWidget(self.shadow_slider, 2, 1)
        self.shadow_label = QLabel("75")
        self.shadow_label.setMinimumWidth(34)
        grid.addWidget(self.shadow_label, 2, 2)
        grid.addWidget(QLabel("描边强度"), 3, 0)
        self.outline_slider = QSlider(Qt.Orientation.Horizontal)
        self.outline_slider.setRange(0, 100)
        self.outline_slider.setSingleStep(5)
        self.outline_slider.setValue(90)
        grid.addWidget(self.outline_slider, 3, 1)
        self.outline_label = QLabel("90")
        self.outline_label.setMinimumWidth(34)
        grid.addWidget(self.outline_label, 3, 2)
        grid.addWidget(QLabel("WebP 质量"), 4, 0)
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(50, 100)
        self.quality_slider.setSingleStep(5)
        self.quality_slider.setValue(DEFAULT_WEBP_QUALITY)
        grid.addWidget(self.quality_slider, 4, 1)
        self.quality_label = QLabel(str(DEFAULT_WEBP_QUALITY))
        self.quality_label.setMinimumWidth(34)
        grid.addWidget(self.quality_label, 4, 2)
        settings_buttons = QHBoxLayout()
        self.color_button = QPushButton("标题颜色…")
        self.color_button.clicked.connect(self.choose_text_color)
        settings_buttons.addWidget(self.color_button)
        self.color_chip = QLabel("#FFFFFF")
        self.color_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.color_chip.setMinimumWidth(76)
        self.color_chip.setStyleSheet("background:#FFFFFF;color:#26344F;padding:5px 10px;border:1px solid #E1E6F2;border-radius:7px;")
        settings_buttons.addWidget(self.color_chip)
        self.preview_button = QPushButton("选择预览图片…")
        self.preview_button.clicked.connect(self.choose_preview)
        settings_buttons.addWidget(self.preview_button)
        grid.addLayout(settings_buttons, 5, 0, 1, 3)
        left_layout.addWidget(settings_box)
        left_layout.addStretch(1)

        # Cover preview card — visually similar to the screenshot's right-hand panel.
        preview_panel = QGroupBox("封面效果预览")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setSpacing(8)
        preview_caption = QLabel("标题会以描边与阴影效果居中显示")
        preview_caption.setStyleSheet("color:#8490AC;font-size:9pt;font-weight:400;")
        preview_layout.addWidget(preview_caption)
        self.preview_label = QLabel("\n\n✦ 选择一张图片\n\n预览水印效果 ✦\n\n")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(300, 285)
        self.preview_label.setStyleSheet("background:#F6F8FF;color:#97A3C3;border:1px dashed #C9D4F0;border-radius:13px;font-size:12pt;font-weight:700;")
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        preview_layout.addWidget(self.preview_label, 1)
        preview_footer = QLabel("支持 JPG · PNG · AVIF · HEIC 等格式")
        preview_footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_footer.setStyleSheet("color:#8B96B2;font-size:8pt;font-weight:400;")
        preview_layout.addWidget(preview_footer)
        right_layout.addWidget(preview_panel, 3)

        queue_box = QGroupBox("待处理图片")
        queue_layout = QVBoxLayout(queue_box)
        queue_hint = QLabel("选择一张图片，然后确认当前游戏标题。")
        queue_hint.setWordWrap(True)
        queue_hint.setStyleSheet("color:#8490AC;font-size:9pt;font-weight:400;")
        queue_layout.addWidget(queue_hint)
        self.queue_list = QListWidget()
        self.queue_list.setMinimumHeight(115)
        self.queue_list.currentItemChanged.connect(self._queue_selection_changed)
        queue_layout.addWidget(self.queue_list)
        right_layout.addWidget(queue_box, 2)

        workspace.addWidget(left_panel, 11)
        workspace.addWidget(right_panel, 9)
        root.addLayout(workspace, 1)

        # Full-width log panel at the bottom for easier diagnostics.
        log_panel = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(10, 10, 10, 10)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMinimumHeight(120)
        self.log_edit.setMaximumHeight(175)
        log_layout.addWidget(self.log_edit)
        root.addWidget(log_panel)

        foot_row = QHBoxLayout()
        foot_note = QLabel("安全提示：成功生成 WebP 并保存防重复记录后，程序会删除原图；请提前备份重要图片。")
        foot_note.setWordWrap(True)
        foot_note.setStyleSheet("color:#B17B51;font-size:8pt;font-weight:600;")
        foot_row.addWidget(foot_note, 1)
        author_label = QLabel(
            '开发者：<b>码铃薯</b>　|　'
            '<a href="https://www.tudoucode.cn">官网</a>　|　'
            f'<a href="{GITHUB_REPO_URL}">GitHub</a>　'
            f'<a href="{GITHUB_RELEASES_URL}">发布下载</a>'
        )
        author_label.setTextFormat(Qt.TextFormat.RichText)
        author_label.setOpenExternalLinks(True)
        author_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        author_label.setStyleSheet("color:#7582A5;font-size:8pt;padding:3px;")
        foot_row.addWidget(author_label)
        root.addLayout(foot_row)

        self.position_combo.currentIndexChanged.connect(self._save_settings)
        self.ratio_slider.valueChanged.connect(lambda v: (self.ratio_label.setText(f"{v}%"), self._save_settings()))
        self.shadow_slider.valueChanged.connect(lambda v: (self.shadow_label.setText(str(v)), self._save_settings()))
        self.outline_slider.valueChanged.connect(lambda v: (self.outline_label.setText(str(v)), self._save_settings()))
        self.quality_slider.valueChanged.connect(lambda v: (self.quality_label.setText(str(v)), self._save_settings()))

    # ----- Floating quick title editor -----
    def _create_quick_title_widget(self):
        """Create an always-on-top editor with a draggable header and live validation."""
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.quick_title_window = QWidget(None, flags)
        self.quick_title_window.setObjectName("quickTitleWindow")
        self.quick_title_window.setWindowTitle(f"{APP_NAME} - 游戏标题快捷修改")
        self.quick_title_window.setWindowIcon(self._make_app_icon())
        self.quick_title_window.setMinimumSize(420, 112)
        self.quick_title_window.setStyleSheet("""
            QWidget#quickTitleWindow {
                background: #F3F6FF;
                color: #293654;
                border: 1px solid #B9C8F7;
                border-radius: 13px;
                font-family: 'Microsoft YaHei UI', 'Segoe UI';
            }
            QLabel {
                background: transparent;
                border: 0;
                color: #5268A8;
                font-size: 9pt;
                font-weight: 800;
            }
            QLabel#quickTitleHint {
                color: #7D89A7;
                font-size: 8pt;
                font-weight: 500;
            }
            QLabel#quickTitleStatus {
                border-radius: 7px;
                padding: 4px 7px;
                font-size: 8pt;
                font-weight: 800;
            }
            QLineEdit {
                background: #FFFFFF;
                color: #293654;
                border: 2px solid #B6C5FF;
                border-radius: 8px;
                padding: 7px 10px;
                font-size: 11pt;
                font-weight: 700;
                selection-background-color: #8298F6;
            }
            QLineEdit:focus { border: 2px solid #8197F4; }
            QLineEdit[titleMissing="true"] { border: 2px solid #E9B76E; }
            QLineEdit[titleDuplicate="true"] { border: 2px solid #E11D48; background: #FFF1F2; }
            QPushButton {
                background: #E7ECFF;
                color: #455B9B;
                border: 1px solid #D0DAFB;
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 9pt;
                font-weight: 700;
            }
            QPushButton:hover { background: #DCE5FF; }
        """)

        layout = QVBoxLayout(self.quick_title_window)
        layout.setContentsMargins(12, 8, 12, 9)
        layout.setSpacing(6)

        # Dragging is deliberately bound to the header, so text editing and buttons
        # remain easy to use without accidentally moving the floating window.
        header = DraggableTitleBar(
            self.quick_title_window,
            on_drag=self._on_quick_title_dragged,
            parent=self.quick_title_window,
        )
        header.setObjectName("quickTitleDragBar")
        header.setMinimumHeight(22)
        header.setStyleSheet("QWidget#quickTitleDragBar { background: transparent; border: 0; }")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        title_label = QLabel("✦ 游戏名称快捷修改")
        title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header_layout.addWidget(title_label)
        hint_label = QLabel("输入即同步 · 自动保存 · 可拖拽")
        hint_label.setObjectName("quickTitleHint")
        hint_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header_layout.addWidget(hint_label)
        header_layout.addStretch(1)
        layout.addWidget(header)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.quick_title_edit = QLineEdit()
        self.quick_title_edit.setPlaceholderText("必填：输入新的游戏标题…")
        self.quick_title_edit.setClearButtonEnabled(True)
        self.quick_title_edit.setToolTip("在这里输入会立即同步到主界面并自动保存，无需点击确认。")
        self.quick_title_edit.setText(self.title_edit.text())
        self.quick_title_edit.textChanged.connect(self._apply_quick_title)
        # Fix: validate the floating editor directly as well as through main-field sync.
        # This keeps the duplicate-title status responsive while the main window is hidden.
        self.quick_title_edit.textChanged.connect(self._update_quick_title_status)
        self.quick_title_edit.textChanged.connect(self._adjust_quick_title_size)
        self.quick_title_edit.textEdited.connect(self._on_title_user_edited)
        self.quick_title_edit.editingFinished.connect(self._on_title_editing_finished)
        input_row.addWidget(self.quick_title_edit, 1)

        restore_button = QPushButton("主界面")
        restore_button.setToolTip("重新打开完整主界面")
        restore_button.setFixedWidth(70)
        restore_button.clicked.connect(self.restore_window)
        input_row.addWidget(restore_button)
        layout.addLayout(input_row)

        self.quick_title_status = QLabel()
        self.quick_title_status.setObjectName("quickTitleStatus")
        self.quick_title_status.setWordWrap(True)
        self.quick_title_status.setTextFormat(Qt.TextFormat.PlainText)
        self.quick_title_status.setMinimumHeight(22)
        layout.addWidget(self.quick_title_status)

        # Keep both editors synchronized, preserving the original validation and save path.
        self.title_edit.textChanged.connect(self._sync_quick_title_from_main)
        self._update_quick_title_status(self.title_edit.text())
        self._adjust_quick_title_size(self.title_edit.text())

    def _apply_quick_title(self, title: str):
        """Push quick-editor input through the original title field and validation logic."""
        if self.title_edit.text() != title:
            self.title_edit.setText(title)

    def _sync_quick_title_from_main(self, title: str):
        """Reflect edits made in the main UI back into the floating input."""
        if self.quick_title_edit is not None and self.quick_title_edit.text() != title:
            self.quick_title_edit.setText(title)

    def _on_quick_title_dragged(self):
        self._quick_title_user_moved = True
        self._quick_title_positioned = True

    def _quick_title_screen_geometry(self):
        # The hidden widget keeps its last geometry; prefer that monitor after the
        # user drags the popup onto a secondary display.
        screen = None
        if self.quick_title_window is not None:
            screen = QApplication.screenAt(self.quick_title_window.frameGeometry().center())
        if screen is None:
            screen = QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def _adjust_quick_title_size(self, title: str = ""):
        """Resize the floating editor to fit the current title and warning without overflowing the screen."""
        if self.quick_title_window is None or self.quick_title_edit is None:
            return
        area = self._quick_title_screen_geometry()
        if area is None:
            screen_width = 1920
            screen_height = 1080
            area_left = area_top = 0
        else:
            screen_width = area.width()
            screen_height = area.height()
            area_left, area_top = area.left(), area.top()

        # Grow with long game names, but stay within the current monitor's work area.
        title_pixels = self.quick_title_edit.fontMetrics().horizontalAdvance(str(title or self.quick_title_edit.text()))
        max_width = max(320, screen_width - 32)
        target_width = min(max_width, max(440, title_pixels + 250))

        status_text = self.quick_title_status.text() if self.quick_title_status is not None else ""
        status_metrics = self.quick_title_status.fontMetrics() if self.quick_title_status is not None else self.quick_title_edit.fontMetrics()
        status_pixels = status_metrics.horizontalAdvance(status_text)
        status_available = max(220, target_width - 42)
        status_lines = max(1, (status_pixels + status_available - 1) // status_available)
        target_height = max(118, 112 + (status_lines - 1) * 17)
        target_height = min(target_height, max(100, screen_height - 32))

        self.quick_title_window.resize(int(target_width), int(target_height))
        # If this is the default bottom-right position, keep that corner anchored as
        # the window changes width. A user-dragged window instead retains its position.
        if self.quick_title_window.isVisible() and not self._quick_title_user_moved:
            x = area_left + screen_width - target_width - 16
            y = area_top + screen_height - target_height - 16
            self.quick_title_window.move(max(area_left, x), max(area_top, y))
        else:
            frame = self.quick_title_window.frameGeometry()
            x = min(max(frame.x(), area_left), area_left + screen_width - target_width)
            y = min(max(frame.y(), area_top), area_top + screen_height - target_height)
            self.quick_title_window.move(x, y)

    def _update_quick_title_status(self, title: Optional[str] = None):
        """Show missing/duplicate/history-error warnings directly in the floating editor."""
        if self.quick_title_status is None or self.quick_title_edit is None:
            return
        # An explicitly empty string means the user cleared the quick editor; do not
        # fall back to the main field, or the stale previous title may be displayed.
        if title is None:
            title = self.quick_title_edit.text()
        title = str(title).strip()
        missing = not bool(title)
        history_error = bool(self.title_memory.load_error and title)
        duplicate = bool(title and not self.title_memory.load_error and self.title_memory.is_used(title))

        self.quick_title_edit.setProperty("titleMissing", "true" if missing else "false")
        self.quick_title_edit.setProperty("titleDuplicate", "true" if duplicate else "false")
        self.quick_title_edit.style().unpolish(self.quick_title_edit)
        self.quick_title_edit.style().polish(self.quick_title_edit)
        self.quick_title_edit.update()

        if history_error:
            message = "⛔ 已用标题记忆读取失败，为避免标题错配，处理已暂停。请检查记忆或显式清空。"
            style = "background:#FFF0F2;color:#B42318;border:1px solid #FDA4AF;"
        elif missing:
            message = "⚠ 尚未填写游戏名称：当前不能开始监控或添加水印。"
            style = "background:#FFF7E8;color:#9A5B13;border:1px solid #F2D19A;"
        elif duplicate:
            shown = title if len(title) <= 48 else title[:45] + "..."
            message = f"⚠ 标题重复！「{shown}」已经使用过，请更换一个新标题。"
            style = "background:#FFF0F2;color:#B42318;border:1px solid #FDA4AF;"
        else:
            shown = title if len(title) <= 60 else title[:57] + "..."
            message = f"✓ 当前水印标题：{shown}"
            style = "background:#ECFDF3;color:#16835A;border:1px solid #B8E9D2;"

        self.quick_title_status.setText(message)
        self.quick_title_status.setStyleSheet(
            "QLabel#quickTitleStatus { " + style + " border-radius:7px; padding:4px 7px; font-size:8pt; font-weight:800; }"
        )
        self._adjust_quick_title_size(title)

    def _show_quick_title_widget(self):
        """Show the floating title editor near bottom-right on first use; preserve dragged position later."""
        if self.quick_title_window is None or self.shutting_down:
            return
        area = self._quick_title_screen_geometry()
        if area is not None and not self._quick_title_positioned:
            margin = 16
            x = max(area.left(), area.right() - self.quick_title_window.width() - margin + 1)
            y = max(area.top(), area.bottom() - self.quick_title_window.height() - margin + 1)
            self.quick_title_window.move(x, y)
        self.quick_title_window.show()
        self.quick_title_window.raise_()
        self._quick_title_positioned = True
        self._adjust_quick_title_size(self.title_edit.text())

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
        self._show_quick_title_widget()
        self.log("主窗口已隐藏到系统托盘，后台监控继续运行。右下角可直接修改游戏标题；右键托盘图标可显示主界面或彻底退出。")

    def restore_window(self):
        if self.shutting_down:
            return
        if self.quick_title_window is not None:
            self.quick_title_window.hide()
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
            # Keep a compact title editor on the desktop even when the system tray
            # is unavailable; when the tray works, the main window is hidden as before.
            self._show_quick_title_widget()
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
        if self.quick_title_window is not None:
            self.quick_title_window.hide()
        self.allow_close = True
        self.close()
        QApplication.quit()

    # ----- Required game title / settings / folder -----
    def _show_duplicate_title_warning(self, title: str, shake: bool = True):
        """Show a vivid duplicate-title warning immediately below the title field."""
        shown = title if len(title) <= 48 else title[:45] + "..."
        self.title_status_label.setText(
            f"⚠ 标题重复！「{shown}」已经使用过，请更换一个新标题。"
        )
        self.title_status_label.setStyleSheet(
            "QLabel { background:#FFF0F2; color:#B42318; border:1px solid #FDA4AF; "
            "border-left:4px solid #E11D48; border-radius:8px; padding:5px 8px; "
            "font-weight:900; font-size:9pt; }"
        )
        key = self.title_memory.normalize_title(title)
        if shake and key and key != self._last_title_warning_key:
            self._last_title_warning_key = key
            self._shake_title_warning()

    def _cancel_title_warning_shake(self):
        """Stop any pending shake callbacks and restore the warning label baseline."""
        self._title_shake_generation += 1
        if hasattr(self, "title_status_label"):
            self.title_status_label.setContentsMargins(6, 4, 6, 4)

    def _show_title_memory_error(self):
        """Fail visibly if title history cannot be read; do not imply a specific duplicate."""
        self._last_title_warning_key = None
        self._cancel_title_warning_shake()
        self.title_status_label.setText(
            "⛔ 已用标题记忆读取失败，为避免标题错配，处理已暂停。请检查记忆或显式清空。"
        )
        self.title_status_label.setStyleSheet(
            "QLabel { background:#FFF0F2; color:#B42318; border:1px solid #FDA4AF; "
            "border-left:4px solid #E11D48; border-radius:8px; padding:5px 8px; "
            "font-weight:800; font-size:9pt; }"
        )

    def _shake_title_warning(self):
        """A short, subtle horizontal shake of the warning text (roughly 240 ms)."""
        self._title_shake_generation += 1
        generation = self._title_shake_generation
        offsets = (6, -6, 5, -5, 3, -3, 0)
        for index, offset in enumerate(offsets):
            QTimer.singleShot(
                index * 35,
                lambda shift=offset, token=generation: self._apply_title_warning_shake(token, shift),
            )

    def _apply_title_warning_shake(self, generation: int, offset: int):
        if generation != self._title_shake_generation or not hasattr(self, "title_status_label"):
            return
        base = 6
        # Keep total margins constant so the layout doesn't jump; only the label text shakes.
        self.title_status_label.setContentsMargins(base + offset, 4, base - offset, 4)

    def _on_title_text_changed(self, *_):
        """Reflect title validity immediately, including a duplicate warning below the field."""
        title = self.title_edit.text().strip()
        missing = not bool(title)
        duplicate = bool(title and not self.title_memory.load_error and self.title_memory.is_used(title))

        self.title_edit.setProperty("titleMissing", "true" if missing else "false")
        self.title_edit.setProperty("titleDuplicate", "true" if duplicate else "false")
        self.title_edit.style().unpolish(self.title_edit)
        self.title_edit.style().polish(self.title_edit)
        self.title_edit.update()

        if self.title_memory.load_error and title:
            self._show_title_memory_error()
        elif missing:
            self._last_title_warning_key = None
            self._cancel_title_warning_shake()
            self.title_status_label.setText("⚠ 尚未填写游戏名称：当前不能开始监控或添加水印。")
            self.title_status_label.setStyleSheet(
                "QLabel { background:#FFF7E8; color:#9A5B13; border:1px solid #F2D19A; "
                "border-radius:8px; padding:5px 8px; font-weight:700; font-size:9pt; }"
            )
        elif duplicate:
            self._show_duplicate_title_warning(title, shake=not self._loading_settings)
        else:
            self._last_title_warning_key = None
            self._cancel_title_warning_shake()
            shown = title if len(title) <= 60 else title[:57] + "..."
            self.title_status_label.setText(f"✓ 当前水印标题：{shown}")
            self.title_status_label.setStyleSheet(
                "QLabel { background:#ECFDF3; color:#16835A; border:1px solid #B8E9D2; "
                "border-radius:8px; padding:5px 8px; font-weight:700; font-size:9pt; }"
            )
        self._update_quick_title_status(title)
        self._save_settings()

    def _on_title_user_edited(self, *_):
        # Pause automatic processing while the user is still typing, so a partial
        # title cannot be attached if a new image arrives mid-edit.
        self.title_edit_pending = True
        self.auto_title_wait_notified = False

    def _on_title_editing_finished(self):
        self.title_edit_pending = False
        self.auto_title_wait_notified = False
        if hasattr(self, "auto_checkbox") and self.auto_checkbox.isChecked():
            self._pump_auto_queue()

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
            saved_position = data.get("position", "center")
            position_index = self.position_combo.findData(saved_position)
            if position_index >= 0:
                self.position_combo.setCurrentIndex(position_index)
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
            "position": self.position_combo.currentData(),
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
            "position": self.position_combo.currentData(),
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
            if original.width > MAX_OUTPUT_WIDTH:
                preview_height = max(1, int(original.height * MAX_OUTPUT_WIDTH / original.width + 0.5))
                original = original.resize((MAX_OUTPUT_WIDTH, preview_height), Image.Resampling.LANCZOS)
            rendered, _font_size = render_titled_image(
                original, title, self.ratio_slider.value() / 100.0, self.text_color,
                self.shadow_slider.value(), self.outline_slider.value(),
                self.position_combo.currentData(),
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
            self.log("自动加水印已开启：每个未使用的标题只会处理一张图片。标题使用后，请换成新标题，自动队列才会继续。")
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
            if self.title_edit_pending:
                self.status_label.setText("等待游戏名称输入完成…")
                self._update_pending_label()
                return
            title = self.title_edit.text().strip()
            if not title:
                if not self.auto_title_wait_notified:
                    self.log("自动模式正在等待游戏名称。填写标题后完成输入，程序才会继续处理。", "error")
                    self.auto_title_wait_notified = True
                self.status_label.setText("自动模式等待游戏名称")
                self._update_pending_label()
                return
            if self.title_memory.load_error:
                if self.auto_title_memory_notified != "__memory_error__":
                    self.log(f"无法读取已用标题记忆，已暂停自动处理以防标题错配：{self.title_memory.load_error}", "error")
                    self.auto_title_memory_notified = "__memory_error__"
                self.status_label.setText("标题记忆异常，自动处理已暂停")
                self._update_pending_label()
                return
            title_key = self.title_memory.normalize_title(title)
            if self.title_memory.is_used(title):
                self._show_duplicate_title_warning(title, shake=True)
                if self.auto_title_memory_notified != title_key:
                    self.log(
                        f"标题“{title}”此前已用于其他图片，已暂停自动处理 {path.name}。"
                        "请更换为尚未使用的游戏名称；如确需重用，请清空‘已用标题记忆’。",
                        "error",
                    )
                    self.auto_title_memory_notified = title_key
                self.status_label.setText("自动模式等待未使用的游戏名称")
                self._update_pending_label()
                return
            self.auto_title_memory_notified = None
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
        if self.title_memory.load_error:
            message = (
                "已用标题记忆无法读取，为避免标题与图片错配，当前不允许继续处理。\n\n"
                f"错误信息：{self.title_memory.load_error}\n\n"
                "请先检查标题记忆文件，或点击‘清空已用标题记忆’以显式重置标题历史。"
            )
            if auto:
                if self.auto_title_memory_notified != "__memory_error__":
                    self.log(message.replace("\n", " "), "error")
                    self.auto_title_memory_notified = "__memory_error__"
                self.status_label.setText("标题记忆异常，自动处理已暂停")
            else:
                QMessageBox.warning(self, APP_NAME, message)
            return
        if self.title_memory.is_used(title):
            self._show_duplicate_title_warning(title, shake=True)
            message = (
                f"标题“{title}”已经用于另一张图片；为避免封面与游戏名称错配，"
                "本次处理已阻止。请在标题框下方的红色提示出现后更换为未使用的标题。"
            )
            if auto:
                title_key = self.title_memory.normalize_title(title)
                if self.auto_title_memory_notified != title_key:
                    self.log(message, "error")
                    self.auto_title_memory_notified = title_key
                self.status_label.setText("自动模式等待未使用的游戏名称")
                self._update_pending_label()
            else:
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
        title_memory_key: Optional[str] = None
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
            # Reserve the title in persistent memory before deleting the source.
            # If any later safety check fails, the title reservation is rolled back.
            title_memory_key = self.title_memory.register(options["title"], source.name, destination.name)
            if file_signature(source) != expected_signature:
                self.title_memory.forget(title_memory_key)
                title_memory_key = None
                self.memory.forget_record(memory_record_id)
                memory_record_id = None
                raise RuntimeError("保存已用标题记忆期间原图发生变化；原图保留。")
            try:
                source.unlink()
                source_deleted = True
            except OSError as exc:
                try:
                    if title_memory_key is not None:
                        self.title_memory.forget(title_memory_key)
                        title_memory_key = None
                except Exception:
                    pass
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
            if title_memory_key and not source_deleted:
                try:
                    self.title_memory.forget(title_memory_key)
                except Exception:
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
            f"确定清空全部 {count} 条图片防重复记录吗？\n\n只清除图片哈希记忆，不会删除现有 WebP 成品，也不会清除已用标题记忆。请使用干净原图重新处理，避免叠加标题。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self.memory.clear_all()
            self._refresh_memory_count()
            self.log(f"已清空图片防重复记忆：{removed} 条；已用标题记忆和现有 WebP 文件均未更改。", "success")
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"清理失败：\n{exc}")

    def clear_used_title_memory(self):
        if self.processing:
            QMessageBox.warning(self, APP_NAME, "当前图片正在处理，请完成后再清空已用标题记忆。")
            return
        count = self.title_memory.count()
        if count <= 0 and not self.title_memory.load_error:
            QMessageBox.information(self, APP_NAME, "当前没有已使用的水印标题记录。")
            self._refresh_title_memory_count()
            return
        answer = QMessageBox.warning(
            self, "清空已用标题记忆",
            f"确定清空全部 {count} 个已用标题记录吗？\n\n"
            "这不会清空图片哈希防重复记忆，也不会删除任何图片。清空后，之前用过的标题就可以再次用于新图片。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self.title_memory.clear_all()
            self.auto_title_memory_notified = None
            self._refresh_title_memory_count()
            self._on_title_text_changed()
            self.log(f"已清空已用标题记忆：{removed} 个；图片防重复记忆保持不变。", "success")
            if self.auto_checkbox.isChecked():
                self._pump_auto_queue()
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"清空已用标题记忆失败：\n{exc}")

    def _refresh_memory_count(self):
        self.memory_count_label.setText(f"防重复记忆：{self.memory.record_count()} 条")

    def _refresh_title_memory_count(self):
        count = self.title_memory.count()
        suffix = "（读取异常）" if self.title_memory.load_error else ""
        self.title_memory_count_label.setText(f"已用标题：{count} 个{suffix}")

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
                    self._refresh_title_memory_count()
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
