import sys
import os
import time
import subprocess
import requests
import traceback
import shutil
import platform
import json
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
QHBoxLayout, QPushButton, QTableWidget, QTableWidgetItem, QProgressBar,
QCheckBox, QLabel, QLineEdit, QDialog, QRadioButton, QButtonGroup, QMenu,
QFrame, QFileDialog)
from PyQt6.QtCore import Qt, QPoint, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QCursor

# ------------------ LOGGING ------------------
log_file = os.path.expanduser("~/Desktop/SpaceCutter_errors.log")

def log_exception(exc_type, exc_value, exc_traceback):
    with open(log_file, "a") as f:
        f.write(f"\n[{datetime.now()}] Uncaught exception:\n")
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)

sys.excepthook = log_exception

# ------------------ PREFS ------------------
PREFS_FILE = os.path.expanduser("~/.spacecutter_prefs.json")

def load_prefs():
    try:
        with open(PREFS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_prefs(prefs):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f)
    except Exception:
        pass

# ------------------ CONSTANTS ------------------
GOLD = "#f5a705"
PLACEHOLDER_TEXT = "Paste Replay URL  —  or use ✂️ Send to Cut Queue below"

STATUSES = {
    "Pending":        "⏳ Pending",
    "Cutting":        "✂️ Cutting",
    "Converting":     "🔄 Converting",
    "Done":           "✅ Done",
    "Cut Error":      "⚠️ Cut Error",
    "Convert Error":  "⚠️ Convert Error",
    "Invalid Time":   "⚠️ Invalid Time",
    "FFmpeg Not Found":"⚠️ Cut Error",
}

def make_status_item(key):
    item = QTableWidgetItem(STATUSES.get(key, key))
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    return item

def is_placeholder(item):
    return item and item.text() == PLACEHOLDER_TEXT

def run_ffmpeg(cmd):
    kwargs = dict(capture_output=True, text=True)
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)

def dialog_style():
    return f"""
        QDialog {{ background-color: #141414; color: #f0f0f0; }}
        QLabel {{ border: none; color: #c0c0c0; font-size: 13px; background-color: transparent; }}
        QRadioButton {{ color: #666; font-size: 12px; background-color: transparent; }}
        QRadioButton:checked {{ color: {GOLD}; }}
        QPushButton {{
            background-color: rgba(255,255,255,0.05);
            color: #e0e0e0;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 6px;
            padding: 6px 12px;
        }}
        QPushButton:hover {{
            background-color: rgba(255,255,255,0.09);
            color: #ffffff;
        }}
    """

