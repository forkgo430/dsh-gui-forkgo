"""独立子会话（Child Session）—— 让 AI 操作电脑时不抢用户的鼠标键盘。

本模块整合三条已验证的路线：

  路线 A · RDP 子会话（本文件主要实现）
    用 RDP ActiveX 的 ConnectToChildSession 建一个同用户的独立桌面会话。
    需要一次 Windows 密码（ClearTextPassword 属性），之后该会话的
    explorer.exe 里可以注入 helper，AI 在那里操作，用户的鼠标键盘完全不受影响。

  路线 B · UIA 优先（调用方职责）
    能用 UI Automation 控件模式完成的操作本来就不动真实指针。
    本模块提供 session 判断，调用方应优先走 UIA。

  路线 C · 内置脚本运行时（本文件沙箱检测部分）
    不建会话，直接用隔离的解释器跑脚本。不需要密码。

凭据来源（对应计划文档 §5.8）：
    首次运行 → CredUIPromptForWindowsCredentials 弹标准框
             → 用户输一次
             → CryptProtectData 加密存进 dsh-gui/Session
    之后每次 → CredReadW + CryptUnprotectData 读出明文

与参考实现的关键差异：我们存的是**标准 DPAPI** blob（本用户跨进程可解），
不是自定义 AES 方案，便于调试和迁移。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------- win32 绑定
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_crypt = ctypes.WinDLL("crypt32", use_last_error=True)
_credui = ctypes.WinDLL("credui", use_last_error=True)
_wts = ctypes.WinDLL("wtsapi32")
_psapi = ctypes.WinDLL("psapi")

CRED_TARGET = "dsh-gui/Session"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CREDUIWIN_GENERIC = 0x00000001
CRYPTPROTECT_UI_FORBIDDEN = 0x1

LOGON32_LOGON_INTERACTIVE = 2
LOGON32_LOGON_NETWORK = 3

SESSION_STATE = {
    0: "active", 1: "connected", 2: "connectquery", 3: "shadow",
    4: "disconnected", 5: "idle", 6: "listen", 7: "reset",
    8: "down", 9: "init",
}


# ---------------------------------------------------------------- 结构定义
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


class _CREDUI_INFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hwndParent", wt.HWND),
        ("pszMessageText", wt.LPCWSTR),
        ("pszCaptionText", wt.LPCWSTR),
        ("hbmBanner", wt.HANDLE),
    ]


class _CRED_ATTR(ctypes.Structure):
    _fields_ = [("Keyword", wt.LPWSTR), ("Flags", wt.DWORD),
                ("ValueSize", wt.DWORD),
                ("Value", ctypes.POINTER(ctypes.c_byte))]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD),
        ("Type", wt.DWORD),
        ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR),
        ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wt.DWORD),
        ("AttributeCount", wt.DWORD),
        ("Attributes", ctypes.POINTER(_CRED_ATTR)),
        ("TargetAlias", wt.LPWSTR),
        ("UserName", wt.LPWSTR),
    ]


class _WSESSION(ctypes.Structure):
    _fields_ = [("SessionId", wt.DWORD), ("pWinStationName", wt.LPWSTR),
                ("State", wt.DWORD)]


class _WPROC(ctypes.Structure):
    _fields_ = [("SessionId", wt.DWORD), ("ProcessId", wt.DWORD),
                ("pProcessName", wt.LPWSTR), ("pUserSid", ctypes.c_void_p)]


# ---------------------------------------------------------------- 凭据：DPAPI
def _dpapi_protect(plaintext: bytes, entropy: bytes = b"dsh-gui-v1") -> bytes:
    buf_in = ctypes.create_string_buffer(plaintext, len(plaintext))
    inb = _DATA_BLOB(len(plaintext),
                     ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_byte)))
    ent = None
    keep_e = None
    if entropy:
        keep_e = ctypes.create_string_buffer(entropy, len(entropy))
        ent = _DATA_BLOB(len(entropy),
                         ctypes.cast(keep_e, ctypes.POINTER(ctypes.c_byte)))
    outb = _DATA_BLOB()
    ok = _crypt.CryptProtectData(
        ctypes.byref(inb), "dsh-gui session password",
        ctypes.byref(ent) if ent else None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(outb))
    if not ok:
        raise OSError(f"CryptProtectData 失败 err={ctypes.get_last_error()}")
    try:
        return ctypes.string_at(outb.pbData, outb.cbData)
    finally:
        _k32.LocalFree(outb.pbData)


def _dpapi_unprotect(blob: bytes, entropy: bytes = b"dsh-gui-v1") -> bytes:
    buf_in = ctypes.create_string_buffer(blob, len(blob))
    inb = _DATA_BLOB(len(blob),
                     ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_byte)))
    ent = None
    keep_e = None
    if entropy:
        keep_e = ctypes.create_string_buffer(entropy, len(entropy))
        ent = _DATA_BLOB(len(entropy),
                         ctypes.cast(keep_e, ctypes.POINTER(ctypes.c_byte)))
    outb = _DATA_BLOB()
    ok = _crypt.CryptUnprotectData(
        ctypes.byref(inb), None, ctypes.byref(ent) if ent else None,
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(outb))
    if not ok:
        raise OSError(f"CryptUnprotectData 失败 err={ctypes.get_last_error()}")
    try:
        return ctypes.string_at(outb.pbData, outb.cbData)
    finally:
        _k32.LocalFree(outb.pbData)


# ---------------------------------------------------------------- 凭据：存取
@dataclass
class Credential:
    user: str
    domain: str
    password: str = field(repr=False)

    def __str__(self) -> str:
        return f"{self.domain}\\{self.user} ({len(self.password)} 字符)"


def save_credential(user: str, domain: str, password: str) -> None:
    """加密后写进 Windows 凭据管理器"""
    payload = f"{domain}\x00{user}\x00{password}".encode("utf-16-le")
    blob = _dpapi_protect(payload)

    c = _CREDENTIALW()
    ctypes.memset(ctypes.byref(c), 0, ctypes.sizeof(c))
    c.Type = CRED_TYPE_GENERIC
    c.TargetName = CRED_TARGET
    c.Comment = "DSH GUI virtual desktop credential"
    c.CredentialBlobSize = len(blob)
    buf = ctypes.create_string_buffer(blob, len(blob))
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
    c.Persist = CRED_PERSIST_LOCAL_MACHINE
    c.UserName = user

    _adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wt.DWORD]
    if not _adv.CredWriteW(ctypes.byref(c), 0):
        raise OSError(f"CredWriteW 失败 err={ctypes.get_last_error()}")


def load_credential() -> Optional[Credential]:
    """读出来解密"""
    _adv.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                               ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    p = ctypes.POINTER(_CREDENTIALW)()
    if not _adv.CredReadW(CRED_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        if not c.CredentialBlobSize:
            return None
        blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        payload = _dpapi_unprotect(blob).decode("utf-16-le").rstrip("\x00")
        parts = payload.split("\x00")
        if len(parts) < 3:
            return None
        return Credential(user=parts[1], domain=parts[0], password=parts[2])
    except Exception:
        return None
    finally:
        _adv.CredFree.argtypes = [ctypes.POINTER(_CREDENTIALW)]
        _adv.CredFree(p)


def delete_credential() -> bool:
    _adv.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
    return bool(_adv.CredDeleteW(CRED_TARGET, CRED_TYPE_GENERIC, 0))


def prompt_credential(message: str = "", caption: str = "DSH 虚拟桌面"
                      ) -> Optional[Credential]:
    """弹 Windows 标准凭据框。用户取消返回 None。"""
    _credui.CredUIPromptForWindowsCredentialsW.argtypes = [
        ctypes.POINTER(_CREDUI_INFOW), wt.DWORD, ctypes.POINTER(wt.DWORD),
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(wt.BOOL)]
    _credui.CredUIPromptForWindowsCredentialsW.restype = wt.DWORD

    if not message:
        message = ("DSH 需要一次你的 Windows 密码，用来创建隔离的虚拟桌面。\n"
                   "密码只存本机，用当前账户的 DPAPI 加密，不会外传。")

    info = _CREDUI_INFOW()
    info.cbSize = ctypes.sizeof(_CREDUI_INFOW)
    info.hwndParent = None
    info.pszMessageText = message
    info.pszCaptionText = caption
    info.hbmBanner = None

    pkg = wt.DWORD(0)
    out_buf = ctypes.c_void_p()
    out_size = wt.DWORD()
    save_flag = wt.BOOL(False)

    rc = _credui.CredUIPromptForWindowsCredentialsW(
        ctypes.byref(info), 0, ctypes.byref(pkg), None, 0,
        ctypes.byref(out_buf), ctypes.byref(out_size),
        ctypes.byref(save_flag), CREDUIWIN_GENERIC)
    if rc != 0:
        return None  # 1223 = 用户取消

    try:
        _credui.CredUnPackAuthenticationBufferW.argtypes = [
            wt.DWORD, ctypes.c_void_p, wt.DWORD,
            wt.LPWSTR, ctypes.POINTER(wt.DWORD),
            wt.LPWSTR, ctypes.POINTER(wt.DWORD),
            wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        _credui.CredUnPackAuthenticationBufferW.restype = wt.BOOL

        u = ctypes.create_unicode_buffer(514)
        un = wt.DWORD(len(u))
        d = ctypes.create_unicode_buffer(514)
        dn = wt.DWORD(len(d))
        p = ctypes.create_unicode_buffer(257)
        pn = wt.DWORD(len(p))

        if not _credui.CredUnPackAuthenticationBufferW(
                0, out_buf, out_size.value,
                u, ctypes.byref(un), d, ctypes.byref(dn), p, ctypes.byref(pn)):
            return None
        return Credential(user=u.value, domain=d.value, password=p.value)
    finally:
        if out_buf:
            _k32.CoTaskMemFree(out_buf)


def verify_credential(c: Credential, logon_type: int = LOGON32_LOGON_NETWORK
                      ) -> tuple[bool, int]:
    """LogonUserW 验证。返回 (ok, error)"""
    _adv.LogonUserW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR,
                                wt.DWORD, wt.DWORD,
                                ctypes.POINTER(wt.HANDLE)]
    _adv.LogonUserW.restype = ctypes.c_bool
    h = wt.HANDLE()
    ok = _adv.LogonUserW(ctypes.c_wchar_p(c.user), ctypes.c_wchar_p(c.domain),
                         ctypes.c_wchar_p(c.password), logon_type, 0,
                         ctypes.byref(h))
    err = ctypes.get_last_error()
    if ok:
        _k32.CloseHandle(h)
        return True, 0
    return False, err


def acquire_token(c: Credential) -> tuple[Optional[int], int]:
    """拿 primary token（建子会话用）。返回 (handle, error)"""
    _adv.LogonUserW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR,
                                wt.DWORD, wt.DWORD,
                                ctypes.POINTER(wt.HANDLE)]
    _adv.LogonUserW.restype = ctypes.c_bool
    h = wt.HANDLE()
    ok = _adv.LogonUserW(ctypes.c_wchar_p(c.user), ctypes.c_wchar_p(c.domain),
                         ctypes.c_wchar_p(c.password),
                         LOGON32_LOGON_INTERACTIVE, 0, ctypes.byref(h))
    if not ok:
        return None, ctypes.get_last_error()
    return h.value, 0


# ---------------------------------------------------------------- 会话查询
def child_sessions_enabled() -> bool:
    e = wt.BOOL(False)
    return bool(_wts.WTSIsChildSessionsEnabled(ctypes.byref(e))) and bool(e.value)


def get_child_session_id() -> Optional[int]:
    sid = wt.DWORD()
    if _wts.WTSGetChildSessionId(ctypes.byref(sid)):
        return sid.value
    return None


def list_sessions() -> list[tuple[int, str, str]]:
    """[(session_id, state, winstation_name)]"""
    p = ctypes.POINTER(_WSESSION)()
    n = wt.DWORD()
    out = []
    if _wts.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(p), ctypes.byref(n)):
        try:
            for i in range(n.value):
                s = p[i]
                out.append((s.SessionId,
                            SESSION_STATE.get(s.State, str(s.State)),
                            s.pWinStationName or ""))
        finally:
            _wts.WTSFreeMemory(p)
    return out


def session_processes(session_id: int) -> list[tuple[int, str]]:
    p = ctypes.POINTER(_WPROC)()
    n = wt.DWORD()
    out = []
    if _wts.WTSEnumerateProcessesW(None, 0, 1, ctypes.byref(p), ctypes.byref(n)):
        try:
            for i in range(n.value):
                s = p[i]
                if s.SessionId == session_id:
                    out.append((s.ProcessId, s.pProcessName or ""))
        finally:
            _wts.WTSFreeMemory(p)
    return out


def session_info(session_id: int) -> dict:
    def q(cls, as_str=True):
        buf = ctypes.c_void_p()
        size = wt.DWORD()
        if _wts.WTSQuerySessionInformationW(None, session_id, cls,
                                            ctypes.byref(buf),
                                            ctypes.byref(size)):
            try:
                return (ctypes.wstring_at(buf) if as_str
                        else ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0])
            finally:
                _wts.WTSFreeMemory(buf)
        return None
    return {
        "user": q(5),
        "domain": q(7),
        "state": q(8, False),
        "protocol": q(16, False),
        "is_remote": q(29, False),
    }


def find_child_explorer() -> Optional[tuple[int, int]]:
    """返回 (session_id, pid) 或 None。

    只认 state 为 active(0) 或 connected(1) 的会话。断开的会话里 explorer.exe
    可能还在（进程没死透），但那个会话已经不能用了——拿它去截图或注入
    都是错的。
    """
    active_states = {0, 1}
    for sid, state, _ in list_sessions():
        if sid < 2 or state not in ("active", "connected"):
            continue
        for pid, name in session_processes(sid):
            if name.lower() == "explorer.exe":
                return sid, pid
    return None


def wait_for_login(timeout: float = 60.0, interval: float = 0.4
                   ) -> Optional[tuple[int, int]]:
    """等子会话登录完成（explorer 出现）。返回 (session_id, pid)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        el = find_child_explorer()
        if el:
            return el
        time.sleep(interval)
    return None


