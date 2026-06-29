# -*- coding: utf-8 -*-
"""
hermes.chat — Chat UI (tkinter)
深色主题聊天窗口，支持图片显示、设备选择、更新提示。
版本: 1.4
"""
__version__ = "1.4"

import os, sys, threading, time, queue, json, uuid

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from hermes.config import (
    VERSION, SERVER_URL, API_KEY, get_base_dir,
    agent_status, agent_log_queue, chat_system_queue, update_queue,
    devices_cache, devices_lock,
)


def run_chat():
    import urllib.request

    CHAT_URL = f"{SERVER_URL}/chat"

    def get_device_id():
        id_file = os.path.join(get_base_dir(), ".chat_device_id")
        if os.path.exists(id_file):
            return open(id_file).read().strip()
        did = uuid.uuid4().hex[:8]
        with open(id_file, "w") as f:
            f.write(did)
        return did

    DEVICE_ID = get_device_id()

    def send_message(msg, target_device_id=None):
        try:
            payload_dict = {"message": msg, "device_id": DEVICE_ID}
            if target_device_id:
                payload_dict["target_device_id"] = target_device_id
            payload = json.dumps(payload_dict).encode()
            req = urllib.request.Request(CHAT_URL, data=payload, headers={
                "Content-Type": "application/json", "X-Api-Key": API_KEY,
                "X-Device-Id": DEVICE_ID,
            })
            resp = urllib.request.urlopen(req, timeout=120)
            r = json.loads(resp.read())
            return r.get("reply", ""), r.get("images", []), r.get("history_len", 0)
        except Exception as e:
            return f"[错误] {e}", [], 0

    def clear_history():
        try:
            req = urllib.request.Request(
                CHAT_URL.replace("/chat", "/clear"), data=b"{}",
                headers={"Content-Type": "application/json", "X-Api-Key": API_KEY,
                         "X-Device-Id": DEVICE_ID})
            urllib.request.urlopen(req, timeout=10)
            return True
        except:
            return False

    class ChatApp:
        def __init__(self, root):
            self.root = root
            self.root.title(f"Hermes Unified v{VERSION}")
            self.root.geometry("600x780")
            self.root.configure(bg="#0d1117")
            self.root.resizable(True, True)
            self.is_sending = False
            self.photo_refs = []
            self.show_logs = True
            self._pending_update = None
            self._device_map = {}

            import tkinter.font as tkfont
            self.msg_font = tkfont.Font(family="Microsoft YaHei", size=11)
            self.input_font = tkfont.Font(family="Microsoft YaHei", size=12)
            self.title_font = tkfont.Font(family="Microsoft YaHei", size=13, weight="bold")
            self.mono_font = tkfont.Font(family="Consolas", size=9)

            self._build_ui()
            self._check_health()
            self._poll_agent_status()
            self._add_system(f"v{VERSION} 启动完成 — Agent + Chat UI 已就绪")

        def _build_ui(self):
            self.header = tk.Frame(self.root, bg="#161b22", height=52)
            self.header.pack(fill=tk.X)
            self.header.pack_propagate(False)
            tk.Label(self.header, text=f"🤖 Hermes v{VERSION}", font=self.title_font,
                     bg="#161b22", fg="#58a6ff", padx=15).pack(side=tk.LEFT, pady=10)

            self.agent_dot = tk.Label(self.header, text="●", font=("Arial", 14),
                                       bg="#161b22", fg="#f0883e")
            self.agent_dot.pack(side=tk.RIGHT, padx=(0, 2), pady=10)
            self.agent_label = tk.Label(self.header, text="Agent: 连接中...",
                                         font=("Microsoft YaHei", 9),
                                         bg="#161b22", fg="#8b949e", padx=5)
            self.agent_label.pack(side=tk.RIGHT, pady=10)

            self.target_device_var = tk.StringVar(value="自动")
            self.target_device_combo = ttk.Combobox(
                self.header, textvariable=self.target_device_var,
                values=["自动"], width=22, state="readonly",
                font=("Microsoft YaHei", 9))
            self.target_device_combo.pack(side=tk.RIGHT, padx=(10, 5), pady=10)
            tk.Label(self.header, text="目标:", font=("Microsoft YaHei", 9),
                     bg="#161b22", fg="#8b949e").pack(side=tk.RIGHT, pady=10)

            self.log_btn = tk.Button(self.header, text="日志", font=("Microsoft YaHei", 8),
                                      bg="#30363d", fg="#e6edf3", relief=tk.FLAT, padx=6,
                                      activebackground="#21262d", cursor="hand2",
                                      command=self._toggle_logs)
            self.log_btn.pack(side=tk.RIGHT, padx=2, pady=10)

            tk.Button(self.header, text="清除", font=("Microsoft YaHei", 8),
                      bg="#21262d", fg="#8b949e", relief=tk.FLAT, padx=6,
                      activebackground="#30363d", cursor="hand2",
                      command=self._clear_chat).pack(side=tk.RIGHT, padx=2, pady=10)

            self.log_frame = tk.Frame(self.root, bg="#0d1117", height=120)
            self.log_frame.pack(fill=tk.X, padx=10, pady=(0, 0))
            self.log_frame.pack_propagate(False)
            self.log_text = tk.Text(self.log_frame, font=self.mono_font,
                                     bg="#161b22", fg="#8b949e", relief=tk.FLAT,
                                     padx=8, pady=4, height=8, state=tk.DISABLED,
                                     highlightbackground="#30363d", highlightthickness=1)
            self.log_text.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
            for tag, color in [("log_err", "#f85149"), ("log_ok", "#3fb950"),
                                ("log_info", "#58a6ff")]:
                self.log_text.tag_config(tag, foreground=color)

            chat_frame = tk.Frame(self.root, bg="#0d1117")
            chat_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 0))
            self.chat_area = scrolledtext.ScrolledText(
                chat_frame, wrap=tk.WORD, font=self.msg_font,
                bg="#161b22", fg="#c9d1d9", insertbackground="#c9d1d9",
                relief=tk.FLAT, padx=12, pady=8, spacing1=2, spacing3=2,
                state=tk.DISABLED, highlightbackground="#30363d", highlightthickness=1)
            self.chat_area.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
            for tag, color in [("user_name", "#58a6ff"), ("ai_name", "#3fb950"),
                                ("user_msg", "#e6edf3"), ("ai_msg", "#c9d1d9"),
                                ("system", "#484f58"), ("image_label", "#8b949e")]:
                self.chat_area.tag_config(tag, foreground=color)
            self.chat_area.tag_config("user_name", font=self.title_font)
            self.chat_area.tag_config("ai_name", font=self.title_font)
            self.chat_area.tag_config("system", justify="center", font=self.mono_font)

            input_frame = tk.Frame(self.root, bg="#161b22", height=70)
            input_frame.pack(fill=tk.X, padx=10, pady=10)
            input_frame.pack_propagate(False)
            self.input_box = tk.Text(input_frame, font=self.input_font, height=2,
                bg="#0d1117", fg="#c9d1d9", insertbackground="#c9d1d9",
                relief=tk.FLAT, padx=10, pady=8, wrap=tk.WORD,
                selectbackground="#264f78",
                highlightbackground="#30363d", highlightthickness=1)
            self.input_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))
            self.input_box.bind("<Return>", self._on_enter)
            self.send_btn = tk.Button(input_frame, text="发送",
                font=("Microsoft YaHei", 11, "bold"),
                bg="#238636", fg="white", relief=tk.FLAT, width=6,
                activebackground="#2ea043", cursor="hand2", command=self._send)
            self.send_btn.pack(side=tk.RIGHT, fill=tk.Y)

            self.status_var = tk.StringVar(value="就绪")
            tk.Label(self.root, textvariable=self.status_var, bg="#161b22",
                     fg="#484f58", font=("Consolas", 9), anchor=tk.W,
                     padx=10).pack(fill=tk.X, side=tk.BOTTOM)

        def _toggle_logs(self):
            self.show_logs = not self.show_logs
            if self.show_logs:
                self.log_btn.configure(bg="#30363d", fg="#e6edf3")
                self.log_frame.pack(fill=tk.X, padx=10, pady=(0, 0),
                                    before=self.root.winfo_children()[3])
            else:
                self.log_btn.configure(bg="#21262d", fg="#8b949e")
                self.log_frame.pack_forget()
            self.log_text.see(tk.END)

        def _on_enter(self, event):
            if not event.state & 0x1:
                self._send()
                return "break"

        def _send(self):
            if self.is_sending:
                return
            msg = self.input_box.get("1.0", tk.END).strip()
            if not msg:
                return
            self.input_box.delete("1.0", tk.END)
            self.is_sending = True
            self.send_btn.configure(state=tk.DISABLED, bg="#21262d")
            self._add_message("你", msg, "user")
            self.status_var.set("⏳ 思考中...")
            label = self.target_device_var.get()
            target_device_id = self._device_map.get(label) if label != "自动" else None
            threading.Thread(target=self._get_reply,
                             args=(msg, target_device_id), daemon=True).start()

        def _get_reply(self, msg, target=None):
            reply, images, hist_len = send_message(msg, target_device_id=target)
            self.root.after(0, lambda: self._on_reply(reply, images, hist_len))

        def _on_reply(self, reply, images, hist_len):
            self._add_message("Hermes", reply, "ai", images=images)
            self.status_var.set(f"就绪 | 历史: {hist_len}")
            self.is_sending = False
            self.send_btn.configure(state=tk.NORMAL, bg="#238636")

        def _clear_chat(self):
            if clear_history():
                self.chat_area.configure(state=tk.NORMAL)
                self.chat_area.delete("1.0", tk.END)
                self.chat_area.configure(state=tk.DISABLED)
                self.photo_refs.clear()
                self._add_system("对话已清除")

        def _add_message(self, sender, text, role, images=None):
            self.chat_area.configure(state=tk.NORMAL)
            if self.chat_area.get("1.0", tk.END).strip():
                self.chat_area.insert(tk.END, "\n")
            prefix = "🧑 " if role == "user" else "🤖 "
            self.chat_area.insert(tk.END, f"{prefix}{sender}\n", f"{role}_name")
            if text:
                self.chat_area.insert(tk.END, f"{text}\n", f"{role}_msg")
            if images and HAS_PIL:
                for url in images:
                    self.chat_area.insert(tk.END, "📷 加载中...\n", "image_label")
                    threading.Thread(target=self._load_img,
                                     args=(url,), daemon=True).start()
            elif images:
                for url in images:
                    self.chat_area.insert(tk.END, f"📷 {url}\n", "image_label")
            self.chat_area.insert(tk.END, "\n")
            self.chat_area.configure(state=tk.DISABLED)
            self.chat_area.see(tk.END)

        def _load_img(self, url):
            import urllib.request, tempfile
            try:
                resp = urllib.request.urlopen(urllib.request.Request(url), timeout=30)
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp.write(resp.read())
                tmp.close()
                self.root.after(0, lambda: self._show_img(tmp.name))
            except Exception as e:
                self.root.after(0, lambda err=str(e): self._replace_loading(f"📷 [{err}]"))

        def _show_img(self, path):
            try:
                img = Image.open(path)
                ratio = min(480 / img.width, 360 / img.height, 1)
                img = img.resize(
                    (int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self.photo_refs.append(photo)
                self.chat_area.configure(state=tk.NORMAL)
                pos = self.chat_area.search("📷 加载中...", "1.0", tk.END)
                if pos:
                    self.chat_area.delete(pos,
                        f"{pos}+{len('📷 加载中...')}c+1l")
                    self.chat_area.image_create(pos, image=photo)
                    self.chat_area.insert(pos, "\n")
                self.chat_area.configure(state=tk.DISABLED)
                self.chat_area.see(tk.END)
                try:
                    os.unlink(path)
                except:
                    pass
            except Exception as e:
                self._replace_loading(f"📷 [{e}]")

        def _replace_loading(self, text):
            self.chat_area.configure(state=tk.NORMAL)
            pos = self.chat_area.search("📷 加载中...", "1.0", tk.END)
            if pos:
                self.chat_area.delete(pos,
                    f"{pos}+{len('📷 加载中...')}c+1l")
                self.chat_area.insert(pos, text + "\n", "image_label")
            self.chat_area.configure(state=tk.DISABLED)

        def _add_system(self, text):
            self.chat_area.configure(state=tk.NORMAL)
            self.chat_area.insert(tk.END, f"── {text} ──\n\n", "system")
            self.chat_area.configure(state=tk.DISABLED)
            self.chat_area.see(tk.END)

        def _show_update_prompt(self, version):
            self.chat_area.configure(state=tk.NORMAL)
            self.chat_area.insert(tk.END, "\n")
            self.chat_area.insert(tk.END,
                f"🔔 新版本 v{version} 已下载完成，重启生效\n", "system")
            btn = tk.Button(self.chat_area, text="🔄 立即重启",
                font=("Microsoft YaHei", 10, "bold"),
                bg="#238636", fg="white", relief=tk.FLAT, padx=12, pady=4,
                activebackground="#2ea043", cursor="hand2",
                command=self._do_restart)
            self.chat_area.window_create(tk.END, window=btn)
            self.chat_area.insert(tk.END, "\n\n")
            self.chat_area.configure(state=tk.DISABLED)
            self.chat_area.see(tk.END)

        def _do_restart(self):
            """重启应用（退出后由 launcher 重新加载新模块）"""
            import subprocess
            base_dir = get_base_dir()
            exe = sys.executable
            # 启动新实例
            creationflags = 0x00000008 if sys.platform == 'win32' else 0  # DETACHED_PROCESS
            kwargs = {"creationflags": creationflags} if sys.platform == 'win32' else {}
            subprocess.Popen([exe], cwd=base_dir, **kwargs)
            os._exit(0)

        def _poll_agent_status(self):
            if agent_status["connected"]:
                self.agent_dot.configure(fg="#3fb950")
                name = agent_status.get("device_name", "") or \
                       agent_status.get("device_id", "")
                self.agent_label.configure(text=f"Agent: ✅ {name}", fg="#3fb950")
            else:
                self.agent_dot.configure(fg="#f0883e")
                err = agent_status.get("last_error", "")
                if err:
                    self.agent_label.configure(text=f"Agent: ❌ {err}", fg="#f85149")
                else:
                    self.agent_label.configure(text="Agent: 连接中...", fg="#f0883e")

            with devices_lock:
                devices = devices_cache.get("devices", {})
            if devices:
                new_map = {"自动": None}
                device_options = ["自动"]
                for did, info in devices.items():
                    label = f"{info.get('name', did)} ({did[:8]})"
                    new_map[label] = did
                    device_options.append(label)
                if set(new_map.keys()) != set(self._device_map.keys()):
                    self._device_map = new_map
                    self.target_device_combo["values"] = device_options

            while not agent_log_queue.empty():
                try:
                    line = agent_log_queue.get_nowait()
                    self.log_text.configure(state=tk.NORMAL)
                    tag = ("log_err" if "ERR" in line or "Error" in line
                           else "log_ok" if "Connected" in line
                           else "log_info")
                    self.log_text.insert(tk.END, line + "\n", tag)
                    self.log_text.see(tk.END)
                    self.log_text.configure(state=tk.DISABLED)
                except:
                    break

            while not chat_system_queue.empty():
                try:
                    line = chat_system_queue.get_nowait()
                    self._add_system(line)
                except:
                    break

            while not update_queue.empty():
                try:
                    msg = update_queue.get_nowait()
                    status = msg.get("status")
                    if status == "downloading":
                        self._add_system(f"📦 正在下载 v{msg['version']} ...")
                    elif status == "ready":
                        self._pending_update = msg
                        self._show_update_prompt(msg["version"])
                    elif status == "error":
                        self._add_system(f"⚠️ 更新失败: {msg.get('msg','')}")
                except:
                    break

            self.root.after(1000, self._poll_agent_status)

        def _check_health(self):
            import urllib.request
            def _check():
                try:
                    req = urllib.request.Request(
                        CHAT_URL.replace("/chat", "/health"),
                        headers={"X-Api-Key": API_KEY})
                    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
                    self.root.after(0, lambda: self._on_health(
                        data.get("bridge_connected", False),
                        data.get("model", "?")))
                except Exception as e:
                    self.root.after(0, lambda err=str(e): self._on_health(False, err))
            threading.Thread(target=_check, daemon=True).start()

        def _on_health(self, ok, model):
            pil = "✅" if HAS_PIL else "❌"
            bridge = "✅" if ok else "❌"
            self._add_system(f"v{VERSION} | 模型: {model} | Bridge: {bridge} | 图片: {pil}")

    root = tk.Tk()
    app = ChatApp(root)
    root.mainloop()
