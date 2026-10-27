"""单实例调度锁。

背景：后台调度（告警扫描、清理任务）没有分布式锁时，多实例部署会重复推送告警、
重复清理文件，并让「15 秒重复下单幂等」失效。
当前生产是 systemd 单实例；这里用文件锁兜底：只有抢到锁的进程启动调度器，
其余实例只提供 HTTP 服务，不跑定时任务。

跨平台：Linux 用 fcntl.flock，Windows 用 msvcrt.locking；抢不到锁返回 False。
锁随进程退出自动释放（文件句柄关闭）。
"""

import os
import sys
import tempfile

_handle = None


def acquire(name="printpilot-scheduler"):
    """尝试获取单实例锁；成功返回 True 并持有句柄，失败返回 False。"""
    global _handle
    if _handle is not None:
        return True
    path = os.path.join(tempfile.gettempdir(), f"{name}.lock")
    handle = open(path, "a+")
    try:
        if sys.platform.startswith("win"):
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _handle = handle
    return True


def held():
    """当前进程是否持有调度锁。"""
    return _handle is not None


def release():
    """显式释放锁（测试/退出用）。"""
    global _handle
    if _handle is None:
        return
    try:
        _handle.close()
    finally:
        _handle = None
