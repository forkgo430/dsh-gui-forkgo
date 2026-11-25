"""IsolatedSession 工具 —— 独立子会话管理。

七个动作：
  status   查当前能力（子会话、凭据、脚本运行时、能不能建会话、guard 死活）
  setup    弹 Windows 标准凭据框，存下密码（只做一次）
  start    起常驻宿主建子会话，默认摆出实时预览窗
  stop     断开宿主 + 注销会话 + 从 Run 键撤下 guard
  inject   往子会话的 explorer 注入一个 DLL
  guard    管理会话内守护，自动关 #32770 对话框
  forget   删掉存下的凭据

为什么要子会话：AI 在独立桌面里操作，用户的鼠标键盘完全不受影响。
代价是要一次 Windows 密码（存在本机，用当前账户 DPAPI 加密）。
"""

from collections.abc import Callable
from typing import Any

from fastmcp import Context
from mcp.types import ToolAnnotations
from windows_mcp.infrastructure import with_analytics

from windows_mcp.desktop import child_session


def _truthy(value: object, default: bool = True) -> bool:
    """把 bool / 字符串开关统一成 bool。

    MCP 客户端有时把布尔参数当字符串发（"false" / "0"），直接 bool("false")
    会得到 True —— 那会把用户明确关掉的功能又打开。这里显式列一遍假值。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("false", "0", "no", "off", ""):
            return False
        return True
    return bool(value)


def register(
    mcp: Any,
    *,
    get_desktop: Callable[[], Any],
    get_analytics: Callable[[], Any],
) -> None:
    @mcp.tool(
        name="IsolatedSession",
        description=(
            "管理一个隔离的 Windows 子会话，让 AI 操作电脑时不抢用户的鼠标键盘。"
            "action='status' 报告可用能力：子会话状态、已存凭据、内置脚本运行时、"
            "能不能建会话、宿主在不在跑。"
            "action='setup' 弹一次 Windows 标准凭据框，密码用当前账户的 DPAPI "
            "加密后存在本机，之后不再问。"
            "action='start' 起一个常驻宿主进程建子会话（默认 1280x720），"
            "等 explorer.exe 出现才算就绪，约 16 秒。宿主是独立进程，"
            "插件重载不影响它——子会话的存活就绑在它身上。"
            "start 会在主屏右下角摆一个预览窗，让用户看得见 AI 在里面操作什么："
            "窗口置顶、有标题栏和关闭按钮，可以拖动；点它会把焦点给宿主，"
            "但 AI 的动作发生在另一个会话里，碰不到用户的键鼠。"
            "传 visible=false 退回旧行为（窗口挪到屏幕外，完全不可见）；"
            "传 left/top 可以指定位置，例如挪到副屏。"
            "注意 Screen 截的是用户自己的桌面，看不到子会话内部——"
            "要看子会话只能看这个预览窗。"
            "action='stop' 让宿主断开，子会话随之销毁。"
            "action='inject' 把 DLL 注入子会话的 explorer.exe"
            "（同用户进程不需要 SeDebugPrivilege）。"
            "action='guard' 管理会话内守护：子会话里跑的代理，负责自动关掉 #32770 对话框"
            "（UAC、报错框、安装确认框这类会卡住自动化的东西）。父会话看不见也碰不到"
            "子会话的窗口，这层代理是唯一能碰到它们的手段。enable=true 挂载、false 卸下；"
            "close_dialogs 控制挂上之后关不关窗；keep_titles 用分号分隔标题白名单。"
            "start 默认已经自动挂载，一般不用单独调它。"
            "action='forget' 删掉存下的凭据。"
            "约束：一个系统同时只能有一个活动子会话；能用 UIA 控件模式做的事"
            "本来就不动真实指针，那些场景不用开子会话。"
        ),
        annotations=ToolAnnotations(
            title="IsolatedSession",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "IsolatedSession-Tool")
    def isolated_session_tool(
        action: str = "status",
        dll_path: str = "",
        width: int = 1280,
        height: int = 720,
        visible: bool | str = True,
        left: int = -1,
        top: int = -1,
        enable: bool | str = True,
        close_dialogs: bool | str = True,
        keep_titles: str = "",
        ctx: Context = None,
    ) -> dict[str, object]:
        act = (action or "status").strip().lower()

        if act == "status":
            caps = child_session.capabilities()
            result: dict[str, object] = {"action": "status", **caps}

            # guard 是唯一能看到子会话内部的通道，status 里必须如实报它的死活。
            result["guard"] = child_session.guard_status()

            # 子会话活着就补一份进程概况
            el = child_session.find_child_explorer()
            if el:
                sid, pid = el
                info = child_session.session_info(sid)
                procs = child_session.session_processes(sid)
                result["session_detail"] = {
                    "session_id": sid,
                    "explorer_pid": pid,
                    "state": info.get("state"),
                    "user": info.get("user"),
                    "domain": info.get("domain"),
                    "protocol": info.get("protocol"),
                    "is_remote": info.get("is_remote"),
                    "process_count": len(procs),
                    "has_explorer": any(
                        n.lower() == "explorer.exe" for _, n in procs),
                    # 用户在任务栏 / 屏幕上找得到那个预览窗吗
                    "preview_window": child_session.preview_window(),
                }
            else:
                result["session_detail"] = None
                result["hint"] = (
                    "没有活动的子会话。建会话需要先 setup 存密码，"
                    "然后由一个常驻的 RDP 宿主进程发起连接——"
                    "子会话的存活依赖那个宿主。"
                    if not child_session.load_credential()
                    else "已存密码，但还没有宿主进程建会话。"
                )
            return result

        if act == "setup":
            existing = child_session.load_credential()
            if existing:
                ok, err = child_session.verify_credential(existing)
                if ok:
                    return {
                        "action": "setup",
                        "already_saved": True,
                        "credential": str(existing),
                        "verified": True,
                        "message": "凭据已存在且验证通过，不用重复设置。",
                    }
                # 存的不对了，重来
            cred = child_session.prompt_credential()
            if cred is None:
                return {
                    "action": "setup",
                    "cancelled": True,
                    "message": "用户取消了凭据输入。",
                }
            ok, err = child_session.verify_credential(cred)
            if not ok:
                hints = {
                    1326: "用户名或密码不对。",
                    1909: "账户当前被锁定，等一会儿再试。",
                    1330: "密码已过期。",
                    1327: "账户限制（可能是空密码策略）。",
                }
                return {
                    "action": "setup",
                    "saved": False,
                    "verified": False,
                    "error_code": err,
                    "message": hints.get(err, f"密码验证失败，错误码 {err}。"),
                }
            child_session.save_credential(cred.user, cred.domain, cred.password)
            return {
                "action": "setup",
                "saved": True,
                "verified": True,
                "credential": str(cred),
                "message": "密码已验证并加密保存到本机凭据管理器。",
            }

        if act == "inject":
            if not dll_path:
                return {
                    "action": "inject",
                    "ok": False,
                    "message": "需要 dll_path 参数。",
                }
            el = child_session.find_child_explorer()
            if not el:
                return {
                    "action": "inject",
                    "ok": False,
                    "message": "没有活动的子会话（找不到 explorer.exe）。",
                }
            sid, pid = el
            ok, msg = child_session.inject_dll(pid, dll_path)
            name = dll_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            return {
                "action": "inject",
                "ok": ok,
                "session_id": sid,
                "target_pid": pid,
                "dll": name,
                "module_present": child_session.module_loaded(pid, name) if ok else False,
                "message": msg,
            }

        if act == "start":
            # visible / guard 相关开关都允许传字符串，MCP 客户端有时候不做类型转换。
            if isinstance(visible, str):
                show = visible.strip().lower() not in ("false", "0", "no", "off", "")
            else:
                show = bool(visible)
            ok, msg, detail = child_session.start_host(
                width=int(width), height=int(height),
                visible=show, left=int(left), top=int(top),
                guard=_truthy(enable),
                guard_close_dialogs=_truthy(close_dialogs),
                guard_keep_titles=keep_titles or "")
            return {
                "action": "start",
                "ok": ok,
                "message": msg,
                "session": detail,
            }

        if act == "guard":
            # 单独管理守护：start 已经默认挂载，这个 action 用于事后开关，
            # 或者在 guard 被别的东西撤掉之后重新挂上。
            if not _truthy(enable):
                removed = child_session.disarm_guard()
                return {
                    "action": "guard",
                    "armed": False,
                    "removed": removed,
                    "message": ("已从 Run 键撤下 guard，下次建会话不会再启动它。"
                                if removed else "Run 键上没有 guard，无需撤下。"),
                }
            ok, msg = child_session.arm_guard(
                close_dialogs=_truthy(close_dialogs),
                keep_titles=keep_titles or "")
            return {
                "action": "guard",
                "armed": ok,
                "ok": ok,
                "message": msg,
                "hint": ("guard 是 Run 键拉起的，只对**之后新建**的子会话生效；"
                         "当前会话里已经在跑的那个不会因为改配置而重载。"
                         if ok else None),
                "status": child_session.guard_status(),
            }

        if act == "stop":
            ok, msg = child_session.stop_host()
            return {"action": "stop", "ok": ok, "message": msg}

        if act == "forget":
            removed = child_session.delete_credential()
            return {
                "action": "forget",
                "removed": removed,
                "message": ("已删除保存的凭据。" if removed
                            else "没有找到保存的凭据。"),
            }

        return {
            "action": act,
            "ok": False,
            "message": f"未知动作 {act!r}，可用：status / setup / start / stop / inject / forget。",
        }
