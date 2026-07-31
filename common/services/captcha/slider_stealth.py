"""
Playwright滑块验证服务

基于Playwright实现滑块验证，支持反检测和轨迹学习
复刻原始 utils/xianyu_slider_stealth.py 的核心逻辑

CAPTCHA_HUMAN_TRAIL 模式：
  复刻真实鼠标引擎的浏览器环境（channel=chrome, no_viewport, real_mouse_shared 目录），
  加载真人录制轨迹用 CDP page.mouse 回放，不走 DrissionPage 兜底。
"""
from __future__ import annotations

import glob
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

from common.services.captcha.concurrency import concurrency_manager, account_browser_lock_manager
from common.services.captcha.strategy_stats import strategy_stats
from common.services.captcha.browser_features import get_random_browser_features, get_stealth_script
from common.services.captcha.trajectory import TrajectoryGenerator
from common.services.captcha.slider_elements import SliderElementFinder
from common.services.captcha.verification_checker import VerificationChecker
from common.services.captcha.history_manager import HistoryManager
from common.services.captcha.playwright_touch import (
    SLIDER_USER_AGENT,
    configure_slider_user_agent,
    dispatch_touch_drag,
)
from common.utils.browser_utils import ensure_playwright_browser_path, get_chromium_executable_path, is_frozen

try:
    from patchright.sync_api import sync_playwright, Page, Browser, BrowserContext, ElementHandle
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Page = Any
    Browser = Any
    BrowserContext = Any
    ElementHandle = Any
    logger.warning("Playwright 未安装")

try:
    from patchright.sync_api import sync_playwright as patchright_sync_playwright
    PATCHRIGHT_AVAILABLE = True
except ImportError:
    patchright_sync_playwright = None
    PATCHRIGHT_AVAILABLE = False


# url_provider 回调可返回的特殊哨兵值：表示"重新请求时 token 已可用、风控已解除，
# 根本不需要滑块验证"。run()/编排层据此提前结束，避免无谓地导航到失效链接或启动兜底引擎。
CAPTCHA_NOT_REQUIRED = "__CAPTCHA_NOT_REQUIRED__"

# 返回值哨兵：表示"验证链接已过期（页面显示'抱歉，页面访问出现了问题'），且本端无法
# 自助重取新链接"。作为 run()/solve() 返回元组中 cookies 位置的特殊值，由编排层识别后
# 以 engine='url_expired' 上报，最终让远程调用方据此刷新 URL 后重试。
URL_EXPIRED = "__URL_EXPIRED__"


def _captcha_remote_cdp_url() -> str:
    """读取发布与验证码共用的宿主 Chrome CDP 地址。"""
    return (
        os.environ.get("CAPTCHA_REMOTE_CDP_URL")
        or os.environ.get("PUBLISH_REMOTE_CDP_URL")
        or ""
    ).strip()


def _captcha_remote_cdp_enabled() -> bool:
    value = os.environ.get("CAPTCHA_REMOTE_CDP_ENABLED", "").strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    return bool(_captcha_remote_cdp_url())


def _captcha_remote_cdp_fallback() -> bool:
    return os.environ.get("CAPTCHA_REMOTE_CDP_FALLBACK_TO_LOCAL", "true").strip().lower() not in (
        "false", "0", "no"
    )


# 与真实鼠标模式保持一致的最小注入：仅隐藏 webdriver 并记录坐标校准事件。
_STEALTH_MINIMAL = """
try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true }); } catch (e) {}
try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
try { delete window.__playwright; delete window.__pw_manual; delete window.__PW_inspect; } catch (e) {}
"""

_CAP_JS = r"""
(() => {
  if (window.__cal) return;
  window.__cal = [];
  document.addEventListener('mousemove', e => {
    window.__cal.push([e.clientX, e.clientY, e.screenX, e.screenY, e.timeStamp, e.buttons]);
  }, true);
})();
"""


# ── 真人轨迹回放模式 ──────────────────────────────────────────────────────

# 真人鼠标模式专用固定目录（与 real_mouse_slider.py 一致）
_HUMAN_TRAIL_BROWSER_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "browser_data", "real_mouse_shared")
)

# 真人轨迹模式专用的精简浏览器参数（复刻 real_mouse_slider.py 的 _BROWSER_ARGS）
_HUMAN_TRAIL_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--force-color-profile=srgb",
    "--lang=zh-CN",
    "--start-maximized",
]


def _is_human_trail_enabled() -> bool:
    """读取 CAPTCHA_HUMAN_TRAIL 开关。"""
    env_val = os.environ.get("CAPTCHA_HUMAN_TRAIL", "").lower()
    if env_val in ("true", "1", "yes"):
        return True
    if env_val in ("false", "0", "no"):
        return False
    try:
        from app.core.config import get_settings
        return bool(getattr(get_settings(), "captcha_human_trail_enabled", False))
    except Exception:
        pass
    try:
        from common.core.config import get_settings
        return bool(getattr(get_settings(), "captcha_human_trail_enabled", False))
    except Exception:
        return False


def _trails_dir() -> str:
    """真人轨迹样本目录。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "human_trails")


def _load_human_trail_drags() -> List[List[Tuple[float, float, float]]]:
    """加载真人通过的业务滑块轨迹（与 real_mouse_slider.py 逻辑一致）。"""
    pattern = "human_trail_pass_*.json"
    files = sorted(glob.glob(os.path.join(_trails_dir(), pattern)))
    preferred_name = "human_trail_pass_1783949589.json"
    preferred_path = os.path.join(_trails_dir(), preferred_name)
    if os.path.isfile(preferred_path):
        files = [preferred_path] + [f for f in files if f != preferred_path]

    drags: List[List[Tuple[float, float, float]]] = []
    for f in files:
        try:
            data = json.load(open(f, encoding="utf-8"))
            trail = data.get("trail", [])
        except Exception as e:
            logger.warning(f"加载真人轨迹失败 {f}: {e}")
            continue
        moves = [e for e in trail if isinstance(e, list) and len(e) >= 5 and e[0] == "mousemove"]
        seg = [e for e in moves if e[4] == 1]
        if len(seg) < 5:
            continue
        while len(seg) > 2:
            gap = seg[-1][3] - seg[-2][3]
            dx = seg[-1][1] - seg[-2][1]
            dy = seg[-1][2] - seg[-2][2]
            if gap > 250.0 and abs(dx) <= 1.0 and abs(dy) <= 1.0:
                seg.pop()
                continue
            break
        x0, y0, prev = seg[0][1], seg[0][2], seg[0][3]
        rel: List[Tuple[float, float, float]] = []
        for p in seg:
            dt = max(0.0, min(180.0, p[3] - prev))
            rel.append((p[1] - x0, p[2] - y0, dt))
            prev = p[3]
        duration_ms = sum(pt[2] for pt in rel)
        distance = rel[-1][0]
        if len(rel) < 20 or duration_ms < 350 or duration_ms > 2600:
            continue
        if distance < 120 or distance > 1200:
            continue
        drags.append(rel)
    return drags


def _scale_trail_to_distance(
    drag: List[Tuple[float, float, float]], distance: float,
) -> List[Tuple[float, float, float]]:
    if not drag or distance <= 0 or drag[-1][0] <= 1:
        return drag
    factor = distance / drag[-1][0]
    scaled = [(dx * factor, dy, dt) for dx, dy, dt in drag]
    scaled[-1] = (distance, scaled[-1][1], scaled[-1][2])
    return scaled


# 上次选中的轨迹索引，用于避免连续选同一条
_last_trail_index = -1


def _choose_trail_drag(drags: List[List[Tuple[float, float, float]]]) -> List[Tuple[float, float, float]]:
    """选择轨迹，避免连续选同一条（轮换策略）。"""
    global _last_trail_index
    if len(drags) <= 1:
        _last_trail_index = 0
        return drags[0] if drags else []
    # 排除上次选中的，从剩余中随机选
    candidates = [i for i in range(len(drags)) if i != _last_trail_index]
    chosen = random.choice(candidates)
    _last_trail_index = chosen
    return drags[chosen]


class PlaywrightSliderService:
    """Playwright滑块验证服务"""

    # 浏览器启动参数
    BROWSER_ARGS = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas",
        "--no-first-run",
        "--no-zygote",
        "--disable-gpu",
        "--disable-web-security",
        "--disable-features=VizDisplayCompositor",
        "--start-maximized",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--disable-plugins",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-translate",
        "--hide-scrollbars",
        "--mute-audio",
        "--no-default-browser-check",
        "--disable-logging",
        "--disable-permissions-api",
        "--disable-notifications",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-hang-monitor",
        "--disable-client-side-phishing-detection",
        "--disable-component-extensions-with-background-pages",
        "--disable-background-mode",
        "--disable-domain-reliability",
        "--disable-features=TranslateUI",
        "--disable-ipc-flooding-protection",
        "--disable-field-trial-config",
        "--disable-background-networking",
        "--disable-back-forward-cache",
        "--disable-breakpad",
        "--disable-component-update",
        "--force-color-profile=srgb",
        "--metrics-recording-only",
        "--password-store=basic",
        "--use-mock-keychain",
        "--no-service-autorun",
        "--export-tagged-pdf",
        "--disable-search-engine-choice-screen",
        "--unsafely-disable-devtools-self-xss-warnings",
        "--allow-pre-commit-input"
    ]

    # Token 滑块专用参数：参考真人模式保留 Chromium 原生能力，避免大量禁用项形成
    # 明显的自动化环境指纹。密码登录仍沿用上面的历史参数，不受本次调整影响。
    SLIDER_BROWSER_ARGS = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    def __init__(
        self,
        user_id: str = "default",
        enable_learning: bool = True,
        headless: bool = True
    ):
        """
        初始化滑块验证服务
        
        Args:
            user_id: 用户ID
            enable_learning: 是否启用轨迹学习
            headless: 是否无头模式
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError("Playwright 未安装，请运行: pip install playwright && playwright install chromium")

        self.user_id = user_id
        self.enable_learning = enable_learning
        # 验证码浏览器单独由 CAPTCHA_BROWSER_HEADLESS 控制；未配置时尊重调用方。
        # 不再让 websocket 的通用 BROWSER_HEADLESS 覆盖 caller 传入的 headed 模式。
        captcha_headless = os.environ.get("CAPTCHA_BROWSER_HEADLESS", "").strip().lower()
        if captcha_headless in ("true", "1", "yes"):
            headless = True
            logger.info(f"【{user_id}】CAPTCHA_BROWSER_HEADLESS=true，使用无头验证码浏览器")
        elif captcha_headless in ("false", "0", "no"):
            headless = False
            logger.info(f"【{user_id}】CAPTCHA_BROWSER_HEADLESS=false，使用有头验证码浏览器")
        self.headless = headless

        self.pure_user_id = concurrency_manager._extract_pure_user_id(user_id)

        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