# ------------------ FFMPEG WORKER ------------------
class FFmpegWorker(QThread):
    progress_signal = pyqtSignal(int)
    status_signal = pyqtSignal(int, str)
    cutting_slow_signal = pyqtSignal(int)

    def __init__(self, table, output_folder, merge=False):
        super().__init__()
        self.table = table
        self.output_folder = output_folder
        self.merge = merge
        self._is_running = True
        self.downloaded_files = []

        ffmpeg_name = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"

        if getattr(sys, 'frozen', False):
            # Running as packaged app
            base_path = os.path.dirname(sys.executable)
            # PyInstaller --onedir puts binaries in _internal/ next to the executable
            internal_ffmpeg  = os.path.join(base_path, '_internal', ffmpeg_name)
            # macOS .app bundle may also place them in Contents/Resources
            resources_ffmpeg = os.path.join(base_path, '..', 'Resources', ffmpeg_name)
            # Some older PyInstaller builds place them beside the executable directly
            beside_ffmpeg    = os.path.join(base_path, ffmpeg_name)

            if os.path.exists(internal_ffmpeg):
                self.ffmpeg_path = internal_ffmpeg
            elif os.path.exists(resources_ffmpeg):
                self.ffmpeg_path = resources_ffmpeg
            elif os.path.exists(beside_ffmpeg):
                self.ffmpeg_path = beside_ffmpeg
            else:
                self.ffmpeg_path = None
        else:
            # Running from source — expect ffmpeg next to the script
            base_path = os.path.dirname(__file__)
            local_ffmpeg = os.path.join(base_path, ffmpeg_name)
            self.ffmpeg_path = local_ffmpeg if os.path.exists(local_ffmpeg) else shutil.which("ffmpeg")

        if not self.ffmpeg_path:
            raise FileNotFoundError("FFmpeg not found.")

    def stop(self):
        self._is_running = False

    def time_to_seconds(self, t):
        h, m, s = map(int, t.split(":"))
        return h*3600 + m*60 + s

    def run(self):
        total = self.table.rowCount()
        valid_rows = []
        for i in range(total):
            url_item = self.table.item(i, 0)
            if url_item and url_item.text().strip() and not is_placeholder(url_item):
                valid_rows.append(i)

        for idx, i in enumerate(valid_rows):
            if not self._is_running:
                break

            url = self.table.item(i, 0).text()
            full_item = self.table.item(i, 1)
            is_full = full_item and full_item.checkState() == Qt.CheckState.Checked
            start = self.table.item(i, 2).text() if self.table.item(i, 2) else "HH:MM:SS"
            end   = self.table.item(i, 3).text() if self.table.item(i, 3) else "HH:MM:SS"

            ts_file  = os.path.join(self.output_folder, f"segment_{i+1}_{int(time.time())}.ts")
            m4a_file = os.path.join(self.output_folder, f"audio_{i+1}_{int(time.time())}.m4a")

            if is_full:
                cut_cmd = [self.ffmpeg_path, "-i", url, "-c", "copy", "-f", "mpegts", ts_file]
            else:
                if start == "HH:MM:SS" or end == "HH:MM:SS":
                    self.status_signal.emit(i, "Invalid Time")
                    continue
                start_sec = self.time_to_seconds(start)
                end_sec   = self.time_to_seconds(end)
                if end_sec <= start_sec:
                    self.status_signal.emit(i, "Invalid Time")
                    continue
                duration = end_sec - start_sec
                cut_cmd = [self.ffmpeg_path, "-ss", start, "-i", url, "-t", str(duration), "-c", "copy", "-f", "mpegts", ts_file]

            self.status_signal.emit(i, "Cutting")

            cut_start = time.time()
            slow_fired = False

            try:
                import threading
                result_holder = [None]
                def do_cut():
                    result_holder[0] = run_ffmpeg(cut_cmd)
                t = threading.Thread(target=do_cut, daemon=True)
                t.start()
                while t.is_alive():
                    if not self._is_running:
                        break
                    if not slow_fired and (time.time() - cut_start) > 300:
                        self.cutting_slow_signal.emit(i)
                        slow_fired = True
                    time.sleep(1)
                t.join()
                cut_process = result_holder[0]
                if cut_process is None or cut_process.returncode != 0:
                    self.status_signal.emit(i, "Cut Error")
                    continue
            except FileNotFoundError:
                self.status_signal.emit(i, "FFmpeg Not Found")
                continue

            convert_cmd = [self.ffmpeg_path, "-i", ts_file, "-c", "copy", "-y", m4a_file]
            self.status_signal.emit(i, "Converting")
            try:
                convert_process = run_ffmpeg(convert_cmd)
                if convert_process.returncode == 0:
                    self.status_signal.emit(i, "Done")
                    self.downloaded_files.append(m4a_file)
                    os.remove(ts_file)
                else:
                    self.status_signal.emit(i, "Convert Error")
            except FileNotFoundError:
                self.status_signal.emit(i, "FFmpeg Not Found")

            self.progress_signal.emit(int((idx+1)/len(valid_rows)*100))

        if self.merge and len(self.downloaded_files) > 1:
            file_list_path = os.path.join(self.output_folder, "file_list.txt")
            with open(file_list_path, "w") as f:
                for file_path in self.downloaded_files:
                    f.write(f"file '{file_path}'\n")
            merged_output = os.path.join(self.output_folder, f"merged_{int(time.time())}.m4a")
            run_ffmpeg([self.ffmpeg_path, "-f", "concat", "-safe", "0", "-i", file_list_path, "-c", "copy", merged_output])


# ------------------ DIALOGS ------------------
class HowToDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("How to get Live/Dynamic URL")
        self.setMinimumWidth(480)
        self.setStyleSheet(dialog_style())
        layout = QVBoxLayout()
        layout.setSpacing(10)
        lang_layout = QHBoxLayout()
        self.btn_en = QRadioButton("EN")
        self.btn_gr = QRadioButton("GR")
        self.btn_en.setChecked(True)
        self.lang_group = QButtonGroup()
        self.lang_group.addButton(self.btn_en)
        self.lang_group.addButton(self.btn_gr)
        self.btn_en.toggled.connect(self.update_text)
        lang_layout.addStretch()
        lang_layout.addWidget(self.btn_en)
        lang_layout.addWidget(self.btn_gr)
        layout.addLayout(lang_layout)
        self.content = QLabel()
        self.content.setWordWrap(True)
        self.content.setStyleSheet("border: none; color: #ccc; font-size: 13px; background-color: transparent;")
        layout.addWidget(self.content)
        close_btn = QPushButton("OK")
        close_btn.setFixedWidth(80)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.clicked.connect(self.close)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
        self.setLayout(layout)
        self.update_text()

    def update_text(self):
        if self.btn_en.isChecked():
            self.content.setText(
                "1. Open the Twitter/X Space in your browser\n\n"
                "2. Press F12 to open Developer Tools\n\n"
                "3. Go to the Network tab\n\n"
                "4. Type 'dynamic' in the filter box\n\n"
                "5. Refresh the page or wait a moment\n\n"
                "6. Look for a request containing 'dynamic_playlist.m3u8?type=live'\n\n"
                "7. Right-click → Copy URL\n\n"
                "8. Paste it in the app below"
            )
        else:
            self.content.setText(
                "1. Άνοιξε το Twitter/X Space στον browser σου\n\n"
                "2. Πάτα F12 για να ανοίξεις τα Developer Tools\n\n"
                "3. Πήγαινε στο tab Network\n\n"
                "4. Γράψε 'dynamic' στο πεδίο φίλτρου\n\n"
                "5. Κάνε refresh τη σελίδα ή περίμενε λίγο\n\n"
                "6. Ψάξε για request που περιέχει 'dynamic_playlist.m3u8?type=live'\n\n"
                "7. Δεξί κλικ → Copy URL\n\n"
                "8. Επικόλλησέ το στο app παρακάτω"
            )


class ListenDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("?")
        self.setMinimumWidth(420)
        self.setStyleSheet(dialog_style())
        layout = QVBoxLayout()
        layout.setSpacing(10)
        lang_layout = QHBoxLayout()
        self.btn_en = QRadioButton("EN")
        self.btn_gr = QRadioButton("GR")
        self.btn_en.setChecked(True)
        self.lang_group = QButtonGroup()
        self.lang_group.addButton(self.btn_en)
        self.lang_group.addButton(self.btn_gr)
        self.btn_en.toggled.connect(self.update_text)
        lang_layout.addStretch()
        lang_layout.addWidget(self.btn_en)
        lang_layout.addWidget(self.btn_gr)
        layout.addLayout(lang_layout)
        self.content = QLabel()
        self.content.setWordWrap(True)
        self.content.setStyleSheet("border: none; color: #ccc; font-size: 13px; background-color: transparent;")
        layout.addWidget(self.content)
        close_btn = QPushButton("OK")
        close_btn.setFixedWidth(80)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.clicked.connect(self.close)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
        self.setLayout(layout)
        self.update_text()

    def update_text(self):
        if self.btn_en.isChecked():
            self.content.setText(
                "Not sure where to cut yet?\n\n"
                "→ Copy the Replay URL and paste it in your browser to listen to the Space first.\n\n"
                "→ Once you know the timestamps, send it to the Cut Queue to clip and download that part."
            )
        else:
            self.content.setText(
                "Δεν ξέρεις ακόμα πού να κόψεις;\n\n"
                "→ Αντέγραψε το Replay URL και επικόλλησέ το στον browser σου για να ακούσεις πρώτα το Space.\n\n"
                "→ Μόλις ξέρεις τα timestamps, στείλε το στην Cut Queue για να κόψεις και να κατεβάσεις το κομμάτι."
            )


class DropHelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Drag & Drop")
        self.setMinimumWidth(400)
        self.setStyleSheet(dialog_style())
        layout = QVBoxLayout()
        layout.setSpacing(10)
        lang_layout = QHBoxLayout()
        self.btn_en = QRadioButton("EN")
        self.btn_gr = QRadioButton("GR")
        self.btn_en.setChecked(True)
        self.lang_group = QButtonGroup()
        self.lang_group.addButton(self.btn_en)
        self.lang_group.addButton(self.btn_gr)
        self.btn_en.toggled.connect(self.update_text)
        lang_layout.addStretch()
        lang_layout.addWidget(self.btn_en)
        lang_layout.addWidget(self.btn_gr)
        layout.addLayout(lang_layout)
        self.content = QLabel()
        self.content.setWordWrap(True)
        self.content.setStyleSheet("border: none; color: #ccc; font-size: 13px; background-color: transparent;")
        layout.addWidget(self.content)
        close_btn = QPushButton("OK")
        close_btn.setFixedWidth(80)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.clicked.connect(self.close)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
        self.setLayout(layout)
        self.update_text()

    def update_text(self):
        if self.btn_en.isChecked():
            self.content.setText(
                "If you have a Replay URL saved in your notes or you're listening to a Space in your browser, "
                "you can drag and drop the URL directly here.\n\n"
                "It will be added to the Cut Queue automatically."
            )
        else:
            self.content.setText(
                "Αν έχεις αποθηκευμένο το Replay URL σε σημειώσεις ή ακούς ένα Space από τον browser σου, "
                "μπορείς να σύρεις και να αποθέσεις το URL κατευθείαν εδώ.\n\n"
                "Θα προστεθεί αυτόματα στην Cut Queue."
            )


class MergeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Merge")
        self.setMinimumWidth(420)
        self.setStyleSheet(dialog_style())
        layout = QVBoxLayout()
        layout.setSpacing(10)
        lang_layout = QHBoxLayout()
        self.btn_en = QRadioButton("EN")
        self.btn_gr = QRadioButton("GR")
        self.btn_en.setChecked(True)
        self.lang_group = QButtonGroup()
        self.lang_group.addButton(self.btn_en)
        self.lang_group.addButton(self.btn_gr)
        self.btn_en.toggled.connect(self.update_text)
        lang_layout.addStretch()
        lang_layout.addWidget(self.btn_en)
        lang_layout.addWidget(self.btn_gr)
        layout.addLayout(lang_layout)
        self.content = QLabel()
        self.content.setWordWrap(True)
        self.content.setStyleSheet("border: none; color: #ccc; font-size: 13px; background-color: transparent;")
        layout.addWidget(self.content)
        close_btn = QPushButton("OK")
        close_btn.setFixedWidth(80)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.clicked.connect(self.close)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
        self.setLayout(layout)
        self.update_text()

    def update_text(self):
        if self.btn_en.isChecked():
            self.content.setText(
                "When enabled, two sets of files are saved after processing:\n\n"
                "① Individual clips — one audio file per row, trimmed exactly "
                "to the start and end times you defined.\n\n"
                "② A merged file — a single continuous audio track that joins "
                "all clips together in the order they appear in the queue.\n\n"
                "Useful when you need both the separate segments and a single "
                "ready-to-use version at the same time."
            )
        else:
            self.content.setText(
                "Όταν είναι ενεργό, αποθηκεύονται δύο τύποι αρχείων:\n\n"
                "① Μεμονωμένα clips — ένα αρχείο ήχου για κάθε γραμμή, "
                "κομμένο ακριβώς στα timestamps που όρισες.\n\n"
                "② Ένα ενιαίο αρχείο — ένα συνεχές audio track που ενώνει "
                "όλα τα clips με τη σειρά που εμφανίζονται στην ουρά.\n\n"
                "Χρήσιμο όταν θέλεις και τα επιμέρους τμήματα αλλά και μια "
                "έτοιμη ενοποιημένη έκδοση ταυτόχρονα."
            )