# ---------------------------------------------------------------- 进程注入
def inject_dll(pid: int, dll_path: str) -> tuple[bool, str]:
    """照抄豆包的 RemoteInjection：OpenProcess → VirtualAllocEx →
    WriteProcessMemory → CreateRemoteThread(LoadLibraryW)。

    同用户进程不需要 SeDebugPrivilege。
    """
    dll_path = os.path.abspath(dll_path)
    if not os.path.isfile(dll_path):
        return False, f"DLL 不存在: {dll_path}"

    PROCESS_ALL = 0x1F0FFF
    MEM_COMMIT, MEM_RESERVE = 0x1000, 0x2000
    PAGE_RW = 0x04

    h = _k32.OpenProcess(PROCESS_ALL, False, pid)
    if not h:
        return False, f"OpenProcess({pid}) 失败 err={ctypes.get_last_error()}"

    try:
        path_b = dll_path.encode("utf-16-le") + b"\x00\x00"

        _k32.VirtualAllocEx.restype = ctypes.c_void_p
        _k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                        ctypes.c_size_t, wt.DWORD, wt.DWORD]
        remote = _k32.VirtualAllocEx(h, None, len(path_b),
                                     MEM_COMMIT | MEM_RESERVE, PAGE_RW)
        if not remote:
            return False, f"VirtualAllocEx 失败 err={ctypes.get_last_error()}"

        _k32.WriteProcessMemory.argtypes = [
            wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t)]
        written = ctypes.c_size_t(0)
        buf = ctypes.create_string_buffer(path_b, len(path_b))
        if not _k32.WriteProcessMemory(h, ctypes.c_void_p(remote), buf,
                                        len(path_b), ctypes.byref(written)):
            return False, f"WriteProcessMemory 失败 err={ctypes.get_last_error()}"

        _k32.GetModuleHandleW.restype = ctypes.c_void_p
        _k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        _k32.GetProcAddress.restype = ctypes.c_void_p
        _k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

        k32 = _k32.GetModuleHandleW("kernel32.dll")
        load_lib = _k32.GetProcAddress(k32, b"LoadLibraryW")
        if not load_lib:
            return False, "取 LoadLibraryW 地址失败"

        _k32.CreateRemoteThread.restype = wt.HANDLE
        _k32.CreateRemoteThread.argtypes = [
            wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
        tid = wt.DWORD()
        th = _k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(load_lib),
                                      ctypes.c_void_p(remote), 0,
                                      ctypes.byref(tid))
        if not th:
            return False, f"CreateRemoteThread 失败 err={ctypes.get_last_error()}"

        try:
            _k32.WaitForSingleObject(th, 15000)
            code = wt.DWORD()
            _k32.GetExitCodeThread(th, ctypes.byref(code))
            ok = code.value != 0
            return ok, (f"注入成功（模块句柄 0x{code.value:X}）" if ok
                        else "LoadLibrary 返回 0")
        finally:
            _k32.CloseHandle(th)
    finally:
        _k32.CloseHandle(h)


