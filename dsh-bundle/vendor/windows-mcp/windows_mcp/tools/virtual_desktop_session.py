"""IsolatedSession 工具 —— 独立子会话管理。

四个动作：
  status   查当前能力（子会话、凭据、脚本运行时、能不能建会话）
  setup    弹 Windows 标准凭据框，存下密码（只做一次）
  inject   往子会话的 explorer 注入一个 DLL
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
            "action='stop' 让宿主断开，子会话随之销毁。"
            "action='inject' 把 DLL 注入子会话的 explorer.exe"
            "（同用户进程不需要 SeDebugPrivilege）。"
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
        ctx: Context = None,
    ) -> dict[str, object]:
        act = (action or "status").strip().lower()

        if act == "status":
            caps = child_session.capabilities()
            result: dict[str, object] = {"action": "status", **caps}

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
            ok, msg, detail = child_session.start_host(
                width=int(width), height=int(height))
            return {
                "action": "start",
                "ok": ok,
                "message": msg,
                "session": detail,
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
