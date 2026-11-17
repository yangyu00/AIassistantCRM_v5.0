# -*- coding: utf-8 -*-

import asyncio
import aiohttp
from src.log_util import my_log
import datetime
import time

# client_timeout / global_config / session_context 统一由 http_session 提供；
# 此处转出 client_timeout 以保持既有 `from IOP.get_token import client_timeout` 可用。
from IOP.http_session import global_config, client_timeout, session_context
from IOP.endpoints import token_request


# 无有效 token 时 deadline 统一置为这个过去时间点，保证 TOKENS.deadline
# 恒为 datetime，`now <= deadline` 永远可比较。
_INVALID_DEADLINE = datetime.datetime.min


class Tokens:
    def __init__(self) -> None:
        self.access_token = None
        self.deadline = _INVALID_DEADLINE


TOKENS = Tokens()

# --- 重试次数 / 指数退避 / 失败冷却 ------------------------------------------
# 全部在 import 时从模块级只读配置解析一次，请求路径不读盘。
# 缺段、类型错误、非有限值、越界一律回落默认，部署沿用旧 config.yaml 不启动失败。
_DEFAULT_RETRY = {
    'max_attempts': 4,        # 单轮刷新「请求总次数」，不是「重试次数」
    'backoff_base': 0.2,      # 第 1 次失败后的退避秒数
    'backoff_factor': 2.0,    # 退避倍率
    'backoff_max': 2.0,       # 单次退避上限
    'cooldown': 5.0,          # 一轮全失败后的冷却秒数
}
_MAX_ATTEMPTS_CAP = 20        # 防止配错导致长时间串行循环


def _retry_profile() -> dict:
    cfg = global_config['IOP'].get('retry', {}) or {}

    def num(key, default, minimum, integer=False):
        try:
            v = float(cfg.get(key, default))
        except (TypeError, ValueError):
            v = float(default)
        # 排除 NaN / inf / 负数 / 零（按项校验）
        if v != v or v in (float('inf'), float('-inf')) or v < minimum:
            v = float(default)
        return int(v) if integer else v

    attempts = int(min(num('max_attempts', _DEFAULT_RETRY['max_attempts'], 1, True),
                       _MAX_ATTEMPTS_CAP))
    return {
        'max_attempts': attempts,
        'backoff_base': num('backoff_base', _DEFAULT_RETRY['backoff_base'], 0.0),
        'backoff_factor': num('backoff_factor', _DEFAULT_RETRY['backoff_factor'], 1.0),
        'backoff_max': num('backoff_max', _DEFAULT_RETRY['backoff_max'], 0.0),
        'cooldown': num('cooldown', _DEFAULT_RETRY['cooldown'], 0.0),
    }


RETRY = _retry_profile()


def backoff_seconds(attempt: int) -> float:
    """第 attempt 次失败后的退避秒数（attempt 从 1 起），指数增长并封顶。"""
    return min(RETRY['backoff_base'] * (RETRY['backoff_factor'] ** (attempt - 1)),
               RETRY['backoff_max'])


# --- token 有效期 -------------------------------------------------------------
# 同样在 import 时从模块级只读配置解析一次，请求路径不读盘。
# 缺段、类型错误、非有限值、越界一律回落默认，部署沿用旧 config.yaml 不启动失败。
_DEFAULT_LIFETIME = {
    'default_expires_in': 600.0,   # 上游未给或非法 expires_in 时的兜底秒数
    'refresh_buffer': 360.0,       # 提前刷新缓冲（秒）
    'min_lifetime': 60.0,          # 有效期下限（秒），保证 deadline 落在未来
    'max_expires_in': 86400.0,     # 上游声明值上限，防 datetime 溢出
}


def _lifetime_profile() -> dict:
    cfg = global_config['IOP'].get('token_lifetime', {}) or {}

    def num(key, default, minimum):
        try:
            v = float(cfg.get(key, default))
        except (TypeError, ValueError):
            v = float(default)
        if v != v or v in (float('inf'), float('-inf')) or v < minimum:
            v = float(default)
        return v

    return {
        'default_expires_in': num('default_expires_in',
                                  _DEFAULT_LIFETIME['default_expires_in'], 1.0),
        'refresh_buffer': num('refresh_buffer',
                              _DEFAULT_LIFETIME['refresh_buffer'], 0.0),
        'min_lifetime': num('min_lifetime',
                            _DEFAULT_LIFETIME['min_lifetime'], 1.0),
        'max_expires_in': num('max_expires_in',
                              _DEFAULT_LIFETIME['max_expires_in'], 1.0),
    }


LIFETIME = _lifetime_profile()