def module_loaded(pid: int, name: str) -> bool:
    h = _k32.OpenProcess(0x0410, False, pid)
    if not h:
        return False
    try:
        mods = (ctypes.c_void_p * 4096)()
        need = wt.DWORD()
        if not _psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods),
                                            ctypes.byref(need), 0x03):
            return False
        n = need.value // ctypes.sizeof(ctypes.c_void_p)
        buf = ctypes.create_unicode_buffer(260)
        for i in range(n):
            if _psapi.GetModuleBaseNameW(h, ctypes.c_void_p(mods[i]), buf, 260):
                if buf.value.lower() == name.lower():
                    return True
        return False
    finally:
        _k32.CloseHandle(h)


# ---------------------------------------------------------------- 脚本沙箱
def find_bundled_runtimes() -> dict:
    """找机器上已装的内置运行时（豆包那套）。只是探测，不自带。

    返回 {"node": path, "python": path, "pwsh": path}
    """
    out = {}
    roots = [
        os.path.expandvars(
            r"%LOCALAPPDATA%\DoubaoWork\User Data\sandbox_runtime\bases"),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base in os.listdir(root):
            bdir = os.path.join(root, base)
            for name, rel in (("node", r"node\node.exe"),
                              ("python", r"python\python.exe"),
                              ("pwsh", r"pwsh\pwsh.exe")):
                p = os.path.join(bdir, rel)
                if os.path.isfile(p) and name not in out:
                    out[name] = p
    return out


def capabilities() -> dict:
    """汇总当前机器能用的能力"""
    el = find_child_explorer()
    cred = load_credential()
    runtimes = find_bundled_runtimes()

    return {
        "child_sessions_enabled": child_sessions_enabled(),
        "child_session_id": get_child_session_id(),
        "child_explorer": ({"session": el[0], "pid": el[1]} if el else None),
        "credential_saved": cred is not None,
        "credential_user": (str(cred) if cred else None),
        "script_runtimes": sorted(runtimes.keys()),
        "can_create_session": bool(cred) and child_sessions_enabled(),
        "can_inject": el is not None,
        "needs_password": cred is None,
        "host_running": host_running(),
    }


# ---------------------------------------------------------------- 常驻宿主
def _host_paths() -> tuple[str, str, str]:
    """(脚本路径, 就绪文件, 停止文件)"""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "host_rdp.ps1")
    tmpdir = os.environ.get("TEMP") or os.path.expanduser("~")
    ready = os.path.join(tmpdir, "dsh-gui-host-ready.txt")
    stop = os.path.join(tmpdir, "dsh-gui-host-stop.txt")
    return script, ready, stop


