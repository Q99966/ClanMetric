"""应急联赛脚本的命令行入口、交互菜单和工作流编排。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from .analysis import (
    DEFAULT_ORDER,
    ORDER_LABELS,
    AnalysisError,
    analyze_archive,
    canonical_tag,
    parse_order,
    write_csvs,
)
from .collector import (
    _target_clan,
    collect_season,
    explicit_season_exists,
    find_season_items,
    official_no_current_league,
)
from .sources import ClashKingClient, FetchResult, OfficialClient, RuntimeSettings, request_summary
from .storage import (
    atomic_write_json,
    canonical_archive_path,
    find_archive,
    merge_archives,
    pending_archive_path,
    read_archive,
    result_paths,
)


# 所有输入、样例与输出路径均相对 script/ 固定，避免依赖调用者当前目录。
SCRIPT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = SCRIPT_DIR / "data"
RESULTS_DIR = SCRIPT_DIR / "results"
# 无参数年月按约定使用上海当地年月。
CURRENT_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 只有这些首参数会切换到独立子命令解析。
ACTION_WORDS = {"export", "backfill", "season", "check-official", "check-clashking"}


def parse_month(value: str | None) -> tuple[str, bool]:
    """验证 YYYYMM 参数；省略时返回上海时区当前月份。

    参数:
        value: 用户指定的六位年月，或 None。
    返回:
        (年月字符串, 是否因省略参数而采用当前月)。
    """
    if value is None:
        return datetime.now(CURRENT_TIMEZONE).strftime("%Y%m"), True
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("年月参数必须为六位 YYYYMM，例如 202412。")
    try:
        datetime.strptime(value, "%Y%m")
    except ValueError as exc:
        raise ValueError("年月无效；月份必须为 01～12。") from exc
    return value, False


def is_full_season_date(value: str) -> bool:
    """判断来源标识是否为有效且规范的 YYYY-MM-DD 日期。

    参数:
        value: 来源返回的赛季标识。
    返回:
        日期有效且字符串格式规范时为 True。
    """
    try:
        return len(value) == 10 and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def show_order_menu() -> None:
    """打印唯一的 A～F 排序菜单和默认输入说明。"""
    print("请选择排名参考项，按优先级从高到低输入代号，统一降序：")
    for key in "ABCDEF":
        print(f"{key}：{ORDER_LABELS[key]}")
    print("例如输入 A B C：先比较 A；相同时再比较 B；再比较 C。")
    print("直接回车：使用默认 A B D C F。全部相同后按玩家标签升序。")


def choose_order(value: str | None, *, interactive: bool) -> tuple[str, ...]:
    """从命令行、交互输入或约定默认值取得一次排序序列。

    参数:
        value: 显式排序代号字符串；None 表示需询问或使用默认值。
        interactive: 是否从终端读取并在输入无效时循环重试。
    返回:
        经校验的优先级元组。
    """
    show_order_menu()
    if value is not None:
        order = parse_order(value)
    elif interactive:
        while True:
            entered = input("请输入排序代号：")
            try:
                order = parse_order(entered)
                break
            except AnalysisError as exc:
                print(f"排序输入无效：{exc} 请重新输入。")
    else:
        order = DEFAULT_ORDER
        print("当前为非交互运行，使用已确认默认排序 A B D C F。")
    print(f"本次排序：{' '.join(order)}（{' → '.join(ORDER_LABELS[item] for item in order)}）")
    return order


def _validate_season_month(season_id: str, target_month: str) -> bool:
    """核验完整赛季标识的 YYYY-MM 部分与用户目标年月一致。

    参数:
        season_id: 来源返回的赛季标识。
        target_month: 六位 YYYYMM 目标年月。
    返回:
        年月一致时为 True。
    """
    return isinstance(season_id, str) and season_id[:7].replace("-", "") == target_month


def _usable_index(result: FetchResult) -> tuple[bool, str | None]:
    """检查 ClashKing 索引请求成功且含 OpenAPI 定义的 items 数组。

    参数:
        result: ClashKing 索引请求结果。
    返回:
        (是否可用, 不可用原因)。
    """
    if not result.ok:
        return False, result.error or f"ClashKing 赛季索引 HTTP {result.status_code}。"
    if not isinstance(result.payload, dict) or not isinstance(result.payload.get("items"), list):
        return False, "ClashKing 赛季索引响应缺少 items 数组。"
    return True, None


def _settings_clan_tag(settings: RuntimeSettings, cli_tag: str | None) -> str:
    """取得并校验目标部落标签，命令行值优先于环境设置。

    参数:
        settings: 从环境及 .env 读取的配置。
        cli_tag: 命令行显式标签，可省略。
    返回:
        规范化的部落标签。
    """
    tag = canonical_tag(cli_tag or settings.clan_tag)
    if not tag:
        raise ValueError("缺少部落标签；请设置 CLAN_TAG 环境变量或传入 --clan-tag。")
    if not re.fullmatch(r"#[A-Z0-9]{3,15}", tag):
        raise ValueError("部落标签格式无效；请提供游戏内完整部落标签。")
    return tag


def _source_proxy_options(settings: RuntimeSettings, *, no_proxy: bool) -> tuple[str | None, bool]:
    """解析网络请求的代理地址及 HTTPX 环境变量开关。

    参数:
        settings: 从环境及 .env 读取的运行配置。
        no_proxy: 为 True 时强制直连，并忽略系统代理环境变量。
    返回:
        (代理 URL, 是否读取 HTTPX 环境变量)。
    """
    if no_proxy:
        return None, False
    return settings.safe_proxy_url(), True


def _make_clashking_client(settings: RuntimeSettings, *, no_proxy: bool = False) -> ClashKingClient:
    """按运行设置创建 ClashKing 客户端，可强制直连。"""
    proxy_url, trust_env = _source_proxy_options(settings, no_proxy=no_proxy)
    return ClashKingClient(proxy_url=proxy_url, trust_env=trust_env)


def _data_dir_and_results() -> tuple[Path, Path]:
    """创建并返回脚本固定使用的数据归档与结果目录。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR, RESULTS_DIR


