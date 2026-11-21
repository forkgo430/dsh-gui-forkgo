"""Read-only MCP control ownership status tool."""

from mcp.types import ToolAnnotations

from windows_mcp.desktop.control import get_controller
from windows_mcp.infrastructure.thread_affinity import uia_worker_status


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="ControlStatus",
        description="查控制权与截图缓存的状态。返回：桌面控制归谁、是否启用、门禁策略、空闲超时与剩余等待秒数，以及 UIA worker 忙不忙、截图帧哈希缓存了几个区域、暖 worker 预热了几次。AI 控制没有时间上限——直到用户接管，或传 release=true 主动交还。设置是只读的，改了要重启服务。",
        annotations=ToolAnnotations(
            title="ControlStatus",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def control_status_tool(release: bool = False) -> dict:
        controller = get_controller()
        status = controller.release() if release else controller.status()
        worker = uia_worker_status()
        from windows_mcp.desktop.frame_cache import (
            get_display_watcher,
            get_frame_cache,
            warmer_stats,
        )
        return {
            **status,
            "ui_busy": worker["busy"],
            "uia_worker": worker,
            "capture": {
                "frame_cache": get_frame_cache().stats(),
                "warm_worker": warmer_stats(),
                "display_watcher": get_display_watcher().stats(),
            },
        }