# ---------------------------------------------------------------- 会话内守护（guard）
#
# 为什么需要：父会话看不见子会话的窗口。Windows 按 session 隔离窗口站，实测父进程
# EnumWindows 拿到的子会话顶层窗口数是 0（自己的能看到 300 多个）。所以在父进程里
# "找 UAC 弹窗再关掉"这条路根本不成立 —— 找不到。
#
# 豆包（DoubaoWork）往子会话的 explorer 注入 pip_remote.dll 来解决，那需要编译器。
# 这台机器没有编译器，于是换一条等价但更轻的路：
#
#   **建会话之前先往 HKCU 的 Run 键预置一个启动项**，子会话登录时系统会在子会话里
#   自动执行它。HKCU 是各会话共享的同一份注册表，子会话读到的是同一份 Run 键。
#
# 实测（本机 Win10 19045）：
#   guard 在子会话 3 里跑起来，mySession=3、consoleSession=1，
#   枚举到子会话里的 #32770 对话框（PowerToys 错误），
#   PostMessage(WM_CLOSE) 返回 True，对话框确实关掉了。
#
# 代价与边界：
#   - Run 键是全局的，所以在 start 之前设、在 stop 之后撤，中间别让用户自己去登录。
#   - 只关 #32770（标准对话框类名）。UAC 的安全桌面是另一个窗口站，够不着；
#     这里关的是普通对话框、报错框、安装确认框这类会阻塞自动化的东西。