# ------------------ SECTION TITLE ------------------
def make_section_title(text):
    label = QLabel(text)
    label.setStyleSheet(f"""
        QLabel {{
            color: {GOLD};
            font-size: 11px;
            font-weight: bold;
            letter-spacing: 2px;
            border: none;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            padding: 4px 2px;
            margin-bottom: 2px;
            background-color: rgba(255,255,255,0.03);
            border-radius: 4px;
        }}
    """)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft)
    return label


# ------------------ DROP AREA ------------------
class DropArea(QWidget):
    def __init__(self, table, parent_window=None):
        super().__init__()
        self.table = table
        self.parent_window = parent_window
        self.setAcceptDrops(True)

        layout = QVBoxLayout()
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        self.main_label = QLabel(
            'Drop a <span style="color:#f5a705;font-style:normal;">Replay</span> URL here<br>'
            '<span style="font-size:11px;">to add it to the Cut Queue</span>'
        )
        self.main_label.setTextFormat(Qt.TextFormat.RichText)
        self.main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_label.setStyleSheet("border: none; color: #3a3a3a; font-size: 12px; font-style: italic; background-color: transparent;")
        layout.addWidget(self.main_label)

        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(4)
        bottom_row.addStretch()

        self.help_btn = QPushButton("?")
        self.help_btn.setFixedSize(22, 22)
        self.help_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.help_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #3a3a3a;
                border: 1px solid #1e1e1e;
                border-radius: 5px;
                font-size: 11px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover { color: #c0c0c0; background-color: rgba(255,255,255,0.06); }
        """)
        self.help_btn.clicked.connect(self.open_help)
        bottom_row.addWidget(self.help_btn)
        layout.addLayout(bottom_row)
        self.setLayout(layout)
        self.set_normal_style()

    def set_normal_style(self):
        self.setStyleSheet("""
            DropArea {
                border: 2px dashed #1a1a1a;
                border-radius: 10px;
                background-color: #060606;
            }
        """)
        self.main_label.setStyleSheet("border: none; color: #3a3a3a; font-size: 12px; font-style: italic; background-color: transparent;")

    def set_hover_style(self):
        self.setStyleSheet(f"""
            DropArea {{
                border: 2px solid {GOLD};
                border-radius: 10px;
                background-color: {GOLD};
            }}
        """)
        self.main_label.setStyleSheet(
            "border: none; color: #1a0f00; font-size: 13px; font-weight: bold; font-style: normal; background-color: transparent;"
        )

    def dragEnterEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()
            self.set_hover_style()

    def dragLeaveEvent(self, event):
        self.set_normal_style()

    def dropEvent(self, event):
        self.set_normal_style()
        text = event.mimeData().text()
        for line in text.splitlines():
            if line.strip():
                self.insert_url(line.strip())

    def open_help(self):
        if self.parent_window:
            DropHelpDialog(self.parent_window).exec()

    def insert_url(self, url):
        for i in range(self.table.rowCount()):
            url_item = self.table.item(i, 0)
            if not url_item or not url_item.text().strip() or is_placeholder(url_item):
                self.table.setItem(i, 0, QTableWidgetItem(url))
                self.table.setRowHeight(i, 26)
                return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(url))
        full_item = QTableWidgetItem()
        full_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        full_item.setCheckState(Qt.CheckState.Unchecked)
        self.table.setItem(row, 1, full_item)
        self.table.setItem(row, 2, QTableWidgetItem("HH:MM:SS"))
        self.table.setItem(row, 3, QTableWidgetItem("HH:MM:SS"))
        self.table.setItem(row, 4, make_status_item("Pending"))
        self.table.setRowHeight(row, 26)


# ------------------ MAIN WINDOW ------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Space Cutter")
        self.setMinimumSize(950, 660)
        self.setMaximumHeight(760)
        prefs = load_prefs()
        self.output_folder = prefs.get("output_folder", os.path.expanduser("~/Desktop"))
        self.worker = None
        self._slow_warned = set()

        central = QWidget()
        central.setStyleSheet("background-color: #0a0a0a;")
        main_layout = QVBoxLayout()
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # ━━━━ SECTION 1 ━━━━
        section1 = QFrame()
        section1.setStyleSheet("""
            QFrame {
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px;
                background-color: rgba(255,255,255,0.03);
            }
        """)
        s1_layout = QVBoxLayout()
        s1_layout.setContentsMargins(10, 8, 10, 10)
        s1_layout.setSpacing(6)

        s1_layout.addWidget(make_section_title("✂️  CUT & DOWNLOAD AUDIO"))

        self.folder_button = QPushButton(f"📁  Output Folder: {self.output_folder}")
        self.folder_button.clicked.connect(self.choose_folder)
        self.folder_button.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.02);
                color: #484848;
                border: 1px solid rgba(255,255,255,0.04);
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 11px;
                text-align: left;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.06);
                color: #c8c8c8;
            }
        """)
        s1_layout.addWidget(self.folder_button)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Replay URL", "Full", "Start", "End", "Status"])
        self.table.setColumnWidth(0, 400)
        self.table.setColumnWidth(1, 40)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 100)
        self.table.setColumnWidth(4, 100)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setWordWrap(False)
        self.table.setMouseTracking(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.CurrentChanged | QTableWidget.EditTrigger.SelectedClicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.horizontalHeaderItem(1).setToolTip("Check to download the full audio without cutting")
        self.table.horizontalHeaderItem(2).setToolTip("You can type without colons, e.g. 013045 → 01:30:45")
        self.table.horizontalHeaderItem(3).setToolTip("You can type without colons, e.g. 013045 → 01:30:45")
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #080808;
                color: #d0d0d0;
                border: 1px solid rgba(255,255,255,0.05);
                gridline-color: rgba(255,255,255,0.03);
                border-radius: 8px;
            }
            QHeaderView::section {
                background-color: rgba(255,255,255,0.025);
                color: #606060;
                border: none;
                border-bottom: 1px solid rgba(255,255,255,0.04);
                border-right: 1px solid rgba(255,255,255,0.03);
                padding: 3px 4px;
                font-size: 11px;
                letter-spacing: 0.5px;
            }
            QTableWidget::item { border-bottom: 1px solid rgba(255,255,255,0.025); padding: 2px 4px; }
            QTableWidget::item:hover { background-color: rgba(255,255,255,0.035); }
            QTableWidget::item:selected { background-color: rgba(255,255,255,0.06); color: #ffffff; }
        """)
        s1_layout.addWidget(self.table)

        self.add_empty_row(first=True)
        self.table.cellChanged.connect(self.on_cell_changed)
        self.table.clearSelection()
        self.table.setCurrentIndex(self.table.model().index(-1, -1))

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)

        controls = QVBoxLayout()
        controls.setSpacing(5)
        controls.setContentsMargins(0, 0, 0, 0)

        self.start_btn = QPushButton("▶  Start")
        self.start_btn.setFixedSize(110, 52)
        self.start_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(255,255,255,0.055);
                color: #c8c8c8;
                border: 1px solid rgba(255,255,255,0.07);
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: rgba(255,255,255,0.095);
                color: #ffffff;
                border: 1px solid rgba(255,255,255,0.07);
            }}
            QPushButton:pressed {{
                background-color: rgba(255,255,255,0.035);
                color: #aaaaaa;
                border: 1px solid rgba(255,255,255,0.07);
            }}
            QPushButton:disabled {{
                background-color: rgba(245,167,5,0.09);
                color: {GOLD};
                border: 1px solid rgba(245,167,5,0.20);
            }}
        """)
        self.start_btn.clicked.connect(self.start_worker)
        controls.addWidget(self.start_btn)

        hint_label = QLabel("e.g.  013045 → 01:30:45")
        hint_label.setStyleSheet("""
            QLabel {
                color: #303030;
                font-size: 10px;
                background-color: transparent;
                border: none;
                padding: 0px;
            }
        """)
        hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_label.setFixedWidth(110)
        controls.addWidget(hint_label)

        clear_btn = QPushButton("🗑  Clear All")
        clear_btn.setFixedHeight(26)
        clear_btn.clicked.connect(self.clear_all)
        controls.addWidget(clear_btn)

        cancel_btn = QPushButton("✕  Cancel")
        cancel_btn.setFixedHeight(26)
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.02);
                color: #484848;
                border: 1px solid rgba(255,255,255,0.04);
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.05);
                color: #888888;
                border: 1px solid rgba(255,255,255,0.04);
            }
            QPushButton:pressed {
                background-color: rgba(255,255,255,0.02);
                color: #666666;
            }
        """)
        cancel_btn.clicked.connect(self.cancel_worker)
        controls.addWidget(cancel_btn)

        self.merge_checkbox = QCheckBox("Merge downloaded files")
        self.merge_checkbox.setStyleSheet(f"""
            QCheckBox {{
                color: #888;
                font-size: 11px;
                background-color: transparent;
            }}
            QCheckBox::indicator {{
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 3px;
                background-color: rgba(255,255,255,0.02);
                width: 13px;
                height: 13px;
            }}
            QCheckBox::indicator:checked {{
                background-color: {GOLD};
                border: 1px solid {GOLD};
            }}
            QCheckBox:hover {{ color: #bbbbbb; }}
        """)
        merge_row = QHBoxLayout()
        merge_row.setSpacing(6)
        merge_row.setContentsMargins(0, 0, 0, 0)
        merge_row.addWidget(self.merge_checkbox)

        merge_help_btn = QPushButton("?")
        merge_help_btn.setFixedSize(22, 22)
        merge_help_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        merge_help_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #555;
                border: 1px solid #2a2a2a;
                border-radius: 5px;
                font-size: 12px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover { color: #d0d0d0; background-color: rgba(255,255,255,0.07); border: 1px solid #444; }
        """)
        merge_help_btn.clicked.connect(lambda: MergeDialog(self).exec())
        merge_row.addWidget(merge_help_btn)
        merge_row.addStretch()
        controls.addLayout(merge_row)
        controls.addStretch()

        bottom_row.addLayout(controls)

        self.drop_area = DropArea(self.table, self)
        bottom_row.addWidget(self.drop_area, 1)

        s1_layout.addLayout(bottom_row)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(7)
        self.progress.setTextVisible(False)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        s1_layout.addWidget(self.progress)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.status_label.setStyleSheet("""
            QLabel {
                color: #3a3a3a;
                font-size: 10px;
                background-color: transparent;
                border: none;
                padding: 0px 2px;
            }
        """)
        self.status_label.setVisible(False)
        s1_layout.addWidget(self.status_label)

        section1.setLayout(s1_layout)
        main_layout.addWidget(section1)

        # ━━━━ SECTION 2 ━━━━
        section2 = QFrame()
        section2.setStyleSheet("""
            QFrame {
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px;
                background-color: rgba(255,255,255,0.02);
            }
        """)
        s2_layout = QVBoxLayout()
        s2_layout.setContentsMargins(10, 8, 10, 10)
        s2_layout.setSpacing(6)

        howto_row = QHBoxLayout()
        howto_row.addWidget(make_section_title("🔗  CONVERT LIVE → REPLAY URL"))
        howto_row.addStretch()
        howto_btn = QPushButton("? How to get Live/Dynamic URL")
        howto_btn.setFixedWidth(225)
        howto_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {GOLD};
                border: 1px solid rgba(245,167,5,0.15);
                border-radius: 6px;
                padding: 2px 8px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: rgba(245,167,5,0.07);
                color: #ffc107;
                border: 1px solid rgba(245,167,5,0.15);
            }}
        """)
        howto_btn.clicked.connect(lambda: HowToDialog(self).exec())
        howto_row.addWidget(howto_btn)
        s2_layout.addLayout(howto_row)

        self.dynamic_input = QLineEdit()
        self.dynamic_input.setPlaceholderText("Paste dynamic (live) URL here")
        s2_layout.addWidget(self.dynamic_input)

        convert_button = QPushButton("🔄  Convert to Replay URL")
        convert_button.setFixedWidth(210)
        convert_button.setFixedHeight(52)
        convert_button.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.055);
                color: #c8c8c8;
                border: 1px solid rgba(255,255,255,0.07);
                border-radius: 8px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.095);
                color: #ffffff;
                border: 1px solid rgba(255,255,255,0.07);
            }
            QPushButton:pressed {
                background-color: rgba(255,255,255,0.035);
                color: #aaaaaa;
            }
        """)
        convert_button.clicked.connect(self.convert_dynamic_url)
        s2_layout.addWidget(convert_button)

        replay_layout = QHBoxLayout()
        self.replay_output = QLineEdit()
        self.replay_output.setReadOnly(True)
        self.replay_output.setPlaceholderText("Converted Replay URL will appear here")
        replay_layout.addWidget(self.replay_output)

        copy_btn = QPushButton("📋  Copy")
        copy_btn.setFixedWidth(75)
        copy_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        copy_btn.clicked.connect(self.copy_replay_url)
        replay_layout.addWidget(copy_btn)

        send_button = QPushButton("✂️  Send to Cut Queue")
        send_button.setFixedWidth(175)
        send_button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        send_button.clicked.connect(self.send_to_table)
        replay_layout.addWidget(send_button)

        listen_btn = QPushButton("?")
        listen_btn.setFixedWidth(34)
        listen_btn.setFixedHeight(32)
        listen_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        listen_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #555;
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 6px;
                font-size: 15px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover {
                color: #d0d0d0;
                background-color: rgba(255,255,255,0.06);
            }
        """)
        listen_btn.clicked.connect(lambda: ListenDialog(self).exec())
        replay_layout.addWidget(listen_btn)

        s2_layout.addLayout(replay_layout)
        section2.setLayout(s2_layout)
        main_layout.addWidget(section2)

        central.setLayout(main_layout)
        self.setCentralWidget(central)

        for btn in self.findChildren(QPushButton):
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def add_empty_row(self, first=False):
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        if first:
            placeholder = QTableWidgetItem(PLACEHOLDER_TEXT)
            placeholder.setForeground(Qt.GlobalColor.darkGray)
            self.table.setItem(row, 0, placeholder)
        else:
            self.table.setItem(row, 0, QTableWidgetItem(""))
        full_item = QTableWidgetItem()
        full_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        full_item.setCheckState(Qt.CheckState.Unchecked)
        full_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 1, full_item)
        self.table.setItem(row, 2, QTableWidgetItem("HH:MM:SS"))
        self.table.setItem(row, 3, QTableWidgetItem("HH:MM:SS"))
        self.table.setItem(row, 4, make_status_item("Pending"))
        self.table.setRowHeight(row, 26)
        self.table.blockSignals(False)

    def on_cell_changed(self, row, column):
        if column == 0:
            self.table.setRowHeight(row, 26)
            item = self.table.item(row, 0)
            if item and item.text().strip() and not is_placeholder(item) and row == self.table.rowCount() - 1:
                self.add_empty_row()

        if column == 1:
            full_item = self.table.item(row, 1)
            if full_item:
                is_full = full_item.checkState() == Qt.CheckState.Checked
                for col in [2, 3]:
                    ts_item = self.table.item(row, col)
                    if ts_item:
                        if is_full:
                            ts_item.setFlags(ts_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                            ts_item.setForeground(Qt.GlobalColor.gray)
                        else:
                            ts_item.setFlags(ts_item.flags() | Qt.ItemFlag.ItemIsEditable)
                            ts_item.setForeground(Qt.GlobalColor.white)

        if column in [2, 3]:
            item = self.table.item(row, column)
            if not item:
                return
            text = ''.join(filter(str.isdigit, item.text()))
            if len(text) == 6:
                formatted = f"{text[:2]}:{text[2:4]}:{text[4:6]}"
                self.table.blockSignals(True)
                item.setText(formatted)
                self.table.blockSignals(False)

    def show_context_menu(self, pos: QPoint):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #141414;
                color: #e0e0e0;
                border: 1px solid rgba(255,255,255,0.07);
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item { padding: 5px 18px; border-radius: 4px; }
            QMenu::item:selected { background-color: rgba(255,255,255,0.06); }
        """)
        paste_action = menu.addAction("📋  Paste URL")
        copy_action  = menu.addAction("📄  Copy URL")
        menu.addSeparator()
        delete_action = menu.addAction("🗑  Delete Row")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))

        if action == paste_action:
            clipboard_text = QApplication.clipboard().text().strip()
            if clipboard_text:
                self.table.setItem(row, 0, QTableWidgetItem(clipboard_text))
                self.table.setRowHeight(row, 26)
        elif action == copy_action:
            url_item = self.table.item(row, 0)
            if url_item and url_item.text() and not is_placeholder(url_item):
                QApplication.clipboard().setText(url_item.text())
        elif action == delete_action:
            if row == self.table.rowCount() - 1:
                url_item = self.table.item(row, 0)
                if not url_item or not url_item.text().strip() or is_placeholder(url_item):
                    return
            self.table.removeRow(row)
            last = self.table.rowCount() - 1
            if last < 0:
                self.add_empty_row(first=True)
                return
            last_item = self.table.item(last, 0)
            if last_item and last_item.text().strip() and not is_placeholder(last_item):
                self.add_empty_row()

    def clear_all(self):
        self.table.setRowCount(0)
        self.add_empty_row(first=True)
        self.table.clearSelection()
        self.table.setCurrentIndex(self.table.model().index(-1, -1))

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_folder = folder
            self.folder_button.setText(f"📁  Output Folder: {folder}")
            save_prefs({"output_folder": folder})

    def convert_dynamic_url(self):
        dynamic = self.dynamic_input.text().strip()
        if dynamic:
            try:
                master = dynamic.replace('/dynamic_playlist.m3u8?type=live', "/master_playlist.m3u8")
                response = requests.get(master)
                data = response.text.split('audio-space/')[1].replace("\n", "")
                replay_url = dynamic.replace('dynamic_playlist.m3u8', data).replace('type=live', "type=replay")
                self.replay_output.setText(replay_url)
            except Exception as e:
                self.replay_output.setText(f"Error: {e}")

    def copy_replay_url(self):
        url = self.replay_output.text().strip()
        if url:
            QApplication.clipboard().setText(url)

    def send_to_table(self):
        url = self.replay_output.text().strip()
        if not url:
            return
        for i in range(self.table.rowCount()):
            url_item = self.table.item(i, 0)
            if not url_item or not url_item.text().strip() or is_placeholder(url_item):
                self.table.setItem(i, 0, QTableWidgetItem(url))
                self.table.setRowHeight(i, 26)
                return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(url))
        full_item = QTableWidgetItem()
        full_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        full_item.setCheckState(Qt.CheckState.Unchecked)
        self.table.setItem(row, 1, full_item)
        self.table.setItem(row, 2, QTableWidgetItem("HH:MM:SS"))
        self.table.setItem(row, 3, QTableWidgetItem("HH:MM:SS"))
        self.table.setItem(row, 4, make_status_item("Pending"))
        self.table.setRowHeight(row, 26)

    def _set_running_state(self, running: bool):
        if running:
            self.start_btn.setText("⏸  Running…")
            self.start_btn.setEnabled(False)
        else:
            self.start_btn.setText("▶  Start")
            self.start_btn.setEnabled(True)

    def start_worker(self):
        if self.worker and self.worker.isRunning():
            return
        merge = self.merge_checkbox.isChecked()
        try:
            self.worker = FFmpegWorker(self.table, self.output_folder, merge)
        except FileNotFoundError:
            self.update_status(0, "FFmpeg Not Found")
            return

        self._slow_warned.clear()
        self.progress.setValue(15)
        self.progress.setVisible(True)
        self.status_label.setText("Starting…")
        self.status_label.setVisible(True)
        self._set_running_state(True)

        self.worker.progress_signal.connect(self.on_progress)
        self.worker.status_signal.connect(self.update_status)
        self.worker.cutting_slow_signal.connect(self.on_cutting_slow)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def cancel_worker(self):
        if self.worker:
            self.worker.stop()

    def on_progress(self, value):
        self.progress.setValue(max(15, value))

    def on_cutting_slow(self, row):
        item = self.table.item(row, 4)
        if item:
            item.setText("✂️ Cutting  (this may take a while)")
        self._slow_warned.add(row)
        self.status_label.setText(f"Row {row+1} — still cutting, large file…")

    def on_worker_finished(self):
        self._set_running_state(False)
        self.progress.setValue(100)
        self.status_label.setText("Done")
        self.status_label.setStyleSheet("""
            QLabel {
                color: #3a7a3a;
                font-size: 10px;
                background-color: transparent;
                border: none;
                padding: 0px 2px;
            }
        """)
        QTimer.singleShot(5000, self.hide_progress)

    def hide_progress(self):
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.status_label.setVisible(False)
        self.status_label.setText("")
        self.status_label.setStyleSheet("""
            QLabel {
                color: #3a3a3a;
                font-size: 10px;
                background-color: transparent;
                border: none;
                padding: 0px 2px;
            }
        """)

    def update_status(self, row, status):
        self.table.setItem(row, 4, make_status_item(status))
        label_map = {
            "Cutting":        f"Row {row+1} — cutting audio…",
            "Converting":     f"Row {row+1} — converting…",
            "Done":           f"Row {row+1} — done ✓",
            "Cut Error":      f"Row {row+1} — cut error",
            "Convert Error":  f"Row {row+1} — convert error",
            "Invalid Time":   f"Row {row+1} — invalid timestamps",
            "FFmpeg Not Found": "FFmpeg not found",
        }
        if status in label_map:
            self.status_label.setText(label_map[status])


