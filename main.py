import sys, os, re, shutil, json, time, html
from datetime import datetime
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTextBrowser, QProgressBar,
    QComboBox, QMessageBox, QSizePolicy, QGroupBox
)

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))

# ----------------- Utils -----------------
def split_exts(text):
    # storage filename extension
    parts = [p.strip().lower().lstrip(".") for p in text.split(",") if p.strip()]
    return {p for p in parts if p}

def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def safe_move(src_path, dst_path):
    # this is fuking important for preventing overwrite
    if not os.path.exists(dst_path):
        shutil.move(src_path, dst_path)
        return dst_path
    base, ext = os.path.splitext(os.path.basename(dst_path))
    folder = os.path.dirname(dst_path)
    i = 1
    while True:
        candidate = os.path.join(folder, f"{base} ({i}){ext}")
        if not os.path.exists(candidate):
            shutil.move(src_path, candidate)
            return candidate
        i += 1

def safe_copy(src_path, dst_path):
    if not os.path.exists(dst_path):
        shutil.copy2(src_path, dst_path)
        return dst_path
    base, ext = os.path.splitext(os.path.basename(dst_path))
    folder = os.path.dirname(dst_path)
    i = 1
    while True:
        candidate = os.path.join(folder, f"{base} ({i}){ext}")
        if not os.path.exists(candidate):
            shutil.copy2(src_path, candidate)
            return candidate
        i += 1

def list_files(folder, exts):
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if not exts or ext in exts:
                yield p

# ----------------- Classify -----------------
class ClassifyWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    done = pyqtSignal(str)  # Feed back mapping dir path（for undo）

    def __init__(self, folder, pattern_text, exts_text, ignore_case=True, parent=None, op_mode="move"):
        super().__init__(parent)
        self.folder = folder
        self.pattern_text = pattern_text
        self.exts = split_exts(exts_text)
        self.flags = re.IGNORECASE if ignore_case else 0
        self._stop = False
        self.op_mode = op_mode  # "move" or "copy"

    def run(self):
        try:
            regex = re.compile(self.pattern_text, self.flags)
        except re.error as e:
            self.log.emit(f"[錯誤] 正則無效：{e}")
            self.progress.emit(0)
            return

        files = list(list_files(self.folder, self.exts))
        total = len(files)
        if total == 0:
            self.log.emit("[訊息] 找不到符合副檔名的檔案。")
            self.progress.emit(0)
            return

        # build mapping record (for undo)
        # Create classify_moves directory in APP_DIR
        classify_moves_dir = os.path.join(APP_DIR, "classify_moves")
        ensure_dir(classify_moves_dir)
        
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mapping_path = os.path.join(classify_moves_dir, f"_classify_moves_{stamp}.json")
        moves = []

        moved, skipped = 0, 0
        for i, path in enumerate(files, start=1):
            if self._stop: break
            name = os.path.basename(path)
            m = regex.search(name)
            if not m or m.lastindex is None or m.lastindex < 1:
                self.log.emit(f"[略過] 未匹配：{name}")
                skipped += 1
            else:
                group_txt = m.group(1).strip() or "Unknown"
                dest_dir = os.path.join(self.folder, group_txt)
                ensure_dir(dest_dir)
                dest_path = os.path.join(dest_dir, name)
                try:
                    if self.op_mode == "copy":
                        final_path = safe_copy(path, dest_path)
                        moves.append({"op": "copy", "from": final_path})
                        moved += 1
                        self.log.emit(f"[複製] {name} → {group_txt}")
                    else:
                        final_path = safe_move(path, dest_path)
                        moves.append({"op": "move", "from": final_path, "to": os.path.join(self.folder, name)})
                        moved += 1
                        self.log.emit(f"[移動] {name} → {group_txt}")
                except Exception as e:
                    self.log.emit(f"[錯誤] 無法處理 {name}：{e}")

            self.progress.emit(int(i / total * 100))

        # Writing mapping file
        if moves:
            try:
                with open(mapping_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "moves": moves,
                        "base": self.folder,
                        "regex": self.pattern_text,
                        "exts": sorted(list(self.exts)),
                        "time": stamp,
                        "op_mode": self.op_mode
                    }, f, ensure_ascii=False, indent=2)
                self.log.emit(f"[完成] 已處理 {moved} 個檔案，未匹配 {skipped}。")
                self.log.emit(f"[紀錄] 還原檔：{os.path.basename(mapping_path)}")
                self.done.emit(mapping_path)
            except Exception as e:
                self.log.emit(f"[警告] 無法寫入還原紀錄：{e}")
                self.done.emit("")
        else:
            self.log.emit("[完成] 沒有檔案被處理。")
            self.done.emit("")

    def stop(self):
        self._stop = True

