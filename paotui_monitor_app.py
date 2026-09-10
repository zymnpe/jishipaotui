import ctypes
import gzip
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import urllib.request

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RAW_REQUEST = os.path.join(BASE_DIR, "task_list_raw.txt")
DEFAULT_DB = os.path.join(BASE_DIR, "paotui_seen.sqlite3")

SKIP_HEADERS = {"host", "connection", "content-length", "accept-encoding"}


def decode_text(body):
    best_text = ""
    best_score = None
    for encoding in ("utf-8", "gb18030"):
        text = body.decode(encoding, errors="replace")
        score = text.count("\ufffd")
        if best_score is None or score < best_score:
            best_text = text
            best_score = score
    return best_text


def parse_raw_request(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().replace("\r\n", "\n")

    head, _, body = text.partition("\n\n")
    lines = [line for line in head.split("\n") if line.strip()]
    if not lines:
        raise ValueError("Raw Request 文件为空")

    parts = lines[0].strip().split()
    if len(parts) < 2:
        raise ValueError("Raw Request 第一行格式不正确")

    method = parts[0].upper()
    target = parts[1]
    host = ""
    headers = {}

    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key.lower() == "host":
            host = value
            continue
        if key.lower() in SKIP_HEADERS:
            continue
        headers[key] = value

    if target.startswith("http://") or target.startswith("https://"):
        url = target
    elif host:
        url = f"https://{host}{target}"
    else:
        raise ValueError("Raw Request 缺少 Host")

    return method, url, headers, body.strip().encode("utf-8")


def request_json(method, url, headers, body, insecure, proxy):
    req = urllib.request.Request(url, data=body or None, headers=headers, method=method)
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    if insecure:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(req, timeout=20) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return resp.status, json.loads(decode_text(data))
    except urllib.error.HTTPError as exc:
        text = decode_text(exc.read())
        try:
            return exc.code, json.loads(text)
        except Exception:
            return exc.code, {"raw": text}


def extract_tasks(payload):
    if not isinstance(payload, dict):
        return []
    tasks = (payload.get("data") or {}).get("list") or []
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def connect_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_tasks ("
        "task_id TEXT PRIMARY KEY,"
        "first_seen_at INTEGER NOT NULL,"
        "title TEXT,"
        "pay_price TEXT,"
        "post_time TEXT,"
        "raw_json TEXT NOT NULL"
        ")"
    )
    conn.commit()
    return conn


def is_seen(conn, task_id):
    return conn.execute("SELECT 1 FROM seen_tasks WHERE task_id=?", (task_id,)).fetchone() is not None


def mark_seen(conn, task):
    task_id = str(task.get("task_id") or "")
    if not task_id:
        return
    conn.execute(
        "INSERT OR IGNORE INTO seen_tasks(task_id, first_seen_at, title, pay_price, post_time, raw_json) "
        "VALUES(?,?,?,?,?,?)",
        (
            task_id,
            int(time.time()),
            str(task.get("title") or ""),
            str(task.get("pay_price") or ""),
            str(task.get("post_time") or ""),
            json.dumps(task, ensure_ascii=False),
        ),
    )
    conn.commit()


def format_task(task):
    title = str(task.get("title") or task.get("task_remark") or "新跑腿单")
    if len(title) > 80:
        title = title[:80] + "..."

    fields = [title]
    pairs = [
        ("金额", task.get("pay_price")),
        ("状态", task.get("task_status_text")),
        ("发布", task.get("post_time")),
        ("校区", task.get("campus_name")),
        ("起点", task.get("send_address")),
        ("终点", task.get("recv_address")),
        ("送达前", task.get("expect_arrive_time_text")),
    ]
    for name, value in pairs:
        value = str(value or "").strip()
        if value:
            fields.append(f"{name}: {value}")
    return "\n".join(fields)


def bark_push(bark_url, title, body):
    if not bark_url:
        return
    payload = {
        "title": title,
        "body": body,
        "level": "active",
        "sound": "default",
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(bark_url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


class MonitorWorker(QThread):
    log = pyqtSignal(str)
    status = pyqtSignal(str)
    new_task = pyqtSignal(dict)
    poll_result = pyqtSignal(int, int)

    def __init__(self, raw_path, db_path, interval, insecure, proxy, bark_url, alert_existing):
        super().__init__()
        self.raw_path = raw_path
        self.db_path = db_path
        self.interval = interval
        self.insecure = insecure
        self.proxy = proxy
        self.bark_url = bark_url
        self.alert_existing = alert_existing
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        try:
            method, url, headers, body = parse_raw_request(self.raw_path)
            conn = connect_db(self.db_path)
        except Exception as exc:
            self.log.emit(f"启动失败: {exc}")
            self.status.emit("启动失败")
            return

        first_poll = True
        self.status.emit("监控中")
        self.log.emit(f"开始监控: {method} {url}")

        while self.running:
            started = time.time()
            try:
                status, payload = request_json(method, url, headers, body, self.insecure, self.proxy)
                tasks = extract_tasks(payload)
                self.poll_result.emit(status, len(tasks))
                self.log.emit(f"轮询完成: status={status}, tasks={len(tasks)}")

                if status != 200:
                    self.log.emit(json.dumps(payload, ensure_ascii=False)[:600])
                else:
                    for task in tasks:
                        task_id = str(task.get("task_id") or "")
                        if not task_id or is_seen(conn, task_id):
                            continue
                        mark_seen(conn, task)
                        if self.alert_existing or not first_poll:
                            self.new_task.emit(task)

                first_poll = False
            except Exception as exc:
                self.log.emit(f"轮询出错: {exc}")

            elapsed = time.time() - started
            wait_left = max(1, self.interval - elapsed)
            for _ in range(int(wait_left * 10)):
                if not self.running:
                    break
                time.sleep(0.1)

        conn.close()
        self.status.emit("已停止")
        self.log.emit("监控已停止")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.setWindowTitle("校园集市跑腿新单提醒")
        self.resize(780, 560)
        self.build_ui()
        self.apply_style()

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(16)

        title = QLabel("校园集市跑腿新单提醒")
        title.setObjectName("Title")
        subtitle = QLabel("使用个人登录后的请求文件轮询大厅，每轮按 task_id 去重，发现新单立即弹窗。")
        subtitle.setObjectName("Subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        settings = QFrame()
        settings.setObjectName("Panel")
        grid = QGridLayout(settings)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        self.raw_input = QLineEdit(DEFAULT_RAW_REQUEST)
        self.raw_input.setPlaceholderText("选择 Fiddler 导出的 /task/list Raw Request")
        browse_btn = QPushButton("选择")
        browse_btn.clicked.connect(self.browse_raw)

        self.interval_input = QSpinBox()
        self.interval_input.setRange(10, 3600)
        self.interval_input.setValue(60)
        self.interval_input.setSuffix(" 秒")

        self.proxy_check = QCheckBox("请求经过 Fiddler 代理")
        self.proxy_input = QLineEdit("http://127.0.0.1:8888")
        self.proxy_input.setEnabled(False)
        self.proxy_check.toggled.connect(self.proxy_input.setEnabled)

        self.bark_check = QCheckBox("同时推送到 Bark")
        self.bark_url_input = QLineEdit()
        self.bark_url_input.setPlaceholderText("https://api.day.app/你的key/校园集市跑腿新单")
        self.bark_url_input.setEnabled(False)
        self.bark_check.toggled.connect(self.bark_url_input.setEnabled)

        self.insecure_check = QCheckBox("忽略 TLS 证书校验")
        self.insecure_check.setChecked(True)

        self.alert_existing_check = QCheckBox("首次轮询也弹窗")

        grid.addWidget(QLabel("请求文件"), 0, 0)
        grid.addWidget(self.raw_input, 0, 1)
        grid.addWidget(browse_btn, 0, 2)
        grid.addWidget(QLabel("轮询间隔"), 1, 0)
        grid.addWidget(self.interval_input, 1, 1)
        grid.addWidget(self.proxy_check, 2, 0)
        grid.addWidget(self.proxy_input, 2, 1, 1, 2)
        grid.addWidget(self.bark_check, 3, 0)
        grid.addWidget(self.bark_url_input, 3, 1, 1, 2)
        grid.addWidget(self.insecure_check, 4, 0)
        grid.addWidget(self.alert_existing_check, 4, 1)

        layout.addWidget(settings)

        actions = QHBoxLayout()
        self.start_btn = QPushButton("开始监控")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(self.start_monitor)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_monitor)
        self.test_btn = QPushButton("测试一次")
        self.test_btn.clicked.connect(self.test_once)
        actions.addWidget(self.start_btn)
        actions.addWidget(self.stop_btn)
        actions.addWidget(self.test_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status_label = QLabel("状态: 未启动")
        self.status_label.setObjectName("Status")
        layout.addWidget(self.status_label)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("运行日志会显示在这里")
        layout.addWidget(self.log_view, 1)

    def apply_style(self):
        self.setStyleSheet(
            """
            QWidget {
                background: #f5f2ea;
                color: #24312f;
                font-family: "Microsoft YaHei";
                font-size: 14px;
            }
            #Title {
                font-size: 28px;
                font-weight: 700;
                color: #173b33;
            }
            #Subtitle {
                color: #66736d;
                font-size: 13px;
            }
            #Panel {
                background: #fffaf0;
                border: 1px solid #dfd2bd;
                border-radius: 8px;
            }
            QLineEdit, QSpinBox, QTextEdit {
                background: #ffffff;
                border: 1px solid #cdbfa8;
                border-radius: 6px;
                padding: 8px;
            }
            QPushButton {
                background: #e4ece0;
                border: 1px solid #9fb49a;
                border-radius: 6px;
                padding: 9px 16px;
            }
            QPushButton:hover {
                background: #d8e6d3;
            }
            QPushButton:disabled {
                color: #999999;
                background: #ece7dd;
                border-color: #d3cabb;
            }
            #Primary {
                background: #236b54;
                border-color: #236b54;
                color: white;
                font-weight: 700;
            }
            #Primary:hover {
                background: #1d5c48;
            }
            #Status {
                color: #355247;
                font-weight: 700;
            }
            """
        )

    def browse_raw(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Raw Request 文件", BASE_DIR, "Text Files (*.txt);;All Files (*)")
        if path:
            self.raw_input.setText(path)

    def append_log(self, message):
        self.log_view.append(f"[{time.strftime('%H:%M:%S')}] {message}")

    def update_status(self, status):
        self.status_label.setText(f"状态: {status}")

    def set_running(self, running):
        self.start_btn.setEnabled(not running)
        self.test_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def build_worker(self, alert_existing=False):
        raw_path = self.raw_input.text().strip()
        if not os.path.exists(raw_path):
            QMessageBox.warning(self, "请求文件不存在", "请选择有效的 /task/list Raw Request 文件。")
            return None
        proxy = self.proxy_input.text().strip() if self.proxy_check.isChecked() else ""
        bark_url = self.bark_url_input.text().strip() if self.bark_check.isChecked() else ""
        return MonitorWorker(
            raw_path=raw_path,
            db_path=DEFAULT_DB,
            interval=self.interval_input.value(),
            insecure=self.insecure_check.isChecked(),
            proxy=proxy,
            bark_url=bark_url,
            alert_existing=alert_existing,
        )

    def start_monitor(self):
        self.worker = self.build_worker(alert_existing=self.alert_existing_check.isChecked())
        if not self.worker:
            return
        self.worker.log.connect(self.append_log)
        self.worker.status.connect(self.update_status)
        self.worker.new_task.connect(self.show_task_popup)
        self.worker.poll_result.connect(self.update_poll_result)
        self.worker.finished.connect(lambda: self.set_running(False))
        self.set_running(True)
        self.worker.start()

    def stop_monitor(self):
        if self.worker:
            self.worker.stop()
            self.update_status("正在停止")

    def test_once(self):
        worker = self.build_worker(alert_existing=False)
        if not worker:
            return
        try:
            method, url, headers, body = parse_raw_request(worker.raw_path)
            status, payload = request_json(method, url, headers, body, worker.insecure, worker.proxy)
            tasks = extract_tasks(payload)
            self.append_log(f"测试完成: status={status}, tasks={len(tasks)}")
            QMessageBox.information(self, "测试结果", f"status={status}\n任务数量: {len(tasks)}")
        except Exception as exc:
            self.append_log(f"测试失败: {exc}")
            QMessageBox.critical(self, "测试失败", str(exc))

    def update_poll_result(self, status, count):
        self.update_status(f"监控中 · status={status} · tasks={count}")

    def show_task_popup(self, task):
        task_id = str(task.get("task_id") or "")
        title = f"校园集市跑腿新单 {task_id}" if task_id else "校园集市跑腿新单"
        body = format_task(task)
        try:
            if self.worker and getattr(self.worker, "bark_url", ""):
                bark_push(self.worker.bark_url, title, body)
        except Exception as exc:
            self.append_log(f"Bark 推送失败: {exc}")
        try:
            ctypes.windll.user32.MessageBeep(0x00000040)
        except Exception:
            pass
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(body)
        box.setIcon(QMessageBox.Information)
        box.setStandardButtons(QMessageBox.Ok)
        box.setWindowModality(Qt.ApplicationModal)
        box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
        box.exec_()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
