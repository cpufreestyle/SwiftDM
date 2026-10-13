#!/usr/bin/env python3

"""单实例锁 —— 阻止 SwiftDM 被重复启动。

参考 qBittorrent：它在 Windows 上用 QtLockedFile 建互斥锁，另有
`Application/InstanceName` 与 /lockfile 两道兜底。重复启动会让多个进程同时抢
同一个下载目录、同一批 part_XXXX 分片文件，后启动的实例还会把先来的实例刚
合并好的成品文件写坏（分段线程各自持有文件句柄，互不知情）。

实现选命名互斥体而非 lock 文件：句柄随进程退出由内核自动释放，进程崩溃或
被任务管理器杀掉时不会留下 stale lock 文件，也就没有「进程没了文件还在」
的误判要处理。

检测失败一律放行——拿不到锁只说明已有实例在跑（调用方自行决定提示与退出），
而创建锁本身出错（权限不足等）不能成为阻止用户启动软件的理由。
"""
import ctypes
import logging

logger = logging.getLogger("SwiftDM")

ERROR_ALREADY_EXISTS = 183
# Global\ 前缀让锁跨登录会话可见：否则管理员权限启动的实例与普通实例
# 分属不同会话，互相看不到对方的锁，多实例照样起得来。
MUTEX_NAME = "Global" + chr(92) + "SwiftDM.SingleInstance.v1"

_mutex_handle = None


def already_running():
    """检测是否已有 SwiftDM 实例在运行。

    返回 True：已有实例，调用方应提示并退出。
    返回 False：本进程是首个实例；此时内部已持有互斥体，调用方无需也无法
    主动释放（模块级变量保活句柄，直到进程退出）。
    任何异常都保守返回 False，避免锁机制自身的问题把软件挡在门外。
    """
    global _mutex_handle
    if _mutex_handle:
        return False  # 本进程已经持有锁（重复调用），直接放行

    try:
        # use_last_error 必须开：否则 ctypes 在调用之间插入自己的 stub 代码，
        # 会把 GetLastError 的真实返回值覆盖掉，导致误判成「没有同名互斥体」。
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p

        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            logger.warning("CreateMutexW 失败，跳过单实例检测")
            return False

        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            # 已有实例：关掉刚拿到的句柄（它不是我们的锁），让调用方去提示用户
            kernel32.CloseHandle(handle)
            return True

        # 关键：句柄存模块级变量，保证活到进程结束。局部变量会在函数返回后被 GC，
        # 句柄关闭等于锁释放，检测就失效了。
        _mutex_handle = handle
        return False
    except AttributeError:
        # 非 Windows 平台（无 WinDLL）：开发与测试环境跳过检测
        return False
    except Exception as e:
        logger.warning("单实例检测失败（忽略，按首个实例处理）: %s", e)
        return False


