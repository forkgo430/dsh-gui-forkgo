# 常驻 RDP 宿主 —— 持有 ActiveX 控件，让子会话一直活着。
#
# 参数：
#   -Password       Windows 密码（明文，从 stdin 读也行）
#   -ReadyFile      登录成功后写这个文件，父进程靠它判断就绪
#   -StopFile       看到这个文件就退出
#   -Width/-Height  子会话分辨率
#
# 退出时会断开连接，子会话随之销毁。

param(
    [string]$Password = "",
    [string]$ReadyFile = "",
    [string]$StopFile = "",
    [int]$Width = 1280,
    [int]$Height = 720
)

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

Write-Host "宿主启动: $DOMAIN\$USER  子会话 ${Width}x${Height}"

$form = New-Object System.Windows.Forms.Form
$form.Text = "DSH 隔离会话宿主"
$form.ClientSize = New-Object System.Drawing.Size($Width, $Height)
$form.StartPosition = 'Manual'
# 挪到屏幕外：宿主不该出现在用户眼前
$form.Location = New-Object System.Drawing.Point(-32000, -32000)
$form.ShowInTaskbar = $false

$ax = New-Object RdpAxHost
$ax.Dock = 'Fill'
$form.Controls.Add($ax)
$form.Show()
[System.Windows.Forms.Application]::DoEvents()

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
while ($true) {
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 200

    if ($StopFile -and (Test-Path $StopFile)) {
        Write-Host "收到停止信号，断开"
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
$form.Close()
$form.Dispose()
Write-Host "宿主结束"
