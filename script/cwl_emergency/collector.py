"""联赛赛季发现、来源关联、逐轮采集和断点归档。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Callable

from .analysis import canonical_tag
from .sources import ClashKingClient, FetchResult, OfficialClient, request_summary


def _season_month(season_id: str) -> str:
    """取完整赛季标识中的 YYYY-MM 月份部分。

    参数:
        season_id: 来源返回的赛季标识。
    返回:
        标识前七位；输入较短时原样返回。
    """
    return season_id[:7] if len(season_id) >= 7 else season_id


def _target_clan(payload: dict[str, Any], clan_tag: str) -> dict[str, Any] | None:
    """在来源 clans 数组中唯一定位目标部落。

    参数:
        payload: 包含 clans 数组的来源对象。
        clan_tag: 目标部落标签。
    返回:
        唯一匹配的部落对象；缺失或重复匹配返回 None。
    """
    clans = payload.get("clans")
    if not isinstance(clans, list):
        return None
    matches = [
        clan for clan in clans
        if isinstance(clan, dict) and canonical_tag(clan.get("tag")) == canonical_tag(clan_tag)
    ]
    return matches[0] if len(matches) == 1 else None


def _roster_from_clan(clan: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从已匹配的部落对象抽取报名成员身份与来源大本字段。

    参数:
        clan: 已通过部落标签匹配的来源部落对象。
    返回:
        有玩家标签的成员快照列表；输入结构无效时为空列表。
    """
    if not isinstance(clan, dict) or not isinstance(clan.get("members"), list):
        return []
    return [
        {
            "tag": member.get("tag"),
            "name": member.get("name"),
            "townHallLevel": member.get("townHallLevel"),
        }
        for member in clan["members"]
        if isinstance(member, dict) and member.get("tag")
    ]


