# 常驻 RDP 宿主 —— 持有 ActiveX 控件，让子会话一直活着。
#
# 参数：
#   -Password       Windows 密码（明文，从 stdin 读也行）
#   -ReadyFile      登录成功后写这个文件，父进程靠它判断就绪
#   -StopFile       看到这个文件就退出
#   -Width/-Height  子会话分辨率
#   -Visible        是否把宿主窗口摆在屏幕上（true=看得见预览，false=挪到屏幕外）
#   -Left/-Top      可见时的窗口位置；不传就自动贴到工作区右下角
#   -SessionMode    子会话接入方式：child（默认，ConnectToChildSession）
#                   或 shadow（照豆包 pip_main.exe 的序列试 ConnectToShadowSession）
#
# -SessionMode shadow 的来路与结论：
#   豆包 pip_main.exe 的日志（rdp_host.cc）记的是
#     Disconnect child session → put Server → put_AllowPromptingForCredentials(false)
#     → put ConnectToShadowSession → Connect
#   并且它二进制里有 28 处 "ConnectToShadowSession" 字符串，用的是
#   IMsRdpExtendedSettings（即本脚本 IRdpExt 那个 {302D8188-...} 接口）的 put_Property。
#   本机照着做三处前置全试过，结果是：
#     put_Property("ConnectToChildSession")  = 0x00000000   ✅
#     put_Property("ConnectToShadowSession") = 0x8000FFFF   ❌
#     put_Property("EnableChildSessions")    = 0x8000FFFF   ❌
#     put_Property("AllowPromptingForCredentials") = 0x8000FFFF ❌
#   而且 C:\Windows\System32\mstscax.dll（10.0.19041.4522）里 UTF-16 和 ASCII
#   都搜不到 "ConnectToShadowSession" / "EnableChildSessions"，
#   唯一存在的是 "ConnectToChildSession"（映射 TS_PROP_CONNECT_TO_CHILD_SESSION）。
#   0x8000FFFF 是 E_UNEXPECTED，这里等于「这个版本不认这个属性名」。
#   所以 shadow 在本机拿不到；本脚本仍然把序列完整实现并逐个打出 hr，
#   一旦属性被接受就走 shadow，任何一步失败就**自动回落 child**（child 路径一行没动）。
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
    [int]$Top = -1,
    # child = 现有 ConnectToChildSession；shadow = 豆包那条 ConnectToShadowSession。
    # 收成字符串的理由同 -Visible：-File 调用时参数都按字符串绑定。
    [string]$SessionMode = "child"
)

