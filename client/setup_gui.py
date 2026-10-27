"""思印客 Silnk · 零配置接入向导（Windows 工控机用）。

用法：双击运行（或 python setup_gui.py）。
    1. 填服务器地址，选本机打印机队列
    2. 点「开始接入」，屏幕显示 4 位接入码
    3. 管理端「设备 → 待接入」里批准
    4. 客户端自动拿到设备令牌、写入 config.json，并可自动启动打印客户端

接入成功后不再需要手改 bat、手设 PRINTER_DEVICE_ID / TENANT_ID / SECRET_KEY。
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import quote

import tkinter as tk
from tkinter import messagebox, ttk
import urllib.request

CLIENT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(CLIENT_DIR, "config.json")
DEFAULT_SERVER = "https://silnk.cn"
POLL_SECONDS = 2

BG = "#ffffff"
FG = "#0f172a"
MUTED = "#64748b"
BLUE = "#2563eb"
GREEN = "#15803d"
RED = "#b91c1c"


def load_config():
    """读取已保存的接入配置。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(updates):
    """合并写入 config.json，保留旧字段（例如 printer_url）。

    先写临时文件再 os.replace：进程被杀不会留下半个 JSON，避免设备变成未配置。
    """
    data = load_config()
    data.update(updates)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return data