def _save_archive_only(incoming: dict[str, Any]) -> Path:
    """合并并原子保存逐轮检查点，不执行排名计算。

    参数:
        incoming: 当前进度对应的部分归档快照。
    返回:
        已写入的正式归档或待识别归档路径。
    """
    data_dir, _ = _data_dir_and_results()
    clan = incoming.get("clan") or {}
    season = incoming.get("season") or {}
    clan_tag = canonical_tag(clan.get("tag"))
    clan_name = str(clan.get("name") or "")
    season_id = str(season.get("id") or "")
    if is_full_season_date(season_id) and clan_name:
        target_path = find_archive(data_dir, clan_tag, season_id) or canonical_archive_path(
            data_dir, clan_name, clan_tag, season_id
        )
    else:
        target_path = pending_archive_path(
            data_dir,
            clan_name or "未识别部落",
            clan_tag,
            season_id or str((incoming.get("discovery") or {}).get("target_month") or "unknown"),
        )
    existing = read_archive(target_path) if target_path.exists() else None
    archive = merge_archives(existing, incoming)
    atomic_write_json(target_path, archive)
    return target_path


def _save_and_export(incoming: dict[str, Any], order: tuple[str, ...]) -> tuple[Path, tuple[Path, Path] | None]:
    """保全当前归档，并在来源身份完整时刷新两份 CSV。

    参数:
        incoming: 当前赛季采集结果。
        order: 本次由高到低排列的排序代号。
    返回:
        (归档路径, 两份 CSV 路径)；不能正式导出时第二项为 None。
    """
    data_dir, results_dir = _data_dir_and_results()
    clan = incoming.get("clan") or {}
    season = incoming.get("season") or {}
    clan_tag = canonical_tag(clan.get("tag"))
    clan_name = str(clan.get("name") or "")
    season_id = str(season.get("id") or "")
    if is_full_season_date(season_id) and clan_name:
        existing_path = find_archive(data_dir, clan_tag, season_id)
        target_path = existing_path or canonical_archive_path(data_dir, clan_name, clan_tag, season_id)
    else:
        target_path = pending_archive_path(data_dir, clan_name or "未识别部落", clan_tag, season_id or str((incoming.get("discovery") or {}).get("target_month") or "unknown"))
    existing = None
    if target_path.exists():
        existing = read_archive(target_path)
    archive = merge_archives(existing, incoming)
    atomic_write_json(target_path, archive)
    print(f"已保存归档：{target_path}")
    if not (is_full_season_date(season_id) and clan_name):
        print(f"赛季标识 {season_id or '缺失'} 或部落名称尚未核实；已保存在待识别归档中，暂不生成正式命名 CSV。")
        return target_path, None
    try:
        result = analyze_archive(archive, order)
        ranking_path, diff_path = result_paths(results_dir, clan_name, clan_tag, season_id, order)
        try:
            write_csvs(archive, result, results_dir, ranking_path, diff_path)
        except PermissionError as exc:
            print(f"归档已保全，但 Windows 拒绝替换 CSV 文件：{exc}")
            print("请关闭 Excel/WPS 中打开的同名 CSV 和文件预览，确认目标文件可写后重试。")
            print(f'可直接离线重导，不会重新请求 API：python emergency_cwl.py export "{target_path}" --sort-order "{" ".join(order)}"')
            print("失败时生成的 .tmp 文件已保留；如确认内容后可用于恢复。")
            return target_path, None
        except OSError as exc:
            print(f"归档已保全，但写入 CSV 失败：{exc}")
            print(f'请检查结果目录权限后离线重试：python emergency_cwl.py export "{target_path}" --sort-order "{" ".join(order)}"')
            return target_path, None
        print(f"排名状态：{'有效' if result.rankable else '暂停排名'}；赛季终局：{'完整' if result.season_complete else '尚未确认完整'}")
        if result.missing:
            print("缺失／待核验：" + "；".join(result.missing))
        print(f"排名 CSV：{ranking_path}")
        print(f"大本差核对 CSV：{diff_path}")
        return target_path, (ranking_path, diff_path)
    except AnalysisError as exc:
        print(f"归档已保全；尚不能生成 CSV：{exc}")
        return target_path, None