GUARD_STATE_NAME = "dsh-gui-guard-state.json"
GUARD_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
GUARD_RUN_VALUE = "DshGuiGuard"

#: 状态文件里超过这么久没心跳，就认为 guard 没在跑（秒）。
GUARD_STALE_SECONDS = 8.0


def _guard_paths() -> tuple[str, str]:
    """(脚本路径, 状态文件路径)"""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "guard_in_session.ps1")
    tmpdir = os.environ.get("TEMP") or os.path.expanduser("~")
    return script, os.path.join(tmpdir, GUARD_STATE_NAME)


def _guard_cmd_path() -> str:
    """Run 键指向的那个 .cmd 包装。

    为什么不能直接把 pwsh 命令行写进 Run 键：Winlogon 启动 Run 项时不走 cmd，
    直接把整串当程序名去 CreateProcess，带引号的长命令行会被截断或解析失败。
    用 .cmd 包一层最稳 —— 实测直接写 pwsh 命令行那次子会话里什么都没启动，
    换成 .cmd 之后同样的脚本就能起来。
    """
    tmpdir = os.environ.get("TEMP") or os.path.expanduser("~")
    d = os.path.join(tmpdir, "dsh-guard")
    return os.path.join(d, "run.cmd")


def _guard_command(script: str, state: str, close_dialogs: bool,
                   interval_ms: int = 1500, keep_titles: str = "") -> str:
    """生成 .cmd 包装文件的内容，返回它的路径；失败返回空串。

    Run 键存的值是 ``"<这个 cmd 的路径>"``。脚本参数全部写死在 cmd 里，
    Run 键那一侧只负责"启动它"。
    """
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        return ""
    cmd_path = _guard_cmd_path()
    try:
        os.makedirs(os.path.dirname(cmd_path), exist_ok=True)
    except OSError:
        return ""

    parts = [
        f'"{pwsh}"', "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", f'"{script}"',
        "-StateFile", f'"{state}"',
        "-IntervalMs", str(int(interval_ms)),
        "-CloseDialogs", ("true" if close_dialogs else "false"),
    ]
    if keep_titles:
        parts += ["-KeepTitles", f'"{keep_titles}"']

    body = "@echo off\r\n" + " ".join(parts) + "\r\n"
    try:
        # 用系统默认编码（GBK）写：路径里可能带中文，UTF-8 会被 cmd 读成乱码。
        with open(cmd_path, "wb") as f:
            f.write(body.encode("mbcs", errors="replace"))
    except (OSError, LookupError):
        try:
            with open(cmd_path, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(body)
        except OSError:
            return ""
    return cmd_path


def arm_guard(close_dialogs: bool = True, interval_ms: int = 1500,
              keep_titles: str = "") -> tuple[bool, str]:
    """在建会话之前把 guard 挂到 HKCU 的 Run 键上。

    必须在 start_host 之前调用 —— 子会话登录时才会读 Run 键，登录完了再设就没用了。
    """
    script, state = _guard_paths()
    if not os.path.isfile(script):
        return False, f"guard 脚本缺失: {script}"

    cmd_path = _guard_command(script, state, close_dialogs, interval_ms, keep_titles)
    if not cmd_path:
        return False, "找不到 pwsh，guard 无法挂载"

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, GUARD_RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, GUARD_RUN_VALUE, 0, winreg.REG_SZ, f'"{cmd_path}"')
    except OSError as e:
        return False, f"写 Run 键失败: {e}"

    # 清掉上一次的状态，免得 status 读到旧心跳以为它还活着
    try:
        os.remove(state)
    except OSError:
        pass
    return True, f"guard 已挂到 Run 键（{cmd_path}），下次子会话登录时启动"