def _extract_ck_war(round_entry: dict[str, Any], clan_tag: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """只接受完整且双方标签唯一匹配目标部落的 ClashKing 战争对象。

    参数:
        round_entry: ClashKing 某一轮的 warTags 记录。
        clan_tag: 目标部落标签。
    返回:
        (完整战争对象, warTag, 错误说明)；不能唯一关联时不猜测对阵关系。
    """
    items = round_entry.get("warTags")
    if not isinstance(items, list):
        return None, None, "ClashKing 第三方字段 rounds[].warTags 不是数组。"
    full_matches: list[tuple[dict[str, Any], str]] = []
    tag_only: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        war_tag = str(item.get("tag") or "").strip()
        clan = item.get("clan")
        opponent = item.get("opponent")
        if isinstance(clan, dict) and isinstance(opponent, dict):
            clan_matches = canonical_tag(clan.get("tag")) == canonical_tag(clan_tag)
            opponent_matches = canonical_tag(opponent.get("tag")) == canonical_tag(clan_tag)
            if clan_matches != opponent_matches:
                if war_tag:
                    full_matches.append((item, war_tag))
                else:
                    return None, None, "目标战争对象缺少 OpenAPI 定义的 tag 字段。"
        elif war_tag:
            tag_only.append(war_tag)
    unique = {tag: war for war, tag in full_matches}
    if len(unique) == 1:
        war_tag, war = next(iter(unique.items()))
        return war, war_tag, None
    if len(unique) > 1:
        return None, None, "同一轮发现多个属于目标部落的完整战争对象。"
    if tag_only and all(tag == "#0" for tag in tag_only):
        return None, "#0", None
    if tag_only:
        return None, None, "ClashKing 只返回 warTag 占位对象，无法确认哪个战争属于目标部落。"
    return None, None, None


def _official_group_applies(
    result: FetchResult | None,
    season_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """确认官方当前联赛组有效且月份与目标赛季一致。

    参数:
        result: 官方当前联赛组请求结果。
        season_id: ClashKing 发现的完整赛季标识。
    返回:
        (可用于同轮 warTag 关联的联赛组, 错误说明)。
    """
    if result is None or not result.ok or not isinstance(result.payload, dict):
        return None, "官方当前联赛组未取得有效 JSON。"
    group = result.payload
    state = str(group.get("state") or "")
    season = str(group.get("season") or "")
    if state == "notInWar":
        return None, "官方明确返回 state=notInWar。"
    if not season:
        return None, "官方当前联赛组没有 season 字段。"
    if _season_month(season) != _season_month(season_id):
        return None, f"官方当前联赛标识 {season} 与目标 {season_id} 月份不匹配。"
    # 官方只提供 YYYY-MM 时不推断日级标识；之后还要核对同轮精确 warTag，
    # 并再次确认官方战争响应中的一方确实是目标部落。
    return group, None


def _official_group_has_war(group: dict[str, Any], round_number: int, war_tag: str) -> bool:
    """确认官方组同一轮明确列出该 ClashKing 目标 warTag。

    参数:
        group: 经月份检查的官方当前联赛组对象。
        round_number: 从 1 开始的轮次编号。
        war_tag: ClashKing 完整战争对象关联的精确标签。
    返回:
        同轮有该标签时为 True。
    """
    rounds = group.get("rounds")
    if not isinstance(rounds, list) or round_number < 1 or round_number > len(rounds):
        return False
    item = rounds[round_number - 1]
    if not isinstance(item, dict) or not isinstance(item.get("warTags"), list):
        return False
    return any(canonical_tag(tag) == canonical_tag(war_tag) for tag in item["warTags"] if isinstance(tag, str))


def _war_matches_target(war: Any, clan_tag: str, war_tag: str, season_id: str) -> tuple[bool, str | None]:
    """验证官方战争对象确实是目标部落、请求标签和目标月份的数据。

    参数:
        war: 官方战争响应；允许来源不回显 tag 或 season。
        clan_tag: 目标部落标签。
        war_tag: 已在同轮官方组中核实的战争标签。
        season_id: 当前处理的完整赛季标识。
    返回:
        (是否有效, 无效原因)。
    """
    if not isinstance(war, dict):
        return False, "官方战争响应不是对象。"
    clan = war.get("clan")
    opponent = war.get("opponent")
    if not isinstance(clan, dict) or not isinstance(opponent, dict):
        return False, "战争响应缺少 clan/opponent 对象。"
    our_match = canonical_tag(clan.get("tag")) == canonical_tag(clan_tag)
    enemy_match = canonical_tag(opponent.get("tag")) == canonical_tag(clan_tag)
    if our_match == enemy_match:
        return False, "战争双方标签没有唯一匹配目标部落。"
    response_tag = war.get("tag")
    if response_tag is not None and canonical_tag(response_tag) != canonical_tag(war_tag):
        return False, "战争响应返回的 tag 与请求的 warTag 不一致。"
    response_season = str(war.get("season") or "")
    if response_season and _season_month(response_season) != _season_month(season_id):
        return False, f"战争响应 season={response_season} 与目标月份不匹配。"
    if not isinstance(war.get("state"), str):
        return False, "战争响应缺少 state 字符串。"
    if not isinstance(clan.get("members"), list) or not isinstance(opponent.get("members"), list):
        return False, "战争响应缺少双方 members 数组。"
    return True, None


def _fingerprint(war: dict[str, Any], clan_tag: str) -> tuple[Any, ...]:
    """按目标/对手方向标准化战争关键字段，比较两来源战绩差异。

    参数:
        war: 完整战争对象。
        clan_tag: 用于将目标侧放在固定方向的部落标签。
    返回:
        可直接比较的战争元组，不包含展示顺序差异。
    """
    clan = war.get("clan") or {}
    opponent = war.get("opponent") or {}
    if canonical_tag(clan.get("tag")) == canonical_tag(clan_tag):
        our_side, enemy_side = clan, opponent
    else:
        our_side, enemy_side = opponent, clan

    def side(value: dict[str, Any]) -> tuple[Any, ...]:
        """将一方成员、大本快照和攻击记录变为稳定可比较的元组。

        参数:
            value: 战争对象中的一方。
        返回:
            按成员和攻击内容规范排序的元组。
        """
        members = []
        for member in value.get("members") or []:
            if not isinstance(member, dict):
                continue
            attacks = []
            for attack in member.get("attacks") or []:
                if isinstance(attack, dict):
                    attacks.append((
                        canonical_tag(attack.get("attackerTag")),
                        canonical_tag(attack.get("defenderTag")),
                        attack.get("stars"),
                        attack.get("destructionPercentage"),
                        attack.get("order"),
                    ))
            members.append((
                canonical_tag(member.get("tag")),
                member.get("townhallLevel", member.get("townHallLevel")),
                tuple(sorted(attacks, key=repr)),
            ))
        return tuple(sorted(members, key=repr))

    return (
        canonical_tag(our_side.get("tag")),
        canonical_tag(enemy_side.get("tag")),
        war.get("preparationStartTime"),
        war.get("startTime"),
        war.get("endTime"),
        side(our_side),
        side(enemy_side),
    )


def _valid_ck_detail(payload: Any, clan_tag: str, season_id: str) -> tuple[bool, str | None, dict[str, Any] | None]:
    """校验 ClashKing 详情的赛季身份、目标部落、轮次和报名名单。

    参数:
        payload: ClashKing 赛季详情 JSON。
        clan_tag: 目标部落标签。
        season_id: 请求中的完整赛季标识。
    返回:
        (是否有效, 错误说明, 唯一目标部落对象)。
    """
    if not isinstance(payload, dict):
        return False, "ClashKing 赛季响应不是 JSON 对象。", None
    returned_season = str(payload.get("season") or "")
    if returned_season != season_id:
        return False, f"ClashKing 返回 season={returned_season or '缺失'}，与请求 {season_id} 不一致。", None
    target = _target_clan(payload, clan_tag)
    if target is None:
        return False, "ClashKing 赛季内 clans 未能唯一匹配目标部落。", None
    if not isinstance(payload.get("rounds"), list):
        return False, "ClashKing 赛季响应缺少 rounds 数组。", None
    roster = _roster_from_clan(target)
    if not roster:
        return False, "ClashKing 目标部落 roster 缺少成员标签。", None
    return True, None, target


def _source_result(result: FetchResult | None, payload: Any = None, error: str | None = None) -> dict[str, Any]:
    """构造可存档的来源请求摘要，必要时附上已验证的响应对象。

    参数:
        result: 来源 HTTP 请求结果；None 表示没有执行请求。
        payload: 通过关联校验后允许保存的来源对象。
        error: 请求或字段校验的非敏感说明。
    返回:
        可写入 JSON 归档的摘要对象。
    """
    data = request_summary(result) if result else {"http_status": None, "request_ok": False, "attempts": 0, "error": "未请求"}
    if payload is not None:
        data["payload"] = payload
    if error:
        data["validation_error"] = error
    return data


def collect_season(
    *,
    clan_tag: str,
    season_id: str,
    discovery_item: dict[str, Any] | None,
    index_result: FetchResult | None,
    season_result: FetchResult | None,
    official_group_result: FetchResult | None,
    official_client: OfficialClient | None,
    index_month_count: int,
    target_month: str,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """合并一届赛季的来源数据并逐轮回调保全进度。

    参数:
        clan_tag: 目标部落标签。
        season_id: 当前这届来源完整赛季标识。
        discovery_item: ClashKing 索引中的对应赛季条目，可缺失。
        index_result: ClashKing 赛季索引请求结果。
        season_result: 该部落、该届 ClashKing 详情请求结果。
        official_group_result: 官方当前联赛组结果，可缺失或不适用。
        official_client: 官方战争查询客户端；无凭据时为 None。
        index_month_count: 当前索引发现的目标月赛季数，仅供元数据。
        target_month: 用户请求的 YYYYMM 月份。
        on_progress: 每轮更新后接收当前部分归档的回调。
    返回:
        含来源证据、报名名单、逐轮结果和采集状态的归档对象。
    """
    clan_tag = canonical_tag(clan_tag)
    ck_valid, ck_error, ck_target = _valid_ck_detail(
        season_result.payload if season_result and season_result.ok else None,
        clan_tag,
        season_id,
    )
    ck_payload = season_result.payload if ck_valid and season_result else None
    ck_target = ck_target if ck_valid else None
    official_group, official_group_error = _official_group_applies(
        official_group_result,
        season_id,
    )
    official_target = _target_clan(official_group, clan_tag) if official_group else None
    if official_group and official_target is None:
        official_group_error = "官方当前联赛组没有唯一匹配目标部落。"
        official_group = None
    # 官方当届名单优先；未取得时保留有效的 ClashKing 目标名单。
    official_roster = _roster_from_clan(official_target)
    ck_roster = _roster_from_clan(ck_target)
    roster = official_roster or ck_roster
    clan_name = str((official_target or ck_target or {}).get("name") or "")

    group_metadata = None
    if official_group_result:
        raw_group = official_group_result.payload if isinstance(official_group_result.payload, dict) else None
        if raw_group:
            target_payload = _target_clan(raw_group, clan_tag)
            group_metadata = {
                "state": raw_group.get("state"),
                "season": raw_group.get("season"),
                "target_clan": target_payload,
                "round_war_tags": [r.get("warTags") for r in raw_group.get("rounds", []) if isinstance(r, dict)],
                "scope_note": "只保留官方当前联赛组元数据与 warTag 列表；战争详情仅请求并归档目标部落实际参加的战争。",
            }

    rounds: list[dict[str, Any]] = []

    def build_archive(round_snapshot: list[dict[str, Any]]) -> dict[str, Any]:
        """从当前已收集轮次生成可立即持久化的完整归档结构。

        参数:
            round_snapshot: 当前时点的逐轮对象快照。
        返回:
            包含身份、名单、来源核验和轮次状态的归档对象。
        """
        return {
            "archive_format": "clanmetric-cwl-archive-v1",
            "clan": {"tag": clan_tag, "name": clan_name},
            "season": {
                "id": season_id,
                "month": _season_month(season_id),
                "state": (ck_payload or {}).get("state") if isinstance(ck_payload, dict) else None,
                "warLeague": (discovery_item or {}).get("warLeague"),
                "warSize": (discovery_item or {}).get("warSize"),
                "index_item": discovery_item,
            },
            "roster": roster,
            "roster_source": "official" if official_roster else ("clashking" if ck_roster else None),
            "rounds": round_snapshot,
            "source_checks": {
                "clashking_index": _source_result(index_result),
                "clashking_season": _source_result(season_result, error=ck_error),
                "official_group": _source_result(official_group_result, payload=group_metadata, error=official_group_error),
            },
            "discovery": {
                "target_month": target_month,
                "requested_season_id": season_id,
                "clashking_index_route": "/v2/cwl/{clanTag}/seasons?limit=200",
                "clashking_index_limit": 200,
                "month_coverage": "unverified",
                "month_coverage_note": "当前 OpenAPI 只定义 items 数组与 limit 参数，没有总数或分页游标；命中的赛季已逐届处理，但不能据此声明该月索引绝对完整。",
                "official_group_mapping": "target-warTag-round-match" if official_group else "unavailable-or-not-matched",
            },
            "collection": {
                "status": "collected" if ck_valid or official_group else "partial_or_failed",
                "clashking_detail_valid": ck_valid,
                "official_group_valid": official_group is not None,
                "errors": [msg for msg in (ck_error, official_group_error) if msg],
            },
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def checkpoint() -> None:
        """深拷贝当前轮次后通知存储层，避免后续更新影响已写快照。"""
        if on_progress:
            on_progress(build_archive(deepcopy(rounds)))

    # 逐轮追加后立即 checkpoint，进程中断时已取得战争不会丢失。
    ckwars = (ck_payload or {}).get("rounds") if isinstance(ck_payload, dict) else []
    ckwars = ckwars if isinstance(ckwars, list) else []
    for round_number, ck_round in enumerate(ckwars, start=1):
        if not isinstance(ck_round, dict):
            rounds.append({"round_number": round_number, "status": "missing", "war_tag": None, "war": None, "sources": {}, "last_attempt": {"clashking": "轮次对象格式无效"}})
            checkpoint()
            continue
        ck_war, war_tag, ck_round_error = _extract_ck_war(ck_round, clan_tag)
        status = "collected" if ck_war else ("future" if war_tag == "#0" or (not war_tag and str((ck_payload or {}).get("state")) != "ended") else "missing")
        # 分别保存每个来源的状态和原始对象，防止胜出来源覆盖另一来源证据。
        source_records: dict[str, Any] = {}
        # 当前轮默认采用有效 ClashKing 战绩；完成官方精确匹配后才切换。
        selected_war = ck_war
        chosen_source = "clashking" if ck_war else None
        last_attempt: dict[str, Any] = {}
        if ck_war and war_tag:
            source_records["clashking"] = _source_result(
                season_result,
                payload=ck_war,
                error=ck_round_error,
            )
            if official_client is not None and official_group is not None and _official_group_has_war(official_group, round_number, war_tag):
                official_result = official_client.get_cwl_war(war_tag)
                official_valid, official_error = _war_matches_target(
                    official_result.payload if official_result.ok else None,
                    clan_tag,
                    war_tag,
                    season_id,
                )
                source_records["official"] = _source_result(
                    official_result,
                    payload=official_result.payload if official_valid else None,
                    error=official_error,
                )
                last_attempt["official"] = source_records["official"].get("error") or source_records["official"].get("validation_error") or "请求完成"
                if official_valid:
                    selected_war = official_result.payload
                    chosen_source = "official"
                    if (
                        str(selected_war.get("state")) in {"warEnded", "ended", "leagueEnded"}
                        and str(ck_war.get("state")) in {"warEnded", "ended", "leagueEnded"}
                    ):
                        source_records["conflict"] = {
                            "detected": _fingerprint(selected_war, clan_tag) != _fingerprint(ck_war, clan_tag),
                            "resolution": "官方数据优先" if _fingerprint(selected_war, clan_tag) != _fingerprint(ck_war, clan_tag) else "一致",
                        }
            elif official_client is not None and official_group is not None:
                last_attempt["official"] = "官方联赛组第对应轮次未列出 ClashKing 目标 warTag；未请求其他部落战争。"
        else:
            last_attempt["clashking"] = ck_round_error or ("该轮尚未出现可验证的目标 warTag。" if status == "future" else "未能从轮次 wars 中唯一确认目标战争。")
            if war_tag and status == "missing":
                source_records["clashking"] = _source_result(season_result, error=last_attempt["clashking"])
        rounds.append({
            "round_number": round_number,
            "war_tag": war_tag,
            "status": status,
            "war": selected_war,
            "chosen_source": chosen_source,
            "sources": source_records,
            "last_attempt": last_attempt,
        })
        checkpoint()

    if not ck_valid and not rounds:
        rounds = [{
            "round_number": 1,
            "war_tag": None,
            "status": "missing",
            "war": None,
            "chosen_source": None,
            "sources": {},
            "last_attempt": {"clashking": ck_error or (season_result.error if season_result else "ClashKing 赛季查询未执行。")},
        }]
        checkpoint()

    return build_archive(rounds)


def find_season_items(index_result: FetchResult, target_month: str) -> list[dict[str, Any]]:
    """从来源索引筛出指定月份的唯一赛季条目并按来源标识排序。

    参数:
        index_result: ClashKing 赛季索引请求结果。
        target_month: YYYY-MM 格式月份。
    返回:
        该月唯一赛季条目；索引不可用时返回空列表。
    """
    if not index_result.ok or not isinstance(index_result.payload, dict):
        return []
    items = index_result.payload.get("items")
    if not isinstance(items, list):
        return []
    matches = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        season_id = item.get("season")
        if not isinstance(season_id, str) or not season_id.startswith(target_month):
            continue
        if season_id not in seen:
            matches.append(item)
            seen.add(season_id)
    return sorted(matches, key=lambda item: str(item.get("season")))


def explicit_season_exists(index_result: FetchResult, season_id: str) -> dict[str, Any] | None:
    """在成功索引结果中精确查找完整赛季标识，不替换为近似项。

    参数:
        index_result: ClashKing 赛季索引请求结果。
        season_id: 要精确匹配的来源赛季标识。
    返回:
        完全相同的索引条目；没有时返回 None。
    """
    if not index_result.ok or not isinstance(index_result.payload, dict):
        return None
    items = index_result.payload.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("season") == season_id:
            return item
    return None


def official_no_current_league(result: FetchResult | None) -> bool:
    """仅当官方成功响应明确给出 notInWar 时确认当前没有联赛。

    参数:
        result: 官方当前联赛组请求结果。
    返回:
        请求成功且 JSON state 精确为 notInWar 时返回 True。
    """
    return bool(result and result.ok and isinstance(result.payload, dict) and result.payload.get("state") == "notInWar")
