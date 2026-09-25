"""
Bump Scheduler Pro — GUI
========================
Non-freezing architecture:

  Main thread  → only reads from _acct_cache and _stat_cache
                  (two plain Python lists, no locks)
  BG thread    → fetches store.list() + sched.status() every 2s,
                  atomically swaps the caches
  All writes   → store.upsert / store.delete / sched.start go to
                  daemon threads — main thread never waits

  Error log    → logs/error.log   (auto-captured via install_exception_hook)
  View errors  → "⚠ View Errors" button always visible in toolbar
"""
from __future__ import annotations

import os
import sys
import time
import threading
import traceback
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Dict, List, Optional
import uuid

from app.accounts.models import Account, ServerTarget
from app.accounts.store import AccountStore
from app.scheduler.auto_scheduler import AutoScheduler
from app.services.credentials import store_credential, get_credential
from app.services.logging_service import get_logger
from app.config.settings import LOG_DIR

log = get_logger(__name__)

# ── Palette ───────────────────────────────────────────────────────────────────
BG      = "#0f1117"
PANEL   = "#1e2130"
CARD    = "#252838"
BORDER  = "#2d3148"
ACCENT  = "#4f8ef7"
GREEN   = "#00c853"
RED     = "#ff4444"
YELLOW  = "#ffaa00"
CYAN    = "#00bcd4"
TEXT    = "#e8eaf6"
TEXT2   = "#9095b0"
TEXT3   = "#5a5f7a"
WHITE   = "#ffffff"
SEL     = "#1a2040"

F_TITLE = ("Segoe UI", 13, "bold")
F_H1    = ("Segoe UI", 11, "bold")
F_H2    = ("Segoe UI", 10, "bold")
F_BODY  = ("Segoe UI", 10)
F_SMALL = ("Segoe UI", 9)
F_MONO  = ("Consolas", 9)

STATUS_C = {"stopped":TEXT3,"waiting":CYAN,"running":GREEN,"paused":YELLOW,"error":RED}
STATUS_D = {"stopped":"○","waiting":"◔","running":"●","paused":"◑","error":"✕"}

def _ts(t): return t[11:19] if t and len(t)>=19 else (t or "—")

def _cd(iso):
    if not iso: return "—"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso.replace("Z","+00:00"))
        d  = (dt-datetime.now(timezone.utc)).total_seconds()
        if d<=0: return "Ready"
        h,r=divmod(int(d),3600); m,s=divmod(r,60)
        return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"
    except: return "—"

def _btn(parent,text,command,bg=ACCENT,fg=WHITE,padx=10,pady=5,font=None,**kw):
    return tk.Button(parent,text=text,command=command,bg=bg,fg=fg,
                     activebackground=CARD,activeforeground=WHITE,
                     relief="flat",font=font or F_SMALL,
                     cursor="hand2",padx=padx,pady=pady,**kw)

def _section(parent,label):
    f=tk.Frame(parent,bg=BG); f.pack(fill="x",padx=24,pady=(14,2))
    tk.Label(f,text=label,font=F_H2,fg=ACCENT,bg=BG).pack(side="left")
    tk.Frame(f,bg=BORDER,height=1).pack(side="left",fill="x",expand=True,padx=(8,0))

def _field(parent,label,default="",hint="",show=None) -> tk.Entry:
    tk.Label(parent,text=label,font=F_SMALL,fg=TEXT2,bg=BG).pack(anchor="w",padx=24,pady=(6,0))
    e=tk.Entry(parent,show=show or "",bg=CARD,fg=TEXT,insertbackground=TEXT,
               relief="flat",font=F_BODY,highlightthickness=1,
               highlightbackground=BORDER,highlightcolor=ACCENT)
    e.pack(fill="x",padx=24,ipady=7,pady=(2,0))
    if default: e.insert(0,str(default))
    if hint: tk.Label(parent,text=f"  {hint}",font=("Segoe UI",8),
                      fg=TEXT3,bg=BG).pack(anchor="w",padx=24,pady=(1,0))
    return e

def _toast(root,msg,color=GREEN,ms=2500):
    try:
        t=tk.Toplevel(root); t.overrideredirect(True); t.configure(bg=color)
        t.attributes("-topmost",True)
        tk.Label(t,text=f"  {msg}  ",font=F_H2,fg=WHITE,bg=color,
                 padx=16,pady=10).pack()
        root.update_idletasks()
        x=root.winfo_x()+root.winfo_width()//2-200
        y=root.winfo_y()+70
        t.geometry(f"+{x}+{y}")
        root.after(ms,lambda: t.destroy() if t.winfo_exists() else None)
    except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# ERROR LOG VIEWER
# ─────────────────────────────────────────────────────────────────────────────

class ErrorLogWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Error Log")
        self.geometry("900x600"); self.configure(bg=BG); self.grab_set()

        hdr=tk.Frame(self,bg=PANEL,height=50); hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Frame(hdr,bg=RED,width=4).pack(side="left",fill="y")
        tk.Label(hdr,text="  ⚠  Error Log",font=F_H1,fg=TEXT,bg=PANEL).pack(side="left",padx=10)

        error_log = LOG_DIR / "error.log"
        app_log   = LOG_DIR / "app.log"
        self._log_path = error_log

        # Tab bar
        tabs_f = tk.Frame(hdr, bg=PANEL); tabs_f.pack(side="right", padx=10)
        self._tab = tk.StringVar(value="error")
        for val,label in [("error","error.log"),("app","app.log")]:
            tk.Radiobutton(tabs_f, text=label, variable=self._tab, value=val,
                           font=F_SMALL, fg=TEXT2, bg=PANEL,
                           selectcolor=CARD, activebackground=PANEL,
                           command=self._switch_tab).pack(side="left", padx=4)

        # Buttons
        bar=tk.Frame(self,bg=CARD); bar.pack(fill="x")
        _btn(bar,"↺ Refresh",self._load,bg=CARD,fg=CYAN,padx=10,pady=5).pack(side="left",padx=8,pady=6)
        _btn(bar,"📋 Copy All",self._copy,bg=CARD,fg=TEXT2,padx=10,pady=5).pack(side="left")
        tk.Label(bar,text=f"  Log folder: {LOG_DIR}",
                 font=("Segoe UI",8),fg=TEXT3,bg=CARD).pack(side="left",padx=16)
        _btn(bar,"Open Folder",self._open_folder,bg=CARD,fg=ACCENT,
             padx=8,pady=5).pack(side="left")

        # Text area
        tf=tk.Frame(self,bg=BG); tf.pack(fill="both",expand=True,padx=10,pady=8)
        self._txt=tk.Text(tf,bg="#0a0d14",fg=TEXT,font=F_MONO,
                          relief="flat",wrap="none",state="disabled")
        vsc=ttk.Scrollbar(tf,orient="vertical",command=self._txt.yview)
        hsc=ttk.Scrollbar(tf,orient="horizontal",command=self._txt.xview)
        self._txt.configure(yscrollcommand=vsc.set,xscrollcommand=hsc.set)
        self._txt.tag_configure("CRITICAL",foreground=RED)
        self._txt.tag_configure("ERROR",   foreground="#ff8888")
        self._txt.tag_configure("WARNING", foreground=YELLOW)
        self._txt.tag_configure("INFO",    foreground=TEXT2)
        self._txt.tag_configure("empty",   foreground=TEXT3)
        vsc.pack(side="right",fill="y"); hsc.pack(side="bottom",fill="x")
        self._txt.pack(side="left",fill="both",expand=True)

        self._load()

    def _switch_tab(self):
        val = self._tab.get()
        self._log_path = LOG_DIR / ("error.log" if val=="error" else "app.log")
        self._load()

    def _load(self):
        self._txt.config(state="normal"); self._txt.delete("1.0","end")
        try:
            if self._log_path.is_file():
                content = self._log_path.read_text(encoding="utf-8",errors="replace")
                if not content.strip():
                    self._txt.insert("end","  No errors recorded yet. 🎉\n","empty")
                else:
                    # Colour by level
                    for line in content.splitlines():
                        tag = ("CRITICAL" if " CRITICAL " in line else
                               "ERROR"    if " ERROR "    in line else
                               "WARNING"  if " WARNING "  in line else "INFO")
                        self._txt.insert("end", line+"\n", tag)
                    self._txt.see("end")
            else:
                self._txt.insert("end",f"  Log file not found:\n  {self._log_path}\n\n"
                                      "  This means no errors have occurred yet.\n","empty")
        except Exception as e:
            self._txt.insert("end",f"Could not read log: {e}\n")
        self._txt.config(state="disabled")

    def _copy(self):
        try:
            content = self._log_path.read_text(encoding="utf-8",errors="replace")
            self.clipboard_clear(); self.clipboard_append(content)
            _toast(self,"✓ Log copied to clipboard",GREEN)
        except Exception as e:
            messagebox.showerror("Error",str(e),parent=self)

    def _open_folder(self):
        try:
            if sys.platform=="win32":
                os.startfile(LOG_DIR)
            elif sys.platform=="darwin":
                import subprocess; subprocess.Popen(["open",str(LOG_DIR)])
            else:
                import subprocess; subprocess.Popen(["xdg-open",str(LOG_DIR)])
        except Exception as e:
            messagebox.showinfo("Log folder",str(LOG_DIR),parent=self)