def disarm_guard() -> bool:
    """把 guard 从 Run 键撤掉。返回是否真的删除了一个值。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, GUARD_RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, GUARD_RUN_VALUE)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def guard_armed() -> bool:
    """Run 键上还挂着 guard 吗。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, GUARD_RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as key:
            winreg.QueryValueEx(key, GUARD_RUN_VALUE)
            return True
    except OSError:
        return False


def guard_status() -> dict:
    """读 guard 的心跳状态。父进程只能通过这个文件知道子会话里发生了什么。"""
    _, state = _guard_paths()
    result: dict = {"armed": guard_armed(), "state_file": state}
    if not os.path.isfile(state):
        result.update({"running": False, "reason": "状态文件还没生成（子会话可能刚起，或 Run 键没生效）"})
        return result
    try:
        import json
        with open(state, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        result.update({"running": False, "reason": f"状态文件读不了: {e}"})
        return result

    result.update({
        "pid": data.get("pid"),
        "session": data.get("session"),
        "console_session": data.get("console_session"),
        "close_dialogs": data.get("close_dialogs"),
        "closed_total": data.get("closed_total", 0),
        "started": data.get("started"),
        "beat_at": data.get("now"),
    })

    # 心跳新鲜度：状态文件每秒写一次，超过阈值就是停了
    age = None
    try:
        age = time.time() - os.path.getmtime(state)
    except OSError:
        pass
    result["beat_age_seconds"] = round(age, 1) if age is not None else None
    result["running"] = bool(age is not None and age <= GUARD_STALE_SECONDS)
    if not result["running"]:
        result["reason"] = f"心跳已停 {result['beat_age_seconds']}s（阈值 {GUARD_STALE_SECONDS}s）"
    return result


def host_running() -> bool:
    """靠「有活动的子会话 explorer」来判断宿主还活着。

    更精确的办法是记 PID，但宿主是独立进程，插件重载后 PID 就丢了。
    子会话活着 ⇒ 宿主一定活着，这个反推足够用。
    """
    return find_child_explorer() is not None


PREVIEW_TITLE = "DSH 子会话"


def preview_window() -> Optional[dict]:
    """找出宿主那个预览窗，报告它到底在哪、看不看得见。

    为什么不让调用方假设：宿主可能被 ``-Visible:$false`` 起成屏幕外，
    也可能被用户拖走了、最小化了、或者关掉了。这三种状态下"有子会话"
    都成立，但用户能不能看见完全是另一回事。status 里报一句空话没用，
    这里真的去枚举窗口。

    返回 None 表示找不到窗口（宿主用了旧脚本，或进程已退）。
    """
    import ctypes.wintypes as _wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumProc = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)

    found: list[int] = []

    def _cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value.startswith(PREVIEW_TITLE):
            found.append(hwnd)
        return True

    try:
        user32.EnumWindows(EnumProc(_cb), 0)
    except Exception:
        return None

    if not found:
        return None

    hwnd = found[0]
    rect = _wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    visible = bool(user32.IsWindowVisible(hwnd))
    minimized = bool(user32.IsIconic(hwnd))
    # 屏幕外判定：左上角跑到 -30000 一带就是老行为
    offscreen = rect.left <= -30000 or rect.top <= -30000
    return {
        "hwnd": hwnd,
        "title": PREVIEW_TITLE,
        "visible": visible,
        "minimized": minimized,
        "offscreen": offscreen,
        "rect": {
            "left": rect.left, "top": rect.top,
            "right": rect.right, "bottom": rect.bottom,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
        },
        # 一句话结论，省得调用方自己去推三个 bool
        "on_screen": visible and not minimized and not offscreen,
    }


