"""给截图加帧哈希去重 + 暖 worker。

两件事：

1. **帧哈希去重**：同一块区域如果这一帧和上一帧像素完全相同，就没必要
   重新编码 PNG（编码是大头，几毫秒到几十毫秒）。在 capture() 出口挂一个
   弱引用缓存，记 (区域, 哈希) → 上一张 Image。调用方拿到的是同一批像素，
   但后续编码可以复用上次的字节。

2. **暖 worker**：第一次截图要初始化 DWM 刷新、DXGI 输出、相机对象，
   冷启动几百毫秒。开一个后台线程定期做一次空转 capture，让这些东西
   一直热着。

哈希用采样：全图逐像素太慢，按网格取 64x64 个点，对 1080p 约 4000 次
像素访问，微秒级。
"""
import hashlib
import threading
import time
from typing import Any

from PIL import Image


# ---------------------------------------------------------------- 帧哈希
def frame_hash(image: Image.Image, grid: int = 64) -> str:
    """对图像做网格采样哈希。

    性能要点（实测）：``convert("RGB")`` 对已经是 RGB 的图也会做一次完整
    拷贝，1080p 上要 37ms，比哈希本身贵十倍。所以只在模式不对时才转。
    ``resize`` 的 BOX 滤波器最快，而采样哈希不需要插值质量。
    """
    w, h = image.size
    if w == 0 or h == 0:
        return ""
    src = image if image.mode == "RGB" else image.convert("RGB")
    small = src.resize((grid, grid), Image.Resampling.BOX)
    return hashlib.blake2b(small.tobytes(), digest_size=16).hexdigest()


class FrameCache:
    """记住上一次捕获的 (区域键, 帧哈希, 图像)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[Any, tuple[str, Image.Image]] = {}

    @staticmethod
    def _key(capture_rect: Any) -> Any:
        if capture_rect is None:
            return None
        return (getattr(capture_rect, "left", None),
                getattr(capture_rect, "top", None),
                getattr(capture_rect, "right", None),
                getattr(capture_rect, "bottom", None))

    def check(self, capture_rect: Any, image: Image.Image) -> tuple[bool, str]:
        """返回 (是否与上一帧相同, 本次哈希)。"""
        key = self._key(capture_rect)
        digest = frame_hash(image)
        with self._lock:
            prev = self._last.get(key)
            same = bool(prev and prev[0] == digest)
            self._last[key] = (digest, image)
        return same, digest

    def last_digest(self, capture_rect: Any) -> str | None:
        """只读地取出某块区域**最近一次捕获**的帧哈希：不比较、不写入。

        为什么需要这个口子：Screen 要回答调用方「这一帧和上一次给你的一样吗」，
        而它能拿到的只有三种图 —— 画过标注框的、加了标尺的、或者已经被缩过的。
        对这三者做哈希都答不了那个问题：标注框的颜色是 ``random.randint`` 生成的，
        同一块静止的桌面每帧也会得到不同哈希；缩放比例一变哈希也跟着变。

        ``capture_with_hash()`` 记进来的才是**原始像素**的哈希，它只受屏幕内容
        影响。所以 Screen 从这条只读出口把它取出来用，既不多算一次哈希
        （3.1ms 也是钱），也不会被上层的画图/缩图污染判断。

        没有任何一帧记录时返回 ``None``（服务刚起、或者刚因为显示器布局变化
        清过缓存），调用方应当按「变化了」处理。
        """
        key = self._key(capture_rect)
        with self._lock:
            entry = self._last.get(key)
        return entry[0] if entry else None

    def clear(self) -> None:
        with self._lock:
            self._last.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"cached_regions": len(self._last)}


_frame_cache = FrameCache()


def get_frame_cache() -> FrameCache:
    return _frame_cache


# ---------------------------------------------------------------- 暖 worker
class WarmWorker:
    """后台空转截图，让截图后端保持热态。

    冷启动时的开销：DWM flush、DXGI 输出枚举、相机对象创建，加起来几百毫秒。
    用户点一下 Snapshot，如果正好赶上冷启动，就是肉眼可见的卡顿。
    这个线程每 interval 秒做一次最小区域捕获，把这些东西一直热着。
    """

    def __init__(self, capture_fn, interval: float = 2.0, warm_rect=None) -> None:
        self._capture = capture_fn
        self._interval = interval
        self._warm_rect = warm_rect
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warmups = 0
        self._errors = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="dsh-gui-warm-screenshot", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        # 第一次立刻做，把冷启动吃掉
        while not self._stop.is_set():
            try:
                self._capture(self._warm_rect)
                self._warmups += 1
            except Exception:
                self._errors += 1
            if self._stop.wait(self._interval):
                return

    def stats(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "warmups": self._warmups,
            "errors": self._errors,
            "interval": self._interval,
        }


_warmer: WarmWorker | None = None


def start_warmer(capture_fn, interval: float = 2.0, warm_rect=None) -> WarmWorker:
    global _warmer
    if _warmer is None:
        _warmer = WarmWorker(capture_fn, interval=interval, warm_rect=warm_rect)
    _warmer.start()
    return _warmer


def stop_warmer() -> None:
    global _warmer
    if _warmer is not None:
        _warmer.stop()
        _warmer = None


def warmer_stats() -> dict[str, Any]:
    return _warmer.stats() if _warmer else {"running": False}


# ---------------------------------------------------------------- DPI 重枚举
class DisplayWatcher:
    """盯着显示器布局与 DPI 缩放，变了就让缓存失效。

    为什么要有：``GetDisplays()`` 本身每次都真实调用 ``GetDpiForMonitor``，
    是实时的；但帧哈希缓存是按 (区域, 哈希) 记的，缩放一变，同一块区域
    的物理像素就换了含义，旧缓存会给出错误的「这一帧没变」判断。

    所以每次截图前问一句「显示器布局变了吗」，变了就丢掉全部缓存。
    检查本身很便宜（一次 EnumDisplayMonitors + 每显示器一次 GetDpiForMonitor），
    但没必要每帧都做，按 interval 节流。
    """

    def __init__(self, interval: float = 1.0) -> None:
        self._interval = interval
        self._last_check = 0.0
        self._signature: tuple | None = None
        self._changes = 0
        self._checks = 0

    @staticmethod
    def _read_signature() -> tuple | None:
        try:
            from windows_mcp import uia
            displays = uia.GetDisplays()
        except Exception:
            return None
        if not displays:
            return None
        out = []
        for d in displays:
            r = d.rect
            out.append((
                d.index,
                r.left, r.top, r.right, r.bottom,
                d.effective_dpi,
                round(d.scale, 4) if d.scale else None,
                d.orientation,
            ))
        return tuple(sorted(out))

    def changed(self) -> bool:
        """距上次检查超过 interval 才真去读，返回布局是否变了。"""
        now = time.monotonic()
        if now - self._last_check < self._interval:
            return False
        self._last_check = now
        self._checks += 1
        sig = self._read_signature()
        if sig is None:
            return False
        if self._signature is None:
            self._signature = sig
            return False
        if sig != self._signature:
            self._signature = sig
            self._changes += 1
            return True
        return False

    def stats(self) -> dict[str, Any]:
        return {
            "checks": self._checks,
            "changes": self._changes,
            "interval": self._interval,
            "displays": len(self._signature) if self._signature else 0,
        }


_display_watcher = DisplayWatcher()


def get_display_watcher() -> DisplayWatcher:
    return _display_watcher