def _construct_sources(
    settings: RuntimeSettings,
    *,
    no_proxy: bool = False,
) -> tuple[ClashKingClient, OfficialClient | None]:
    """建立 ClashKing 客户端和（存在密钥时的）官方客户端。

    参数:
        settings: 含 API 密钥和代理的运行配置。
        no_proxy: 为 True 时强制直连，并忽略系统代理环境变量。
    返回:
        (必需的 ClashKing 客户端, 可选的官方客户端)。
    """
    proxy_url, trust_env = _source_proxy_options(settings, no_proxy=no_proxy)
    ck = ClashKingClient(proxy_url=proxy_url, trust_env=trust_env)
    official = OfficialClient(settings.coc_api_token, proxy_url=proxy_url, trust_env=trust_env) if settings.coc_api_token else None
    return ck, official


def _describe_official_status(group_result: FetchResult | None, target_month: str, current_month: str) -> None:
    """用经过验证的状态解释官方请求结果，不把错误当成无联赛。

    参数:
        group_result: 官方当前联赛组请求结果。
        target_month: 本次处理的 YYYYMM。
        current_month: 上海时区当前 YYYYMM，用于措辞区分历史请求。
    """
    if group_result is None:
        print("官方接口未执行：缺少 COC_API_KEY；此情况不等同于确认没有联赛。")
        return
    if official_no_current_league(group_result):
        if target_month == current_month:
            print("当前联赛并未进行，切换 ClashKing 渠道。")
        else:
            print(f"官方当前接口返回 state=notInWar；这不能证明历史目标 {target_month} 没有联赛，将按指定月份查询 ClashKing。")
    elif group_result.ok and isinstance(group_result.payload, dict):
        season = str(group_result.payload.get("season") or "未知")
        if season[:7].replace("-", "") != target_month:
            print(f"官方当前联赛标识为 {season}，不匹配目标 {target_month}；仍查询指定月份的 ClashKing 赛季索引。")
    elif group_result.status_code in (401, 403):
        payload = group_result.payload if isinstance(group_result.payload, dict) else {}
        detail = str(payload.get("message") or payload.get("reason") or "")
        suffix = f"；接口说明：{detail}" if detail else ""
        print(f"官方接口鉴权或访问权限失败（HTTP {group_result.status_code}）{suffix}；不会将其解释为没有联赛。")
    elif group_result.status_code == 429:
        print(f"官方接口限流；停止该来源请求。Retry-After={group_result.retry_after or '来源未提供'}。")
    elif group_result.status_code == 404:
        payload = group_result.payload if isinstance(group_result.payload, dict) else {}
        reason = str(payload.get("reason") or "未知")
        message = str(payload.get("message") or "")
        if reason == "notFound":
            print(
                "官方当前联赛组接口返回 HTTP 404（reason=notFound）："
                "请求的当前联赛组资源未找到。这不证明目标月份没有历史联赛；继续查询 ClashKing。"
            )
        else:
            detail = f"reason={reason}" + (f"，{message}" if message else "")
            print(
                f"官方当前联赛组接口未找到请求资源（HTTP 404，{detail}）；"
                "不会将其解释成目标月份没有历史联赛，继续查询 ClashKing。"
            )
    elif 500 <= group_result.status_code < 600:
        detail = group_result.error or f"HTTP {group_result.status_code}"
        print(f"官方接口服务端暂不可用（{detail}）；不会将其解释为没有联赛，改查 ClashKing。")
    else:
        payload = group_result.payload if isinstance(group_result.payload, dict) else {}
        api_detail = "；".join(
            f"{key}={payload[key]}"
            for key in ("reason", "message", "type")
            if isinstance(payload.get(key), str)
        )
        detail = group_result.error or (
            f"HTTP {group_result.status_code}" if group_result.status_code else "未取得 HTTP 响应"
        )
        if api_detail:
            detail = f"{detail}（{api_detail}）"
        print(f"官方接口请求未成功（{detail}）；不会将其解释为没有联赛。")


