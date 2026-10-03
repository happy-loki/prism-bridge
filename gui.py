#!/usr/bin/env python3
"""Desktop window for the bridge: sign in, start and stop the local service, read its log.

The window never drives Playwright itself. Every action re-invokes this program with a bridge
subcommand (`serve`, `login`) as a child process and shows its output, so the same file runs as a
script and as a packaged executable:

    gui.py                 open the window
    gui.py serve|login|... same as bridge.py, plus `install-browser`
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import bridge as core

APP_TITLE = "Prism Bridge"
AUTHOR_NOTE = "作者 QQ 群 608041120"
SETTINGS_FILE = core.PROFILE_DIR / "gui.json"
# Set for the window's children: they exit when their stdin closes.
CHILD_ENV = "PRISM_GUI_CHILD"
PACKAGED = bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
POLL_MS = 150
PROBE_EVERY_SEC = 3.0
# How long a child gets to leave on its own before its process tree is killed.
STOP_GRACE_SEC = 10.0
LOG_KEEP_LINES = 3000
# Playwright's wording when its Chromium build has not been downloaded.
MISSING_BROWSER = "Executable doesn't exist"

GREEN = "#1a7f37"
AMBER = "#b26a00"
RED = "#c62828"
GRAY = "#8a8a8a"
MUTED = "#666666"


# ---------------------------------------------------------------------------
# Child side: `gui.py <subcommand>`
# ---------------------------------------------------------------------------

def _leave_with_window() -> None:
    """The window keeps our stdin open; EOF means it asked us to stop, or it is gone."""
    try:
        fd = sys.stdin.fileno()
    except Exception:
        return
    try:
        # Raw reads: a thread parked inside sys.stdin's buffer aborts the interpreter when the
        # command finishes on its own ("could not acquire lock for <stdin> at interpreter shutdown").
        while os.read(fd, 4096):
            pass
    except OSError:
        pass
    worker = core.WORKER
    if worker is not None:
        # Ends the Playwright thread's loop so Chromium closes and releases the profile.
        worker.q.put(None)
        for t in threading.enumerate():
            if t.name == "prism-pw":
                t.join(timeout=5)
    os._exit(0)


def install_browser() -> int:
    """Download Playwright's Chromium with the driver shipped inside the playwright package."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    driver = compute_driver_executable()
    cmd = [str(c) for c in driver] if isinstance(driver, (tuple, list)) else [str(driver)]
    return subprocess.call([*cmd, "install", "chromium"], env=get_driver_env())


def run_core() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            # The window reads the pipe as UTF-8, line by line; a pipe defaults to the ANSI code page.
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    if os.environ.get(CHILD_ENV):
        threading.Thread(target=_leave_with_window, name="gui-stdin", daemon=True).start()
    if sys.argv[1] == "install-browser":
        sys.exit(install_browser())
    core.main()


# ---------------------------------------------------------------------------
# Window side
# ---------------------------------------------------------------------------