def effective_lifetime(raw):
    """把上游 expires_in 换算成写入 TOKENS.deadline 的实际秒数。

    返回 ``(seconds, note)``；note 为 None 表示值正常，否则是需要记日志的原因。
    - 非数字 / bool / NaN / inf / <= 0 → 回落 ``default_expires_in``
    - 超过 ``max_expires_in`` → 截断到上限
    - 有效期 = ``min(max(上游值 - refresh_buffer, min_lifetime), 上游值)``：
      既保证 deadline 永远落在未来（上游值过短时不再出现负的有效期），
      也保证永不超过上游自己声明的有效期。
    """
    p = LIFETIME
    note = None
    try:
        if isinstance(raw, bool):
            raise ValueError('bool is not a lifetime')
        v = float(raw)
    except (TypeError, ValueError):
        v = p['default_expires_in']
        note = f"非数字({raw!r})"
    if v != v or v in (float('inf'), float('-inf')) or v <= 0:
        v = p['default_expires_in']
        note = note or f"非有限或非正({raw!r})"
    if v > p['max_expires_in']:
        v = p['max_expires_in']
        note = note or f"超过上限({raw!r} > {p['max_expires_in']:.0f}s)"
    ttl = min(max(v - p['refresh_buffer'], p['min_lifetime']), v)
    return ttl, note


# 冷却截止点用 time.monotonic()，不受系统时间回拨影响；0 表示不在冷却期。
_COOLDOWN_UNTIL = 0.0
# 同一个冷却周期只打一条 INFO 提示，避免每个失败请求都刷屏。
_COOLDOWN_WARNED_UNTIL = 0.0

# --- token 单飞 ---------------------------------------------------------------
# asyncio.Lock 一旦发生过争用就绑定创建它的 event loop，跨 loop 使用会抛
# RuntimeError。生产是单进程单 loop，但 TestClient、独立脚本可能多次
# asyncio.run()，故按当前 loop 懒创建/重建，不放在模块级裸锁。
_REFRESH_LOCK = None
_REFRESH_LOCK_LOOP = None

# 刷新「代次」：每完成一轮刷新（成功或失败）+1。并发请求在等锁前记下代次，
# 拿锁后若代次已变，说明已有请求替大家刷过一轮，直接复用其结果，不再重复刷新。
# 这样刷新失败时也不会让 N 个等待者各刷一轮；配合失败冷却，同一批并发
# 与冷却期内的新批次都不会重复打认证服务。
_REFRESH_GEN = 0


def _get_refresh_lock() -> asyncio.Lock:
    """返回绑定当前 event loop 的单飞锁；跨 loop 自动重建。"""
    global _REFRESH_LOCK, _REFRESH_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _REFRESH_LOCK is None or _REFRESH_LOCK_LOOP is not loop:
        _REFRESH_LOCK = asyncio.Lock()
        _REFRESH_LOCK_LOOP = loop
    return _REFRESH_LOCK


def token_valid() -> bool:
    """token 是否有效。

    `deadline` 恒为 datetime（无有效 token 时是 `_INVALID_DEADLINE`），
    `now <= deadline` 可直接比较。`access_token is not None` 是
    「拿没拿到 token」的业务条件。
    """
    return (TOKENS.access_token is not None
            and datetime.datetime.now() <= TOKENS.deadline)


async def ensure_token():
    """确保 TOKENS 里有有效 access_token，业务侧统一调用本函数。

    不要直接调用 token2global()，否则绕开并发保护。
    - token 已有效：快速路径直接返回，连锁都不碰
    - 冷却期内：直接返回（不刷新），调用方按「无标签」降级；
      一个冷却周期只打一条 INFO 提示（本项目 WARNING 不落盘）
    - 并发同时判定过期：只有第一个请求真正刷新，其余等锁后二次检查复用结果
    - 单轮刷新：max_attempts 次请求（默认 4），成功即返回；失败之间按
      backoff_base × factor^(k-1) 指数退避并封顶；每次失败/重试都有日志
    - 单轮全失败：进入 cooldown 冷却，刷新失败不抛异常
    """
    global _REFRESH_GEN, _COOLDOWN_UNTIL, _COOLDOWN_WARNED_UNTIL
    if token_valid():
        return

    now = time.monotonic()
    if now < _COOLDOWN_UNTIL:
        if _COOLDOWN_WARNED_UNTIL != _COOLDOWN_UNTIL:
            _COOLDOWN_WARNED_UNTIL = _COOLDOWN_UNTIL
            # src/log_util.py 只落盘 INFO / ERROR，WARNING 不会写进日志文件。
            # 冷却是预期内的快速降级，用 INFO；不加级别则生产侧看不到这条提示。
            my_log.info(
                f"IOP token 处于失败冷却期 | remain={_COOLDOWN_UNTIL - now:.1f}s | "
                f"本轮请求直接降级为空标签"
            )
        return

    gen = _REFRESH_GEN
    async with _get_refresh_lock():
        # 二次检查：别人已刷成功、已有人替本批并发刷过一轮（含失败），
        # 或持锁等待期间刚进入冷却期
        if (token_valid() or _REFRESH_GEN != gen
                or time.monotonic() < _COOLDOWN_UNTIL):
            return

        n = RETRY['max_attempts']
        start = time.perf_counter()
        my_log.info(f"IOP token 刷新开始 | max_attempts={n}")
        try:
            for attempt in range(1, n + 1):
                await token2global()
                if token_valid():
                    my_log.info(
                        f"IOP token 刷新成功 | attempt={attempt}/{n} | "
                        f"cost={(time.perf_counter() - start) * 1000:.0f}ms"
                    )
                    _COOLDOWN_UNTIL = 0.0
                    return
                if attempt < n:
                    delay = backoff_seconds(attempt)
                    # 同样用 ERROR：WARNING 不落盘，且这确实是一次失败尝试，
                    # 应与 token2global 的 IOP token 请求失败 一起出现在 ERROR 日志里。
                    my_log.error(
                        f"IOP token 刷新重试 | attempt={attempt}/{n} | "
                        f"backoff={delay:.2f}s"
                    )
                    await asyncio.sleep(delay)
            _COOLDOWN_UNTIL = time.monotonic() + RETRY['cooldown']
            my_log.error(
                f"IOP token 刷新失败 | attempts={n} | "
                f"cooldown={RETRY['cooldown']:.1f}s | "
                f"cost={(time.perf_counter() - start) * 1000:.0f}ms"
            )
        finally:
            _REFRESH_GEN += 1