def _run_season(
    *,
    clan_tag: str,
    target_month: str,
    requested_id: str,
    discovery_item: dict[str, Any] | None,
    index_result: FetchResult,
    ck: ClashKingClient,
    official: OfficialClient | None,
    official_group_result: FetchResult | None,
    month_count: int,
    order: tuple[str, ...],
) -> None:
    """取得并处理单届赛季详情，逐轮保存后更新该届 CSV。

    参数:
        clan_tag: 目标部落标签。
        target_month: 用户指定的 YYYYMM。
        requested_id: 索引发现或用户显式指定的赛季标识。
        discovery_item: 对应的索引条目，可缺失。
        index_result: 已执行的 ClashKing 索引结果。
        ck: ClashKing 客户端。
        official: 可选的官方客户端。
        official_group_result: 官方当前组结果。
        month_count: 本次发现的该月赛季条数。
        order: 输出排序优先级。
    """
    season_result = ck.get_season(clan_tag, requested_id)
    # 只采用响应明确返回且仍属于目标月份的来源标识。
    actual_id = requested_id
    if season_result.ok and isinstance(season_result.payload, dict):
        returned = season_result.payload.get("season")
        if isinstance(returned, str) and _validate_season_month(returned, target_month):
            actual_id = returned
        elif isinstance(returned, str):
            print(f"跳过来源身份不符的结果：请求月份 {target_month}，响应 season={returned}。")
    incoming = collect_season(
        clan_tag=clan_tag,
        season_id=actual_id,
        discovery_item=discovery_item,
        index_result=index_result,
        season_result=season_result,
        official_group_result=official_group_result,
        official_client=official,
        index_month_count=month_count,
        target_month=target_month,
        on_progress=_save_archive_only,
    )
    if actual_id != requested_id:
        incoming.setdefault("collection", {}).setdefault("notes", []).append(
            f"来源按请求 {requested_id} 返回完整赛季标识 {actual_id}；归档采用来源返回值。"
        )
        incoming["season"]["id"] = actual_id
    if season_result.ok and isinstance(season_result.payload, dict):
        returned = season_result.payload.get("season")
        if isinstance(returned, str) and not _validate_season_month(returned, target_month):
            incoming["collection"]["status"] = "identity_mismatch"
            incoming["collection"]["errors"].append(f"ClashKing 返回 season={returned}，与目标月份不符。")
            incoming["season"]["id"] = requested_id
    _save_and_export(incoming, order)


def collect_month(yyyymm: str | None, args: argparse.Namespace) -> int:
    """执行指定年月全部已发现赛季的逐届采集流程。

    参数:
        yyyymm: 用户年月；省略时使用上海时区当前月份。
        args: 主命令解析结果，含部落标签、排序输入和代理模式。
    返回:
        命令退出码；一个月份内每届仍独立保全和导出。
    """
    try:
        target_month, is_current = parse_month(yyyymm)
        settings = RuntimeSettings()
        clan_tag = _settings_clan_tag(settings, args.clan_tag)
        order = choose_order(args.sort_order, interactive=args.sort_order is None and sys.stdin.isatty())
    except (ValueError, AnalysisError) as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    current_month = datetime.now(CURRENT_TIMEZONE).strftime("%Y%m")
    print(f"目标年月：{target_month}；当前年月按 Asia/Shanghai 判定为 {current_month}。")
    if is_current:
        print(f"未提供年月，使用当前年月；指定年月用法：python emergency_cwl.py {current_month}")
    print(f"目标部落：{clan_tag}")

    ck, official = _construct_sources(settings, no_proxy=args.no_proxy)
    try:
        index_result = ck.list_seasons(clan_tag)
        index_ok, index_error = _usable_index(index_result)
        if not index_ok:
            print(f"ClashKing 赛季索引不可用：{index_error}")
        group_result = official.get_current_group(clan_tag) if official else None
        _describe_official_status(group_result, target_month, current_month)
        # ClashKing 索引未声明总数/游标，因此只处理已发现的精确月份条目。
        items = find_season_items(index_result, f"{target_month[:4]}-{target_month[4:]}") if index_ok else []
        season_ids = [str(item.get("season")) for item in items]
        if not items and group_result and group_result.ok and isinstance(group_result.payload, dict):
            official_season = str(group_result.payload.get("season") or "")
            if official_season[:7].replace("-", "") == target_month and official_season:
                items = [{"season": official_season, "state": group_result.payload.get("state"), "discovery": "official_current_group_fallback"}]
                season_ids = [official_season]
        if not items:
            if index_ok:
                print(f"ClashKing 索引未返回 {target_month[:4]}-{target_month[4:]} 的赛季；没有切换查询其他月份。")
            else:
                print("目标月赛季索引未核实；没有切换查询其他月份。若官方响应取得了战绩，请使用来源检查／补采入口保全。")
            return 1
        print("发现的来源完整赛季标识：" + "、".join(season_ids))
        print("月份覆盖状态：未核实（ClashKing OpenAPI 未提供索引总数／分页游标）；逐届结果仍将分别保全。")
        for item in items:
            raw_id = str(item.get("season") or "")
            if not raw_id:
                print("跳过缺少 season 标识的索引项。")
                continue
            print(f"\n处理赛季：{raw_id}")
            _run_season(
                clan_tag=clan_tag,
                target_month=target_month,
                requested_id=raw_id,
                discovery_item=item,
                index_result=index_result,
                ck=ck,
                official=official,
                official_group_result=group_result,
                month_count=len(items),
                order=order,
            )
        return 0
    finally:
        ck.close()
        if official:
            official.close()


