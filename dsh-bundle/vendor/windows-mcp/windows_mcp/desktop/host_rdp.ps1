# 常驻 RDP 宿主 —— 持有 ActiveX 控件，让子会话一直活着。
#
# 参数：
#   -Password       Windows 密码（明文，从 stdin 读也行）
#   -ReadyFile      登录成功后写这个文件，父进程靠它判断就绪
#   -StopFile       看到这个文件就退出
#   -Width/-Height  子会话分辨率
#   -Visible        是否把宿主窗口摆在屏幕上（true=看得见预览，false=挪到屏幕外）
#   -Left/-Top      可见时的窗口位置；不传就自动贴到工作区右下角
#
# 退出时会断开连接，子会话随之销毁。
#
# 为什么要能看见：子会话跑起来后，用户在 Session 1 里用 Screen 截到的还是自己的
# 桌面 —— 子会话的画面只存在于这个 RDP 控件内部。窗口一旦挪到屏幕外，AI 在里面
# 干什么用户完全看不见。豆包的「小窗口虚拟桌面」就是把这个控件摆在明面上。
#
# 为什么原来要藏起来：宿主窗口会抢焦点。但那是 AI 在子会话里操作，本来就碰不到
# 用户的键鼠；用户主动点预览窗才需要它拿到焦点。所以现在默认可见，靠
# -Visible:$false 还能退回旧行为。

param(
    [string]$Password = "",
    [string]$ReadyFile = "",
    [string]$StopFile = "",
    [int]$Width = 1280,
    [int]$Height = 720,
    # 为什么是 [string] 不是 [bool]：用 -File 从别的进程调用时，PowerShell 把所有
    # 参数都当字符串送进绑定器，而 [bool] 参数会直接拒绝 "1"/"$true" 这类字符串
    # （实测报 "无法将值 System.String 转换为 System.Boolean"）。
    # 收成字符串、自己解析，调用方传什么都行。
    [string]$Visible = "true",
    [int]$Left = -1,
    [int]$Top = -1
)

# 宽容解析：true/1/yes/on → 可见；其余（含空串）→ 不可见。
# 空串按不可见处理，因为那多半是调用方拼参数时漏了值，看得见比看不见更该显式要求。
$ShowWindow = $Visible.Trim().ToLower() -in @('true', '1', 'yes', 'on')

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.ComponentModel.Primitives -ErrorAction SilentlyContinue

Add-Type -Namespace W -Name T -MemberDefinition @'
[DllImport("wtsapi32.dll", SetLastError=true)] public static extern bool WTSGetChildSessionId(out uint id);
[DllImport("wtsapi32.dll", SetLastError=true)] public static extern bool WTSEnumerateProcessesW(IntPtr h, uint r, uint v, ref IntPtr p, ref uint c);
[DllImport("wtsapi32.dll")] public static extern void WTSFreeMemory(IntPtr p);
'@

$refs = @(
    [System.Windows.Forms.Form].Assembly.Location
    [System.Drawing.Point].Assembly.Location
    [System.ComponentModel.Component].Assembly.Location
    [System.Runtime.InteropServices.Marshal].Assembly.Location
)

$src = @'
using System;
using System.Runtime.InteropServices;
using System.Windows.Forms;