def start_host(width: int = 1280, height: int = 720,
               timeout: float = 70.0, visible: bool = True,
               left: int = -1, top: int = -1,
               guard: bool = True, guard_close_dialogs: bool = True,
               guard_keep_titles: str = "") -> tuple[bool, str, Optional[dict]]:
    """起常驻宿主，等它把子会话拉起来。

    返回 (成功, 消息, 会话信息)。宿主是一个独立进程，插件卸载 / 重载
    都不影响它——这正是子会话能长期活着的原因。

    ``visible=True``（默认）把宿主窗口摆在主屏右下角，用户能看见 AI 在里面
    操作什么。这不会让子会话的输入影响用户：AI 动手是在另一个会话里，
    用户的键鼠本来就不在那边。窗口只在用户主动点它时才会拿到焦点。

    ``left``/``top`` 传非负值可以指定位置，例如用户想把窗挪到副屏。

    ``guard=True``（默认）会在建会话**之前**把守护脚本挂到 HKCU 的 Run 键上，
    子会话登录时自动在子会话里跑起来，负责关掉 #32770 对话框（UAC、报错框、
    安装确认框这类会阻塞自动化的东西）。父会话看不见子会话的窗口，这层代理是
    唯一能碰到那些对话框的手段。``guard_close_dialogs=False`` 只挂载不关窗。
    """
    import subprocess

    if host_running():
        el = find_child_explorer()
        sid, pid = el  # type: ignore[misc]
        info = session_info(sid)
        return True, "宿主已在运行，复用现有子会话", {
            "session_id": sid, "explorer_pid": pid,
            "user": info.get("user"), "state": info.get("state"),
            "guard": guard_status(),
        }

    cred = load_credential()
    if cred is None:
        return False, "还没有保存凭据，先执行 setup", None

    script, ready_file, stop_file = _host_paths()
    if not os.path.isfile(script):
        return False, f"宿主脚本缺失: {script}", None

    # 清掉上次的残留标记
    for p in (ready_file, stop_file):
        try:
            os.remove(p)
        except OSError:
            pass

    # guard 必须在建会话之前挂上：子会话登录时才读 Run 键，登录完再设就没用了。
    guard_msg = ""
    if guard:
        guard_ok, guard_msg = arm_guard(
            close_dialogs=guard_close_dialogs, keep_titles=guard_keep_titles)
        if not guard_ok:
            # 挂不上不算致命错误：子会话照样能建，只是没有自动关窗能力。
            # 把这个事实带回去，别让调用方以为 guard 在跑。
            guard_msg = f"guard 未挂载（{guard_msg}），子会话仍会创建"

    # 找 pwsh；没有就退回 powershell.exe
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        return False, "找不到 PowerShell", None

    cmd = [
        pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", script,
        "-Password", cred.password,
        "-ReadyFile", ready_file,
        "-StopFile", stop_file,
        "-Width", str(width), "-Height", str(height),
        # [bool] 参数只接受真正的布尔值或数字。从 subprocess 传进来的每个
        # token 都是字符串，所以 "$true" 会以字面量 "System.String '$true'"
        # 到达，参数绑定直接失败（"无法将值 System.String 转换为 System.Boolean"）。
        # 数字是 PowerShell 明确支持的布尔字面量，1/0 最稳。
        "-Visible", ("1" if visible else "0"),
    ]
    if left >= 0:
        cmd += ["-Left", str(int(left))]
    if top >= 0:
        cmd += ["-Top", str(int(top))]

    creationflags = 0
    if os.name == "nt":
        # 隐藏控制台窗口。注意这跟预览窗无关：CREATE_NO_WINDOW 只压控制台，
        # WinForms 的窗体照样能显示。
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as e:
        return False, f"启动宿主失败: {e}", None

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(ready_file):
            el = find_child_explorer()
            if el:
                sid, pid = el
                info = session_info(sid)
                # guard 是 Run 键拉起的，登录完成后还要几秒才写出第一份心跳。
                # 这里不等它，先把会话状态报回去；调用方要看 guard 就再查 status。
                return True, f"宿主已就绪（pid {proc.pid}），子会话 {sid}", {
                    "session_id": sid, "explorer_pid": pid,
                    "user": info.get("user"), "state": info.get("state"),
                    "host_pid": proc.pid,
                    "preview_visible": visible,
                    "guard_armed": guard_armed(),
                    "guard_note": guard_msg,
                }
        if proc.poll() is not None:
            return False, f"宿主进程已退出（码 {proc.returncode}）", None
        time.sleep(0.4)

    return False, f"等 {timeout:.0f} 秒仍未登录成功", {"host_pid": proc.pid}