def collect_explicit_season(season_id: str, args: argparse.Namespace, *, archive_path: Path | None = None) -> int:
    """按用户给出的完整来源赛季标识执行单届采集或补采。

    参数:
        season_id: ClashKing 返回的完整赛季标识。
        args: 命令解析结果，含可选目标标签、排序优先级和代理模式。
        archive_path: 预留的已有归档路径；普通显式赛季入口可省略。
    返回:
        命令退出码。
    """
    if not season_id or len(season_id) > 40:
        print("赛季标识无效。", file=sys.stderr)
        return 2
    settings = RuntimeSettings()
    try:
        clan_tag = _settings_clan_tag(settings, args.clan_tag)
        order = choose_order(args.sort_order, interactive=args.sort_order is None and sys.stdin.isatty())
    except (ValueError, AnalysisError) as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    target_month = season_id[:7].replace("-", "")
    if not re.fullmatch(r"\d{6}", target_month):
        print("赛季标识必须以 YYYY-MM 或 YYYY-MM-DD 日期开头。", file=sys.stderr)
        return 2
    ck, official = _construct_sources(settings, no_proxy=args.no_proxy)
    try:
        index_result = ck.list_seasons(clan_tag)
        discovery_item = explicit_season_exists(index_result, season_id)
        if discovery_item is None and index_result.ok:
            print(f"ClashKing 赛季索引没有精确列出 {season_id}；将直接核验指定完整标识，不替换为其他赛季。")
        group_result = official.get_current_group(clan_tag) if official else None
        if official and group_result and group_result.ok and isinstance(group_result.payload, dict):
            _describe_official_status(group_result, target_month, datetime.now(CURRENT_TIMEZONE).strftime("%Y%m"))
        _run_season(
            clan_tag=clan_tag,
            target_month=target_month,
            requested_id=season_id,
            discovery_item=discovery_item,
            index_result=index_result,
            ck=ck,
            official=official,
            official_group_result=group_result,
            month_count=1 if discovery_item else 0,
            order=order,
        )
        return 0
    finally:
        ck.close()
        if official:
            official.close()


def export_archive(input_path: Path, sort_order: str | None) -> int:
    """只读取本地归档并重新计算、写出两份 CSV。

    参数:
        input_path: 已保存的 JSON 归档路径。
        sort_order: 排序优先级字符串；None 使用默认顺序。
    返回:
        命令退出码；此工作流不创建 HTTP 客户端。
    """
    try:
        archive = read_archive(input_path)
        order = parse_order(sort_order)
        season_id = str((archive.get("season") or {}).get("id") or "")
        if not is_full_season_date(season_id):
            raise AnalysisError("归档没有已核实的 YYYY-MM-DD 来源完整赛季标识，不能生成正式命名 CSV。")
        clan = archive.get("clan") or {}
        clan_name = str(clan.get("name") or "")
        clan_tag = canonical_tag(clan.get("tag"))
        if not clan_name or not clan_tag:
            raise AnalysisError("归档缺少已核实的部落名称或标签。")
        result = analyze_archive(archive, order)
        rank_path, diff_path = result_paths(RESULTS_DIR, clan_name, clan_tag, season_id, order)
        write_csvs(archive, result, RESULTS_DIR, rank_path, diff_path)
        print(f"离线重算完成；未请求任何外部接口。排序：{' '.join(order)}")
        print(f"排名 CSV：{rank_path}")
        print(f"大本差核对 CSV：{diff_path}")
        print(f"排名状态：{'有效' if result.rankable else '暂停排名'}；终局：{'完整' if result.season_complete else '尚未确认完整'}")
        if result.missing:
            print("缺失／待核验：" + "；".join(result.missing))
        return 0
    except (OSError, json.JSONDecodeError, ValueError, AnalysisError) as exc:
        print(f"离线导出失败：{exc}", file=sys.stderr)
        return 1


def backfill_archive(input_path: Path, args: argparse.Namespace) -> int:
    """按已有归档的部落和完整赛季标识补采缺失轮次并更新结果。

    参数:
        input_path: 已有归档 JSON 路径。
        args: 补采命令解析结果，含可选标签、排序序列和代理模式。
    返回:
        命令退出码。
    """
    try:
        existing = read_archive(input_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"无法读取归档：{exc}", file=sys.stderr)
        return 1
    clan = existing.get("clan") or {}
    season = existing.get("season") or {}
    clan_tag = canonical_tag(clan.get("tag"))
    season_id = str(season.get("id") or "")
    if not clan_tag or not season_id:
        print("归档缺少部落标签或赛季标识，不能补采。", file=sys.stderr)
        return 2
    settings = RuntimeSettings()
    if not settings.clan_tag:
        settings.clan_tag = clan_tag
    try:
        order = choose_order(args.sort_order, interactive=args.sort_order is None and sys.stdin.isatty())
    except AnalysisError as exc:
        print(f"排序参数错误：{exc}", file=sys.stderr)
        return 2
    target_month = season_id[:7].replace("-", "")
    ck, official = _construct_sources(settings, no_proxy=args.no_proxy)
    try:
        index_result = ck.list_seasons(clan_tag)
        discovery_item = explicit_season_exists(index_result, season_id)
        group_result = official.get_current_group(clan_tag) if official else None
        if group_result:
            _describe_official_status(group_result, target_month, datetime.now(CURRENT_TIMEZONE).strftime("%Y%m"))
        _run_season(
            clan_tag=clan_tag,
            target_month=target_month,
            requested_id=season_id,
            discovery_item=discovery_item,
            index_result=index_result,
            ck=ck,
            official=official,
            official_group_result=group_result,
            month_count=1 if discovery_item else 0,
            order=order,
            archive_path=input_path,
        )
        return 0
    finally:
        ck.close()
        if official:
            official.close()