def self_command(*args: str) -> list[str]:
    if PACKAGED:
        return [os.path.abspath(sys.argv[0]), *args]
    exe = Path(sys.executable)
    # pythonw starts without console streams; the console build next to it has them.
    console = exe.with_name("python.exe")
    return [str(console if console.exists() else exe), str(Path(__file__).resolve()), *args]


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def parse_port(text: str) -> int | None:
    try:
        port = int(text.strip())
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def probe(opener, port: int, ask_health: bool) -> dict | None:
    """None: nothing listens on the port. Otherwise the bridge's /health body ({} when not asked)."""
    if not core.is_port_listening("127.0.0.1", port):
        return None
    if not ask_health:
        return {}
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # A degraded bridge answers 503 with the reason in the same JSON body.
        try:
            return json.loads(e.read())
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def reap(proc: subprocess.Popen) -> None:
    """Give a child that was asked to stop time to do so, then take its process tree down."""
    try:
        proc.wait(timeout=STOP_GRACE_SEC)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            # /T also ends the Playwright driver and Chromium, which hold the profile lock.
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=NO_WINDOW,
            )
        else:
            proc.kill()


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue = queue.Queue()
        # "serve" / "login" / "install" -> the running child of that kind.
        self.children: dict[str, subprocess.Popen] = {}
        # Last probe of the port: None = nothing listens, dict = /health body.
        self.health: dict | None = None
        self.stopping = False
        self.serve_failed = False
        # Login shares the browser profile with the service: stop it first, start it again after.
        self.login_pending = False
        self.resume_serve = False
        self.closing = False
        self.port = parse_port(str(load_settings().get("port", ""))) or core.PORT
        self._build()
        self._refresh()
        threading.Thread(target=self._watch, name="gui-probe", daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_MS, self._drain)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.title(APP_TITLE)
        scale = root.winfo_fpixels("1i") / 96
        root.geometry(f"{int(760 * scale)}x{int(580 * scale)}")
        root.minsize(int(620 * scale), int(440 * scale))

        account = ttk.LabelFrame(root, text="账号", padding=10)
        account.pack(fill="x", padx=12, pady=(10, 0))
        account.columnconfigure(1, weight=1)
        self.account_dot = ttk.Label(account, text="●")
        self.account_dot.grid(row=0, column=0, sticky="w")
        self.account_text = ttk.Label(account)
        self.account_text.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.account_detail = ttk.Label(account, foreground=MUTED)
        self.account_detail.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        self.login_btn = ttk.Button(account, width=12, command=self._on_login)
        self.login_btn.grid(row=0, column=2, rowspan=2, sticky="e")

        service = ttk.LabelFrame(root, text="服务", padding=10)
        service.pack(fill="x", padx=12, pady=(10, 0))
        service.columnconfigure(2, weight=1)
        self.service_dot = ttk.Label(service, text="●")
        self.service_dot.grid(row=0, column=0, sticky="w")
        self.service_text = ttk.Label(service)
        self.service_text.grid(row=0, column=1, columnspan=2, sticky="w", padx=(6, 0))
        self.start_btn = ttk.Button(service, width=12, command=self._on_start)
        self.start_btn.grid(row=0, column=3, sticky="e")

        ttk.Label(service, text="接口地址", foreground=MUTED).grid(row=1, column=1, sticky="w", padx=(6, 8), pady=(8, 0))
        self.url_var = tk.StringVar()
        ttk.Entry(service, textvariable=self.url_var, state="readonly").grid(row=1, column=2, sticky="ew", pady=(8, 0))
        self.copy_btn = ttk.Button(service, text="复制", width=12, command=self._on_copy)
        self.copy_btn.grid(row=1, column=3, sticky="e", padx=(8, 0), pady=(8, 0))

        ttk.Label(service, text="端口", foreground=MUTED).grid(row=2, column=1, sticky="w", padx=(6, 8), pady=(8, 0))
        port_row = ttk.Frame(service)
        port_row.grid(row=2, column=2, columnspan=2, sticky="w", pady=(8, 0))
        self.port_var = tk.StringVar(value=str(self.port))
        self.port_var.trace_add("write", self._port_edited)
        self.port_entry = ttk.Entry(port_row, textvariable=self.port_var, width=8)
        self.port_entry.pack(side="left")
        ttk.Label(
            port_row,
            text="把接口地址填到客户端的 Base URL，API Key 填任意非空值。",
            foreground=MUTED,
        ).pack(side="left", padx=(10, 0))

        # Shown only after a launch failed because Playwright's Chromium is not on this machine.
        self.notice = ttk.Frame(root)
        ttk.Label(self.notice, text="这台电脑上还没有桥要用的 Chromium 浏览器内核。", foreground=RED).pack(side="left", padx=(11, 0))
        self.install_btn = ttk.Button(self.notice, width=12, command=self._on_install)
        self.install_btn.pack(side="right", padx=(0, 11))

        self.log_frame = ttk.LabelFrame(root, text="日志", padding=6)
        self.log_frame.pack(fill="both", expand=True, padx=12, pady=(10, 0))
        scroll = ttk.Scrollbar(self.log_frame)
        scroll.pack(side="right", fill="y")
        self.log = tk.Text(
            self.log_frame,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            relief="flat",
            yscrollcommand=scroll.set,
        )
        self.log.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.log.yview)

        ttk.Label(root, text=AUTHOR_NOTE, foreground=MUTED).pack(anchor="e", padx=12, pady=(4, 8))

    # -- state -> widgets --------------------------------------------------

    def _alive(self, kind: str) -> bool:
        proc = self.children.get(kind)
        return proc is not None and proc.poll() is None

    def _refresh(self) -> None:
        self._refresh_account()
        self._refresh_service()
        self.install_btn.config(
            text="下载中…" if self._alive("install") else "下载浏览器",
            state="disabled" if self._alive("install") else "normal",
        )

    def _refresh_account(self) -> None:
        if self._alive("login"):
            self.account_dot.config(foreground=AMBER)
            self.account_text.config(text="等待在浏览器里完成登录…")
            self.account_detail.config(text="登录成功后浏览器会自动关闭，最多等待 5 分钟。")
            self.login_btn.config(text="取消登录")
            return
        cookie = core.load_cookie()
        if not cookie:
            color, text = GRAY, "未登录"
            detail = "点击“登录”，在弹出的浏览器里登录你的 OpenAI 账号。"
        else:
            claims = core.get_token_claims(cookie)
            exp = core.token_expiry(cookie)
            detail = f"用户 {claims.get('user_id') or '未知'}　方案 {claims.get('plan') or 'default'}"
            if exp:
                detail += "　有效期至 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
            if exp and exp <= time.time():
                color, text = RED, "登录已过期"
            else:
                color, text = GREEN, "已登录"
                if exp:
                    detail += f"（剩余 {(exp - time.time()) / 3600:.1f} 小时）"
        self.account_dot.config(foreground=color)
        self.account_text.config(text=text)
        self.account_detail.config(text=detail)
        self.login_btn.config(text="重新登录" if cookie else "登录")

    def _refresh_service(self) -> None:
        ours = self._alive("serve")
        if ours and self.stopping:
            color, text = AMBER, "正在停止…"
        elif ours and self.health is None:
            color, text = AMBER, "启动中…（唤醒浏览器和 Prism 工作区，约 15–25 秒）"
        elif ours and self.health.get("ok"):
            color, text = GREEN, "运行中"
        elif ours:
            color, text = RED, "异常：" + str(self.health.get("error") or "Prism 工作区未就绪")
        elif self.health is not None:
            color, text = GREEN, "已在运行（由其他窗口启动，请在那个窗口里停止）"
        elif self.serve_failed:
            color, text = RED, "已停止：服务异常退出，原因见下方日志"
        else:
            color, text = GRAY, "未启动"
        self.service_dot.config(foreground=color)
        self.service_text.config(text=text)
        self.url_var.set(f"http://127.0.0.1:{self.port}/v1")
        idle = not ours and self.health is None
        self.start_btn.config(
            text="停止服务" if ours else "启动服务",
            state="normal" if (ours and not self.stopping) or idle else "disabled",
        )
        self.port_entry.config(state="normal" if idle else "disabled")

    def _append(self, line: str) -> None:
        box = self.log
        at_end = box.yview()[1] >= 0.999
        box.config(state="normal")
        box.insert("end", line + "\n")
        extra = int(box.index("end-1c").split(".")[0]) - LOG_KEEP_LINES
        if extra > 0:
            box.delete("1.0", f"{extra + 1}.0")
        box.config(state="disabled")
        if at_end:
            box.see("end")

    # -- children ----------------------------------------------------------

    def _spawn(self, kind: str, *args: str) -> None:
        try:
            proc = subprocess.Popen(
                self_command(*args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=dict(os.environ, **{CHILD_ENV: "1"}),
                creationflags=NO_WINDOW,
            )
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"无法启动子进程：{e}")
            return
        self.children[kind] = proc
        threading.Thread(target=self._pump, args=(kind, proc), name=f"gui-{kind}", daemon=True).start()
        self._refresh()

    def _pump(self, kind: str, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.events.put(("log", kind, line.rstrip("\r\n")))
        self.events.put(("exit", kind, proc, proc.wait()))

    def _stop(self, kind: str) -> None:
        proc = self.children.get(kind)
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        threading.Thread(target=reap, args=(proc,), name="gui-reap", daemon=True).start()

    def _watch(self) -> None:
        # A system proxy must not be asked for 127.0.0.1.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while True:
            ours, port = self._alive("serve"), self.port
            # /health is only asked of our own child: every call prints a line in the bridge's log.
            self.events.put(("probe", ours, port, probe(opener, port, ours)))
            time.sleep(PROBE_EVERY_SEC)

    def _drain(self) -> None:
        changed = False
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "log":
                    self._on_log(ev[1], ev[2])
                elif ev[0] == "exit":
                    self._on_exit(ev[1], ev[2], ev[3])
                    changed = True
                elif ev[0] == "probe":
                    # Drop a probe taken before the service started, stopped or changed port.
                    if ev[1] == self._alive("serve") and ev[2] == self.port:
                        self.health = ev[3]
                        changed = True
        except queue.Empty:
            pass
        if self.closing:
            return
        if changed:
            self._refresh()
        self.root.after(POLL_MS, self._drain)

    def _on_log(self, kind: str, line: str) -> None:
        if line.startswith("[http] GET /health"):
            return
        if MISSING_BROWSER in line:
            self.notice.pack(fill="x", padx=12, pady=(10, 0), before=self.log_frame)
        self._append(line)

    def _on_exit(self, kind: str, proc: subprocess.Popen, code: int) -> None:
        if self.children.get(kind) is proc:
            del self.children[kind]
        if kind == "serve":
            self.health = None
            self.serve_failed = code != 0 and not self.stopping
            self.stopping = False
            self._append(f"[界面] 服务已停止（退出码 {code}）")
            if self.login_pending:
                self.login_pending = False
                self._spawn("login", "login")
        elif kind == "login":
            if self.resume_serve:
                self.resume_serve = False
                if core.load_cookie():
                    self._start_serve()
        elif kind == "install":
            if code == 0:
                self.notice.pack_forget()
                self._append("[界面] 浏览器内核已就绪，可以登录或启动服务了。")
            else:
                self._append(f"[界面] 浏览器内核下载失败（退出码 {code}），请检查网络后重试。")

    # -- buttons -----------------------------------------------------------

    def _busy(self) -> bool:
        if self._alive("install"):
            messagebox.showinfo(APP_TITLE, "浏览器内核还在下载，请等它完成。")
            return True
        return False

    def _on_start(self) -> None:
        if self._alive("serve"):
            self._stop_serve()
        else:
            self._start_serve()

    def _start_serve(self) -> None:
        port = parse_port(self.port_var.get())
        if port is None:
            messagebox.showwarning(APP_TITLE, "端口要填 1–65535 之间的数字。")
            return
        if self._busy():
            return
        if self._alive("login"):
            messagebox.showinfo(APP_TITLE, "登录还没结束。请先在浏览器里完成登录，或点“取消登录”。")
            return
        if not core.load_cookie():
            messagebox.showinfo(APP_TITLE, "还没有登录。请先点击“登录”。")
            return
        if core.is_port_listening("127.0.0.1", port):
            messagebox.showwarning(APP_TITLE, f"端口 {port} 已被占用。\n桥可能已经在另一个窗口运行；否则请换一个端口。")
            return
        self.port = port
        save_settings({"port": port})
        self.serve_failed = False
        self.health = None
        self._spawn("serve", "serve", "--port", str(port))

    def _stop_serve(self) -> None:
        self.stopping = True
        self._stop("serve")
        self._refresh()

    def _on_login(self) -> None:
        if self._alive("login"):
            self._stop("login")
            return
        if self._busy():
            return
        if self._alive("serve"):
            if not messagebox.askokcancel(
                APP_TITLE,
                "登录和服务共用同一份浏览器数据，需要先停止服务。\n登录结束后会自动重新启动。",
            ):
                return
            self.login_pending = True
            self.resume_serve = True
            self._stop_serve()
            return
        if self.health is not None:
            messagebox.showwarning(APP_TITLE, "桥正在另一个窗口运行，占用着浏览器数据。\n请先关闭那个窗口，再登录。")
            return
        self._spawn("login", "login")

    def _on_install(self) -> None:
        if not self._alive("install"):
            self._append("[界面] 开始下载 Chromium 浏览器内核（约几百 MB，只需一次）…")
            self._spawn("install", "install-browser")

    def _on_copy(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url_var.get())
        self.copy_btn.config(text="已复制")
        self.root.after(1200, lambda: self.copy_btn.config(text="复制"))

    def _port_edited(self, *_) -> None:
        port = parse_port(self.port_var.get())
        if port and port != self.port and not self._alive("serve"):
            self.port = port
            self.health = None
            self._refresh_service()

    def _on_close(self) -> None:
        if self._alive("serve") and not messagebox.askokcancel(APP_TITLE, "关闭窗口会同时停止服务。确定退出吗？"):
            return
        self.closing = True
        for kind in list(self.children):
            self._stop(kind)
        self.root.withdraw()
        self._finish_close()

    def _finish_close(self) -> None:
        if any(p.poll() is None for p in self.children.values()):
            self.root.after(100, self._finish_close)
            return
        self.root.destroy()


def make_root() -> tk.Tk:
    if os.name == "nt":
        try:
            import ctypes

            # Without this Windows bitmap-stretches the window on scaled displays.
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    if os.name == "nt":
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=9)
    return root


def run_gui() -> None:
    root = make_root()
    App(root)
    root.mainloop()


def main() -> None:
    if len(sys.argv) > 1:
        run_core()
    else:
        run_gui()


if __name__ == "__main__":
    main()