def normalize_server(text):
    """补全协议并去掉末尾斜杠。"""
    value = (text or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value.rstrip("/")


def http_json(url, payload=None, timeout=12, token=""):
    """极简 JSON 请求：返回 (成功, 解析后的响应体)。

    token 只在需要设备身份时传（例如重新配对前注销自己）。
    """
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 网络错误统一提示给界面
        return False, {"message": str(exc)}


def discover_printers():
    """复用打印客户端的队列发现逻辑（Windows PowerShell / Linux lpstat）。"""
    try:
        if CLIENT_DIR not in sys.path:
            sys.path.insert(0, CLIENT_DIR)
        import main as client_main  # noqa: WPS433 延迟导入，避免启动即扫队列
        return client_main._local_printers()  # noqa: SLF001 同目录共用实现
    except Exception:
        return []


def machine_fingerprint():
    """本机稳定指纹：主机名 + 网卡 MAC，用于管理端区分同一台机器。"""
    return f"{socket.gethostname()}-{uuid.getnode():012x}"


class SetupApp:
    """零配置接入向导主窗口。"""

    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.printers = []
        self.claiming = False
        root.title("思印客 Silnk · 零配置接入")
        root.configure(bg=BG)
        root.geometry("980x760")
        root.minsize(860, 700)

        base_font = ("Microsoft YaHei UI", 12)
        big_font = ("Microsoft YaHei UI", 54, "bold")
        small_font = ("Microsoft YaHei UI", 10)

        tk.Label(root, text="思印客 Silnk", bg=BG, fg=BLUE,
                 font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w", padx=32, pady=(24, 0))
        tk.Label(root, text="给旧打印机装大脑 · 零配置接入", bg=BG, fg=MUTED,
                 font=base_font).pack(anchor="w", padx=32, pady=(2, 18))

        form = tk.Frame(root, bg=BG)
        form.pack(fill="x", padx=32)
        tk.Label(form, text="服务器地址", bg=BG, fg=FG, font=base_font).grid(
            row=0, column=0, sticky="w", pady=6)
        self.server_var = tk.StringVar(value=(load_config().get("server_host")
                                              or DEFAULT_SERVER))
        tk.Entry(form, textvariable=self.server_var, font=base_font, width=46,
                 relief="solid", bd=1).grid(row=0, column=1, sticky="w", padx=12)

        tk.Label(form, text="打印机队列", bg=BG, fg=FG, font=base_font).grid(
            row=1, column=0, sticky="w", pady=6)
        self.printer_var = tk.StringVar()
        self.printer_box = ttk.Combobox(form, textvariable=self.printer_var,
                                        font=base_font, width=44, state="readonly")
        self.printer_box.grid(row=1, column=1, sticky="w", padx=12)
        tk.Button(form, text="刷新", font=small_font, command=self.refresh_printers,
                  relief="solid", bd=1, bg=BG, padx=12).grid(row=1, column=2)

        tk.Label(form, text="纸盒容量（张）", bg=BG, fg=FG, font=base_font).grid(
            row=2, column=0, sticky="w", pady=6)
        self.capacity_var = tk.StringVar(
            value=str(load_config().get("paper_capacity") or 250))
        tk.Entry(form, textvariable=self.capacity_var, font=base_font, width=10,
                 relief="solid", bd=1).grid(row=2, column=1, sticky="w", padx=12)

        card = tk.Frame(root, bg="#f8fafc", highlightbackground="#e2e8f0",
                        highlightthickness=1)
        card.pack(fill="x", padx=32, pady=22)
        tk.Label(card, text="接入码（请让管理员在后台批准）", bg="#f8fafc", fg=MUTED,
                 font=base_font).pack(pady=(18, 0))
        self.code_label = tk.Label(card, text="----", bg="#f8fafc", fg=BLUE,
                                   font=big_font)
        self.code_label.pack(pady=(2, 0))
        self.status_label = tk.Label(card, text="未接入", bg="#f8fafc", fg=MUTED,
                                     font=base_font)
        self.status_label.pack(pady=(2, 18))

        actions = tk.Frame(root, bg=BG)
        actions.pack(fill="x", padx=32)
        self.claim_button = tk.Button(actions, text="开始接入", command=self.start_claim,
                                      bg=BLUE, fg="white", font=("Microsoft YaHei UI", 13, "bold"),
                                      relief="flat", padx=26, pady=10)
        self.claim_button.pack(side="left")
        self.launch_button = tk.Button(actions, text="启动打印客户端", command=self.launch_client,
                                       bg=BG, fg=FG, font=base_font, relief="solid", bd=1,
                                       padx=18, pady=8, state="disabled")
        self.launch_button.pack(side="left", padx=12)
        tk.Button(actions, text="重新配对", command=self.reset_claim, font=base_font,
                  relief="solid", bd=1, bg=BG, padx=18, pady=8).pack(side="left")

        self.auto_start_var = tk.BooleanVar(value=True)
        tk.Checkbutton(root, text="接入成功后自动启动打印客户端", variable=self.auto_start_var,
                       bg=BG, fg=MUTED, font=small_font, activebackground=BG,
                       selectcolor=BG).pack(anchor="w", padx=32, pady=(10, 0))

        tk.Label(root, text="运行日志", bg=BG, fg=MUTED, font=small_font).pack(
            anchor="w", padx=32, pady=(14, 4))
        self.log_text = tk.Text(root, height=7, font=("Consolas", 10), bg="#0f172a",
                                fg="#e2e8f0", relief="flat", padx=10, pady=8)
        self.log_text.pack(fill="both", expand=True, padx=32, pady=(0, 24))
        self.log_text.configure(state="disabled")

        self.refresh_printers()
        self.restore_bound_state()
        self.root.after(120, self.pump_events)

    # ---------- 界面状态 ----------

    def log(self, message):
        """追加一行日志。"""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S ") + str(message) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, text, color=MUTED):
        self.status_label.configure(text=text, fg=color)

    def restore_bound_state(self):
        """已经接入过的机器：直接显示设备 ID，并允许启动客户端。"""
        config = load_config()
        if config.get("device_id") and config.get("token"):
            self.code_label.configure(text="已接入", fg=GREEN, font=("Microsoft YaHei UI", 34, "bold"))
            self.set_status(f"设备 {config['device_id']} · {config.get('server_host', '')}", GREEN)
            self.launch_button.configure(state="normal")
            self.claim_button.configure(state="disabled")
            self.log(f"发现已有配置：{config['device_id']}")
        else:
            self.log("未发现配置，点「开始接入」生成接入码")

    def refresh_printers(self):
        """扫描本机打印队列并填入下拉框。"""
        self.printers = discover_printers()
        names = [p.get("name", "") for p in self.printers if p.get("name")]
        self.printer_box["values"] = names
        if names:
            saved = load_config().get("printer_name")
            self.printer_var.set(saved if saved in names else names[0])
            self.log(f"发现 {len(names)} 个打印机队列：{'、'.join(names)}")
        else:
            self.printer_var.set("")
            self.log("未发现打印机队列（可先安装驱动，再点刷新）")

    def reset_claim(self):
        """重新配对：先让服务端注销旧设备，再备份并清空本地配置。"""
        if self.claiming:
            messagebox.showinfo("提示", "正在接入中，请稍候")
            return
        config = load_config()
        if config:
            # 旧设备主动注销，避免反复配对堆出一堆幽灵设备、最后撞上配额
            try:
                if config.get("server_host") and config.get("token"):
                    ok, resp = http_json(
                        config["server_host"] + "/api/device/self/retire",
                        {}, token=config["token"])
                    if ok and resp.get("message") == "success":
                        self.log("旧设备已从服务端注销")
                    else:
                        self.log(f"旧设备注销失败（继续本地重置）：{resp.get('message')}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"旧设备注销异常：{exc}")
            backup = CONFIG_PATH + ".bak"
            try:
                with open(backup, "w", encoding="utf-8") as handle:
                    json.dump(config, handle, ensure_ascii=False, indent=2)
                os.remove(CONFIG_PATH)
                self.log(f"旧配置已备份到 {os.path.basename(backup)}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"备份旧配置失败：{exc}")
                if os.path.exists(CONFIG_PATH):
                    # 旧配置没删掉就不能假装未接入：setting.py 仍会读到旧 token
                    messagebox.showerror(
                        "重置失败", "无法删除旧配置，请关闭正在运行的客户端后重试")
                    return
        self.code_label.configure(text="----", fg=BLUE,
                                  font=("Microsoft YaHei UI", 54, "bold"))
        self.set_status("未接入")
        self.launch_button.configure(state="disabled")
        self.claim_button.configure(state="normal")

    # ---------- 配对流程 ----------

    def start_claim(self):
        if self.claiming:
            return
        server = normalize_server(self.server_var.get())
        if not server:
            messagebox.showwarning("提示", "请填写服务器地址")
            return
        self.server_var.set(server)
        # 没有选中队列就别申请：否则批下来的是空设备，打印链路必然失败
        selected = self.printer_var.get().strip()
        if not selected:
            messagebox.showwarning("提示", "请先选择打印机队列，再点开始接入")
            return
        self.claiming = True
        self.claim_button.configure(state="disabled")
        self.set_status("正在申请接入码…")
        self.log(f"连接服务器 {server}")
        # Tk 变量只能在主线程读：先取值再交给后台线程
        try:
            capacity = int(self.capacity_var.get() or 250)
        except ValueError:
            capacity = 250
        printers = list(self.printers)
        thread = threading.Thread(
            target=self.claim_worker,
            args=(server, selected, capacity, printers), daemon=True)
        thread.start()

    def claim_worker(self, server, selected, capacity, printers):
        """后台线程：申请接入码并轮询批准结果。"""
        try:
            model = ""
            for item in printers:
                if item.get("name") == selected:
                    model = item.get("model", "")
                    break
            payload = {
                "hostname": socket.gethostname(),
                "fingerprint": machine_fingerprint(),
                "model": model,
                "printer_name": selected,
                "printers": printers,
                "paper_capacity": capacity,
            }
            ok, response = http_json(f"{server}/api/device/claim", payload)
            if not ok:
                self.events.put(("error", f"申请失败：{response.get('message')}"))
                return
            data = response.get("data") or {}
            claim_id = data.get("claim_id")
            secret = data.get("secret")
            code = data.get("code")
            expires_at = int(data.get("expires_at") or 0)
            if not (claim_id and secret and code):
                self.events.put(("error", response.get("message") or "服务器未返回接入码"))
                return
            self.events.put(("code", code))
            self.events.put(("log", f"接入码 {code}，等待管理端批准（30 分钟内有效）"))
            url = (f"{server}/api/device/claim/{quote(claim_id)}"
                   f"?secret={quote(secret)}")
            while time.time() < expires_at:
                time.sleep(POLL_SECONDS)
                # 带时间戳避免被中间代理/缓存当成同一个 GET 反复返回 pending
                poll_url = f"{url}&_ts={int(time.time() * 1000)}"
                ok, response = http_json(poll_url)
                if not ok:
                    self.events.put(("log", f"轮询失败，稍后重试：{response.get('message')}"))
                    continue
                data = response.get("data") or {}
                status = data.get("status")
                if status == "bound" and data.get("token"):
                    self.events.put(("bound", {
                        "server": server,
                        "device_id": data.get("device_id", ""),
                        "token": data.get("token"),
                    }))
                    return
                if status in ("expired", "rejected"):
                    self.events.put(("error", "接入码已过期或被拒绝，请重新申请"))
                    return
                if status == "token_taken":
                    self.events.put(("error", "令牌已被取走，请点「重新配对」再试"))
                    return
            self.events.put(("error", "接入码已过期，请重新申请"))
        except Exception as exc:  # noqa: BLE001 后台线程兜底
            self.events.put(("error", f"接入失败：{exc}"))

    def pump_events(self):
        """把后台线程的事件搬回 Tk 主线程。

        单个事件处理失败（磁盘只读、控件异常等）不能让事件泵停摆，
        否则界面会永远停在「等待管理端批准」。
        """
        try:
            while True:
                kind, value = self.events.get_nowait()
                try:
                    if kind == "code":
                        self.code_label.configure(text=str(value), fg=BLUE)
                        self.set_status("等待管理端批准…")
                    elif kind == "log":
                        self.log(value)
                    elif kind == "bound":
                        self.on_bound(value)
                    elif kind == "error":
                        self.claiming = False
                        self.claim_button.configure(state="normal")
                        self.code_label.configure(text="----", fg=RED)
                        self.set_status(str(value), RED)
                        self.log(value)
                except Exception as exc:  # noqa: BLE001 单条事件失败不影响后续
                    self.claiming = False
                    try:
                        self.set_status(f"接入出错：{exc}", RED)
                        self.claim_button.configure(state="normal")
                        self.log(f"处理接入事件失败：{exc}")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        finally:
            self.root.after(150, self.pump_events)

    def on_bound(self, data):
        """配对成功：写 config.json，按需自动启动客户端。"""
        self.claiming = False
        try:
            capacity = int(self.capacity_var.get() or 250)
        except ValueError:
            capacity = 250
        config = save_config({
            "server_host": data["server"],
            "device_id": data["device_id"],
            "token": data["token"],
            "printer_name": self.printer_var.get(),
            # Linux 出纸走 CUPS 的 IPP 地址；Windows 走 SumatraPDF + 队列名
            "printer_url": ("" if sys.platform.startswith("win")
                            else "ipp://localhost:631/printers/"
                                 + quote(self.printer_var.get())),
            "printer_display_name": socket.gethostname(),
            "paper_capacity": capacity,
        })
        self.code_label.configure(text="已接入", fg=GREEN,
                                  font=("Microsoft YaHei UI", 34, "bold"))
        self.set_status(f"设备 {config['device_id']} · {config['server_host']}", GREEN)
        self.launch_button.configure(state="normal")
        self.claim_button.configure(state="disabled")
        self.log(f"接入成功，配置已写入 {os.path.basename(CONFIG_PATH)}")
        if self.auto_start_var.get():
            self.launch_client()

    # ---------- 启动打印客户端 ----------

    def launch_client(self):
        """以独立进程启动 main.py，避免界面阻塞。"""
        config = load_config()
        if not config.get("device_id") or not config.get("token"):
            messagebox.showwarning("提示", "还没有接入成功，请先完成接入")
            return
        main_py = os.path.join(CLIENT_DIR, "main.py")
        if not os.path.isfile(main_py):
            messagebox.showerror("错误", "找不到 main.py，请确认文件完整")
            return
        creation_flags = 0
        if sys.platform.startswith("win"):
            creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen([sys.executable, main_py], cwd=CLIENT_DIR,
                                       creationflags=creation_flags)
            self.log(f"打印客户端已启动（PID {process.pid}）")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启动失败", str(exc))
            self.log(f"启动失败：{exc}")


def main():
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