self._remote_cdp_mode = False
        self._cdp_context_owned = False
        self._slide_response_code: Optional[int] = None
        self._cdp_touch_enabled = False
        self._slider_profile_tempdir: Optional[tempfile.TemporaryDirectory] = None

        # 真人轨迹回放模式：复刻真实鼠标引擎的共享浏览器目录
        self._human_trail_mode = _is_human_trail_enabled()
        if self._human_trail_mode:
            self.user_data_dir = _HUMAN_TRAIL_BROWSER_DIR
            logger.info(f"【{self.pure_user_id}】真人轨迹回放模式：使用共享浏览器目录 {self.user_data_dir}")
        else:
            # 持久化用户数据目录（参照旧框架）
            self.user_data_dir = os.path.join(os.getcwd(), 'browser_data', f'user_{self.pure_user_id}')
        os.makedirs(self.user_data_dir, exist_ok=True)
        logger.debug(f"【{self.pure_user_id}】使用用户数据目录: {self.user_data_dir}")

        # 初始化子模块
        self.trajectory_generator = TrajectoryGenerator(user_id)
        self.history_manager = HistoryManager(user_id, enable_learning)

        # 等待并发槽位（阻塞直到有空闲槽位）
        logger.info(f"【{self.pure_user_id}】检查并发限制...")
        if not concurrency_manager.wait_for_slot(self.user_id):
            stats = concurrency_manager.get_stats()
            raise Exception(f"滑块验证等待槽位超时，当前活跃: {stats['active_count']}/{stats['max_concurrent']}")

        # 标记槽位已获取，用于close时释放
        self._slot_acquired = True
        self._account_lock_acquired = False

        # 获取账号级互斥锁（防止同账号并发占用同一 user_data_dir 导致 Chrome PROFILE_IN_USE）
        # 超时时间复用全局槽位的等待超时，避免长时间阻塞
        try:
            account_lock_timeout = float(getattr(concurrency_manager, 'wait_timeout', 120) or 120)
        except Exception:
            account_lock_timeout = 120.0
        logger.info(
            f"【{self.pure_user_id}】获取账号级浏览器互斥锁（超时 {account_lock_timeout:.0f} 秒）..."
        )
        if not account_browser_lock_manager.acquire(self.pure_user_id, timeout=account_lock_timeout):
            # 获取账号锁失败，释放已获取的全局槽位后抛出
            try:
                concurrency_manager.unregister_instance(self.user_id)
                self._slot_acquired = False
            except Exception:
                pass
            raise Exception(
                f"账号 {self.pure_user_id} 已被另一个浏览器实例占用，等待 {account_lock_timeout:.0f} 秒未释放"
            )
        self._account_lock_acquired = True
        logger.info(f"【{self.pure_user_id}】账号级浏览器互斥锁已获取")

    def _setup_browser_path(self):
        """设置浏览器路径环境变量，解决 exe 打包后找不到浏览器的问题"""
        if not is_frozen():
            return

        browser_dir = ensure_playwright_browser_path()
        if browser_dir:
            logger.info(f"【{self.pure_user_id}】设置浏览器路径: {browser_dir}")
            return

        logger.warning(f"【{self.pure_user_id}】未找到 Playwright 浏览器目录，请先安装浏览器")

    def _find_browser_executable(self) -> Optional[str]:
        """定位 Chromium 可执行文件路径。"""
        if not is_frozen():
            return None
        browser_package = "patchright" if self._cdp_touch_enabled else "playwright"
        return get_chromium_executable_path(
            browser_package,
            strict_revision=self._cdp_touch_enabled,
        )

    def _clean_singleton_lock_files(self) -> None:
        """清理 user_data_dir 中残留的 Chrome Singleton 锁文件。

        Chrome 启动时会在 user_data_dir 创建 SingletonLock / SingletonCookie / SingletonSocket
        三个锁（Windows 上同名为普通文件，Linux 上为符号链接）。如果上一次浏览器进程没有干净退出，
        这些文件会残留，导致下次 launch_persistent_context 立即以 exit code 21 (PROFILE_IN_USE) 失败。

        本方法仅在已持有账号级互斥锁（``self._account_lock_acquired == True``）的前提下被调用，
        因此可以安全地认为：当前 user_data_dir 不可能有别的合法 Chrome 实例正在使用，
        发现的任何 Singleton* 文件都是孤儿，删除即可。
        """
        try:
            if not getattr(self, 'user_data_dir', None) or not os.path.isdir(self.user_data_dir):
                return

            for fname in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                fpath = os.path.join(self.user_data_dir, fname)
                # 同时处理普通文件和符号链接（os.path.exists 对已断的链接返回 False）
                exists = os.path.exists(fpath) or os.path.islink(fpath)
                if not exists:
                    continue
                try:
                    if os.path.islink(fpath):
                        os.unlink(fpath)
                    else:
                        os.remove(fpath)
                    logger.warning(
                        f"【{self.pure_user_id}】已清理残留 Chrome 锁文件: {fpath}"
                    )
                except Exception as inner_e:
                    logger.warning(
                        f"【{self.pure_user_id}】清理 {fname} 失败（可忽略）: {inner_e}"
                    )
        except Exception as e:
            logger.warning(f"【{self.pure_user_id}】清理 Singleton 锁文件时出错（可忽略）: {e}")

    def init_browser(self, add_stealth_script: bool = True) -> Optional[Page]:
        """初始化浏览器 - 使用持久化上下文（参照旧框架）
        
        Args:
            add_stealth_script: 是否添加反检测脚本（密码登录时不需要）
        """
        try:
            logger.info(f"【{self.pure_user_id}】启动Playwright...")
            
            # 设置浏览器路径环境变量（解决 exe 打包后找不到浏览器的问题）
            self._setup_browser_path()
            
            # Windows/Linux兼容性处理
            import sys
            import asyncio
            
            # Windows特殊处理：Playwright需要ProactorEventLoop来支持子进程
            if sys.platform == 'win32':
                try:
                    # 使用 ProactorEventLoop（支持子进程）
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                    logger.debug(f"【{self.pure_user_id}】已设置Windows ProactorEventLoop策略")
                except Exception as e:
                    logger.warning(f"【{self.pure_user_id}】设置事件循环策略失败: {e}")
            
            # 不要创建新的事件循环，让 Playwright 使用默认的
            # 创建新事件循环会导致线程切换问题
            
            patchright_browser_available = False
            if add_stealth_script and PATCHRIGHT_AVAILABLE:
                patchright_browser_available = bool(
                    get_chromium_executable_path(
                        "patchright",
                        strict_revision=True,
                    )
                )
            if add_stealth_script and PATCHRIGHT_AVAILABLE and patchright_browser_available:
                self.playwright = patchright_sync_playwright().start()
                self._cdp_touch_enabled = True
                logger.info(f"【{self.pure_user_id}】Patchright Playwright启动成功")
            else:
                self.playwright = sync_playwright().start()
                if add_stealth_script:
                    logger.warning(
                        f"【{self.pure_user_id}】Patchright 或其 Chromium 未安装，"
                        "滑块回退普通 Playwright"
                    )
                logger.info(f"【{self.pure_user_id}】Playwright启动成功")

            # ── 优先复用发布使用的宿主 Chrome/CDP 环境 ──
            cdp_url = _captcha_remote_cdp_url()
            if _captcha_remote_cdp_enabled() and cdp_url:
                logger.info(f"【{self.pure_user_id}】验证码复用宿主 Chrome/CDP: {cdp_url}")
                try:
                    self.browser = self.playwright.chromium.connect_over_cdp(cdp_url, timeout=30000)
                    contexts = list(self.browser.contexts)
                    if not contexts:
                        raise RuntimeError("宿主 Chrome 没有可复用的默认 context")
                    # 与发布器一致：复用默认 context 的真实 profile，但仅创建本次验证码新页签。
                    self.context = contexts[0]
                    self._remote_cdp_mode = True
                    self._cdp_context_owned = False
                    self.page = self.context.new_page()
                    self.page.set_default_timeout(30000)
                    self.page.set_default_navigation_timeout(60000)
                    if add_stealth_script:
                        self.page.add_init_script(_STEALTH_MINIMAL)
                        self.page.add_init_script(_CAP_JS)
                    self.element_finder = SliderElementFinder(self.page, self.user_id)
                    self.verification_checker = VerificationChecker(self.page, self.user_id)
                    logger.info(f"【{self.pure_user_id}】验证码页签已在宿主 Chrome 默认环境中创建")
                    return self.page
                except Exception as cdp_error:
                    self._remote_cdp_mode = False
                    self._cdp_context_owned = False
                    self.context = None
                    self.browser = None
                    if not _captcha_remote_cdp_fallback():
                        raise
                    logger.warning(
                        f"【{self.pure_user_id}】连接宿主 Chrome/CDP 失败，回退本地浏览器: {cdp_error}"
                    )