# ----------------- Undo System -----------------
class UndoWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, mapping_path, parent=None):
        super().__init__(parent)
        self.mapping_path = mapping_path

    def run(self):
        try:
            data = json.load(open(self.mapping_path, "r", encoding="utf-8"))
        except Exception as e:
            self.log.emit(f"[錯誤] 讀取還原紀錄失敗：{e}")
            return
        moves = data.get("moves", [])
        total = len(moves)
        if total == 0:
            self.log.emit("[訊息] 紀錄中沒有可還原的移動。")
            self.done.emit()
            return

        for i, item in enumerate(moves, start=1):
            op = item.get("op", "move")
            if op == "copy":
                src = item.get("from")
                try:
                    if src and os.path.exists(src):
                        os.remove(src)
                        self.log.emit(f"[刪除複製] {os.path.basename(src)}")
                    else:
                        self.log.emit(f"[跳過] 找不到：{src}")
                except Exception as e:
                    self.log.emit(f"[錯誤] 刪除失敗 {src}：{e}")
            else:
                src, dst = item.get("from"), item.get("to")
                try:
                    ensure_dir(os.path.dirname(dst))
                    final = safe_move(src, dst) if (src and os.path.exists(src)) else None
                    if final:
                        self.log.emit(f"[還原] {os.path.basename(src)}")
                    else:
                        self.log.emit(f"[跳過] 找不到：{src}")
                except Exception as e:
                    self.log.emit(f"[錯誤] 還原失敗 {src}：{e}")
            self.progress.emit(int(i / total * 100))
        self.log.emit("[完成] 已嘗試還原所有檔案。")
        self.done.emit()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("檔名分類小工具 by TheNano")
        self.resize(980, 720)
        self.settings = QSettings(os.path.join(APP_DIR, "settings.ini"), QSettings.IniFormat)
        self.last_mapping_path = ""

        self.build_ui()
        self.load_settings()
        self.apply_cute_theme()
        self.update_exts_placeholder()
        self.update_preview()

    # ------- UI -------
    def build_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel("檔名分類小工具 v0.1")
        title.setStyleSheet("font-size: 26px; font-weight: 600; color:#b44a7a;")
        root.addWidget(title)

        # Settings
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(8)

        g.addWidget(QLabel("選擇資料夾："), 0, 0)
        self.folder_edit = QLineEdit()
        self.folder_btn = QPushButton("瀏覽…")
        g.addWidget(self.folder_edit, 0, 1)
        g.addWidget(self.folder_btn, 0, 2)
        self.folder_btn.clicked.connect(self.pick_folder)
        self.folder_edit.textChanged.connect(self.on_settings_changed)

        g.addWidget(QLabel("篩選方式（Regex）："), 1, 0)
        self.regex_edit = QLineEdit(r"artist[_\s-]*([^,]+)")
        g.addWidget(self.regex_edit, 1, 1, 1, 2)
        self.regex_edit.textChanged.connect(self.on_regex_changed)

        g.addWidget(QLabel("篩選檔案類別："), 2, 0)
        self.kind_combo = QComboBox()
        self.kind_combo.addItems(["圖片", "文件", "其他"])
        g.addWidget(self.kind_combo, 2, 1)

        self.exts_edit = QLineEdit()
        self.exts_edit.setPlaceholderText("以逗號分隔，如：jpg, png, jpeg")
        g.addWidget(self.exts_edit, 2, 2)
        self.kind_combo.currentIndexChanged.connect(self.update_exts_placeholder)
        self.kind_combo.currentIndexChanged.connect(self.on_settings_changed)
        self.exts_edit.textChanged.connect(self.on_settings_changed)

        g.addWidget(QLabel("分類為："), 3, 0)
        self.op_combo = QComboBox()
        self.op_combo.addItems(["移動", "複製"])
        g.addWidget(self.op_combo, 3, 1)
        self.op_combo.currentIndexChanged.connect(self.on_settings_changed)

        preview_box = QGroupBox("正則預覽（綠色為成功匹配檔案）")
        pv_layout = QVBoxLayout(preview_box)
        self.preview_browser = QTextBrowser()
        self.preview_browser.setOpenExternalLinks(False)
        self.preview_browser.setStyleSheet("background: #fff;")
        pv_layout.addWidget(self.preview_browser)
        g.addWidget(preview_box, 4, 0, 1, 3)

        root.addLayout(g)

        ops = QHBoxLayout()
        self.preview_btn = QPushButton("刷新預覽")
        self.go_btn = QPushButton("開始分類")
        self.undo_btn = QPushButton("後悔還原")
        self.undo_btn.setEnabled(False)
        ops.addWidget(self.preview_btn)
        ops.addStretch(1)
        ops.addWidget(self.go_btn)
        ops.addWidget(self.undo_btn)
        root.addLayout(ops)

        self.preview_btn.clicked.connect(self.update_preview)
        self.go_btn.clicked.connect(self.start_classify)
        self.undo_btn.clicked.connect(self.start_undo)

        # Console + ProgressBar
        self.console = QTextBrowser()
        self.console.setStyleSheet("background:#ffffff;")
        self.console.setMinimumHeight(200)
        root.addWidget(self.console)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        tips = QLabel("Tip：可直接編輯副檔名清單；設定會自動保存。")
        tips.setStyleSheet("color:#7a7a7a;")
        root.addWidget(tips)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "選擇要分類的資料夾", self.folder_edit.text() or os.path.expanduser("~"))
        if d:
            self.folder_edit.setText(d)
            self.update_preview()

    def update_preview(self):
        folder = self.folder_edit.text().strip()
        pattern_text = self.regex_edit.text().strip()
        exts = split_exts(self.exts_edit.text().strip())

        try:
            regex = re.compile(pattern_text, re.IGNORECASE)
        except re.error as e:
            self.preview_browser.setHtml(f"<p style='color:#c0392b;'>正則錯誤：{html.escape(str(e))}</p>")
            return

        samples = []
        if os.path.isdir(folder):
            for i, path in enumerate(list_files(folder, exts)):
                samples.append(os.path.basename(path))
                if i >= 100:
                    break
        if not samples:
            # default example
            samples = [
                "未找到符合條件的檔案"
            ]

        # preview
        lines = []
        for i, s in enumerate(samples):
            esc = html.escape(s)
            m = regex.search(s)
            if len(samples) >= 100:
                lines.append(f"<div>• {esc} <span style='color:#999'>(還有{i - 99}項...)</span></div>")
                break
            if m and m.lastindex and m.lastindex >= 1:
                g1 = m.group(1)
                if g1 is None:
                    lines.append(f"<div>• {esc} <span style='color:#999'>(無第1組)</span></div>")
                else:
                    start, end = m.span(1)
                    # use green to hint the first group
                    before = html.escape(s[:start])
                    target = html.escape(s[start:end])
                    after = html.escape(s[end:])
                    lines.append(f"<div>• {before}<span style='background:#c9f7c9; color:#20613a; padding:1px 3px; border-radius:4px;'>{target}</span>{after}</div>")
            else:
                lines.append(f"<div>• {esc} <span style='color:#999'>(未匹配)</span></div>")

        self.preview_browser.setHtml("<div style='line-height:1.7'>" + "\n".join(lines) + "</div>")

    def start_classify(self):
        folder = self.folder_edit.text().strip()
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "提醒", "請先選擇有效的資料夾。")
            return
        pattern_text = self.regex_edit.text().strip()
        if not pattern_text:
            QMessageBox.warning(self, "提醒", "請輸入正則表達式。")
            return

        self.console.clear()
        self.progress.setValue(0)
        self.log(f"[開始] 分類資料夾：{folder}")
        self.save_settings()

        self.go_btn.setEnabled(False)
        self.undo_btn.setEnabled(False)

        op_mode = "copy" if self.op_combo.currentText() == "複製" else "move"
        self.worker = ClassifyWorker(folder, pattern_text, self.exts_edit.text().strip(), True, op_mode=op_mode)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.done.connect(self.on_classify_done)
        self.worker.start()

    def on_classify_done(self, mapping_path):
        self.go_btn.setEnabled(True)
        self.last_mapping_path = mapping_path
        self.undo_btn.setEnabled(bool(mapping_path))
        if mapping_path:
            self.log("[提示] 如需還原，點擊「後悔還原」。")

    # idk maybe it's important?
    def start_undo(self):
        if not self.last_mapping_path or not os.path.exists(self.last_mapping_path):
            QMessageBox.information(self, "訊息", "找不到可用的還原紀錄。")
            return
        self.log(f"[開始] 還原：{os.path.basename(self.last_mapping_path)}")
        self.progress.setValue(0)
        self.go_btn.setEnabled(False)
        self.undo_btn.setEnabled(False)

        self.undo_worker = UndoWorker(self.last_mapping_path)
        self.undo_worker.log.connect(self.log)
        self.undo_worker.progress.connect(self.progress.setValue)
        self.undo_worker.done.connect(self.on_undo_done)
        self.undo_worker.start()

    def on_undo_done(self):
        self.go_btn.setEnabled(True)
        self.undo_btn.setEnabled(False)
        self.log("[提示] 還原程序已完成。")

    # 
    # options
    #
    def update_exts_placeholder(self):
        kind = self.kind_combo.currentText()
        defaults = {
            "圖片": "jpg, jpeg, png, webp, bmp, gif",
            "文件": "pdf, doc, docx, xls, xlsx, ppt, pptx, txt, md",
            "其他": "zip, 7z, rar, mp4, mp3, wav"
        }
        if not self.exts_edit.text().strip():
            self.exts_edit.setText(defaults.get(kind, ""))

    def on_regex_changed(self):
        self.on_settings_changed()
        self.update_preview()

    def on_settings_changed(self):
        self.save_settings()

    def save_settings(self):
        self.settings.setValue("folder", self.folder_edit.text())
        self.settings.setValue("regex", self.regex_edit.text())
        self.settings.setValue("kind", self.kind_combo.currentIndex())
        self.settings.setValue("exts", self.exts_edit.text())
        self.settings.setValue("op", self.op_combo.currentIndex())

    def load_settings(self):
        self.folder_edit.setText(self.settings.value("folder", ""))
        self.regex_edit.setText(self.settings.value("regex", r"artist[_\s-]*([^,]+)"))
        self.kind_combo.setCurrentIndex(int(self.settings.value("kind", 0)))
        self.exts_edit.setText(self.settings.value("exts", "jpg, jpeg, png, webp, bmp, gif"))
        op_index = int(self.settings.value("op", 0))
        # ensure widget exists before setting
        try:
            self.op_combo.setCurrentIndex(op_index)
        except Exception:
            pass

    # customize log system
    def log(self, text):
        t = time.strftime("%H:%M:%S")
        self.console.append(f"<span style='color:#8e44ad'>[{t}]</span> {html.escape(text)}")

    #theme
    def apply_cute_theme(self):
        self.setStyleSheet("""
            QWidget { background: #fff6fb; font-size: 14px; }
            QGroupBox {
                border: 2px solid #ffd3e2; border-radius: 8px; margin-top: 8px;
                background: #ffffff;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color:#b44a7a; }
            QLineEdit, QTextBrowser, QComboBox {
                border: 2px solid #ffcfe1; border-radius: 8px; padding: 6px 8px; background: #fff;
            }
            QLineEdit:focus, QComboBox:focus { border-color: #ff9cc1; }
            QPushButton {
                background: #ffd6e7; border: none; border-radius: 18px; padding: 8px 16px;
                color: #7a2d52; font-weight: 600;
            }
            QPushButton:hover { background: #ffc2dd; }
            QPushButton:pressed { background: #ffb3d2; }
            QProgressBar {
                background: #ffeaf2; border: 2px solid #ffcfe1; border-radius: 10px; text-align: center;
                height: 18px;
            }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                      stop:0 #c5f6d6, stop:1 #8ee6b8); border-radius: 8px; }
        """)

def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
