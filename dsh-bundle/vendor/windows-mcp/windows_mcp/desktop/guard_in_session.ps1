# 子会话内的守护脚本（guard）—— 由 Run 键在子会话登录时拉起。
#
# 为什么需要它：
#   父会话（AI 所在的 Session 1）**看不见也碰不到**子会话里的窗口。Windows 按
#   session 隔离窗口站，实测父会话枚举到的子会话顶层窗口数是 0（自己的看到 315 个）。
#   所以"在父进程里 EnumWindows 找到 UAC 弹窗再关掉"这条路从一开始就不成立。
#
# 豆包（DoubaoWork）的做法是往子会话的 explorer 注入 pip_remote.dll，靠命名管道回连。
# 那需要编译器做 DLL，这台机器没有。这里用等价但更轻的路：
#   **建会话之前先往 HKCU 的 Run 键预置本脚本**，子会话登录时系统会自动在子会话里
#   执行它。HKCU 是所有会话共享的同一份注册表，子会话读到的是同一个 Run 键。
#
#   实测：mySession=<子会话号>，能看到并关掉子会话里的 #32770 对话框（WM_CLOSE=True）。
#
# 它做的事：
#   1. 周期性枚举本会话（自己所在的会话）的顶层窗口
#   2. 发现 #32770 对话框，按配置决定关不关
#   3. 把心跳和关窗计数写进状态文件，父进程通过 status 读得到
#
# 这个脚本**只会**在子会话里跑（启动时先确认自己的 session 不是 console session），
# 避免用户在父会话手动启动它时误关父会话的对话框。

param(
    [string]$StateFile = "",
    [int]$IntervalMs = 1500,
    # 要不要自动关对话框。关的是 #32770 一类，包括 UAC、报错框、安装确认框。
    [string]$CloseDialogs = "true",
    # 除了这些标题之外都关。空表示不设白名单。
    [string]$KeepTitles = "",
    # 最长活多久（秒）。子会话销毁时进程会被一起带走，这个只是兜底。
    [int]$MaxSeconds = 28800
)

$ErrorActionPreference = "Continue"

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class GuardNative {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("kernel32.dll")] public static extern bool ProcessIdToSessionId(uint pid, out uint sid);
    [DllImport("kernel32.dll")] public static extern uint WTSGetActiveConsoleSessionId();

    public struct Dlg { public long Hwnd; public uint Pid; public uint Session; public string Title; }

    public static uint MySession() {
        uint sid; ProcessIdToSessionId((uint)System.Diagnostics.Process.GetCurrentProcess().Id, out sid);
        return sid;
    }
    /// 只枚举 #32770（标准对话框类名），只看可见的。
    public static List<Dlg> Dialogs() {
        var res = new List<Dlg>();
        EnumWindows((h, l) => {
            if (!IsWindowVisible(h)) return true;
            var cls = new StringBuilder(256); GetClassName(h, cls, 256);
            if (cls.ToString() != "#32770") return true;
            var tt = new StringBuilder(512); GetWindowText(h, tt, 512);
            uint pid; GetWindowThreadProcessId(h, out pid);
            uint sid; ProcessIdToSessionId(pid, out sid);
            res.Add(new Dlg { Hwnd = h.ToInt64(), Pid = pid, Session = sid, Title = tt.ToString() });
            return true;
        }, IntPtr.Zero);
        return res;
    }
    public static bool Close(long hwnd) {
        // WM_CLOSE 比 WM_COMMAND(IDNO) 通用：不知道按钮 ID 时也能关。
        return PostMessage(new IntPtr(hwnd), 0x0010, IntPtr.Zero, IntPtr.Zero);
    }
}
"@

$mySession = [GuardNative]::MySession()
$consoleSession = [GuardNative]::WTSGetActiveConsoleSessionId()

# 只在子会话里干活。用户在父会话手动跑这个脚本时直接退出，不要误关他的对话框。
if ($mySession -eq $consoleSession) {
    if ($StateFile) {
        "skip: running in console session $mySession" | Out-File -FilePath $StateFile -Encoding utf8
    }
    exit 0
}

$closeEnabled = $CloseDialogs.Trim().ToLower() -in @('true', '1', 'yes', 'on')
$keep = @()
if ($KeepTitles) {
    $keep = $KeepTitles -split '[;,]' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
}

$closedTotal = 0
$started = Get-Date
$lastWrite = [datetime]::MinValue

function Write-State($beat) {
    if (-not $StateFile) { return }
    $payload = [ordered]@{
        pid = $PID
        session = $mySession
        console_session = $consoleSession
        close_dialogs = $closeEnabled
        closed_total = $closedTotal
        beat = $beat
        started = $started.ToString('HH:mm:ss')
        now = (Get-Date).ToString('HH:mm:ss')
    }
    try {
        ($payload | ConvertTo-Json -Compress) | Out-File -FilePath $StateFile -Encoding utf8
    } catch {}
}

Write-State 0

while ($true) {
    $elapsed = ((Get-Date) - $started).TotalSeconds
    if ($elapsed -gt $MaxSeconds) { break }

    if ($closeEnabled) {
        try {
            $dlgs = [GuardNative]::Dialogs()
        } catch {
            $dlgs = @()
        }
        foreach ($d in $dlgs) {
            # 只动本会话的窗口：跨会话 PostMessage 会因为窗口站隔离失败，
            # 这里显式筛一遍，好在日志里说清楚跳过了什么。
            if ($d.Session -ne $mySession) { continue }
            $skip = $false
            foreach ($k in $keep) {
                if ($d.Title -like "*$k*") { $skip = $true; break }
            }
            if ($skip) { continue }
            if ([GuardNative]::Close($d.Hwnd)) { $closedTotal++ }
        }
    }

    # 状态文件按秒写，避免每轮都碰磁盘
    if (((Get-Date) - $lastWrite).TotalSeconds -ge 1) {
        Write-State ($closedTotal)
        $lastWrite = Get-Date
    }
    Start-Sleep -Milliseconds $IntervalMs
}

Write-State $closedTotal