# ─────────────────────────────────────────────────────────────────────────────
# SERVER DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class ServerDialog(tk.Toplevel):
    def __init__(self,parent,server:Optional[ServerTarget]=None,
                 on_save:Optional[Callable]=None):
        super().__init__(parent)
        self._sv     = server or ServerTarget(server_id=str(uuid.uuid4())[:8],name="My Server")
        self._on_save= on_save
        is_edit      = server is not None
        self.title("Edit Server" if is_edit else "Add Server")
        self.configure(bg=BG); self.geometry("460x330")
        self.resizable(False,False); self.grab_set()

        hdr=tk.Frame(self,bg=PANEL,height=48); hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Frame(hdr,bg=ACCENT,width=4).pack(side="left",fill="y")
        tk.Label(hdr,text=f"  {'Edit' if is_edit else 'Add'} Server",
                 font=F_H1,fg=TEXT,bg=PANEL).pack(side="left",padx=10,pady=12)

        # Footer first (side=bottom) — always visible
        tk.Frame(self,bg=BORDER,height=1).pack(fill="x",side="bottom")
        ft=tk.Frame(self,bg=PANEL,height=52); ft.pack(fill="x",side="bottom"); ft.pack_propagate(False)
        _btn(ft,"Cancel",self.destroy,bg=CARD,fg=TEXT2,padx=14,pady=8).pack(side="right",padx=8,pady=10)
        _btn(ft,"💾  Save",self._save,padx=14,pady=8).pack(side="right",padx=4,pady=10)

        b=tk.Frame(self,bg=BG); b.pack(fill="both",expand=True)
        _section(b,"Server Info")
        self._e_name  = _field(b,"Server Name *",self._sv.name)
        self._e_guild = _field(b,"Guild ID  (Server ID) *",self._sv.guild_id,
            hint="Right-click server name → Copy Server ID  •  Need Developer Mode: Settings → Advanced")
        self._e_chan  = _field(b,"Channel ID *",self._sv.channel_id,
            hint="Right-click the bump channel → Copy Channel ID")

    def _save(self):
        name=self._e_name.get().strip()
        guild=self._e_guild.get().strip()
        chan =self._e_chan.get().strip()
        if not name:
            messagebox.showerror("Missing","Server name is required.",parent=self)
            self._e_name.focus_set(); return
        if not guild or not guild.isdigit():
            messagebox.showerror("Invalid",
                f"Guild ID must be a number (got: '{guild}').\n\n"
                "Right-click your server name → Copy Server ID\n"
                "Make sure Developer Mode is ON (Settings → Advanced → Developer Mode)",
                parent=self); self._e_guild.focus_set(); return
        if not chan or not chan.isdigit():
            messagebox.showerror("Invalid",
                f"Channel ID must be a number (got: '{chan}').\n\n"
                "Right-click the bump channel → Copy Channel ID",
                parent=self); self._e_chan.focus_set(); return
        self._sv.name=name; self._sv.guild_id=guild; self._sv.channel_id=chan
        if self._on_save: self._on_save(self._sv)
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# ACCOUNT DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class AccountDialog(tk.Toplevel):
    def __init__(self,parent,account:Optional[Account]=None,
                 on_save:Optional[Callable]=None,prefill_token:str=""):
        super().__init__(parent)
        self._ac      = account or Account(account_id=str(uuid.uuid4())[:8],name="My Account")
        self._on_save = on_save
        self._show_t  = False
        is_edit = account is not None
        self.title("Edit Account" if is_edit else "Add Account")
        self.configure(bg=BG); self.geometry("520x560")
        self.minsize(480,500); self.resizable(True,True); self.grab_set()

        hdr=tk.Frame(self,bg=PANEL,height=48); hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Frame(hdr,bg=ACCENT,width=4).pack(side="left",fill="y")
        tk.Label(hdr,text=f"  {'Edit' if is_edit else 'Add'} Account",
                 font=F_H1,fg=TEXT,bg=PANEL).pack(side="left",padx=10,pady=12)

        # Footer first (always visible)
        tk.Frame(self,bg=BORDER,height=1).pack(fill="x",side="bottom")
        ft=tk.Frame(self,bg=PANEL,height=52); ft.pack(fill="x",side="bottom"); ft.pack_propagate(False)
        _btn(ft,"Cancel",self.destroy,bg=CARD,fg=TEXT2,padx=14,pady=8).pack(side="right",padx=8,pady=10)
        _btn(ft,"💾  Save",self._save,padx=14,pady=8).pack(side="right",padx=4,pady=10)

        # Scrollable body
        wrap=tk.Frame(self,bg=BG); wrap.pack(fill="both",expand=True)
        cv=tk.Canvas(wrap,bg=BG,highlightthickness=0)
        sb=ttk.Scrollbar(wrap,orient="vertical",command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y"); cv.pack(side="left",fill="both",expand=True)
        self._body=tk.Frame(cv,bg=BG)
        win=cv.create_window((0,0),window=self._body,anchor="nw")
        cv.bind("<Configure>",lambda e:cv.itemconfig(win,width=e.width))
        self._body.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        def _wheel(e): cv.yview_scroll(-1 if e.delta>0 else 1,"units")
        cv.bind_all("<MouseWheel>",_wheel)
        self.protocol("WM_DELETE_WINDOW",lambda:(cv.unbind_all("<MouseWheel>"),self.destroy()))

        self._build_form(prefill_token)

    def _build_form(self,prefill=""):
        b=self._body
        _section(b,"Account Name")
        self._e_name=_field(b,"Display name  (just for you)",default=self._ac.name)

        _section(b,"Token Type")
        self._tv=tk.StringVar(value=self._ac.token_type)
        tr=tk.Frame(b,bg=BG); tr.pack(fill="x",padx=24,pady=(6,0))
        for val,label,color in [("user","👤  User Token  (your own account)",CYAN),
                                  ("bot", "🤖  Bot Token   (a bot you created)",GREEN)]:
            tk.Radiobutton(tr,text=label,variable=self._tv,value=val,
                           font=F_BODY,fg=color,bg=BG,selectcolor=CARD,
                           activebackground=BG,activeforeground=color).pack(anchor="w",pady=3)

        _section(b,"Token")
        tok_row=tk.Frame(b,bg=BG); tok_row.pack(fill="x",padx=24,pady=(6,0))
        self._e_tok=tk.Entry(tok_row,show="•",bg=CARD,fg=TEXT,insertbackground=TEXT,
                              relief="flat",font=F_MONO,highlightthickness=1,
                              highlightbackground=BORDER,highlightcolor=ACCENT)
        self._e_tok.pack(side="left",fill="x",expand=True,ipady=7)
        saved=get_credential(self._ac.account_id)
        fill=prefill or saved or ""
        if fill: self._e_tok.insert(0,fill)
        self._b_eye=tk.Button(tok_row,text="👁",command=self._eye,
                               bg=CARD,fg=TEXT2,relief="flat",font=F_SMALL,
                               cursor="hand2",padx=8)
        self._b_eye.pack(side="left",padx=(2,0),ipady=7)
        tk.Label(b,text="  How to get your token:\n"
                        "  User → open discord.com in browser → F12 → Application\n"
                        "         → Local Storage → discord.com → find 'token' row\n"
                        "  Bot  → discord.com/developers/applications → Bot → Reset Token",
                 font=("Segoe UI",8),fg=TEXT3,bg=BG,justify="left").pack(anchor="w",padx=24,pady=(4,0))

        _section(b,"Schedule  (minutes)")
        sr=tk.Frame(b,bg=BG); sr.pack(fill="x",padx=24,pady=(8,0))
        def spin(p,label,val,w=6):
            tk.Label(p,text=label,font=F_SMALL,fg=TEXT2,bg=BG).pack(side="left")
            e=tk.Entry(p,bg=CARD,fg=TEXT,insertbackground=TEXT,relief="flat",
                       font=F_BODY,width=w,highlightthickness=1,
                       highlightbackground=BORDER,highlightcolor=ACCENT)
            e.insert(0,str(int(val))); e.pack(side="left",padx=(4,18),ipady=5)
            return e
        self._e_scd=spin(sr,"Server cooldown", self._ac.server_cooldown_min)
        self._e_acd=spin(sr,"Account cooldown",self._ac.account_cooldown_min)
        self._e_rnd=spin(sr,"Random ±",        self._ac.random_offset_min)
        timing_hint = tk.Label(b,
            text="",  # built dynamically below
            font=("Segoe UI",8),fg=TEXT3,bg=BG,justify="left")
        timing_hint.pack(anchor="w",padx=24,pady=(2,0))

        def _update_hint(*_):
            try:
                scd = int(float(self._e_scd.get() or 120))
                acd = int(float(self._e_acd.get() or 30))
            except ValueError:
                return
            parts = [
                "  With these settings and 3 servers:",
                "  12:00  Server 1 bumped  (next in " + str(scd) + "m)",
                "  12:" + str(acd).zfill(2) + "  Server 2 bumped  (after " + str(acd) + "m gap)",
                "  12:" + str(acd*2).zfill(2) + "  Server 3 bumped  (after " + str(acd) + "m gap)",
                "  NOTE: DISBOARD requires server cooldown >= 120 min.",
            ]
            timing_hint.config(text="\n".join(parts))

        self._e_scd.bind("<KeyRelease>", _update_hint)
        self._e_acd.bind("<KeyRelease>", _update_hint)
        self._e_rnd.bind("<KeyRelease>", _update_hint)
        _update_hint()

        _section(b,"Options")
        self._auto=tk.BooleanVar(value=self._ac.auto_start)
        tk.Checkbutton(b,text="▶  Auto-start when the app opens",
                       variable=self._auto,font=F_BODY,fg=TEXT,bg=BG,
                       selectcolor=CARD,activebackground=BG).pack(anchor="w",padx=24,pady=(8,0))
        tk.Label(b,text="",bg=BG).pack(pady=10)

    def _eye(self):
        self._show_t=not self._show_t
        self._e_tok.config(show="" if self._show_t else "•")
        self._b_eye.config(fg=ACCENT if self._show_t else TEXT2)

    def _save(self):
        name=self._e_name.get().strip()
        if not name:
            messagebox.showerror("Missing","Account name is required.",parent=self)
            self._e_name.focus_set(); return
        tok=self._e_tok.get().strip()
        if not tok:
            messagebox.showerror("Missing Token",
                "Paste your Discord token.\n\n"
                "How:\n"
                "  1. Open discord.com in Chrome/Edge\n"
                "  2. Press F12\n"
                "  3. Click Application tab\n"
                "  4. Left panel: Local Storage → https://discord.com\n"
                "  5. Find the 'token' key → copy its value\n\n"
                "Or click '🔑 Get Token' in the main window.",
                parent=self); self._e_tok.focus_set(); return
        try:
            scd=float(self._e_scd.get() or 120)
            acd=float(self._e_acd.get() or 30)
            rnd=float(self._e_rnd.get() or 0)
        except ValueError:
            messagebox.showerror("Invalid","Cooldown values must be numbers.",parent=self); return
        if scd<=0: messagebox.showerror("Invalid","Server cooldown must be >0.",parent=self); return
        if acd<0:  messagebox.showerror("Invalid","Account cooldown must be ≥0.",parent=self); return
        self._ac.name=name; self._ac.token_type=self._tv.get()
        self._ac.server_cooldown_min=scd; self._ac.account_cooldown_min=acd
        self._ac.random_offset_min=rnd;   self._ac.auto_start=self._auto.get()
        if self._on_save: self._on_save(self._ac,tok)
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# CACHE REFRESHER  (background thread — only thing that touches locks)
# ─────────────────────────────────────────────────────────────────────────────

class _CacheRefresher:
    """
    Runs one daemon thread that every INTERVAL seconds:
      1. Calls store.list()   → updates _accts
      2. Calls sched.status() → updates _status
    Both writes are done atomically via a single lock swap.
    The main thread reads _accts / _status via get() — lock-free snapshot.
    """
    INTERVAL = 2.0   # seconds between refreshes

    def __init__(self, store: AccountStore, sched: AutoScheduler):
        self._store = store
        self._sched = sched
        self._lock  = threading.Lock()
        self._accts: List[Account]  = []
        self._status: List[dict]    = []
        self._errors: List[str]     = []   # recent errors for UI
        self._running = True
        t = threading.Thread(target=self._loop, name="CacheRefresher", daemon=True)
        t.start()

    def get(self) -> tuple[List[Account], Dict[str, dict]]:
        """Return (accounts_list, status_map_by_account_id) — no locks held."""
        with self._lock:
            accts  = list(self._accts)
            smap   = {s["account_id"]: s for s in self._status}
        return accts, smap

    def get_errors(self) -> List[str]:
        with self._lock:
            return list(self._errors)

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            start = time.monotonic()
            try:
                accts  = self._store.list()
                status = self._sched.status()
                with self._lock:
                    self._accts  = accts
                    self._status = status
            except Exception as e:
                err = f"{time.strftime('%H:%M:%S')}  {type(e).__name__}: {e}"
                log.error("CacheRefresher: %s", err)
                with self._lock:
                    self._errors.append(err)
                    if len(self._errors) > 100:
                        self._errors = self._errors[-100:]
            elapsed = time.monotonic() - start
            time.sleep(max(0.1, self.INTERVAL - elapsed))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────────────────────────────────────

class BumpSchedulerGUI:

    def __init__(self, store: AccountStore, sched: AutoScheduler):
        self._store  = store
        self._sched  = sched
        self._sel:   Optional[str] = None

        # Non-blocking cache — main thread ONLY reads this
        self._cache = _CacheRefresher(store, sched)

        self.root = tk.Tk()
        self.root.title("Bump Scheduler Pro")
        self.root.geometry("1100x700")
        self.root.minsize(900,580)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW",self._close)

        self._build()
        self._poll()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        self._build_titlebar()
        tk.Frame(self.root,bg=BORDER,height=1).pack(fill="x")
        body=tk.Frame(self.root,bg=BG); body.pack(fill="both",expand=True)
        self._build_left(body)
        tk.Frame(body,bg=BORDER,width=1).pack(side="left",fill="y")
        self._build_right(body)
        self._build_statusbar()

    def _build_titlebar(self):
        tb=tk.Frame(self.root,bg=PANEL,height=52)
        tb.pack(fill="x"); tb.pack_propagate(False)
        tk.Frame(tb,bg=ACCENT,width=4).pack(side="left",fill="y")
        tk.Label(tb,text="  ⚡  Bump Scheduler Pro",font=F_TITLE,fg=TEXT,bg=PANEL).pack(side="left",padx=8)
        tk.Label(tb,text="  Multi-account · Multi-server · Auto",font=F_SMALL,fg=TEXT3,bg=PANEL).pack(side="left",pady=16)

        r=tk.Frame(tb,bg=PANEL); r.pack(side="right",padx=12)
        self._lbl_sys=tk.Label(r,text="● Ready",font=F_BODY,fg=GREEN,bg=PANEL)
        self._lbl_sys.pack(side="right",padx=10)
        self._lbl_utc=tk.Label(r,text="",font=F_SMALL,fg=TEXT2,bg=PANEL)
        self._lbl_utc.pack(side="right",padx=8)

        # Error log button — always visible
        self._b_errs=tk.Button(r,text="⚠ Errors",command=self._view_errors,
                                bg=CARD,fg=TEXT3,relief="flat",font=F_SMALL,
                                cursor="hand2",padx=8,pady=4)
        self._b_errs.pack(side="right",padx=4,pady=12)

        _btn(r,"🔑 Get Token",self._open_token_extractor,
             bg=CYAN,fg=BG,font=("Segoe UI",9,"bold"),padx=8,pady=4
             ).pack(side="right",padx=6,pady=12)

        self._tick_utc()

    def _tick_utc(self):
        self._lbl_utc.config(text=time.strftime("UTC %Y-%m-%d  %H:%M:%S"))
        self.root.after(1000,self._tick_utc)

    def _build_left(self,parent):
        frame=tk.Frame(parent,bg=PANEL,width=300)
        frame.pack(side="left",fill="y"); frame.pack_propagate(False)
        hdr=tk.Frame(frame,bg=PANEL); hdr.pack(fill="x",padx=12,pady=10)
        tk.Label(hdr,text="Accounts",font=F_H1,fg=TEXT,bg=PANEL).pack(side="left")
        br=tk.Frame(hdr,bg=PANEL); br.pack(side="right")
        _btn(br,"▶ All",self._start_all,bg=GREEN,padx=8,pady=3).pack(side="left",padx=(0,3))
        _btn(br,"⏹ All",self._stop_all, bg=RED,  padx=8,pady=3).pack(side="left",padx=(0,3))
        _btn(br,"+ Add",self._add_acct, padx=8,  pady=3).pack(side="left")
        tk.Frame(frame,bg=BORDER,height=1).pack(fill="x")

        s=ttk.Style(); s.theme_use("clam")
        for st,bg in [("A.Treeview",PANEL),("S.Treeview",CARD)]:
            s.configure(st,background=bg,foreground=TEXT,fieldbackground=bg,
                         rowheight=28,borderwidth=0,font=F_BODY)
            s.configure(st+".Heading",background=CARD,foreground=TEXT2,
                         relief="flat",font=F_SMALL)
            s.map(st,background=[("selected",SEL)],foreground=[("selected",WHITE)])

        tf=tk.Frame(frame,bg=PANEL); tf.pack(fill="both",expand=True)
        self._list=ttk.Treeview(tf,columns=("dot","name","srv","next"),
                                  show="headings",style="A.Treeview",selectmode="browse")
        for col,head,w,anc in [("dot","",26,"center"),("name","Account",138,"w"),
                                ("srv","Srvs",36,"center"),("next","Next",76,"w")]:
            self._list.heading(col,text=head)
            self._list.column(col,width=w,minwidth=20,anchor=anc)
        for st,c in STATUS_C.items(): self._list.tag_configure(st,foreground=c)
        vsb=ttk.Scrollbar(tf,orient="vertical",command=self._list.yview)
        self._list.configure(yscrollcommand=vsb.set)
        self._list.pack(side="left",fill="both",expand=True)
        vsb.pack(side="right",fill="y")
        self._list.bind("<<TreeviewSelect>>",self._on_list_sel)

        self._list_empty=tk.Frame(frame,bg=PANEL)
        tk.Label(self._list_empty,text="⚡",font=("Segoe UI",26),fg=TEXT3,bg=PANEL).pack(pady=(40,4))
        tk.Label(self._list_empty,text="No accounts yet",font=F_H2,fg=TEXT2,bg=PANEL).pack()
        _btn(self._list_empty,"+ Add Account",self._add_acct,padx=14,pady=7).pack(pady=10)
        tk.Label(self._list_empty,text="Need a token?  Click 🔑 Get Token ↑",
                 font=F_SMALL,fg=TEXT3,bg=PANEL,justify="center").pack()

    def _build_right(self,parent):
        frame=tk.Frame(parent,bg=PANEL); frame.pack(side="left",fill="both",expand=True)
        self._right=frame

        self._no_sel=tk.Frame(frame,bg=PANEL)
        self._no_sel.place(relx=0,rely=0,relwidth=1,relheight=1)
        tk.Label(self._no_sel,text="⚡",font=("Segoe UI",38),fg=TEXT3,bg=PANEL).pack(pady=(90,8))
        tk.Label(self._no_sel,text="Select an account",font=F_H1,fg=TEXT2,bg=PANEL).pack()
        tk.Label(self._no_sel,text="or click + Add to create one",font=F_BODY,fg=TEXT3,bg=PANEL).pack(pady=4)

        self._det=tk.Frame(frame,bg=PANEL)
        self._build_detail(self._det)

    def _build_detail(self,p):
        top=tk.Frame(p,bg=PANEL); top.pack(fill="x",padx=14,pady=(12,4))
        self._d_name=tk.Label(top,text="",font=F_H1,fg=TEXT,bg=PANEL); self._d_name.pack(side="left")
        self._d_sub =tk.Label(top,text="",font=F_SMALL,fg=TEXT3,bg=PANEL); self._d_sub.pack(side="left",padx=8)
        ctrl=tk.Frame(top,bg=PANEL); ctrl.pack(side="right")
        self._b_start=_btn(ctrl,"▶ Start", self._start_sel, bg=GREEN,padx=8,pady=4); self._b_start.pack(side="left",padx=(0,3))
        self._b_stop =_btn(ctrl,"⏹ Stop",  self._stop_sel,  bg=RED,  padx=8,pady=4); self._b_stop.pack(side="left",padx=(0,3))
        self._b_pause=_btn(ctrl,"⏸",       self._pause_sel, bg=YELLOW,fg=BG,padx=6,pady=4); self._b_pause.pack(side="left",padx=(0,3))
        _btn(ctrl,"🔑",lambda:self._open_token_extractor(self._sel),bg=CYAN,fg=BG,padx=6,pady=4).pack(side="left",padx=(0,3))
        _btn(ctrl,"✏", self._edit_sel, bg=CARD,fg=TEXT2,padx=6,pady=4).pack(side="left",padx=(0,3))
        _btn(ctrl,"🗑", self._del_sel,  bg=CARD,fg=RED,  padx=6,pady=4).pack(side="left")
        tk.Frame(p,bg=BORDER,height=1).pack(fill="x",padx=14)

        sc=tk.Frame(p,bg=CARD,highlightthickness=1,highlightbackground=BORDER)
        sc.pack(fill="x",padx=14,pady=8)
        def scard(lbl,attr):
            f=tk.Frame(sc,bg=CARD); f.pack(side="left",expand=True,fill="x")
            tk.Label(f,text=lbl,font=F_SMALL,fg=TEXT2,bg=CARD).pack(pady=(8,2))
            lb=tk.Label(f,text="—",font=("Segoe UI",11,"bold"),fg=TEXT,bg=CARD); lb.pack(pady=(0,8))
            setattr(self,attr,lb)
        for i,(lbl,attr) in enumerate([("Status","_ds_st"),("Next Run","_ds_next"),
                                        ("Last Run","_ds_last"),("OK / ERR","_ds_runs"),
                                        ("Success","_ds_pct")]):
            scard(lbl,attr)
            if i<4: tk.Frame(sc,bg=BORDER,width=1).pack(side="left",fill="y",pady=6)

        sh=tk.Frame(p,bg=PANEL); sh.pack(fill="x",padx=14,pady=(6,2))
        tk.Label(sh,text="Servers",font=F_H2,fg=TEXT,bg=PANEL).pack(side="left")
        _btn(sh,"+ Add Server",self._add_server,padx=8,pady=3).pack(side="right")

        sf=tk.Frame(p,bg=CARD,highlightthickness=1,highlightbackground=BORDER)
        sf.pack(fill="x",padx=14)
        self._srvt=ttk.Treeview(sf,columns=("en","name","guild","next","ok"),
                                  show="headings",style="S.Treeview",height=5)
        for col,head,w,anc in [("en","",26,"center"),("name","Server",160,"w"),
                                ("guild","Guild ID",125,"w"),("next","Next",100,"w"),("ok","OK/ERR",65,"center")]:
            self._srvt.heading(col,text=head)
            self._srvt.column(col,width=w,minwidth=20,anchor=anc)
        self._srvt.tag_configure("ok",foreground=GREEN)
        self._srvt.tag_configure("err",foreground=RED)
        self._srvt.tag_configure("dis",foreground=TEXT3)
        v2=ttk.Scrollbar(sf,orient="vertical",command=self._srvt.yview)
        self._srvt.configure(yscrollcommand=v2.set)
        self._srvt.pack(side="left",fill="x",expand=True)
        v2.pack(side="right",fill="y")
        self._srvt.bind("<Double-1>",self._edit_srv_dbl)
        self._srvt.bind("<Button-3>",self._srv_ctx)

        tk.Frame(p,bg=BORDER,height=1).pack(fill="x",padx=14,pady=(8,0))
        lh=tk.Frame(p,bg=PANEL); lh.pack(fill="x",padx=14,pady=(4,2))
        tk.Label(lh,text="Activity",font=F_H2,fg=TEXT,bg=PANEL).pack(side="left")
        tk.Button(lh,text="Clear",command=self._clr_log,bg=PANEL,fg=TEXT3,
                  relief="flat",font=F_SMALL,cursor="hand2").pack(side="right")
        self._log=tk.Text(p,bg="#0d1020",fg=TEXT,font=F_MONO,relief="flat",
                           wrap="word",state="disabled",height=7)
        v3=ttk.Scrollbar(p,command=self._log.yview)
        self._log.configure(yscrollcommand=v3.set)
        for tag,col in [("SUCCESS",GREEN),("ERROR",RED),("WARN",YELLOW),("INFO",CYAN),("TS",TEXT3)]:
            self._log.tag_configure(tag,foreground=col)
        self._log.pack(side="left",fill="both",expand=True,padx=(14,0),pady=(0,10))
        v3.pack(side="right",fill="y",pady=(0,10))

    def _build_statusbar(self):
        bar=tk.Frame(self.root,bg=PANEL,height=34)
        bar.pack(fill="x",side="bottom"); bar.pack_propagate(False)
        tk.Frame(self.root,bg=BORDER,height=1).pack(fill="x",side="bottom")
        self._lbl_bar=tk.Label(bar,text="",font=F_SMALL,fg=TEXT2,bg=PANEL)
        self._lbl_bar.pack(side="left",padx=14)
        # Error log path hint
        self._lbl_logpath=tk.Label(bar,
            text=f"Error log: {LOG_DIR / 'error.log'}",
            font=("Segoe UI",8),fg=TEXT3,bg=PANEL,cursor="hand2")
        self._lbl_logpath.pack(side="right",padx=14)
        self._lbl_logpath.bind("<Button-1>",lambda e:self._view_errors())

    # ── Account actions ───────────────────────────────────────────────────────

    def _add_acct(self):
        AccountDialog(self.root,on_save=self._on_acct_saved)

    def _on_acct_saved(self,acct:Account,token:str):
        def _bg():
            try:
                store_credential(acct.account_id,token)
                self._store.upsert(acct)
                if acct.auto_start:
                    time.sleep(0.5)
                    self._sched.start_account(acct.account_id)
            except Exception as e:
                log.error("Save account: %s",e)
                self.root.after(0,messagebox.showerror,"Error",str(e))
        threading.Thread(target=_bg,daemon=True).start()
        self.root.after(0,lambda:self._select_after_save(acct.account_id,acct.name))

    def _select_after_save(self,aid:str,name:str):
        _toast(self.root,f"✓ '{name}' saved")
        self._log_line("INFO",f"Account '{name}' saved")
        # Wait for cache to refresh then select
        self.root.after(2500,lambda:self._try_select(aid))

    def _try_select(self,aid:str):
        if self._list.exists(aid):
            self._list.selection_set(aid)
            self._on_list_sel()
        else:
            self.root.after(1000,lambda:self._try_select(aid))

    def _edit_sel(self):
        if not self._sel: return
        accts,_ = self._cache.get()
        acct = next((a for a in accts if a.account_id==self._sel),None)
        if acct: AccountDialog(self.root,account=acct,on_save=self._on_acct_saved)

    def _del_sel(self):
        if not self._sel: return
        accts,_=self._cache.get()
        acct=next((a for a in accts if a.account_id==self._sel),None)
        if not acct: return
        if not messagebox.askyesno("Remove",
                f"Remove '{acct.name}' and all its servers?\nCannot be undone."): return
        aid=self._sel
        def _bg():
            try:
                self._sched.remove_account(aid)
                self._store.delete(aid)
            except Exception as e:
                log.error("Delete account: %s",e)
        threading.Thread(target=_bg,daemon=True).start()
        self._sel=None
        self._no_sel.place(relx=0,rely=0,relwidth=1,relheight=1)
        self._det.place_forget()
        _toast(self.root,f"Removed '{acct.name}'",color=YELLOW)

    def _start_sel(self):
        if not self._sel: return
        accts,_=self._cache.get()
        acct=next((a for a in accts if a.account_id==self._sel),None)
        if not acct: return
        tok=get_credential(acct.account_id)
        if not tok:
            messagebox.showerror("No Token",
                f"No token for '{acct.name}'.\n\nClick ✏ Edit → paste token.\nOr click 🔑 Get Token.")
            return
        if not acct.servers:
            messagebox.showerror("No Servers",
                f"'{acct.name}' has no servers.\n\nClick '+ Add Server' first.")
            return
        def _bg():
            try:
                self._sched.start_account(acct.account_id)
                self.root.after(0,self._log_line,"SUCCESS",f"Started: {acct.name}")
            except Exception as e:
                log.error("Start account: %s",e)
                self.root.after(0,messagebox.showerror,"Error",str(e))
        threading.Thread(target=_bg,daemon=True).start()

    def _stop_sel(self):
        if not self._sel: return
        threading.Thread(target=self._sched.stop_account,
                         args=(self._sel,),daemon=True).start()
        self._log_line("WARN","Stop requested")

    def _pause_sel(self):
        if not self._sel: return
        threading.Thread(target=self._sched.pause_account,
                         args=(self._sel,),daemon=True).start()
        self._log_line("WARN","Pause requested")

    def _start_all(self):
        accts,_=self._cache.get()
        if not accts:
            _toast(self.root,"No accounts yet — click + Add first",color=YELLOW); return
        threading.Thread(target=lambda:self._sched.start(auto_only=False),daemon=True).start()
        self._log_line("INFO","Starting all accounts…")

    def _stop_all(self):
        threading.Thread(target=self._sched.stop_all,daemon=True).start()
        self._log_line("WARN","Stopping all…")

    # ── Server actions ────────────────────────────────────────────────────────

    def _add_server(self):
        if not self._sel: return
        ServerDialog(self.root,on_save=self._on_srv_saved)

    def _on_srv_saved(self,srv:ServerTarget):
        if not self._sel: return
        sel=self._sel
        def _bg():
            try: self._store.update_server(sel,srv)
            except Exception as e: log.error("Save server: %s",e)
        threading.Thread(target=_bg,daemon=True).start()
        _toast(self.root,f"✓ Server '{srv.name}' saved")
        self._log_line("INFO",f"Server '{srv.name}' saved")

    def _edit_srv_dbl(self,_=None):
        sel=self._srvt.selection()
        if not sel or not self._sel: return
        accts,_=self._cache.get()
        acct=next((a for a in accts if a.account_id==self._sel),None)
        if not acct: return
        srv=next((s for s in acct.servers if s.server_id==sel[0]),None)
        if srv: ServerDialog(self.root,server=srv,on_save=self._on_srv_saved)

    def _srv_ctx(self,event):
        iid=self._srvt.identify_row(event.y)
        if not iid or not self._sel: return
        accts,_=self._cache.get()
        acct=next((a for a in accts if a.account_id==self._sel),None)
        if not acct: return
        srv=next((s for s in acct.servers if s.server_id==iid),None)
        if not srv: return
        m=tk.Menu(self.root,tearoff=0,bg=CARD,fg=TEXT,
                  activebackground=ACCENT,activeforeground=WHITE)
        m.add_command(label="✏  Edit",command=self._edit_srv_dbl)
        m.add_command(label="🔇  Disable" if srv.enabled else "🔊  Enable",
                      command=lambda:self._toggle_srv(srv))
        m.add_separator()
        m.add_command(label="🗑  Remove",command=lambda:self._remove_srv(srv))
        m.post(event.x_root,event.y_root)

    def _toggle_srv(self,srv:ServerTarget):
        if not self._sel: return
        srv.enabled=not srv.enabled
        sel=self._sel
        threading.Thread(target=self._store.update_server,args=(sel,srv),daemon=True).start()

    def _remove_srv(self,srv:ServerTarget):
        if not self._sel: return
        if not messagebox.askyesno("Remove",f"Remove '{srv.name}'?"): return
        sel=self._sel; sid=srv.server_id
        threading.Thread(target=self._store.delete_server,args=(sel,sid),daemon=True).start()
        _toast(self.root,f"Removed '{srv.name}'",color=YELLOW)

    # ── Token extractor ───────────────────────────────────────────────────────

    def _open_token_extractor(self,target_id:str=None):
        def _on_use(token:str,username:str):
            aid=target_id or self._sel
            if aid:
                accts,_=self._cache.get()
                acct=next((a for a in accts if a.account_id==aid),None)
                if acct:
                    def _bg():
                        store_credential(aid,token)
                    threading.Thread(target=_bg,daemon=True).start()
                    self._log_line("SUCCESS",f"Token for '{username}' → {acct.name}")
                    _toast(self.root,f"✓ Token stored for {acct.name}",GREEN)
                    return
            AccountDialog(self.root,prefill_token=token,on_save=self._on_acct_saved)
        try:
            from app.gui.token_extractor import TokenExtractorWindow
            TokenExtractorWindow(parent=self.root,on_use_token=_on_use)
        except Exception as e:
            log.error("Token extractor: %s\n%s",e,traceback.format_exc())
            messagebox.showerror("Error",f"Could not open token extractor:\n{e}")

    def _view_errors(self):
        ErrorLogWindow(self.root)

    # ── Tree ──────────────────────────────────────────────────────────────────

    def _on_list_sel(self,_=None):
        sel=self._list.selection()
        if not sel: return
        self._sel=sel[0]
        self._no_sel.place_forget()
        self._det.place(relx=0,rely=0,relwidth=1,relheight=1)

    # ── Poll (MAIN THREAD — only reads cache, never touches any lock) ─────────

    def _poll(self):
        try:
            # Read from cache — instant, no locks
            accts, smap = self._cache.get()

            # ── Update account list ─────────────────────────────────
            existing = set(self._list.get_children())
            shown    = set()
            for acct in accts:
                s  = smap.get(acct.account_id,{})
                st = s.get("status","stopped")
                ns = [sv.get("next_run_at") for sv in s.get("servers",[]) if sv.get("next_run_at")]
                nx = _cd(min(ns) if ns else None)
                vals=(STATUS_D.get(st,"?"),acct.name,str(len(acct.servers)),nx)
                if acct.account_id in existing:
                    self._list.item(acct.account_id,values=vals,tags=(st,))
                else:
                    self._list.insert("","end",iid=acct.account_id,values=vals,tags=(st,))
                shown.add(acct.account_id)
            for old in existing-shown: self._list.delete(old)
            if not accts:
                self._list_empty.pack(fill="both",expand=True)
            else:
                self._list_empty.pack_forget()

            # ── Update detail panel ─────────────────────────────────
            if self._sel and self._sel in {a.account_id for a in accts}:
                acct = next(a for a in accts if a.account_id==self._sel)
                s    = smap.get(self._sel,{})
                st   = s.get("status","stopped")
                col  = STATUS_C.get(st,TEXT3)

                self._d_name.config(text=acct.name)
                acd_min = s.get("account_cooldown_min", acct.account_cooldown_min)
                scd_min = s.get("server_cooldown_min",  acct.server_cooldown_min)
                rnd_min = s.get("random_offset_min",    acct.random_offset_min)
                self._d_sub.config(
                    text=f"{'👤 User' if acct.token_type=='user' else '🤖 Bot'}"
                         f"  Server CD: {scd_min:.0f}m"
                         f"  ·  Account gap: {acd_min:.0f}m"
                         f"  ·  Offset ±{rnd_min:.0f}m")

                self._ds_st.config(text=f"{STATUS_D.get(st,'?')} {st}",fg=col)
                ns=[sv.get("next_run_at") for sv in s.get("servers",[]) if sv.get("next_run_at")]
                ls=[sv.get("last_run_at") for sv in s.get("servers",[]) if sv.get("last_run_at")]
                self._ds_next.config(text=_cd(min(ns) if ns else None),fg=col)
                self._ds_last.config(text=_ts(max(ls) if ls else None))
                ok=sum(sv.get("total_ok",0)  for sv in s.get("servers",[]))
                er=sum(sv.get("total_fail",0) for sv in s.get("servers",[]))
                tot=ok+er
                self._ds_runs.config(text=f"{ok} / {er}")
                pct=int(ok/tot*100) if tot else 0
                self._ds_pct.config(text=f"{pct}%",
                    fg=GREEN if pct>=80 else YELLOW if pct>=50 else RED if tot else TEXT)

                stopped=st in("stopped","error"); running=st in("running","waiting")
                self._b_start.config(state="normal" if stopped else "disabled",
                                      bg=GREEN if stopped else CARD)
                self._b_stop.config(state="normal"  if running else "disabled",
                                     bg=RED   if running else CARD)
                self._b_pause.config(state="normal" if running else "disabled",
                                      bg=YELLOW if running else CARD,
                                      fg=BG if running else TEXT3)

                # Server list
                srv_st={sv["server_id"]:sv for sv in s.get("servers",[])}
                ex2=set(self._srvt.get_children()); sh2=set()
                for srv in acct.servers:
                    ss=srv_st.get(srv.server_id,{})
                    en="☑" if srv.enabled else "☐"
                    # Calculate estimated next run for queued servers (next_run_at=None)
                    raw_next = ss.get("next_run_at") or (
                        srv.next_run_at.strftime("%Y-%m-%dT%H:%M:%SZ") if srv.next_run_at else None)
                    if not raw_next and not ss.get("last_run_at") and s.get("last_action_at"):
                        # Server hasn't run yet — estimate based on account cooldown queue position
                        from datetime import datetime, timezone, timedelta
                        try:
                            last_act = datetime.fromisoformat(
                                s["last_action_at"].replace("Z", "+00:00"))
                            acd_s = s.get("account_cooldown_min", acct.account_cooldown_min) * 60
                            # Count how many servers ran before this one (by total_ok > 0 ordering)
                            done_count = sum(1 for sv in acct.servers
                                            if sv.total_ok > 0 or sv.next_run_at is not None)
                            queued_pos = max(1, done_count)  # position in queue
                            est = last_act + timedelta(seconds=acd_s * queued_pos)
                            raw_next = est.strftime("%Y-%m-%dT%H:%M:%SZ")
                        except Exception:
                            pass
                    nx = _cd(raw_next) if raw_next else "queued"
                    ok2=srv.total_ok; er2=srv.total_fail; cf=ss.get("consec_fail",0)
                    tag="dis" if not srv.enabled else "err" if cf>0 else "ok"
                    vals=(en,srv.name,srv.guild_id,nx,f"{ok2}/{er2}")
                    if srv.server_id in ex2:
                        self._srvt.item(srv.server_id,values=vals,tags=(tag,))
                    else:
                        self._srvt.insert("","end",iid=srv.server_id,values=vals,tags=(tag,))
                    sh2.add(srv.server_id)
                for old in ex2-sh2: self._srvt.delete(old)

            elif self._sel and self._sel not in {a.account_id for a in accts}:
                # Account was deleted
                self._sel=None
                self._no_sel.place(relx=0,rely=0,relwidth=1,relheight=1)
                self._det.place_forget()

            # ── Status bar ──────────────────────────────────────────
            n_srv  = sum(len(a.servers) for a in accts)
            active = sum(1 for s in smap.values() if s.get("status") in("running","waiting"))
            self._lbl_bar.config(
                text=f"{len(accts)} account(s)  ·  {n_srv} server(s)  ·  {active} active")
            self._lbl_sys.config(
                text=f"● {'Active' if active else 'Ready'}",
                fg=GREEN if active else TEXT2)

            # Error indicator
            errs=self._cache.get_errors()
            if errs:
                self._b_errs.config(fg=RED,bg="#2a1010",text=f"⚠ {len(errs)} Errors")
            else:
                self._b_errs.config(fg=TEXT3,bg=CARD,text="⚠ Errors")

        except Exception as e:
            log.error("Poll error: %s\n%s", e, traceback.format_exc())

        self.root.after(1500,self._poll)

    # ── Log ───────────────────────────────────────────────────────────────────

    def _log_line(self,level:str,msg:str):
        """Always safe to call from any thread."""
        def _write():
            try:
                ts=time.strftime("[%H:%M:%S]")
                self._log.config(state="normal")
                self._log.insert("end",f"{ts} ","TS")
                self._log.insert("end",f"{level:8s}  {msg}\n",level)
                lines=int(self._log.index("end-1c").split(".")[0])
                if lines>400: self._log.delete("1.0",f"{lines-400}.0")
                self._log.see("end")
                self._log.config(state="disabled")
            except Exception: pass
        self.root.after(0,_write)

    def log_event(self,level:str,msg:str):
        self._log_line(level,msg)

    def _clr_log(self):
        self._log.config(state="normal")
        self._log.delete("1.0","end")
        self._log.config(state="disabled")

    # ── Close ─────────────────────────────────────────────────────────────────

    def _close(self):
        h=self._sched.health()
        if h.get("active",0) and not messagebox.askyesno("Exit","Jobs running. Exit anyway?"): return
        self._cache.stop()
        self._lbl_sys.config(text="● Shutting down…",fg=YELLOW)
        self.root.update()
        threading.Thread(target=lambda:(self._sched.stop_all(),
            self.root.after(0,self.root.destroy)),daemon=True).start()

    def run(self):
        self.root.mainloop()