# ---------------------------------------------------------------------------


def _fail_token(status_code, reason):
    """所有失败分支统一清空 TOKENS 并记 ERROR，绝不沿用可能已过期的旧值。

    `deadline` 置回 `_INVALID_DEADLINE`（过去时间），保持「恒为 datetime」
    的不变量。日志前缀为 `IOP token 请求失败`，附带 status 与具体原因；
    项目只落盘 INFO / ERROR，故用 ERROR。
    """
    TOKENS.access_token = None
    TOKENS.deadline = _INVALID_DEADLINE
    my_log.error(f"IOP token 请求失败 | status={status_code} | {reason}")


async def token2global():
    """单次刷新 token 并写入 TOKENS。

    只由 ensure_token() 在单飞锁内调用；业务侧请勿直接调用，否则绕开
    并发保护。先查状态码，再校验响应结构；所有失败分支统一走 _fail_token()，
    不让「既无 access_token 也无 error」的响应沿用旧 token。
    成功分支的有效期由 effective_lifetime() 换算，保证 deadline 落在未来
    且不抛 ValueError。
    """
    # 上游仅提供 http 明文 token 端点；client_id / client_secret 以表单串提交，
    # 凭据存放于 config.yaml。均为上游与部署环境的既定要求，不可改动。
    url, body = token_request(global_config)

    # 复用应用级共享 session（lifespan 创建/关闭）。
    # session 已带 timeout 与 skip_auto_headers，这里只发请求、不再构造参数。
    status_code = None
    try:
        async with session_context() as client:
            async with client.post(url, data=body) as response:
                status_code = response.status
                # 先查状态码。502 / 网关错误页即使 body 是合法 JSON，
                # 也不该进入「按字段判断」的路径；不读 body 直接失败。
                if status_code != 200:
                    _fail_token(status_code, "HTTP 状态码非 200")
                    return
                try:
                    # 上游返回的 Content-Type 不规范，不做类型校验
                    res_data = await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError) as e:
                    _fail_token(status_code, f"响应解析失败 | error={type(e).__name__}: {e}")
                    return
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        # 超时/网络异常就地收口，不让异常穿透到 main.py（否则 IOP 侧没有日志）
        _fail_token(status_code, f"error={type(e).__name__}: {e}")
        return

    # res_data 可能是 list / 字符串 / None（HTML 错误页被强行解析时也可能如此）
    if not isinstance(res_data, dict):
        _fail_token(status_code, f"响应结构异常 | type={type(res_data).__name__}")
        return

    if 'access_token' in res_data:
        access_token = res_data['access_token']
        if not access_token:
            _fail_token(status_code, "access_token 为空")
            return
        # expires_in 缺失时静默用默认值；字段存在但非法/过短/过大时
        # 由 effective_lifetime() 兜底或夹取，并记一条 INFO（token 本身有效，
        # 不算失败，故不用 ERROR）。
        if 'expires_in' in res_data:
            lifetime, note = effective_lifetime(res_data['expires_in'])
        else:
            lifetime, note = effective_lifetime(LIFETIME['default_expires_in'])
        if note:
            my_log.info(
                f"IOP token expires_in 异常 | {note} | "
                f"按 {lifetime:.0f}s 计算有效期"
            )
        TOKENS.deadline = datetime.datetime.now() + datetime.timedelta(seconds=lifetime)
        TOKENS.access_token = access_token
        return

    if 'error' in res_data:
        _fail_token(status_code, f"上游返回错误 | error={res_data.get('error')} | "
                                f"error_description={res_data.get('error_description', '')}")
        return

    # 既无 access_token 也无 error（如网关返回的业务错误 JSON），
    # 不能沿用旧的、可能已过期的 access_token。
    _fail_token(status_code, "响应缺少 access_token 且无 error 字段")
