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
    """返回 (session_id, pid) 或 None"""
    for sid, state, _ in list_sessions():
        if sid < 2:
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
    }
