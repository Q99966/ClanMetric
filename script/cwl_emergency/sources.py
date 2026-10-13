"""官方 Clash of Clans 与 ClashKing JSON 接口的轻量客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential


# 只保存 API 根地址；身份验证和传输配置由客户端对象单独管理。
OFFICIAL_BASE = "https://api.clashofclans.com/v1"
CLASHKING_BASE = "https://api.clashk.ing"
# 单次 HTTP 请求各阶段上限，避免连接或读取无限等待。
CONNECT_TIMEOUT_SECONDS = 5.0  # 建立服务端连接的最长秒数。
READ_TIMEOUT_SECONDS = 20.0  # 等待响应数据的最长秒数。
WRITE_TIMEOUT_SECONDS = 10.0  # 发送请求数据的最长秒数。
POOL_TIMEOUT_SECONDS = 5.0  # 等待连接池空位的最长秒数。
# 仅网络/超时与服务端 5xx 重试；429 和所有 4xx 不重试。
MAX_ATTEMPTS = 3  # 最多尝试次数，包含第一次请求。
RETRY_WAIT_MIN_SECONDS = 1  # 指数退避最短等待秒数。
RETRY_WAIT_MAX_SECONDS = 2  # 指数退避最长等待秒数。


class RuntimeSettings(BaseSettings):
    """从 script/.env 和进程环境读取的运行配置。

    属性:
        clan_tag: 默认目标部落标签，可由命令行参数覆盖。
        coc_api_token: 官方 API 密钥，使用 SecretStr 防止意外显示。
        proxy_url: IP 配置项中的完整 HTTP(S) 代理 URL，也以 SecretStr 保存。
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        case_sensitive=False,
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
    )

    clan_tag: str | None = None
    coc_api_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("COC_API_KEY", "COC_API_TOKEN"),
    )
    proxy_url: SecretStr | None = Field(default=None, validation_alias="IP")

    def safe_proxy_url(self) -> str | None:
        """验证代理 URL 并返回供 HTTPX 使用的原值。

        返回:
            带协议和端口的 HTTP(S) URL；未配置时返回 None。
        异常:
            配置不是完整 HTTP(S) 代理地址时抛出 ValueError。
        """
        value = self.proxy_url.get_secret_value().strip() if self.proxy_url else ""
        if not value:
            return None
        parsed = httpx.URL(value)
        if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.port is None:
            raise ValueError("script/.env 的 IP 必须是带协议和端口的 HTTP(S) 代理 URL。")
        return value


class RetryableServerError(RuntimeError):
    """可重试的 HTTP 服务端错误。"""

    pass


class RetryableNetworkError(RuntimeError):
    """可重试的连接、读取或写入网络错误。"""

    pass


class SourceRequestError(RuntimeError):
    """来源请求或数据验证失败的统一异常类型。"""

    pass


@dataclass
class FetchResult:
    """一次来源请求的状态、响应数据和安全摘要字段。

    status_code 是 HTTP 状态；payload 保存解析后的 JSON；error 保存非敏感错误。
    retry_after 与 attempts 分别记录来源等待提示和本次实际尝试次数。
    """
    status_code: int
    payload: Any = None
    error: str | None = None
    retry_after: str | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        """仅在 2xx 且 JSON/请求未出错时返回 True。"""
        return 200 <= self.status_code < 300 and self.error is None