def _report_path(name: str) -> Path:
    """生成带时间戳的来源验证报告路径。

    参数:
        name: 来源短名，用于区分官方与 ClashKing 报告。
    返回:
        results 目录中的新报告路径。
    """
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR / f"source_validation_{name}_{stamp}.json"


def check_official(args: argparse.Namespace) -> int:
    """独立检查官方部落、当前组及可选已核实 warTag。

    参数:
        args: 来源检查参数，包含目标标签、可选战争标签和代理模式。
    返回:
        检查命令退出码。
    """
    settings = RuntimeSettings()
    try:
        clan_tag = _settings_clan_tag(settings, args.clan_tag)
    except ValueError as exc:
        report = {
            "source": "Clash of Clans Official API",
            "status": "not_run",
            "reason": str(exc),
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "request_reached": False,
        }
        path = _report_path("official")
        atomic_write_json(path, report)
        print(f"官方接口检查未运行：{exc}\n报告：{path}")
        return 2
    if not settings.coc_api_token:
        report = {
            "source": "Clash of Clans Official API",
            "status": "not_run",
            "reason": "COC_API_KEY 未配置；请在 script/.env 配置后重试。",
            "target_clan_tag": clan_tag,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "request_reached": False,
            "http_timeout_seconds": {"connect": 5, "read": 20, "write": 10, "pool": 5},
            "retry": {"max_attempts": 3, "conditions": ["network/timeout", "HTTP 5xx"], "wait_seconds": [1, 2], "no_retry": ["401/403", "429", "other 4xx"]},
        }
        path = _report_path("official")
        atomic_write_json(path, report)
        print(f"官方接口检查未运行：未设置 COC_API_TOKEN。\n报告：{path}")
        return 2
    proxy_url, trust_env = _source_proxy_options(settings, no_proxy=args.no_proxy)
    client = OfficialClient(settings.coc_api_token, proxy_url=proxy_url, trust_env=trust_env)
    try:
        clan_result = client.get_clan(clan_tag)
        group_result = client.get_current_group(clan_tag) if clan_result.ok else None
        clan_payload = clan_result.payload if isinstance(clan_result.payload, dict) else {}
        clan_linked = canonical_tag(clan_payload.get("tag")) == clan_tag
        group_payload = group_result.payload if group_result and isinstance(group_result.payload, dict) else {}
        group_clans = group_payload.get("clans") if isinstance(group_payload.get("clans"), list) else []
        group_linked = any(isinstance(item, dict) and canonical_tag(item.get("tag")) == clan_tag for item in group_clans)
        war_test: dict[str, Any] | None = None
        if args.war_tag:
            war_result = client.get_cwl_war(args.war_tag)
            war = war_result.payload if isinstance(war_result.payload, dict) else None
            valid = bool(war and (canonical_tag((war.get("clan") or {}).get("tag")) == clan_tag or canonical_tag((war.get("opponent") or {}).get("tag")) == clan_tag))
            war_test = {**request_summary(war_result), "requested_war_tag": args.war_tag, "target_clan_linked": valid}
        report = {
            "source": "Clash of Clans Official API",
            "status": "checked",
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "target_clan_tag": clan_tag,
            "clan_endpoint": {**request_summary(clan_result), "name": clan_payload.get("name"), "target_tag_linked": clan_linked},
            "current_group_endpoint": {
                **(request_summary(group_result) if group_result else {"http_status": None, "request_ok": False, "error": "部落接口未成功，未请求联赛组。"}),
                "state": group_payload.get("state"),
                "season": group_payload.get("season"),
                "target_clan_linked": group_linked,
                "roster_count": len((next((x for x in group_clans if isinstance(x, dict) and canonical_tag(x.get("tag")) == clan_tag), {}) or {}).get("members") or []),
                "round_count": len(group_payload.get("rounds") or []) if isinstance(group_payload.get("rounds"), list) else None,
            },
            "war_tag_endpoint": war_test,
            "data_completeness": "未验证整月覆盖；官方接口仅提供当前联赛组。战争字段完整性仅在指定 --war-tag 后检查。",
            "proxy_configured": proxy_url is not None,
            "http_timeout_seconds": {"connect": 5, "read": 20, "write": 10, "pool": 5},
            "retry": {"max_attempts": 3, "conditions": ["network/timeout", "HTTP 5xx"], "wait_seconds": [1, 2], "no_retry": ["401/403", "429", "other 4xx"]},
        }
        path = _report_path("official")
        atomic_write_json(path, report)
        print(f"官方接口请求与字段检查已完成。部落标签匹配：{'是' if clan_linked else '否'}；报告：{path}")
        if group_result is not None:
            current_month = datetime.now(CURRENT_TIMEZONE).strftime("%Y%m")
            _describe_official_status(group_result, current_month, current_month)
        return 0 if clan_result.ok and clan_linked else 1
    finally:
        client.close()