[ComImport, Guid("302D8188-0052-4807-806A-362B628F9AC5"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IRdpExt
{
    [PreserveSig] int put_Property([MarshalAs(UnmanagedType.BStr)] string n,
                                   [MarshalAs(UnmanagedType.Struct)] ref object v);
    [PreserveSig] int get_Property([MarshalAs(UnmanagedType.BStr)] string n,
                                   [MarshalAs(UnmanagedType.Struct)] out object v);
}

public class RdpAxHost : AxHost
{
    public RdpAxHost() : base("8b918b82-7985-4c24-89df-c33ad2bbfbcd") { }
    public object Ocx { get { return GetOcx(); } }

    const System.Reflection.BindingFlags SET = System.Reflection.BindingFlags.SetProperty;
    const System.Reflection.BindingFlags GET = System.Reflection.BindingFlags.GetProperty;
    const System.Reflection.BindingFlags INV = System.Reflection.BindingFlags.InvokeMethod;

    public string Set(object o, string p, object v)
    {
        try { o.GetType().InvokeMember(p, SET, null, o, new object[] { v }); return "ok"; }
        catch (Exception e) { var i = e.InnerException; return "EX " + (i != null ? i.Message : e.Message); }
    }
    public int GetI(object o, string p)
    {
        try { return Convert.ToInt32(o.GetType().InvokeMember(p, GET, null, o, null)); }
        catch { return -999; }
    }
    public string SetChild(object ocx, bool v)
    {
        try { var r = (IRdpExt)ocx; object o = v; int hr = r.put_Property("ConnectToChildSession", ref o);
              return "hr=0x" + hr.ToString("X8"); }
        catch (Exception e) { return "EX " + e.Message; }
    }
    public string Invoke(object o, string m)
    {
        try { o.GetType().InvokeMember(m, INV, null, o, null); return "ok"; }
        catch (Exception e) { var i = e.InnerException; return "EX " + (i != null ? i.Message : e.Message); }
    }
}
'@

Add-Type -TypeDefinition $src -ReferencedAssemblies $refs

function Get-Procs([int]$sid) {
    $p = [IntPtr]::Zero; $n = [uint32]0
    $names = @()
    if ([W.T]::WTSEnumerateProcessesW([IntPtr]::Zero, 0, 1, [ref]$p, [ref]$n)) {
        for ($i = 0; $i -lt $n; $i++) {
            $addr = [IntPtr]($p.ToInt64() + $i * 24)
            if ([System.Runtime.InteropServices.Marshal]::ReadInt32($addr) -eq $sid) {
                $np = [System.Runtime.InteropServices.Marshal]::ReadIntPtr([IntPtr]($addr.ToInt64() + 8))
                if ($np -ne [IntPtr]::Zero) {
                    $names += [System.Runtime.InteropServices.Marshal]::PtrToStringUni($np)
                }
            }
        }
        [W.T]::WTSFreeMemory($p)
    }
    return ($names | Sort-Object -Unique)
}

$USER = $env:USERNAME
$DOMAIN = $env:COMPUTERNAME

Write-Host "宿主启动: $DOMAIN\$USER  子会话 ${Width}x${Height}  可见=$ShowWindow"

$form = New-Object System.Windows.Forms.Form
$form.Text = "DSH 子会话 — AI 正在这里面操作"
$form.ClientSize = New-Object System.Drawing.Size($Width, $Height)
$form.StartPosition = 'Manual'

if ($ShowWindow) {
    # 预览窗：能拖动、能改大小、在任务栏上有一席之地。
    #
    # 标题栏必须留着。子会话的分辨率是固定的（DesktopWidth/Height 在连接前就定了），
    # 拖动边框改窗口大小只会让控件缩放或者留黑边，所以这里锁掉可调整边框——
    # FixedToolWindow 给的是小号标题栏 + 关闭按钮，省出来的高度都给了画面。
    $form.FormBorderStyle = 'FixedToolWindow'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $true
    $form.ShowInTaskbar = $true
    $form.TopMost = $true

    # 默认贴到主屏工作区右下角，留 24px 边距。
    if ($Left -lt 0 -or $Top -lt 0) {
        $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
        $frameW = $Width + 16   # 估上边框，精确定位在 Show() 之后再做一次
        $frameH = $Height + 39
        $Left = $wa.Right - $frameW - 24
        $Top = $wa.Bottom - $frameH - 24
    }
    $form.Location = New-Object System.Drawing.Point($Left, $Top)
} else {
    # 旧行为：挪到屏幕外，什么都不显示
    $form.FormBorderStyle = 'None'
    $form.ShowInTaskbar = $false
    $form.Location = New-Object System.Drawing.Point(-32000, -32000)
}

$ax = New-Object RdpAxHost
$ax.Dock = 'Fill'
$form.Controls.Add($ax)
$form.Show()
[System.Windows.Forms.Application]::DoEvents()

if ($ShowWindow) {
    # Show() 之后窗口才有真实外框尺寸，这时候再校正一次默认位置，
    # 免得边框估算差几像素把窗口顶出屏幕。
    $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $needW = $form.Width
    $needH = $form.Height
    $nx = [Math]::Max($wa.Left, $wa.Right - $needW - 24)
    $ny = [Math]::Max($wa.Top, $wa.Bottom - $needH - 24)
    $form.Location = New-Object System.Drawing.Point($nx, $ny)
    [System.Windows.Forms.Application]::DoEvents()
    Write-Host "预览窗位置: $($form.Location.X),$($form.Location.Y)  尺寸 $($form.Width)x$($form.Height)"
}

$ocx = $ax.Ocx
$W = $form.ClientSize.Width -band -bnot 1
$H = $form.ClientSize.Height -band -bnot 1

$ax.Set($ocx, "Server", "localhost") | Out-Null
$ax.Set($ocx, "DesktopWidth", $W) | Out-Null
$ax.Set($ocx, "DesktopHeight", $H) | Out-Null
$r = $ax.SetChild($ocx, $true)
Write-Host "ConnectToChildSession: $r"

$adv = $ocx.AdvancedSettings9
$ax.Set($adv, "EnableCredSspSupport", $true) | Out-Null
$ax.Set($adv, "SmartSizing", $true) | Out-Null
$ax.Set($adv, "RedirectClipboard", $true) | Out-Null
$ax.Set($adv, "RedirectDrives", $false) | Out-Null
$ax.Set($adv, "ClearTextPassword", $Password) | Out-Null
$ax.Set($ocx, "UserName", $USER) | Out-Null
$ax.Set($ocx, "Domain", $DOMAIN) | Out-Null

$sec = $ocx.SecuredSettings3
$ax.Set($sec, "KeyboardHookMode", 1) | Out-Null

Write-Host "Connect: $($ax.Invoke($ocx, 'Connect'))"

# 等登录完成（explorer 出现）
$sw = [Diagnostics.Stopwatch]::StartNew()
$ready = $false
while ($sw.Elapsed.TotalSeconds -lt 60) {
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 250
    $sid = [uint32]0
    if ([W.T]::WTSGetChildSessionId([ref]$sid)) {
        $procs = Get-Procs $sid
        if ($procs -match 'explorer') {
            $ready = $true
            Write-Host "登录完成，子会话 $sid，explorer 已起"
            if ($ReadyFile) {
                "$sid" | Out-File -FilePath $ReadyFile -Encoding utf8 -NoNewline
            }
            break
        }
    }
}

if (-not $ready) {
    Write-Host "60 秒内未登录成功，退出"
    try { $ax.Invoke($ocx, 'Disconnect') | Out-Null } catch {}
    $form.Close()
    exit 2
}

# 常驻：消息泵 + 检查停止文件
$alive = [Diagnostics.Stopwatch]::StartNew()
$userClosed = $false
while ($true) {
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 200

    if ($StopFile -and (Test-Path $StopFile)) {
        Write-Host "收到停止信号，断开"
        break
    }
    # 用户把预览窗关掉了。既要断开连接，也要把停止标记写出来 ——
    # 否则下一轮 start 看到的是「子会话还在」，会去复用一具尸体。
    if ($ShowWindow -and $form.IsDisposed) {
        Write-Host "用户关闭了预览窗，断开"
        $userClosed = $true
        if ($StopFile) {
            try { "closed-by-user" | Out-File -FilePath $StopFile -Encoding utf8 -NoNewline } catch {}
        }
        break
    }
    # 连接掉了就退出
    if ($ax.GetI($ocx, "Connected") -eq 0) {
        Write-Host "连接已断开，宿主退出"
        break
    }
    # 兜底：最长 8 小时
    if ($alive.Elapsed.TotalHours -gt 8) {
        Write-Host "达到最长存活时间，退出"
        break
    }
}

try { $ax.Invoke($ocx, 'Disconnect') | Out-Null } catch {}
if (-not $form.IsDisposed) {
    $form.Close()
    $form.Dispose()
}
Write-Host "宿主结束"