class JsonHTTPClient:
    """带超时、连接池和有限重试策略的同步 JSON GET 客户端。

    参数:
        base_url: 来源 API 根地址。
        headers: 可选请求头；授权信息只在官方客户端实例内传递。
        proxy_url: 可选 HTTP(S) 代理地址，不写入结果或日志。
        trust_env: 是否读取 HTTPX 的系统代理与证书相关环境变量。
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        proxy_url: str | None = None,
        trust_env: bool = True,
    ):
        """创建 HTTPX 会话并应用统一传输限制。

        参数:
            base_url: 来源 API 根地址。
            headers: 该来源请求需要的额外请求头。
            proxy_url: 可选代理地址。
            trust_env: 是否采用 HTTPX 的系统环境配置；强制直连时为 False。
        """
        self.base_url = base_url.rstrip("/")
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
            write=WRITE_TIMEOUT_SECONDS,
            pool=POOL_TIMEOUT_SECONDS,
        )
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Accept": "application/json", **(headers or {})},
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            proxy=proxy_url,
            trust_env=trust_env,
        )

    def close(self) -> None:
        """释放底层 HTTPX 连接池资源。"""
        self.client.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> FetchResult:
        """执行 GET 并解析 JSON；按来源状态执行受限重试。

        参数:
            path: 相对 API 根地址的路径。
            params: 可选查询参数。
        返回:
            含 HTTP 状态、JSON 响应或非敏感错误的 FetchResult。
        """
        attempts = 0
        last_status_code = 0

        def request() -> FetchResult:
            """执行单次 HTTP 尝试；仅抛出策略允许重试的错误。"""
            nonlocal attempts, last_status_code
            attempts += 1
            # 本次若未取得响应，状态码必须回到 0，不能沿用上次重试的 HTTP 状态。
            last_status_code = 0
            try:
                response = self.client.get(path, params=params)
            except httpx.TimeoutException as exc:
                raise RetryableNetworkError(f"请求超时（{type(exc).__name__}）。") from exc
            except httpx.NetworkError as exc:
                raise RetryableNetworkError(f"网络请求失败（{type(exc).__name__}）。") from exc
            last_status_code = response.status_code
            if response.status_code >= 500:
                raise RetryableServerError(f"来源返回 HTTP {response.status_code}。")
            if response.status_code == 429:
                return FetchResult(
                    status_code=429,
                    error="来源限流；本轮不重试，避免绕过来源等待要求。",
                    retry_after=response.headers.get("Retry-After"),
                    attempts=attempts,
                )
            try:
                payload = response.json()
            except ValueError:
                return FetchResult(
                    status_code=response.status_code,
                    error="响应不是有效 JSON。",
                    attempts=attempts,
                )
            return FetchResult(status_code=response.status_code, payload=payload, attempts=attempts)

        # Tenacity 只包装临时网络与 5xx 错误，客户端错误不会被重放。
        retrying = Retrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN_SECONDS, max=RETRY_WAIT_MAX_SECONDS),
            retry=retry_if_exception_type((RetryableServerError, RetryableNetworkError)),
            reraise=True,
        )
        try:
            for attempt in retrying:
                with attempt:
                    return request()
        except (RetryableServerError, RetryableNetworkError) as exc:
            # 保留已收到的最后一个 HTTP 状态；只有从未收到响应时才使用 0。
            return FetchResult(
                status_code=last_status_code,
                error=f"{exc} 已尝试 {attempts} 次。",
                attempts=attempts,
            )
        return FetchResult(status_code=0, error="请求未执行。", attempts=attempts)


class ClashKingClient(JsonHTTPClient):
    """ClashKing 联赛索引和赛季详情接口客户端。

    参数:
        base_url: ClashKing API 根地址，默认使用公开服务地址。
        proxy_url: 可选 HTTP(S) 代理地址。
        trust_env: 是否读取 HTTPX 的系统环境配置。
    """

    def __init__(
        self,
        base_url: str = CLASHKING_BASE,
        proxy_url: str | None = None,
        trust_env: bool = True,
    ):
        """初始化 ClashKing 请求客户端。

        参数:
            base_url: API 根地址。
            proxy_url: 可选代理地址。
            trust_env: 是否采用 HTTPX 的系统环境配置。
        """
        super().__init__(base_url, proxy_url=proxy_url, trust_env=trust_env)

    def list_seasons(self, clan_tag: str, limit: int = 200) -> FetchResult:
        """读取部落可见的联赛赛季索引。

        参数:
            clan_tag: 目标部落标签。
            limit: 请求索引项上限；来源没有声明总数或分页游标。
        返回:
            索引端点的 FetchResult。
        """
        path = f"/v2/cwl/{quote(clan_tag, safe='')}/seasons"
        return self.get_json(path, params={"limit": limit})

    def get_season(self, clan_tag: str, season_id: str) -> FetchResult:
        """读取部落与指定完整赛季标识关联的详情。

        参数:
            clan_tag: 目标部落标签。
            season_id: 来源索引中的完整赛季日期标识。
        返回:
            赛季详情端点的 FetchResult。
        """
        path = f"/cwl/{quote(clan_tag, safe='')}/{quote(season_id, safe='')}"
        return self.get_json(path)


class OfficialClient(JsonHTTPClient):
    """官方 API 客户端，支持部落、当前联赛组和指定战争查询。

    参数:
        api_token: 官方密钥；SecretStr 或普通字符串均可。
        base_url: 官方 API 根地址。
        proxy_url: 可选 HTTP(S) 代理地址。
        trust_env: 是否读取 HTTPX 的系统环境配置。
    """

    def __init__(
        self,
        api_token: SecretStr | str | None,
        base_url: str = OFFICIAL_BASE,
        proxy_url: str | None = None,
        trust_env: bool = True,
    ):
        """验证密钥并建立带 Bearer 授权头的官方 API 会话。

        参数:
            api_token: 官方 API 密钥。
            base_url: 官方 API 根地址。
            proxy_url: 可选代理地址。
            trust_env: 是否采用 HTTPX 的系统环境配置。
        """
        if isinstance(api_token, SecretStr):
            token = api_token.get_secret_value()
        else:
            token = api_token
        if not token:
            raise ValueError("未设置 COC_API_TOKEN。")
        super().__init__(
            base_url,
            headers={"Authorization": f"Bearer {token}"},
            proxy_url=proxy_url,
            trust_env=trust_env,
        )

    def get_clan(self, clan_tag: str) -> FetchResult:
        """查询指定部落当前资料。

        参数:
            clan_tag: 完整部落标签。
        返回:
            官方部落接口请求结果。
        """
        return self.get_json(f"/clans/{quote(clan_tag, safe='')}")

    def get_current_group(self, clan_tag: str) -> FetchResult:
        """查询指定部落当前联赛组；HTTP 错误不会解释成没有联赛。

        参数:
            clan_tag: 完整部落标签。
        返回:
            官方当前联赛组接口请求结果。
        """
        return self.get_json(f"/clans/{quote(clan_tag, safe='')}/currentwar/leaguegroup")

    def get_cwl_war(self, war_tag: str) -> FetchResult:
        """按已核实的 warTag 查询一场官方联赛战争。

        参数:
            war_tag: 已通过目标关系或同轮来源信息核实的战争标签。
        返回:
            官方战争详情请求结果。
        """
        return self.get_json(f"/clanwarleagues/wars/{quote(war_tag, safe='')}")


def request_summary(result: FetchResult) -> dict[str, Any]:
    """生成不含密钥、代理地址或完整战绩的来源请求摘要。

    参数:
        result: 已完成的来源请求。
    返回:
        HTTP 状态、请求成功标记、字段名与安全错误摘要。
    """
    payload = result.payload if isinstance(result.payload, dict) else None
    # 保留官方错误对象中的少量诊断字段；不把任意响应正文写入报告。
    api_error = {
        key: payload[key]
        for key in ("reason", "message", "type")
        if not result.ok and payload is not None and isinstance(payload.get(key), str)
    }
    return {
        "http_status": result.status_code,
        "request_ok": result.ok,
        "attempts": result.attempts,
        "top_level_fields": sorted(payload) if payload is not None else [],
        "api_error": api_error or None,
        "error": result.error,
        "retry_after": result.retry_after,
    }