# ------------------ QSS THEME ------------------
APP_QSS = f"""
    QMainWindow, QWidget {{
        background-color: #0a0a0a;
        color: #e0e0e0;
    }}
    QPushButton {{
        background-color: rgba(255,255,255,0.05);
        color: #c0c0c0;
        border: 1px solid rgba(255,255,255,0.07);
        border-radius: 6px;
        padding: 5px 10px;
        font-size: 12px;
    }}
    QPushButton:hover {{
        background-color: rgba(255,255,255,0.09);
        color: #e8e8e8;
        border: 1px solid rgba(255,255,255,0.07);
    }}
    QPushButton:pressed {{
        background-color: rgba(255,255,255,0.03);
        color: #999999;
        border: 1px solid rgba(255,255,255,0.07);
    }}
    QPushButton:disabled {{
        background-color: rgba(245,167,5,0.08);
        color: {GOLD};
        border: 1px solid rgba(245,167,5,0.20);
    }}
    QLineEdit {{
        background-color: rgba(255,255,255,0.03);
        color: #e0e0e0;
        border: 1px solid rgba(255,255,255,0.07);
        border-radius: 5px;
        padding: 4px 8px;
        font-size: 12px;
    }}
    QLineEdit:focus {{
        border: 1px solid rgba(255,255,255,0.16);
        background-color: rgba(255,255,255,0.05);
    }}
    QLineEdit:hover {{
        border: 1px solid rgba(255,255,255,0.11);
    }}
    QProgressBar {{
        background-color: rgba(255,255,255,0.04);
        border: none;
        border-radius: 3px;
    }}
    QProgressBar::chunk {{
        background-color: qlineargradient(
            x1:0, y1:0, x2:1, y2:0,
            stop:0 #0a55cc, stop:1 #0a7aff
        );
        border-radius: 3px;
    }}
    QScrollBar:vertical {{
        background-color: transparent;
        width: 6px;
        border-radius: 3px;
    }}
    QScrollBar::handle:vertical {{
        background-color: rgba(255,255,255,0.08);
        border-radius: 3px;
        min-height: 20px;
    }}
    QScrollBar::handle:vertical:hover {{
        background-color: rgba(255,255,255,0.16);
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
    QScrollBar:horizontal {{
        background-color: transparent;
        height: 6px;
        border-radius: 3px;
    }}
    QScrollBar::handle:horizontal {{
        background-color: rgba(255,255,255,0.08);
        border-radius: 3px;
        min-width: 20px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background-color: rgba(255,255,255,0.16);
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
"""

# ------------------ RUN ------------------
if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        app.setStyleSheet(APP_QSS)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except Exception as e:
        with open(log_file, "a") as f:
            f.write(f"\n[{datetime.now()}] Startup error: {e}\n")
        raise