# 宽容解析，认不出的一律当 child —— 这是默认且唯一验证能跑的那条。
$Mode = $SessionMode.Trim().ToLower()
if ($Mode -ne 'shadow') { $Mode = 'child' }

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
[DllImport("wtsapi32.dll", SetLastError=true)] public static extern bool WTSIsChildSessionsEnabled(out bool enabled);
[DllImport("wtsapi32.dll", SetLastError=true)] public static extern bool WTSEnableChildSessions(bool enable);
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
    // 通用版：豆包那串前置全走这个（ConnectToShadowSession / EnableChildSessions /
    // AllowPromptingForCredentials），名字和值都由调用方给。
    // 单独留 SetChild 是因为它是唯一 hr=0 的成功路径，不想让它跟新代码一起回归。
    public string Put(object ocx, string name, object v)
    {
        try { var r = (IRdpExt)ocx; int hr = r.put_Property(name, ref v);
              return "hr=0x" + hr.ToString("X8"); }
        catch (Exception e) { var i = e.InnerException; return "EX " + (i != null ? i.Message : e.Message); }
    }
    public string GetProp(object ocx, string name)
    {
        try { var r = (IRdpExt)ocx; object v = null; int hr = r.get_Property(name, out v);
              return "hr=0x" + hr.ToString("X8") + " v=" + (v == null ? "<null>" : v.ToString()); }
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

# ---------------------------------------------------------------- 接入方式
# child 路径保持原样（一行没动），shadow 是照豆包序列新加的。
# shadow 任何一步的 hr 不是 0 就整段作废、回落 child —— 不允许出现
# 「shadow 半配好、child 也没打开」这种两个都没成的中间态。
$accessMode = "child"

if ($Mode -eq 'shadow') {
    Write-Host "SessionMode=shadow，按 pip_main 的序列尝试"

    # 前置 1：WTS 层的子会话开关。豆包日志里 WTSIsChildSessionsEnabled /
    # WTSEnableChildSessions 都调了；这里两个都试（WTS API + 控件属性）。
    $en = $false
    $hrEn = [W.T]::WTSIsChildSessionsEnabled([ref]$en)
    Write-Host "  WTSIsChildSessionsEnabled: ret=$hrEn enabled=$en"
    if (-not $en) {
        $rEn = [W.T]::WTSEnableChildSessions($true)
        Write-Host "  WTSEnableChildSessions(true): ret=$rEn"
    }
    # 控件上的同名属性。本机这个版本不认，返回 0x8000FFFF，只记录不中断：
    # 真正的判定标准是下面 ConnectToShadowSession 能不能被接受。
    Write-Host "  put EnableChildSessions: $($ax.Put($ocx, 'EnableChildSessions', $true))"

    # 前置 2：连接前先 Disconnect 一次（豆包日志里的 "Disconnect child session"）。
    # 控件刚创建时断开是空操作，失败很正常，不当作错误。
    Write-Host "  Disconnect(预热): $($ax.Invoke($ocx, 'Disconnect'))"

    # 前置 3：禁掉凭据提示，再设 shadow。
    # AllowPromptingForCredentials 在 mstscax 里是个脚本属性名（本机
    # AdvancedSettings9 上没有，DISP_E_UNKNOWNNAME），所以两条路各试一遍。
    Write-Host "  put AllowPromptingForCredentials: $($ax.Put($ocx, 'AllowPromptingForCredentials', $false))"
    Try { Write-Host "  adv.AllowPromptingForCredentials: $($ax.Set($ocx.AdvancedSettings9, 'AllowPromptingForCredentials', $false))" }
    Catch { Write-Host "  adv.AllowPromptingForCredentials: 不可用" }

    $hrShadow = $ax.Put($ocx, 'ConnectToShadowSession', $true)
    Write-Host "  ConnectToShadowSession: $hrShadow"

    # get 一次确认属性真的被吃进去了（有些属性 put 返回 0 但内部丢弃）
    $chk = $ax.GetProp($ocx, 'ConnectToShadowSession')
    Write-Host "  readback ConnectToShadowSession: $chk"

    if ($hrShadow -eq 'hr=0x00000000' -and $chk -like '*v=True*') {
        $accessMode = "shadow"
        Write-Host "  → shadow 被接受，走 shadow"
    } else {
        # 回落：把 child 打开。shadow 的失败已经打在上面了，这里只补一句结论。
        Write-Host "  → shadow 不可用（本机 mstscax 10.0.19041.4522 无此扩展属性），回落 child"
        $rChild = $ax.SetChild($ocx, $true)
        Write-Host "  ConnectToChildSession: $rChild"
        $accessMode = "child(shadow 回落)"
        if ($rChild -ne 'hr=0x00000000') {
            Write-Host "  回落也失败，无法建立任何子会话"
        }
    }
} else {
    $r = $ax.SetChild($ocx, $true)
    Write-Host "ConnectToChildSession: $r"
}

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
            Write-Host "登录完成，子会话 $sid，explorer 已起  (接入方式: $accessMode)"
            if ($ReadyFile) {
                # 内容从「纯 session id」扩成两行：第一行 id（老解析照样能用），
                # 第二行是实际生效的接入方式，父进程靠它回报 shadow 到底成没成。
                "$sid`n$accessMode" | Out-File -FilePath $ReadyFile -Encoding utf8 -NoNewline
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