def check_clashking(args: argparse.Namespace) -> int:
    """独立检查 ClashKing 索引及指定或索引最新的赛季详情。

    参数:
        args: 检查参数，含目标标签、可选完整赛季标识和代理模式。
    返回:
        检查命令退出码。
    """
    settings = RuntimeSettings()
    try:
        clan_tag = _settings_clan_tag(settings, args.clan_tag)
    except ValueError as exc:
        report = {
            "source": "ClashKing API",
            "status": "not_run",
            "reason": str(exc),
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "request_reached": False,
        }
        path = _report_path("clashking")
        atomic_write_json(path, report)
        print(f"ClashKing 接口检查未运行：{exc}\n报告：{path}")
        return 2
    client = _make_clashking_client(settings, no_proxy=args.no_proxy)
    try:
        index_result = client.list_seasons(clan_tag)
        index_ok, index_error = _usable_index(index_result)
        items = index_result.payload.get("items", []) if index_ok and isinstance(index_result.payload, dict) else []
        selected_item = next((item for item in items if isinstance(item, dict) and item.get("season") == args.season_id), None) if args.season_id else None
        if selected_item is None and not args.season_id and items:
            date_items = [item for item in items if isinstance(item, dict) and isinstance(item.get("season"), str)]
            selected_item = max(date_items, key=lambda item: str(item.get("season"))) if date_items else None
        season_result = None
        season_id = args.season_id or (selected_item or {}).get("season")
        validation = None
        if season_id:
            season_result = client.get_season(clan_tag, str(season_id))
            returned = season_result.payload if isinstance(season_result.payload, dict) else None
            target = _target_clan(returned, clan_tag) if returned else None
            valid, error, _ = _valid_clashking_smoke(returned, clan_tag, str(season_id)) if returned else (False, season_result.error or "赛季响应不是对象。", None)
            war_count, war_member_count, attack_count = _count_target_wars(returned, clan_tag) if valid and returned else (0, 0, 0)
            validation = {
                **request_summary(season_result),
                "requested_season_id": season_id,
                "returned_season_id": returned.get("season") if returned else None,
                "target_clan_linked": target is not None,
                "roster_count": len((target or {}).get("members") or []),
                "round_count": len(returned.get("rounds") or []) if returned and isinstance(returned.get("rounds"), list) else None,
                "target_war_count": war_count,
                "target_war_member_count": war_member_count,
                "target_war_attack_count": attack_count,
                "required_fields_valid": valid,
                "validation_error": error,
            }
        report = {
            "source": "ClashKing API",
            "status": "checked",
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "target_clan_tag": clan_tag,
            "season_index_endpoint": {**request_summary(index_result), "items_count": len(items) if isinstance(items, list) else None, "schema_error": index_error},
            "season_detail_endpoint": validation,
            "month_coverage": "not_verified",
            "month_coverage_reason": "OpenAPI response exposes items and limit, but no total or pagination cursor.",
            "proxy_configured": False if args.no_proxy else bool(settings.safe_proxy_url()),
            "http_timeout_seconds": {"connect": 5, "read": 20, "write": 10, "pool": 5},
            "retry": {"max_attempts": 3, "conditions": ["network/timeout", "HTTP 5xx"], "wait_seconds": [1, 2], "no_retry": ["429", "all 4xx"]},
        }
        path = _report_path("clashking")
        atomic_write_json(path, report)
        if validation:
            print(f"ClashKing 接口检查完成；赛季字段有效：{'是' if validation['required_fields_valid'] else '否'}；目标届战争：{validation['target_war_count']}。\n报告：{path}")
            return 0 if validation["required_fields_valid"] else 1
        print(f"ClashKing 索引检查完成，但没有可用于详情验证的目标赛季。\n报告：{path}")
        return 1 if not index_ok else 0
    finally:
        client.close()


def _valid_clashking_smoke(payload: Any, clan_tag: str, requested_id: str) -> tuple[bool, str | None, dict[str, Any] | None]:
    """验证只读烟测响应中的赛季身份、目标名单和必要结构。

    参数:
        payload: ClashKing 赛季详情响应。
        clan_tag: 目标部落标签。
        requested_id: 请求的完整赛季标识。
    返回:
        (是否有效, 错误说明, 匹配的目标部落对象)。
    """
    if not isinstance(payload, dict):
        return False, "ClashKing 赛季响应不是 JSON 对象。", None
    if payload.get("season") != requested_id:
        return False, "返回 season 与请求标识不一致。", None
    target = _target_clan(payload, clan_tag)
    if target is None:
        return False, "响应中没有唯一匹配目标部落。", None
    if not isinstance(payload.get("state"), str) or not isinstance(payload.get("rounds"), list):
        return False, "缺少 state 字符串或 rounds 数组。", None
    roster = target.get("members")
    if not isinstance(roster, list) or any(not isinstance(m, dict) or not m.get("tag") for m in roster):
        return False, "目标部落报名名单成员标签不完整。", None
    return True, None, target


