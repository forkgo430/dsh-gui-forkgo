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
            "Manage an isolated Windows child session so AI can operate the computer "
            "without stealing the user's mouse and keyboard. "
            "action='status' reports what is available (child session state, saved "
            "credential, bundled script runtimes, whether a session can be created). "
            "action='setup' shows the standard Windows credential dialog once and stores "
            "the password locally encrypted with the current user's DPAPI. "
            "action='inject' loads a DLL into the child session's explorer.exe "
            "(no SeDebugPrivilege needed for same-user processes). "
            "action='forget' deletes the stored credential. "
            "A child session only survives while its RDP host process lives, so keep "
            "the host running. UIA-based tools never move the real pointer and should "
            "be preferred when they can do the job."
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
            "message": f"未知动作 {act!r}，可用：status / setup / inject / forget。",
        }