def logoff_session(session_id: int, wait_logon: bool = False) -> tuple[bool, int]:
    """注销一个会话。返回 (成功, 错误码)。

    为什么必须有这一步：RDP 宿主断开只是让会话变成 disconnected，
    会话本身还活着——explorer、dwm、csrss 全在跑，每个占几百 MB。
    而且杀了 explorer 三秒就自动重启，因为那是个完整的登录会话。
    只有注销才能真正清掉。

    ``wait_logon=False`` 表示不等登录完成（子会话没有登录界面，
    等它反而是浪费时间），传 True 时会阻塞到会话真正退出。
    """
    _wts.WTSLogoffSession.argtypes = [wt.HANDLE, wt.DWORD, wt.BOOL]
    _wts.WTSLogoffSession.restype = wt.BOOL
    ok = _wts.WTSLogoffSession(None, wt.DWORD(session_id),
                                wt.BOOL(wait_logon))
    if ok:
        return True, 0
    return False, ctypes.get_last_error()


def wait_session_gone(session_id: int, timeout: float = 20.0) -> bool:
    """等某个会话彻底从会话列表里消失。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(sid == session_id for sid, _, _ in list_sessions()):
            return True
        time.sleep(0.4)
    return False


def stop_host(timeout: float = 30.0) -> tuple[bool, str]:
    """关掉子会话。

    三步，缺一不可：
      1. 写停止标记，让 RDP 宿主自己断开并退出
      2. **注销会话**——宿主断开只是让会话变 disconnected，不注销的话
         那 30 多个进程会一直留着
      3. 等会话真的从列表里消失

    之前只做了第 1 步就报「已关闭」，于是 start/stop 反复跑会不断堆积
    孤儿会话，每个几百 MB。
    """
    _, ready_file, stop_file = _host_paths()

    # 先把 guard 从 Run 键撤掉。Run 键是全局的，留着的话用户下次自己登录
    # 也会被拉起一个守护进程，那不是他要的。
    guard_removed = disarm_guard()

    # 先记下当前有哪些子会话，注销时按这个名单来
    targets = [sid for sid, _, _ in list_sessions() if sid >= 2]
    host_alive = host_running()

    if not targets and not host_alive:
        return True, ("没有运行中的子会话" + ("（guard 已撤下）" if guard_removed else ""))

    # 第 1 步：让宿主断开
    if host_alive:
        try:
            with open(stop_file, "w", encoding="utf-8") as f:
                f.write("stop")
        except OSError as e:
            return False, f"写停止标记失败: {e}"

        deadline = time.time() + min(timeout / 2, 10.0)
        while time.time() < deadline and host_running():
            time.sleep(0.3)

    # 第 2 步：注销会话。重新扫一遍，把宿主断开后残留的也算上。
    to_logoff = {sid for sid, _, _ in list_sessions() if sid >= 2} | set(targets)
    logged_off = []
    failed = []
    for sid in sorted(to_logoff):
        ok, err = logoff_session(sid)
        if ok:
            logged_off.append(sid)
        else:
            failed.append(f"{sid}(err={err})")

    # 第 3 步：等它们真的消失
    still = []
    for sid in to_logoff:
        if not wait_session_gone(sid, timeout=max(5.0, timeout / max(1, len(to_logoff)))):
            still.append(sid)

    # 收尾：清掉标记文件
    for p in (ready_file, stop_file):
        try:
            os.remove(p)
        except OSError:
            pass

    # guard 的状态文件也清掉，免得下次 status 读到上一次的旧心跳
    _, guard_state = _guard_paths()
    try:
        os.remove(guard_state)
    except OSError:
        pass

    if still:
        return False, (f"注销了 {logged_off or '无'}，但这些会话仍在：{still}"
                       + (f"；注销失败：{', '.join(failed)}" if failed else ""))
    if failed:
        return False, f"注销失败：{', '.join(failed)}"
    if logged_off:
        return True, (f"子会话已注销：{logged_off}"
                      + ("；guard 已从 Run 键撤下" if guard_removed else ""))
    return True, "没有需要注销的子会话"