# Linux 有头模式必须有可用显示服务；Docker 场景通过宿主 X11 :99 挂载提供。
            if not self.headless and sys.platform == "linux":
                display = (
                    os.environ.get("CAPTCHA_DISPLAY")
                    or os.environ.get("DISPLAY")
                    or ":99"
                )
                try:
                    check = subprocess.run(
                        ["xdpyinfo", "-display", display],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                except (FileNotFoundError, subprocess.SubprocessError) as e:
                    raise RuntimeError(f"验证码无法检查虚拟显示屏 DISPLAY={display}: {e}") from e
                if check.returncode != 0:
                    raise RuntimeError(f"验证码无法连接虚拟显示屏 DISPLAY={display}")
                # Chrome/Playwright 本身读取进程 DISPLAY，而不是 CAPTCHA_DISPLAY。
                # 将其切到已验证的隐藏 Xvfb，避免仍继承 compose 的 DISPLAY=:99。
                os.environ["DISPLAY"] = display
                logger.info(f"【{self.pure_user_id}】验证码浏览器使用虚拟显示屏: DISPLAY={display}")

            # ── 真人轨迹回放模式：复刻真实鼠标引擎的浏览器环境 ──
            if self._human_trail_mode:
                logger.info(f"【{self.pure_user_id}】真人轨迹回放模式：使用真实鼠标引擎同款浏览器环境")
                launch_kwargs = {
                    'channel': 'chrome',        # 本机真实 Chrome（自然指纹）
                    'headless': self.headless,
                    'args': _HUMAN_TRAIL_BROWSER_ARGS,
                    'no_viewport': True,        # 保留真实窗口尺寸
                    'locale': 'zh-CN',
                    'timezone_id': 'Asia/Shanghai',
                    'ignore_https_errors': True,
                    'extra_http_headers': {'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'},
                }
            else:
                # ── 默认模式：干净环境（精简 args + 真 Chrome + no_viewport） ──
                launch_kwargs = {
                    'channel': 'chrome',
                    'headless': self.headless,
                    'args': _HUMAN_TRAIL_BROWSER_ARGS,
                    'no_viewport': True,
                    'locale': 'zh-CN',
                    'timezone_id': 'Asia/Shanghai',
                    'ignore_https_errors': True,
                    'extra_http_headers': {'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'},
                }
                logger.info(f"【{self.pure_user_id}】默认模式：干净环境（channel=chrome, 精简args）+ 合成轨迹")

            # 启动前清理可能残留的 Singleton 锁文件（已持有账号锁，清理是安全的）
            self._clean_singleton_lock_files()

            # 启动浏览器；如果首次因 PROFILE_IN_USE 等原因失败，再清理一次锁文件并重试一次
            # timeout 设为 30 秒，防止网络不通或 Chrome 启动卡住导致浏览器进程永远不关闭
            self.context = None
            launch_attempts = 2
            last_launch_error: Optional[Exception] = None
            for attempt in range(1, launch_attempts + 1):
                try:
                    self.context = self.playwright.chromium.launch_persistent_context(
                        self.user_data_dir,
                        timeout=30000,
                        **launch_kwargs
                    )
                    if attempt > 1:
                        logger.info(
                            f"【{self.pure_user_id}】第 {attempt} 次尝试启动浏览器成功"
                        )
                    break
                except Exception as launch_e:
                    last_launch_error = launch_e
                    err_text = str(launch_e)
                    logger.warning(
                        f"【{self.pure_user_id}】第 {attempt}/{launch_attempts} 次启动浏览器失败: {err_text}"
                    )
                    # 仅在还有重试机会时才清理并继续
                    if attempt < launch_attempts:
                        if prefer_system_chrome and launch_kwargs.get('channel') == 'chrome':
                            launch_kwargs.pop('channel', None)
                            logger.warning(
                                f"【{self.pure_user_id}】系统 Chrome 启动失败，"
                                "回退 Playwright 内置 Chromium"
                            )
                        logger.info(
                            f"【{self.pure_user_id}】尝试清理残留锁文件后重试启动..."
                        )
                        self._clean_singleton_lock_files()
                        # 短暂等待 Chrome 子进程彻底退出，避免文件被占用导致清理失败
                        time.sleep(1)

            if not self.context:
                # 重试仍失败，把最后一次错误抛出
                raise last_launch_error if last_launch_error else Exception("浏览器上下文创建失败")

            # 持久化上下文的browser属性
            self.browser = self.context.browser

            logger.info(f"【{self.pure_user_id}】浏览器上下文创建成功（持久化模式）")

            # 创建新页面
            logger.info(f"【{self.pure_user_id}】创建新页面...")
            if add_stealth_script and self.context.pages:
                self.page = self.context.pages[0]
            else:
                self.page = self.context.new_page()

            if not self.page:
                raise Exception("页面创建失败")
            logger.info(f"【{self.pure_user_id}】页面创建成功（{'最大化窗口模式' if not self.headless else '无头模式'}）")

            # 添加最小反检测脚本（与真实鼠标模式保持一致）
            if add_stealth_script:
if add_stealth_script:
                logger.info(f"【{self.pure_user_id}】添加最小反检测脚本...")
                self.page.add_init_script(_STEALTH_MINIMAL)
                self.page.add_init_script(_CAP_JS)
                # 配置 UA Client Hints（与真实鼠标模式保持一致）
                try:
                    configure_slider_user_agent(self.context, self.page)
                except Exception as user_agent_error:
                    logger.warning(
                        f"【{self.pure_user_id}】配置滑块 UA Client Hints 失败，继续运行: "
                        f"{user_agent_error}"
                    )

                def capture_slide_response(response: Any) -> None:
                    """记录 Baxia 滑块接口返回码，供轨迹迭代诊断。"""
                    try:
                        if "/slide" not in response.url:
                            return
                        result = response.json().get("result") or {}
                        code = result.get("code")
                        if code is not None:
                            self._slide_response_code = int(code)
                            logger.info(
                                f"【{self.pure_user_id}】Baxia 滑块响应码: "
                                f"{self._slide_response_code}"
                            )
                    except Exception:
                        return

                self.page.on("response", capture_slide_response)
            logger.info(f"【{self.pure_user_id}】浏览器初始化完成")

            # 初始化元素查找器和验证检查器
            self.element_finder = SliderElementFinder(self.page, self.user_id)
            self.verification_checker = VerificationChecker(self.page, self.user_id)

            return self.page

        except Exception as e:
            logger.error(f"【{self.pure_user_id}】初始化浏览器失败: {e}")
            import traceback
            logger.error(f"【{self.pure_user_id}】详细错误堆栈: {traceback.format_exc()}")
            self._cleanup()
            raise

    def solve_slider(self, max_retries: int = 3) -> bool:
        """
        处理滑块验证
        
        Args:
            max_retries: 最大重试次数
            
        Returns:
            是否成功
        """
        failure_records = []

        # 滑动前快照 x5sec 旧值，供严格判定使用
        pre_x5sec = self._read_x5sec_value()
        logger.info(
            f"【{self.pure_user_id}】滑动前 x5sec 快照："
            f"{(pre_x5sec[:40] + '…') if pre_x5sec else '<不存在>'}"
        )
        self.verification_checker.set_pre_x5sec(pre_x5sec)
        self.verification_checker.set_cookies_reader(self._read_all_cookies_dict)

        try:
            for attempt in range(1, max_retries + 1):
                # 根据尝试次数选择策略
                if attempt == 1:
                    current_strategy = "default"
                elif attempt == 2:
                    current_strategy = "cautious"
                else:
                    current_strategy = random.choice(["fast", "slow"])

                logger.info(f"【{self.pure_user_id}】滑块验证尝试 {attempt}/{max_retries}，策略: {current_strategy}")

                try:
                    # 查找滑块元素
                    slider_container, slider_button, slider_track = self.element_finder.find_slider_elements()

                    if not slider_button or not slider_track:
                        # 滑块元素消失，可能是人工已手动通过验证，必须检查是否生成了新 x5sec。
                        logger.warning(f"【{self.pure_user_id}】未找到滑块元素，检查是否已验证通过...")

                        x5sec_value = self._read_x5sec_value()
                        if x5sec_value and x5sec_value != pre_x5sec:
                            logger.info(f"【{self.pure_user_id}】✅ 未找到滑块但检测到新 x5sec cookie，判定为验证已通过（可能人工完成）")
                            return True
                        if x5sec_value:
                            logger.warning(
                                f"【{self.pure_user_id}】未找到滑块但仅检测到滑动前已有的 x5sec，"
                                "共享默认环境下不判定为本次验证成功"
                            )
                        else:
                            logger.warning(
                                f"【{self.pure_user_id}】滑块元素消失但未检测到 x5sec，"
                                "不判定为成功"
                            )

                        # 未拿到新 x5sec 时，页面元素消失并不能证明风控已放行。
                        logger.info(f"【{self.pure_user_id}】验证未通过，尝试点击重试按钮")
                        self._click_slider_refresh()
                        time.sleep(2)
                        continue

                    # 同步frame引用到验证检查器
                    self.verification_checker.set_detected_frame(self.element_finder.get_detected_frame())

                    # 计算滑动距离
                    slide_distance = self.verification_checker.calculate_slide_distance(slider_button, slider_track)
                    if slide_distance <= 0:
                        logger.warning(f"【{self.pure_user_id}】滑动距离计算失败")
                        continue

                    # 生成轨迹
                    trajectory = self.trajectory_generator.generate_human_trajectory(slide_distance)
                    if not trajectory:
                        logger.warning(f"【{self.pure_user_id}】轨迹生成失败")
                        continue

                    # 执行滑动
                    slide_success = self._simulate_slide(slider_button, trajectory)
                    if not slide_success:
                        logger.warning(f"【{self.pure_user_id}】滑动执行失败")
                        strategy_stats.record_attempt(attempt, current_strategy, success=False)
                        continue

                    # 检查验证结果
                    success = self.verification_checker.check_verification_success_fast(slider_button)

                    if success:
                        logger.info(f"【{self.pure_user_id}】✅ 滑块验证成功（第{attempt}次）")

                        # 记录策略成功
                        strategy_stats.record_attempt(attempt, current_strategy, success=True)
                        logger.info(f"【{self.pure_user_id}】📊 记录策略: 第{attempt}次-{current_strategy}策略-成功")

                        # 保存成功记录
                        if self.enable_learning:
                            trajectory_data = self.trajectory_generator.get_trajectory_data()
                            self.history_manager.save_success_record(trajectory_data)
                            logger.info(f"【{self.pure_user_id}】已保存成功记录用于参数优化")

                        if attempt > 1:
                            logger.info(f"【{self.pure_user_id}】经过{attempt}次尝试后验证成功")

                        strategy_stats.log_summary()
                        return True
                    else:
                        logger.warning(f"【{self.pure_user_id}】❌ 第{attempt}次验证失败")

                        # 记录策略失败
                        strategy_stats.record_attempt(attempt, current_strategy, success=False)
                        logger.info(f"【{self.pure_user_id}】📊 记录策略: 第{attempt}次-{current_strategy}策略-失败")

                        # 分析失败原因
                        trajectory_data = self.trajectory_generator.get_trajectory_data()
                        failure_info = self.history_manager.analyze_failure(attempt, slide_distance, trajectory_data)
                        failure_records.append(failure_info)

                        if attempt < max_retries:
                            # 真人轨迹模式：失败后点重试按钮重置滑块，下次换一条轨迹
                            if self._human_trail_mode:
                                logger.info(f"【{self.pure_user_id}】真人轨迹回放失败，点击重试按钮重置滑块")
                                self._click_slider_refresh()
                                time.sleep(random.uniform(1.5, 2.5))
                            else:
                                time.sleep(random.uniform(1, 2))
                            continue

                except Exception as e:
                    logger.error(f"【{self.pure_user_id}】第{attempt}次处理滑块验证时出错: {str(e)}")
                    if attempt < max_retries:
                        if self._human_trail_mode:
                            self._click_slider_refresh()
                            time.sleep(random.uniform(1.5, 2.5))
                        continue

            # 所有尝试都失败了
            logger.error(f"【{self.pure_user_id}】滑块验证失败，已尝试{max_retries}次")

            # 输出失败分析摘要
            if failure_records:
                logger.info(f"【{self.pure_user_id}】失败分析摘要:")
                for record in failure_records:
                    logger.info(
                        f"  - 第{record['attempt']}次: 距离{record['slide_distance']}px, "
                        f"步数{record['total_steps']}, 最终位置{record['final_left_px']}px"
                    )

            strategy_stats.log_summary()
            return False

        except Exception as e:
            logger.error(f"【{self.pure_user_id}】滑块验证异常: {e}")
            return False

    def run(
        self,
        url: str,
        browser_timeout: int = 20,
        url_provider: Optional[Callable[[], Optional[str]]] = None,
    ) -> Tuple[bool, Optional[Dict[str, str]]]:
        """
        运行滑块验证流程
        
        Args:
            url: 验证页面URL
            browser_timeout: 浏览器验证超时时间（秒），默认20秒
            url_provider: 可选的"重新获取验证链接"回调。由于 __init__ 中需要等待并发槽位/
                账号锁，再加上浏览器启动耗时，传入的 url（punish?x5secdata=...）可能在导航前
                就已过期，导致页面显示"抱歉，页面访问出现了问题"。提供该回调后：仅当导航命中
                过期页时才用它按需重新拉取链接并重试（链接未过期则完全不调用，避免多余请求）；
                若回调发现 token 已可用（风控解除），返回哨兵 CAPTCHA_NOT_REQUIRED，本方法据此
                提前结束。回调返回新 URL 字符串 / 哨兵 / None（None 时沿用原链接）。
            
        Returns:
            (是否成功, cookies字典)
        """
        cookies = None
        browser_start_time = None
        timeout_timer = None
        timed_out = False
        
        def _force_close_on_timeout():
            """超时后强制杀掉浏览器进程树，解除阻塞的 Playwright 调用。

            关键：这里绝不能调用 self.context.close()/self.browser.close()。
            本回调运行在 threading.Timer 的独立线程，而 sync Playwright 对象
            绑定其创建线程（浏览器任务线程池的某个线程）。跨线程关闭会抛
            "cannot switch to a different thread" 而失败，历史上正因如此导致
            Chrome 无法回收、堆积成 <defunct> 僵尸进程，最终 "can't start new
            thread"。因此超时路径只按 user_data_dir 做 OS 级进程强杀（跨线程
            安全）；强杀后创建线程上阻塞的 Playwright 调用会立即抛错返回，再由
            run() 的 finally 在正确的线程上执行 self.close() 完成 Playwright
            侧清理。
            """
            nonlocal timed_out
            timed_out = True
            logger.error(f"【{self.pure_user_id}】⏰ 浏览器超时守护触发（{browser_timeout}秒）")
            if self._remote_cdp_mode:
                # 宿主 CDP 模式不能杀 Chrome；run() 的 finally 会在同线程关闭本次页签。
                logger.warning(f"【{self.pure_user_id}】宿主 CDP 模式仅标记超时，保留宿主 Chrome")
                return
            logger.error(f"【{self.pure_user_id}】强制杀掉本地浏览器进程释放资源")
            killed = self._kill_browser_processes()
            if killed > 0:
                logger.info(f"【{self.pure_user_id}】超时守护：已强杀 {killed} 个浏览器进程")
            elif killed < 0:
                logger.info(f"【{self.pure_user_id}】超时守护：已尝试强杀本次浏览器进程")
            else:
                logger.warning(f"【{self.pure_user_id}】超时守护：未匹配到需强杀的浏览器进程（可能尚未启动或已退出）")
        
        try:
            # 初始化浏览器
            self.init_browser()
            # 共享默认 context 会保留历史 x5sec。导航前先做快照，避免普通页面或其他
            # 账号留下的旧 cookie 被“页面无验证码”分支误报为本次验证成功。
            pre_navigation_x5sec = self._read_x5sec_value()
            browser_start_time = time.time()
            logger.info(f"【{self.pure_user_id}】浏览器已启动，{browser_timeout}秒内未完成验证将自动关闭")

            # 启动超时守护定时器
            import threading
            timeout_timer = threading.Timer(browser_timeout, _force_close_on_timeout)
            timeout_timer.daemon = True
            timeout_timer.start()

            def _get_fresh_url() -> Optional[str]:
                """调用 url_provider 重新获取一个新鲜的验证链接；失败返回 None。"""
                if url_provider is None:
                    return None
                try:
                    fresh = url_provider()
                    if fresh and isinstance(fresh, str):
                        return fresh
                    logger.info(f"【{self.pure_user_id}】重新获取验证链接返回空，沿用原链接")
                except Exception as up_e:
                    logger.warning(f"【{self.pure_user_id}】重新获取验证链接异常，沿用原链接: {up_e}")
                return None

            def _load_and_read(target_url: str) -> Optional[str]:
                """导航到目标URL并读取页面内容。返回 page_content；需要中止时返回 None。"""
                logger.info(f"【{self.pure_user_id}】导航到URL: {target_url}")
                try:
                    # goto 超时设为 browser_timeout 的一半，确保不会卡住超过总超时
                    goto_timeout = min(browser_timeout * 1000 // 2, 15000)
                    self.page.goto(target_url, wait_until="domcontentloaded", timeout=goto_timeout)
                except Exception as e:
                    if timed_out:
                        logger.warning(f"【{self.pure_user_id}】页面加载被超时守护中断")
                        return None
                    # 页面加载可能会有各种异常，但只要页面对象存在就继续
                    logger.warning(f"【{self.pure_user_id}】页面加载异常，尝试继续: {str(e)}")
                    time.sleep(1)

                if timed_out:
                    return None
                if self._check_browser_timeout(browser_start_time, browser_timeout):
                    return None

                # 短暂延迟
                delay = random.uniform(0.3, 0.8)
                logger.info(f"【{self.pure_user_id}】等待页面加载: {delay:.2f}秒")
                time.sleep(delay)
                if timed_out:
                    return None

                # CDP 触摸模式不注入额外鼠标/滚轮事件，避免把两种输入源混在同一挑战中。
                if not self._cdp_touch_enabled:
                    self.page.mouse.move(640, 360)
                    time.sleep(random.uniform(0.02, 0.05))
                    self.page.mouse.wheel(0, random.randint(200, 500))
                    time.sleep(random.uniform(0.02, 0.05))
                if timed_out:
                    return None

                # 检查页面标题
                page_title = self.page.title()
                logger.info(f"【{self.pure_user_id}】页面标题: {page_title}")

                # 检查页面内容
                content = self.page.content()
                if timed_out:
                    return None
                if self._check_browser_timeout(browser_start_time, browser_timeout):
                    return None
                return content

            # 先用原链接导航；仅当命中"页面访问出现了问题"过期页时，才按需重新取链接，
            # 最多重试 2 次。这样链接未过期时不会产生任何额外的 token 接口调用，避免无谓地
            # 增加调用频率（频繁调用 token 接口更易被风控盯上）。
            url_refresh_count = 0
            max_url_refreshes = 2 if url_provider is not None else 0
            page_content = None
            while True:
                page_content = _load_and_read(url)
                if page_content is None:
                    return False, None

                if "抱歉，页面访问出现了问题" in page_content:
                    if url_provider is not None and url_refresh_count < max_url_refreshes:
                        if self._check_browser_timeout(browser_start_time, browser_timeout):
                            return False, None
                        url_refresh_count += 1
                        logger.warning(
                            f"【{self.pure_user_id}】页面访问出现问题（链接已过期），"
                            f"第{url_refresh_count}次重新获取验证链接后重试"
                        )
                        retry_url = _get_fresh_url()
                        if retry_url == CAPTCHA_NOT_REQUIRED:
                            logger.info(f"【{self.pure_user_id}】重取链接时检测到 token 已可用，无需滑块验证，提前结束")
                            return True, None
                        if retry_url:
                            url = retry_url
                            continue
                        logger.error(f"【{self.pure_user_id}】重新获取验证链接失败，返回失败")
                    else:
                        logger.error(f"【{self.pure_user_id}】页面访问出现问题，直接返回失败")
                    # 链接已过期且无法自助重取：返回过期哨兵，供编排层/远程调用方刷新URL重试
                    return False, URL_EXPIRED
                break

            if "崩溃" in page_content or "STATUS_BREAKPOINT" in page_content:
                logger.error(f"【{self.pure_user_id}】页面崩溃（STATUS_BREAKPOINT），直接返回失败")
                return False, None
            if any(keyword in page_content for keyword in ["验证码", "captcha", "滑块", "slider"]):
                logger.info(f"【{self.pure_user_id}】页面内容包含验证码相关关键词")

                # 处理滑块验证（带超时检查）
                success = self._solve_slider_with_timeout(browser_start_time, browser_timeout)

                if success:
                    logger.info(f"【{self.pure_user_id}】滑块验证成功")

                    # 等待页面完全加载和跳转，让新的cookie生效
                    try:
                        logger.info(f"【{self.pure_user_id}】等待页面响应...")
                        time.sleep(1)  # 等待页面响应
                        
                        # 不等待networkidle，直接继续（参照旧框架）
                        logger.info(f"【{self.pure_user_id}】开始获取cookie")
                    except Exception as e:
                        logger.debug(f"【{self.pure_user_id}】等待页面响应时出错: {str(e)}")

                    # 获取cookie
                    try:
                        cookies = self._get_cookies_after_success()
                        logger.info(f"【{self.pure_user_id}】已获取cookie，准备关闭浏览器")
                    except Exception as e:
                        logger.warning(f"【{self.pure_user_id}】获取cookie时出错: {str(e)}")

                    return success, cookies
                else:
                    logger.warning(f"【{self.pure_user_id}】滑块验证失败")
                    return False, None
            else:
                # 页面没有滑块文案也不能代表验证已通过；只有明确的
                # CAPTCHA_NOT_REQUIRED 哨兵或 x5sec cookie 才能报告成功。
                x5sec_value = self._read_x5sec_value()
                if x5sec_value and x5sec_value != pre_navigation_x5sec:
                    logger.info(f"【{self.pure_user_id}】页面无验证码元素且检测到新 x5sec，确认验证通过")
                    return True, self._get_cookies_after_success()
                if x5sec_value:
                    logger.warning(f"【{self.pure_user_id}】页面无验证码元素但仅检测到共享环境旧 x5sec，不判定为成功")
                else:
                    logger.warning(f"【{self.pure_user_id}】页面无验证码元素且未检测到 x5sec，不判定为成功")
                return False, None

        except Exception as e:
            if timed_out:
                logger.warning(f"【{self.pure_user_id}】执行过程被超时守护中断: {str(e)}")
            else:
                logger.error(f"【{self.pure_user_id}】执行过程中出错: {str(e)}")
            return False, None

        finally:
            # 取消超时守护定时器
            if timeout_timer is not None:
                timeout_timer.cancel()
            self.close()
    
    def _check_browser_timeout(self, start_time: float, timeout: int) -> bool:
        """检查浏览器是否超时
        
        Args:
            start_time: 浏览器启动时间
            timeout: 超时时间（秒）
            
        Returns:
            是否超时
        """
        if start_time is None:
            return False
        
        elapsed = time.time() - start_time
        if elapsed >= timeout:
            logger.error(f"【{self.pure_user_id}】⏰ 浏览器验证超时（{elapsed:.1f}秒 >= {timeout}秒），关闭浏览器并清理资源")
            return True
        
        remaining = timeout - elapsed
        if remaining <= 10:
            logger.warning(f"【{self.pure_user_id}】⏰ 剩余时间不足: {remaining:.1f}秒")
        
        return False
    
    def _read_x5sec_value(self) -> Optional[str]:
        """读取当前 context 里的 x5sec cookie 值；不存在返回 None。"""
        try:
            for c in (self.context.cookies() if self.context else []):
                if c.get("name") == "x5sec":
                    return c.get("value")
        except Exception:
            return None
        return None

    def _read_all_cookies_dict(self) -> Dict[str, str]:
        """快照当前 context 的全部 cookies 为 name->value dict。"""
        try:
            return {c["name"]: c["value"] for c in (self.context.cookies() if self.context else [])}
        except Exception:
            return {}

    def _click_slider_refresh(self) -> None:
        """点击滑块验证失败后的重试/刷新按钮，让滑块重新加载。

        滑块验证失败后，页面会隐藏轨道元素并显示 #nc_1_refresh1 或类似的
        刷新按钮（文案通常为"验证失败，点击框体重试"）。点击后滑块会重新
        初始化，轨道元素重新出现。
        """
        # 重试按钮的常见选择器
        refresh_selectors = [
            "#nc_1_refresh1",
            ".nc_iconfont.btn_refresh",
            ".errloading",
            "[class*='refresh']",
            ".nc-container",
        ]

        # 在主页面和所有 frame 中查找并点击
        frames_to_check = [self.page]
        if self.element_finder and self.element_finder.get_detected_frame():
            frames_to_check.insert(0, self.element_finder.get_detected_frame())

        for frame in frames_to_check:
            try:
                # 检查 frame 是否还有效
                if frame != self.page:
                    try:
                        _ = frame.url if hasattr(frame, 'url') else None
                    except Exception:
                        continue

                for selector in refresh_selectors:
                    try:
                        element = frame.query_selector(selector)
                        if element and element.is_visible():
                            element.click()
                            logger.info(f"【{self.pure_user_id}】✓ 已点击滑块重试按钮: {selector}")
                            return
                    except Exception:
                        continue
            except Exception:
                continue

        logger.warning(f"【{self.pure_user_id}】未找到滑块重试按钮")

    def _solve_slider_with_timeout(self, browser_start_time: float, browser_timeout: int) -> bool:
        """带超时检查的滑块验证
        
        Args:
            browser_start_time: 浏览器启动时间
            browser_timeout: 浏览器超时时间
            
        Returns:
            是否成功
        """
        failure_records = []
        max_retries = 3

        # 滑动前快照 x5sec 旧值，供严格判定使用
        pre_x5sec = self._read_x5sec_value()
        logger.info(
            f"【{self.pure_user_id}】滑动前 x5sec 快照："
            f"{(pre_x5sec[:40] + '…') if pre_x5sec else '<不存在>'}"
        )
        # 同步给 checker，后续二次确认会用
        self.verification_checker.set_pre_x5sec(pre_x5sec)
        self.verification_checker.set_cookies_reader(self._read_all_cookies_dict)

        try:
            for attempt in range(1, max_retries + 1):
                # 检查超时
                if self._check_browser_timeout(browser_start_time, browser_timeout):
                    logger.error(f"【{self.pure_user_id}】滑块验证因浏览器超时而终止")
                    return False
                
                # 根据尝试次数选择策略
                if attempt == 1:
                    current_strategy = "default"
                elif attempt == 2:
                    current_strategy = "cautious"
                else:
                    current_strategy = random.choice(["fast", "slow"])

                logger.info(f"【{self.pure_user_id}】滑块验证尝试 {attempt}/{max_retries}，策略: {current_strategy}")

                try:
                    # 查找滑块元素
                    slider_container, slider_button, slider_track = self.element_finder.find_slider_elements()

                    if not slider_button or not slider_track:
                        # 滑块元素消失，可能是人工已手动通过验证，必须检查是否生成了新 x5sec。
                        logger.warning(f"【{self.pure_user_id}】未找到滑块元素，检查是否已验证通过...")

                        x5sec_value = self._read_x5sec_value()
                        if x5sec_value and x5sec_value != pre_x5sec:
                            logger.info(f"【{self.pure_user_id}】✅ 未找到滑块但检测到新 x5sec cookie，判定为验证已通过（可能人工完成）")
                            return True
                        if x5sec_value:
                            logger.warning(
                                f"【{self.pure_user_id}】未找到滑块但仅检测到滑动前已有的 x5sec，"
                                "共享默认环境下不判定为本次验证成功"
                            )
                        else:
                            logger.warning(
                                f"【{self.pure_user_id}】滑块元素消失但未检测到 x5sec，"
                                "不判定为成功"
                            )

                        # 未拿到新 x5sec 时，页面元素消失并不能证明风控已放行。
                        logger.info(f"【{self.pure_user_id}】验证未通过，尝试点击重试按钮")
                        self._click_slider_refresh()
                        time.sleep(2)
                        continue
                    
                    # 检查超时
                    if self._check_browser_timeout(browser_start_time, browser_timeout):
                        return False

                    # 同步frame引用到验证检查器
                    self.verification_checker.set_detected_frame(self.element_finder.get_detected_frame())

                    # 计算滑动距离
                    slide_distance = self.verification_checker.calculate_slide_distance(slider_button, slider_track)
                    if slide_distance <= 0:
                        logger.warning(f"【{self.pure_user_id}】滑动距离计算失败")
                        continue

                    # 生成轨迹
                    trajectory = self.trajectory_generator.generate_human_trajectory(slide_distance)
                    if not trajectory:
                        logger.warning(f"【{self.pure_user_id}】轨迹生成失败")
                        continue

                    # 执行滑动
                    slide_success = self._simulate_slide(slider_button, trajectory)
                    if not slide_success:
                        logger.warning(f"【{self.pure_user_id}】滑动执行失败")
                        strategy_stats.record_attempt(attempt, current_strategy, success=False)
                        continue

                    # code=300 是 Baxia 明确拒绝，无需再等待视觉状态；其他情况继续
                    # 严格确认 x5sec/跳转，避免把按钮暂时消失误判为成功。
                    if self._slide_response_code == 300:
                        success = False
                        logger.warning(
                            f"【{self.pure_user_id}】Baxia 已拒绝当前轨迹，立即进入重试"
                        )
                    else:
                        # 等 x5sec 落盘的上限按剩余总预算动态给定，预留取 cookie 时间。
                        elapsed = time.time() - browser_start_time
                        confirm_budget = max(1.0, browser_timeout - elapsed - 3.0)
                        success = self.verification_checker.check_verification_success_fast(
                            slider_button, max_confirm_wait=confirm_budget
                        )

                    if success:
                        logger.info(f"【{self.pure_user_id}】✅ 滑块验证成功（第{attempt}次）")

                        # 记录策略成功
                        strategy_stats.record_attempt(attempt, current_strategy, success=True)
                        logger.info(f"【{self.pure_user_id}】📊 记录策略: 第{attempt}次-{current_strategy}策略-成功")

                        # 保存成功记录
                        if self.enable_learning:
                            trajectory_data = self.trajectory_generator.get_trajectory_data()
                            self.history_manager.save_success_record(trajectory_data)
                            logger.info(f"【{self.pure_user_id}】已保存成功记录用于参数优化")

                        if attempt > 1:
                            logger.info(f"【{self.pure_user_id}】经过{attempt}次尝试后验证成功")

                        strategy_stats.log_summary()
                        return True
                    else:
                        logger.warning(f"【{self.pure_user_id}】❌ 第{attempt}次验证失败")

                        # 记录策略失败
                        strategy_stats.record_attempt(attempt, current_strategy, success=False)
                        logger.info(f"【{self.pure_user_id}】📊 记录策略: 第{attempt}次-{current_strategy}策略-失败")

                        # 分析失败原因
                        trajectory_data = self.trajectory_generator.get_trajectory_data()
                        failure_info = self.history_manager.analyze_failure(attempt, slide_distance, trajectory_data)
                        failure_records.append(failure_info)

                        if attempt < max_retries:
                            # 真人轨迹模式：失败后点重试按钮重置滑块，下次换一条轨迹
                            if self._human_trail_mode:
                                logger.info(f"【{self.pure_user_id}】真人轨迹回放失败，点击重试按钮重置滑块")
                                self._click_slider_refresh()
                                time.sleep(random.uniform(1.5, 2.5))
                            elif self._slide_response_code == 300:
                                # 响应码 300 表示需要刷新滑块，点击重试
                                self._click_slider_refresh()
                                time.sleep(random.uniform(1, 2))
                            else:
                                time.sleep(random.uniform(1, 2))
                            continue

                except Exception as e:
                    logger.error(f"【{self.pure_user_id}】第{attempt}次处理滑块验证时出错: {str(e)}")
                    if attempt < max_retries:
                        if self._human_trail_mode:
                            self._click_slider_refresh()
                            time.sleep(random.uniform(1.5, 2.5))
                        continue

            # 所有尝试都失败了
            logger.error(f"【{self.pure_user_id}】滑块验证失败，已尝试{max_retries}次")

            # 输出失败分析摘要
            if failure_records:
                logger.info(f"【{self.pure_user_id}】失败分析摘要:")
                for record in failure_records:
                    logger.info(
                        f"  - 第{record['attempt']}次: 距离{record['slide_distance']}px, "
                        f"步数{record['total_steps']}, 最终位置{record['final_left_px']}px"
                    )

            strategy_stats.log_summary()
            return False

        except Exception as e:
            logger.error(f"【{self.pure_user_id}】滑块验证异常: {e}")
            return False

    def _simulate_slide(self, slider_button: ElementHandle, trajectory: List[Tuple[float, float, float]]) -> bool:
        """模拟滑动

        Args:
            slider_button: 滑块按钮元素
            trajectory: 轨迹点列表（默认模式使用；真人轨迹模式忽略此参数）

        Returns:
            是否执行成功
        """
        try:
            # 真人轨迹回放模式（仅 CAPTCHA_HUMAN_TRAIL=true 时）；默认模式用合成轨迹 + 干净环境
            if self._human_trail_mode:
                return self._simulate_slide_human_trail(slider_button)

            logger.info(f"【{self.pure_user_id}】开始优化滑动模拟...")

            # 等待页面稳定
            time.sleep(random.uniform(0.1, 0.3))

            # 获取滑块按钮中心位置
            button_box = slider_button.bounding_box()
            if not button_box:
                logger.error(f"【{self.pure_user_id}】无法获取滑块按钮位置")
                return False

            start_x = button_box["x"] + button_box["width"] / 2
            start_y = button_box["y"] + button_box["height"] / 2
            logger.info(f"【{self.pure_user_id}】滑块位置: ({start_x}, {start_y})")

            if self._cdp_touch_enabled:
                # 触摸拖动必须禁止页面滚动，否则浏览器会在中途取消 pointer 流；
                # 事件完全由 CDP 派发，不触碰系统鼠标，也不抢占桌面焦点。
                slider_button.evaluate(
                    "element => { element.style.touchAction = 'none'; }"
                )
                self._slide_response_code = None
                dispatch_touch_drag(self.page, start_x, start_y, trajectory)
                logger.info(
                    f"【{self.pure_user_id}】已通过 CDP 触摸轨迹完成滑动："
                    f"{len(trajectory)}点，末点=({trajectory[-1][0]:.1f},"
                    f"{trajectory[-1][1]:.1f})"
                )
                return True

            # 第一阶段：移动到滑块附近
            try:
                offset_x = random.uniform(-30, -10)
                offset_y = random.uniform(-15, 15)
                self.page.mouse.move(
                    start_x + offset_x,
                    start_y + offset_y,
                    steps=random.randint(5, 10)
                )
                time.sleep(random.uniform(0.15, 0.3))

                self.page.mouse.move(
                    start_x,
                    start_y,
                    steps=random.randint(3, 6)
                )
                time.sleep(random.uniform(0.1, 0.25))
            except Exception as e:
                logger.warning(f"【{self.pure_user_id}】移动到滑块失败: {e}，继续尝试")

            # 第二阶段：按下鼠标。前面的连续接近轨迹已经自然触发 hover，避免再调用
            # ElementHandle.hover 生成一段额外的 Playwright 默认轨迹。
            try:
                time.sleep(random.uniform(0.05, 0.15))
                self.page.mouse.down()
                self._slide_response_code = None
                time.sleep(random.uniform(0.05, 0.09))
            except Exception as e:
                logger.error(f"【{self.pure_user_id}】按下鼠标失败: {e}")
                return False

            # 第三阶段：执行滑动轨迹
            try:
                start_time = time.time()
                current_x = start_x
                current_y = start_y

                # Python 逐点调用 page.mouse.move 会为每个点产生一次跨进程往返，实测
                # 30 个点会把约 0.5 秒的目标轨迹拉长到 3 秒以上。这里把轨迹压成
                # 每个真人样本分位锚点单独派发，不再使用 Playwright 的线性补点，
                # 完整保留先加速、越过框体、末端减速的原始速度形态。
                max_segments = len(trajectory)
                segment_size = max(1, (len(trajectory) + max_segments - 1) // max_segments)
                segment_starts = range(0, len(trajectory), segment_size)

                for segment_start in segment_starts:
                    segment = trajectory[segment_start:segment_start + segment_size]
                    x, y, _ = segment[-1]
                    current_x = start_x + x
                    current_y = start_y + y

                    self.page.mouse.move(
                        current_x,
                        current_y,
                        steps=len(segment)
                    )

                    time.sleep(sum(point[2] for point in segment))

                    # 记录最终位置
                    if segment_start + len(segment) >= len(trajectory):
                        try:
                            current_style = slider_button.get_attribute("style")
                            if current_style and "left:" in current_style:
                                left_match = re.search(r'left:\s*([^;]+)', current_style)
                                if left_match:
                                    left_value = left_match.group(1).strip()
                                    left_px = float(left_value.replace('px', ''))
                                    self.trajectory_generator.update_trajectory_data("final_left_px", left_px)
                                    logger.info(f"【{self.pure_user_id}】滑动完成: {len(trajectory)}步 - 最终位置: {left_value}")
                        except Exception:
                            pass

                # 刮刮乐特殊处理
                is_scratch = self.verification_checker.is_scratch_captcha()
                if is_scratch:
                    pause_duration = random.uniform(0.3, 0.5)
                    logger.warning(f"【{self.pure_user_id}】🎨 刮刮乐模式：在目标位置停顿{pause_duration:.2f}秒观察...")
                    time.sleep(pause_duration)

                # 释放鼠标
                # 真人模式优选样本在越过框体后仍按住约 249ms，再松开鼠标。
                time.sleep(random.uniform(0.20, 0.30))
                self.page.mouse.up()
                time.sleep(random.uniform(0.03, 0.08))

                if self._slide_response_code is not None:
                    logger.info(
                        f"【{self.pure_user_id}】本次轨迹 Baxia 返回码: "
                        f"{self._slide_response_code}"
                    )

                elapsed_time = time.time() - start_time
                logger.info(f"【{self.pure_user_id}】滑动完成: 耗时={elapsed_time:.2f}秒, 最终位置=({current_x:.1f}, {current_y:.1f})")

                return True

            except Exception as e:
                logger.error(f"【{self.pure_user_id}】执行滑动轨迹失败: {e}")
                import traceback
                logger.error(traceback.format_exc())
                try:
                    self.page.mouse.up()
                except Exception:
                    pass
                return False

        except Exception as e:
            logger.error(f"【{self.pure_user_id}】滑动模拟异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def _simulate_slide_human_trail(self, slider_button: ElementHandle) -> bool:
        """真人轨迹回放：加载真人录制的轨迹，用 CDP page.mouse 回放。

        浏览器环境已复刻真实鼠标引擎（channel=chrome, no_viewport, real_mouse_shared），
        轨迹加载/缩放逻辑与 real_mouse_slider.py 完全一致。
        """
        try:
            drags = _load_human_trail_drags()
            if not drags:
                logger.error(f"【{self.pure_user_id}】真人轨迹回放：无可用样本")
                return False

            time.sleep(random.uniform(0.1, 0.3))

            button_box = slider_button.bounding_box()
            if not button_box:
                logger.error(f"【{self.pure_user_id}】真人轨迹回放：无法获取滑块按钮位置")
                return False

            start_x = button_box["x"] + button_box["width"] / 2
            start_y = button_box["y"] + button_box["height"] / 2

            # 计算滑动距离（优先 DOM 精确计算，与真实鼠标引擎一致）
            slide_distance = 0.0
            detected_frame = self.element_finder.get_detected_frame()
            if detected_frame:
                try:
                    slide_distance = detected_frame.evaluate(
                        """() => {
                            const btn = document.querySelector('#nc_1_n1z');
                            const trk = document.querySelector('#nc_1_n1t') || document.querySelector('.nc_scale');
                            if (!btn || !trk) return null;
                            return trk.getBoundingClientRect().width - btn.getBoundingClientRect().width;
                        }"""
                    )
                    if slide_distance and slide_distance > 0:
                        slide_distance = float(slide_distance)
                except Exception:
                    pass
            if slide_distance <= 0:
                try:
                    for frame in self.page.frames:
                        track_el = frame.query_selector("#nc_1_n1t") or frame.query_selector(".nc_scale")
                        if track_el:
                            track_box = track_el.bounding_box()
                            if track_box and button_box:
                                slide_distance = track_box["width"] - button_box["width"]
                                break
                except Exception:
                    pass
            if slide_distance <= 0:
                logger.error(f"【{self.pure_user_id}】真人轨迹回放：无法确定滑动距离")
                return False

            # 选择并缩放轨迹
            selected_drag = _choose_trail_drag(drags)
            source_distance = selected_drag[-1][0]
            scaled_drag = _scale_trail_to_distance(selected_drag, slide_distance)
            logger.info(
                f"【{self.pure_user_id}】真人轨迹回放：点数={len(scaled_drag)}, "
                f"位移={source_distance:.1f}px->{slide_distance:.1f}px, "
                f"时长={sum(pt[2] for pt in scaled_drag):.0f}ms"
            )

            # 接近 → 悬停 → 按下
            try:
                self.page.mouse.move(
                    start_x + random.uniform(-40, -15),
                    start_y + random.uniform(-20, 20),
                    steps=random.randint(8, 15),
                )
                time.sleep(random.uniform(0.2, 0.4))
                self.page.mouse.move(start_x, start_y, steps=random.randint(5, 8))
                time.sleep(random.uniform(0.15, 0.3))
            except Exception as e:
                logger.warning(f"【{self.pure_user_id}】真人轨迹回放：接近失败: {e}")
            try:
                slider_button.hover(timeout=2000)
                time.sleep(random.uniform(0.1, 0.3))
            except Exception:
                pass
            try:
                self.page.mouse.move(start_x, start_y)
                time.sleep(random.uniform(0.05, 0.15))
                self.page.mouse.down()
                time.sleep(random.uniform(0.08, 0.15))
            except Exception as e:
                logger.error(f"【{self.pure_user_id}】真人轨迹回放：按下失败: {e}")
                return False

            # 回放真人轨迹
            try:
                start_time = time.time()
                current_x = start_x
                current_y = start_y

                for i, (dx, dy, dt) in enumerate(scaled_drag):
                    current_x = start_x + dx
                    current_y = start_y + dy
                    self.page.mouse.move(current_x, current_y, steps=1)

                    if dt > 0:
                        # CDP RTT 补偿：真人轨迹 dt 是毫秒级精密时序，
                        # CDP 传输本身有 ~80ms 延迟，实际 sleep 要减去这部分
                        cdp_rtt = 0.08
                        sleep_time = max(0.0, dt / 1000.0 - cdp_rtt)
                        if sleep_time > 0:
                            time.sleep(sleep_time * random.uniform(0.85, 1.15))

                    if i == len(scaled_drag) - 1:
                        try:
                            style = slider_button.get_attribute("style")
                            if style and "left:" in style:
                                m = re.search(r'left:\s*([^;]+)', style)
                                if m:
                                    left_px = float(m.group(1).strip().replace('px', ''))
                                    self.trajectory_generator.update_trajectory_data("final_left_px", left_px)
                                    logger.info(f"【{self.pure_user_id}】真人轨迹回放完成: {len(scaled_drag)}步, 最终位置={left_px:.1f}px")
                        except Exception:
                            pass

                # 刮刮乐
                if self.verification_checker.is_scratch_captcha():
                    time.sleep(random.uniform(0.3, 0.5))

                time.sleep(random.uniform(0.02, 0.05))
                self.page.mouse.up()

                elapsed = time.time() - start_time
                logger.info(f"【{self.pure_user_id}】真人轨迹回放完成: 耗时={elapsed:.2f}s")
                return True

            except Exception as e:
                logger.error(f"【{self.pure_user_id}】真人轨迹回放执行失败: {e}")
                try:
                    self.page.mouse.up()
                except Exception:
                    pass
                return False

        except Exception as e:
            logger.error(f"【{self.pure_user_id}】真人轨迹回放异常: {e}")
            return False

    def _get_cookies_after_success(self) -> Optional[Dict[str, str]]:
        """滑块验证成功后获取cookie

        增强逻辑：
        - 取前再检一次当前 URL；如果仍在 punish/x5step=2/pureCaptcha
          路径上，说明实际上未通过，直接返回 None
        - 只返回 x5* 系列；若一个都没有或不含 x5sec，也返回 None
          避免上层误以为过了
        """
        try:
            logger.info(f"【{self.pure_user_id}】开始获取滑块验证成功后的页面cookie...")

            current_url = ""
            try:
                current_url = self.page.url or ""
            except Exception:
                pass
            logger.info(f"【{self.pure_user_id}】当前页面URL: {current_url}")

            try:
                logger.info(f"【{self.pure_user_id}】当前页面标题: {self.page.title()}")
            except Exception:
                pass

            # 从 punish 跳走后留个充足的窗口，让 set-cookie 落盘
            time.sleep(1)

            # 检查 URL 是否还在 punish
            punish_kw = ("punish", "x5step=2", "action=captcha", "pureCaptcha")
            if any(k in current_url for k in punish_kw):
                logger.error(
                    f"【{self.pure_user_id}】❌ 取cookie前发现URL仍在punish，"
                    f"验证未真正通过，跳过cookie获取: {current_url[:120]}"
                )
                return None

            # 获取浏览器中的所有cookie
            cookies = self.context.cookies()
            if not cookies:
                logger.warning(f"【{self.pure_user_id}】未获取到任何cookie")
                return None

            new_cookies: Dict[str, str] = {c["name"]: c["value"] for c in cookies}
            logger.info(
                f"【{self.pure_user_id}】滑块验证成功后已获取cookie，共{len(new_cookies)}个cookie"
            )
            logger.info(
                f"【{self.pure_user_id}】获取到的所有cookie: {list(new_cookies.keys())}"
            )

            # 筛选 x5* 相关 cookies
            filtered: Dict[str, str] = {}
            for name, value in new_cookies.items():
                lname = name.lower()
                if lname.startswith("x5") or "x5sec" in lname:
                    filtered[name] = value
                    logger.info(
                        f"【{self.pure_user_id}】x5相关cookie已获取: {name} = "
                        f"{value[:80]}{'...' if len(value) > 80 else ''}"
                    )

            logger.info(
                f"【{self.pure_user_id}】找到{len(filtered)}个x5相关cookies: "
                f"{list(filtered.keys())}"
            )

            # 必须含 x5sec 才算真过；仅有 x5secdata/x5sectag 等是验证未通过的信号
            if "x5sec" not in filtered:
                logger.error(
                    f"【{self.pure_user_id}】❌ x5相关cookie中没有 x5sec，验证未真正通过。"
                    f"已有key: {list(filtered.keys())}"
                )
                return None

            return filtered

        except Exception as e:
            logger.error(f"【{self.pure_user_id}】获取滑块验证成功后的cookie失败: {str(e)}")
            return None

    def _kill_browser_processes(self) -> int:
        """按本次唯一的 user_data_dir 精确强杀 Chromium 进程（含子进程）。

        仅用于超时守护等"跨线程"场景：sync Playwright 对象绑定创建线程，
        无法在定时器线程上安全关闭；OS 级按进程强杀则跨线程安全、Linux/
        Windows 均可靠。BROWSER_ARGS 含 --no-zygote，Chromium 各子进程命令行
        都会带 --user-data-dir，故按 user_data_dir 匹配可覆盖主进程与子进程。

        user_data_dir 每个实例唯一（见 __init__），只匹配命令行包含该目录的
        进程，绝不误伤用户自己的 Chrome。

        Returns:
            实际强杀的进程数量；Windows 无法精确计数时返回 -1 表示"已尝试"；
            未匹配到任何进程返回 0。
        """
        udir = getattr(self, 'user_data_dir', '') or ''
        if not udir:
            return 0
        try:
            if sys.platform == 'win32':
                # Windows：用 WMI 精确匹配 --user-data-dir 启动参数后强杀。
                # 用 -match + [regex]::Escape + 右边界，避免 user_1 误伤 user_10，
                # 也不会命中用户自己的 Chrome（该目录为本项目专用）。
                ps = (
                    "$re = '--user-data-dir[= ]' + [regex]::Escape('" + udir + "') "
                    "+ '($|[\\s\"''])'; "
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.CommandLine -match $re } | "
                    "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue } catch {} }"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                    capture_output=True, timeout=15,
                )
                return -1
            # Linux/macOS：按命令行匹配后逐个 SIGKILL
            return self._kill_by_cmdline_match(udir)
        except Exception as e:
            logger.warning(f"【{self.pure_user_id}】超时强杀浏览器进程失败（可忽略）: {e}")
            return 0

    def _kill_by_cmdline_match(self, udir: str) -> int:
        """（非 Windows）扫描进程命令行，SIGKILL 所有以本次 user_data_dir 启动的进程。

        优先读取 /proc（Linux/Docker 主场景，纯 stdlib、无需 psutil）；无 /proc
        的环境（如 macOS）退回 ps 命令兜底。

        为避免 user_1 误伤 user_10 这类前缀包含，匹配 --user-data-dir 启动参数
        且其后为边界（空格/引号/结尾），而不是简单的子串包含；同时该目录
        （browser_data/user_*）为本项目专用，绝不会命中用户自己的 Chrome。

        Args:
            udir: 本次实例唯一的 user_data_dir，用于精确匹配

        Returns:
            实际强杀的进程数量
        """
        # --user-data-dir=<udir> 或 --user-data-dir <udir>，其后必须是边界字符
        pattern = re.compile(r"--user-data-dir[= ]" + re.escape(udir) + r"(?=$|[\s\"'])")
        self_pid = os.getpid()
        killed = 0
        if os.path.isdir("/proc"):
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                if pid == self_pid:
                    continue
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as f:
                        cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                if pattern.search(cmdline):
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed += 1
                    except (ProcessLookupError, PermissionError):
                        continue
            return killed
        # 无 /proc：用 ps 兜底
        try:
            out = subprocess.run(
                ["ps", "-eo", "pid=,command="],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            return killed
        for line in out.splitlines():
            line = line.strip()
            if not line or not pattern.search(line):
                continue
            pid_str = line.split(None, 1)[0]
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            if pid == self_pid:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                continue
        return killed

    def _cleanup(self):
        """清理资源 - 与旧框架保持一致的简洁实现"""
        pure_id = getattr(self, 'pure_user_id', 'unknown')
        
        # 关闭页面
        try:
            if hasattr(self, 'page') and self.page:
                self.page.close()
                logger.debug(f"【{pure_id}】页面已关闭")
                self.page = None
        except Exception as e:
            # 忽略线程错误，这是正常的清理过程
            if "cannot switch to a different thread" not in str(e):
                logger.warning(f"【{pure_id}】关闭页面时出错: {e}")

        # 关闭上下文：远程 CDP 默认 context 属于宿主 Chrome，绝不能关闭。
        try:
            if hasattr(self, 'context') and self.context:
                if self._remote_cdp_mode and not self._cdp_context_owned:
                    logger.debug(f"【{pure_id}】CDP模式复用宿主 context，跳过关闭")
                else:
                    self.context.close()
                    logger.debug(f"【{pure_id}】上下文已关闭")
                self.context = None
        except Exception as e:
            # 忽略线程错误，这是正常的清理过程
            if "cannot switch to a different thread" not in str(e):
                logger.warning(f"【{pure_id}】关闭上下文时出错: {e}")

        # 远程 CDP 模式的 browser 是宿主 Chrome，绝不能关闭。
        try:
            if hasattr(self, 'browser') and self.browser:
                if self._remote_cdp_mode:
                    logger.debug(f"【{pure_id}】CDP模式复用宿主 Chrome，跳过关闭")
                else:
                    self.browser.close()
                    logger.info(f"【{pure_id}】浏览器已关闭")
                self.browser = None
        except Exception as e:
            # 忽略线程错误，这是正常的清理过程
            if "cannot switch to a different thread" not in str(e):
                logger.warning(f"【{pure_id}】关闭浏览器时出错: {e}")

        # 【修复】同步停止Playwright，确保资源真正释放
        try:
            if hasattr(self, 'playwright') and self.playwright:
                self.playwright.stop()  # 直接同步停止，不使用异步任务
                logger.info(f"【{pure_id}】Playwright已停止")
                self.playwright = None
        except Exception as e:
            # 忽略线程错误，这是正常的清理过程
            if "cannot switch to a different thread" not in str(e):
                logger.warning(f"【{pure_id}】停止Playwright时出错: {e}")

        # 密码登录保留持久化 user_data_dir；Token 滑块只清理本次创建的临时 profile。
        if self._slider_profile_tempdir is not None:
            try:
                self._slider_profile_tempdir.cleanup()
                logger.info(f"【{pure_id}】滑块临时浏览器目录已清理")
            except Exception as e:
                logger.warning(f"【{pure_id}】清理滑块临时浏览器目录失败: {e}")
            finally:
                self._slider_profile_tempdir = None

    def close(self):
        """关闭服务"""
        pure_id = getattr(self, 'pure_user_id', 'unknown')
        logger.info(f"【{pure_id}】开始清理资源...")
        
        # 先清理浏览器资源
        self._cleanup()

        # 释放账号级互斥锁（必须先于全局槽位释放，让排队中的同账号实例尽快拿到锁）
        if getattr(self, '_account_lock_acquired', False):
            try:
                account_browser_lock_manager.release(pure_id)
                self._account_lock_acquired = False
                logger.info(f"【{pure_id}】账号级浏览器互斥锁已释放")
            except Exception as e:
                logger.warning(f"【{pure_id}】释放账号锁时出错: {e}")

        # 释放槽位（只有获取过槽位才释放）
        if getattr(self, '_slot_acquired', False):
            try:
                concurrency_manager.unregister_instance(self.user_id)
                self._slot_acquired = False
                logger.info(f"【{pure_id}】槽位已释放")
            except Exception as e:
                logger.warning(f"【{pure_id}】释放槽位时出错: {e}")

        logger.info(f"【{pure_id}】资源清理完成")

    def __del__(self):
        """析构函数，确保资源释放"""
        try:
            # 检查是否有未释放的资源
            has_browser = hasattr(self, 'browser') and self.browser
            has_slot = getattr(self, '_slot_acquired', False)
            has_account_lock = getattr(self, '_account_lock_acquired', False)
            
            if has_browser or has_slot or has_account_lock:
                pure_id = getattr(self, 'pure_user_id', 'unknown')
                logger.warning(
                    f"【{pure_id}】析构函数检测到未释放的资源"
                    f"（浏览器={has_browser}, 槽位={has_slot}, 账号锁={has_account_lock}），执行清理"
                )
                self.close()
        except Exception as e:
            try:
                pure_id = getattr(self, 'pure_user_id', 'unknown')
                logger.debug(f"【{pure_id}】析构函数清理时出错: {e}")
            except Exception:
                pass

    def __enter__(self):
        self.init_browser()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def get_slider_stats() -> Dict[str, Any]:
    """获取滑块验证并发统计信息"""
    return concurrency_manager.get_stats()


def run_slider_verification(
    user_id: str, 
    url: str, 
    enable_learning: bool = True, 
    headless: bool = False,
    browser_timeout: int = 20,
    url_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Tuple[bool, Optional[Dict[str, str]]]:
    """在独立进程中运行滑块验证（模块级别函数，支持ProcessPoolExecutor）
    
    Args:
        user_id: 用户ID
        url: 验证页面URL
        enable_learning: 是否启用学习
        headless: 是否无头模式
        browser_timeout: 浏览器验证超时时间（秒），默认20秒
        url_provider: 可选回调，浏览器就绪后用于重新获取新鲜验证链接，避免链接过期
        
    Returns:
        (是否成功, cookies字典)
    """
    slider = None
    try:
        slider = PlaywrightSliderService(
            user_id=user_id,
            enable_learning=enable_learning,
            headless=headless
        )
        return slider.run(url, browser_timeout=browser_timeout, url_provider=url_provider)
    except Exception as e:
        logger.error(f"【{user_id}】滑块验证进程执行失败: {e}")
        return False, None
    finally:
        # 确保资源被释放，即使发生异常
        if slider is not None:
            try:
                slider.close()
            except Exception as close_e:
                logger.warning(f"【{user_id}】滑块验证清理资源时出错: {close_e}")