def _count_target_wars(payload: dict[str, Any], clan_tag: str) -> tuple[int, int, int]:
    """统计实测对象中目标战争数、双方成员记录数和明细攻击数。

    参数:
        payload: 已验证的 ClashKing 赛季详情。
        clan_tag: 目标部落标签。
    返回:
        (目标战争数, 双方成员对象数, 成员攻击明细总数)。
    """
    war_count = member_count = attack_count = 0
    for round_item in payload.get("rounds") or []:
        if not isinstance(round_item, dict):
            continue
        for war in round_item.get("warTags") or []:
            if not isinstance(war, dict):
                continue
            clan = war.get("clan")
            opponent = war.get("opponent")
            if not isinstance(clan, dict) or not isinstance(opponent, dict):
                continue
            if canonical_tag(clan.get("tag")) == canonical_tag(clan_tag):
                target_side, enemy_side = clan, opponent
            elif canonical_tag(opponent.get("tag")) == canonical_tag(clan_tag):
                target_side, enemy_side = opponent, clan
            else:
                continue
            war_count += 1
            for side in (target_side, enemy_side):
                for member in side.get("members") or []:
                    if isinstance(member, dict):
                        member_count += 1
                        attack_count += len(member.get("attacks") or [])
    return war_count, member_count, attack_count


def _main_parser() -> argparse.ArgumentParser:
    """构造默认的 YYYYMM 采集命令解析器。"""
    parser = argparse.ArgumentParser(
        prog="emergency_cwl.py",
        description="按部落查询目标年月全部联赛，逐届保存 JSON 并生成两份 CSV。默认无参数时用当前年月。",
        epilog="例：python emergency_cwl.py 202412 --clan-tag '#ABC123'；Python 3.13，输出位于 script/data 与 script/results。",
    )
    parser.add_argument("yyyymm", nargs="?", help="目标年月 YYYYMM；省略时使用 Asia/Shanghai 当前年月")
    parser.add_argument("--clan-tag", help="目标部落标签；也可使用 CLAN_TAG 环境变量")
    parser.add_argument("--sort-order", help="排序优先级，例如 'A B C'；省略时交互输入或使用默认 A B D C F")
    parser.add_argument("--no-proxy", action="store_true", help="强制直连，并忽略 .env 与系统代理设置")
    return parser


def _mode_parser(mode: str) -> argparse.ArgumentParser:
    """按子命令名称构造独立入口参数解析器。

    参数:
        mode: ACTION_WORDS 中的子命令名。
    返回:
        配置完成的 argparse 解析器。
    """
    descriptions = {
        "export": "从 JSON 归档离线生成排名和大本差核对 CSV，不请求外部接口。",
        "backfill": "根据已有 JSON 中的部落和完整赛季标识补采并更新同届归档。",
        "season": "查询 ClashKing 返回的指定完整历史赛季标识。",
        "check-official": "独立检查官方 API 的部落、当前联赛组与可选战争标签。",
        "check-clashking": "独立检查 ClashKing 赛季索引与指定或最新赛季详情。",
    }
    parser = argparse.ArgumentParser(prog=f"emergency_cwl.py {mode}", description=descriptions[mode])
    if mode in {"export", "backfill"}:
        parser.add_argument("input", type=Path, help="JSON 归档路径")
    if mode == "season":
        parser.add_argument("season_id", help="ClashKing 返回的完整赛季标识，例如 2024-12-02")
        parser.add_argument("--clan-tag", help="目标部落标签；也可使用 CLAN_TAG 环境变量")
        parser.add_argument("--no-proxy", action="store_true", help="强制直连，并忽略 .env 与系统代理设置")
    if mode == "export":
        parser.add_argument("--sort-order", help="例如 'D A B'；默认 A B D C F")
    if mode in {"backfill", "check-official", "check-clashking"}:
        parser.add_argument("--clan-tag", help="目标部落标签；也可使用 CLAN_TAG 环境变量")
        parser.add_argument("--sort-order", help="例如 'D A B'；默认 A B D C F")
        parser.add_argument("--no-proxy", action="store_true", help="强制直连，并忽略 .env 与系统代理设置")
    if mode == "check-official":
        parser.add_argument("--war-tag", help="可选：验证已确认属于目标部落的战争标签")
    if mode == "check-clashking":
        parser.add_argument("--season-id", help="可选：检查指定的 ClashKing 完整赛季标识；默认使用索引中日期最新项")
    return parser


def main(argv: list[str] | None = None) -> int:
    """解析命令行并分派采集、补采、离线导出或来源验证。

    参数:
        argv: 可选参数列表；None 表示使用进程命令行。
    返回:
        被选工作流的退出码。
    """
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in ACTION_WORDS:
        mode = args_list.pop(0)
        args = _mode_parser(mode).parse_args(args_list)
        if mode == "export":
            return export_archive(args.input, args.sort_order)
        if mode == "backfill":
            return backfill_archive(args.input, args)
        if mode == "season":
            return collect_explicit_season(args.season_id, args)
        if mode == "check-official":
            return check_official(args)
        if mode == "check-clashking":
            return check_clashking(args)
    args = _main_parser().parse_args(args_list)
    return collect_month(args.yyyymm, args)
