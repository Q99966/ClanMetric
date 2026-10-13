"""应急联赛归档的规则校验、排名计算与 CSV 输出。"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


# 无交互输入时沿用已确认的默认优先级。
DEFAULT_ORDER = ("A", "B", "D", "C", "F")
# 排序代号与中文表头说明共用，避免菜单与结果解释出现不同含义。
ORDER_LABELS = {
    "A": "进攻总星数",
    "B": "进攻摧毁率累计",
    "C": "进攻大本差合计",
    "D": "总杯分",
    "E": "防住摧毁率累计",
    "F": "防御大本差合计",
}
# 只把来源样本已核实的战争终局状态计为结束。
TERMINAL_WAR_STATES = {"warEnded"}


class AnalysisError(ValueError):
    """归档缺少必要字段、字段格式无效或违反计算规则时抛出的异常。"""

    pass


def canonical_tag(value: Any) -> str:
    """统一部落或玩家标签的大小写与井号格式。

    参数:
        value: 来源或用户输入的标签；非字符串按缺失处理。
    返回:
        大写且以井号开头的标签，空输入返回空字符串。
    """
    if not isinstance(value, str):
        return ""
    tag = value.strip().upper()
    return tag if tag.startswith("#") else (f"#{tag}" if tag else "")


def parse_order(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """解析一次性排序优先级，并拒绝重复或非 A～F 的代号。

    参数:
        value: 空值用默认序列；也可传空格/逗号分隔字符串或代号序列。
    返回:
        从最高优先级到最低优先级的排序代号元组。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_ORDER
    if isinstance(value, str):
        pieces = value.replace(",", " ").split()
        if len(pieces) == 1 and len(pieces[0]) > 1:
            pieces = list(pieces[0])
    else:
        pieces = [str(item) for item in value]
    order = tuple(piece.strip().upper() for piece in pieces)
    invalid = [item for item in order if item not in ORDER_LABELS]
    if invalid:
        raise AnalysisError(f"非法排序代号：{' '.join(invalid)}；仅允许 A～F。")
    if len(set(order)) != len(order):
        raise AnalysisError("排序代号不能重复，请按优先级重新输入。")
    if not order:
        return DEFAULT_ORDER
    return order


def decimal_value(value: Any, *, field_name: str) -> Decimal:
    """把来源数值安全转换为有限 Decimal，缺失或非法时停止计算。

    参数:
        value: 来源返回的数值。
        field_name: 用于错误说明的字段名。
    返回:
        可参与精确计算的有限 Decimal。
    """
    if isinstance(value, bool) or value is None:
        raise AnalysisError(f"必要数值字段 {field_name} 缺失。")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AnalysisError(f"必要数值字段 {field_name} 无效。") from exc
    if not number.is_finite():
        raise AnalysisError(f"必要数值字段 {field_name} 不是有限数。")
    return number


def round_half_up(value: Decimal) -> int:
    """按项目规则将 Decimal 四舍五入，恰为 .5 时远离零取整。

    参数:
        value: 待舍入的 Decimal 数值。
    返回:
        最近的整数。
    """
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def attack_cups(stars: int, destruction: Decimal) -> int:
    """按联赛方案把单次进攻星数与摧毁率换算为杯分。

    参数:
        stars: 本次进攻星数。
        destruction: 本次进攻摧毁率百分比。
    返回:
        四舍五入后的进攻杯分。
    """
    if stars >= 3:
        raw = Decimal(40)
    elif stars == 2:
        raw = Decimal(16) + (destruction - Decimal(50)) / Decimal(3)
    elif stars == 1:
        raw = Decimal(5) + (destruction - Decimal(1)) / Decimal(9)
    else:
        raw = destruction / Decimal(10)
    return round_half_up(raw)


def defense_cups(stars: int, destruction: Decimal) -> int:
    """按防御方案计算单次防御杯分。

    参数:
        stars: 对手本次进攻取得的星数。
        destruction: 对手本次进攻摧毁率百分比。
    返回:
        本次防御获得的杯分。
    """
    return 40 if stars == 0 else 40 - attack_cups(stars, destruction)


def final_defense_cups(round_minima: Iterable[int], total_rounds: int) -> int | None:
    """用已发生防御轮次的逐轮最低杯分均值补齐未防御轮次。

    参数:
        round_minima: 每个有防御记录轮次的最低单次防御杯分。
        total_rounds: 本届联赛总轮数。
    返回:
        终局补偿后的防御杯分；无防御记录时返回 None。
    """
    values = list(round_minima)
    if not values:
        return None
    if total_rounds < len(values):
        raise AnalysisError("有防御记录的轮数超过本届总轮数。")
    subtotal = Decimal(sum(values))
    mean = subtotal / Decimal(len(values))
    return round_half_up(subtotal + Decimal(total_rounds - len(values)) * mean)


def _member_th(member: dict[str, Any]) -> int | None:
    """读取成员历史战争对象中的大本等级，不从当前部落信息推断。

    参数:
        member: 来源中的战争成员对象。
    返回:
        有效大本等级；缺失或无法转换时返回 None。
    """
    for key in ("townhallLevel", "townHallLevel"):
        value = member.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _side_by_tag(war: dict[str, Any], clan_tag: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """依据标签识别目标部落一侧，并返回目标与对手对象。

    参数:
        war: 一场战争的完整来源对象。
        clan_tag: 已规范化的目标部落标签。
    返回:
        (目标部落一侧, 对手一侧)。
    异常:
        双方标签缺失、重复或不含目标标签时抛出 AnalysisError。
    """
    clan = war.get("clan")
    opponent = war.get("opponent")
    if not isinstance(clan, dict) or not isinstance(opponent, dict):
        raise AnalysisError("战争响应缺少 clan/opponent 对象。")
    clan_match = canonical_tag(clan.get("tag")) == clan_tag
    opponent_match = canonical_tag(opponent.get("tag")) == clan_tag
    if clan_match == opponent_match:
        raise AnalysisError("战争双方标签缺失、重复或与目标部落不匹配。")
    return (clan, opponent) if clan_match else (opponent, clan)


def _war_round_status(round_record: dict[str, Any]) -> str:
    """将归档轮次状态统一为已采集、尚未开始或缺失三类。

    参数:
        round_record: 归档中的单轮对象。
    返回:
        `collected`、`future` 或 `missing`。
    """
    status = str(round_record.get("status", "")).lower()
    if status in {"future", "placeholder", "not_started"}:
        return "future"
    if status != "collected" or not isinstance(round_record.get("war"), dict):
        return "missing"
    return "collected"


def _validate_side_attack_aggregate(
    side: dict[str, Any],
    round_number: int,
    side_name: str,
    missing: list[str],
) -> None:
    """核对成员攻击明细数量与来源侧级汇总数，验证省略字段语义。

    参数:
        side: 一方战争对象，需含 members 与来源汇总 attacks。
        round_number: 当前轮次编号，用于定位错误。
        side_name: 错误文本中的一方名称。
        missing: 完整性问题共享列表；不在此处中断其余成员计算。
    """
    members = side.get("members")
    if not isinstance(members, list):
        return
    # 成员 attacks 是可选字段；只有侧级合计吻合时，缺键才可解释为零次。
    observed = 0
    for member in members:
        if not isinstance(member, dict):
            missing.append(f"第 {round_number} 轮{side_name}成员记录无效，无法核验攻击总数")
            continue
        if "attacks" not in member:
            # The source omits this optional member field when no attack record
            # is present. It is accepted as zero only when the side total below
            # reconciles with all listed member attacks.
            continue
        attacks = member.get("attacks")
        if attacks is None or not isinstance(attacks, list):
            missing.append(f"第 {round_number} 轮{side_name}玩家 {canonical_tag(member.get('tag'))} 的 attacks 字段格式无效")
            continue
        observed += len(attacks)

    aggregate = side.get("attacks")
    try:
        if isinstance(aggregate, bool) or aggregate is None:
            raise ValueError
        expected = Decimal(str(aggregate))
        if not expected.is_finite() or expected < 0 or expected != expected.to_integral_value():
            raise ValueError
    except (InvalidOperation, ValueError):
        missing.append(f"第 {round_number} 轮{side_name}缺少有效攻击总数，无法核验成员攻击记录")
        return
    if int(expected) != observed:
        missing.append(
            f"第 {round_number} 轮{side_name}攻击总数为 {int(expected)}，但成员明细合计 {observed}，数据不完整"
        )


@dataclass
class MemberTotals:
    """单名报名成员跨整届联赛累积的统计与逐轮原始明细。

    tag/name 是玩家身份与显示名；roster_town_hall 是报名大本等级。
    attack_* 与 defense_* 保存排名指标；*_by_round 保存逐轮明细。
    town_hall_snapshots 保存当轮历史等级，errors 保存成员级问题。
    """
    tag: str
    name: str
    roster_town_hall: int | None
    attack_count: int = 0
    attack_stars: int = 0
    attack_cups: int = 0
    attack_destruction: Decimal = Decimal(0)
    attack_th_diff: int = 0
    defense_count: int = 0
    defended_stars: int = 0
    defense_prevented_destruction: Decimal = Decimal(0)
    defense_th_diff: int = 0
    defense_by_round: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    attack_by_round: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    town_hall_snapshots: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """一次归档计算的排名行、轮次信息与完整性判定。

    rows 是全体报名成员；rankable 控制名次是否有效；season_complete
    控制终局防御补偿；missing 保存问题；order 是实际采用的排序序列。
    """
    rows: list[dict[str, Any]]
    round_columns: list[dict[str, Any]]
    rankable: bool
    season_complete: bool
    missing: list[str]
    order: tuple[str, ...]


def analyze_archive(archive: dict[str, Any], sort_order: str | Iterable[str] | None = None) -> AnalysisResult:
    """根据归档快照计算报名成员统计、名次和逐轮完整性。

    参数:
        archive: `clanmetric-cwl-archive-v1` 格式的完整或部分归档。
        sort_order: 由高到低排列的 A～F 指标；空值采用默认序列。
    返回:
        含统计结果、暂停排名原因和逐轮状态的 AnalysisResult。
    说明:
        缺失字段会保留可计算的成员行但暂停名次；仅完整终局应用防御补偿。
    """
    order = parse_order(sort_order)
    clan = archive.get("clan") or {}
    clan_tag = canonical_tag(clan.get("tag"))
    if not clan_tag:
        raise AnalysisError("归档缺少目标部落标签。")
    roster = archive.get("roster")
    if not isinstance(roster, list) or not roster:
        raise AnalysisError("归档缺少完整报名名单。")
    rounds = archive.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        raise AnalysisError("归档缺少轮次信息。")

    totals: dict[str, MemberTotals] = {}
    missing: list[str] = []
    for member in roster:
        if not isinstance(member, dict):
            missing.append("报名名单包含无法解析的成员项")
            continue
        tag = canonical_tag(member.get("tag"))
        if not tag:
            missing.append("报名名单成员缺少玩家标签")
            continue
        if tag in totals:
            missing.append(f"报名名单玩家标签重复：{tag}")
            continue
        totals[tag] = MemberTotals(
            tag=tag,
            name=str(member.get("name") or ""),
            roster_town_hall=_member_th(member),
        )
    if not totals:
        raise AnalysisError("没有可识别的报名成员。")

    all_wars_terminal = True
    # 先统一每轮状态，缺失战争不会被误当成零进攻轮次。
    normalized_rounds: list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str]] = []
    for index, round_record in enumerate(rounds, start=1):
        round_number = int(round_record.get("round_number") or index)
        status = _war_round_status(round_record)
        if status == "future":
            all_wars_terminal = False
            normalized_rounds.append((round_record, None, None, status))
            continue
        if status == "missing":
            all_wars_terminal = False
            missing.append(f"第 {round_number} 轮战绩未取得或来源结构无效")
            normalized_rounds.append((round_record, None, None, status))
            continue
        war = round_record["war"]
        try:
            our_side, enemy_side = _side_by_tag(war, clan_tag)
        except AnalysisError as exc:
            missing.append(f"第 {round_number} 轮：{exc}")
            all_wars_terminal = False
            normalized_rounds.append((round_record, None, None, "missing"))
            continue
        state = str(war.get("state") or "")
        is_terminal = state in TERMINAL_WAR_STATES
        if not is_terminal:
            all_wars_terminal = False
        our_members = our_side.get("members")
        enemy_members = enemy_side.get("members")
        if not isinstance(our_members, list) or not isinstance(enemy_members, list):
            missing.append(f"第 {round_number} 轮缺少双方成员与历史大本快照")
            normalized_rounds.append((round_record, None, None, "missing"))
            continue
        _validate_side_attack_aggregate(our_side, round_number, "本部落", missing)
        _validate_side_attack_aggregate(enemy_side, round_number, "对手", missing)
        # 用双方当轮成员快照关联攻击者、受击者和大本差。
        our_by_tag = {canonical_tag(m.get("tag")): m for m in our_members if isinstance(m, dict) and canonical_tag(m.get("tag"))}
        enemy_by_tag = {canonical_tag(m.get("tag")): m for m in enemy_members if isinstance(m, dict) and canonical_tag(m.get("tag"))}
        for tag, raw_member in our_by_tag.items():
            if tag in totals:
                th = _member_th(raw_member)
                if th is not None:
                    totals[tag].town_hall_snapshots[round_number] = th
        normalized_rounds.append((round_record, our_side, enemy_side, "collected"))

        for member in our_members:
            if not isinstance(member, dict):
                continue
            attacker_tag = canonical_tag(member.get("tag"))
            if attacker_tag not in totals:
                continue
            if "attacks" not in member:
                attacks = []
            else:
                attacks = member.get("attacks")
                if attacks is None:
                    attacks = []
            if not isinstance(attacks, list):
                missing.append(f"第 {round_number} 轮玩家 {attacker_tag} 的 attacks 字段格式无效")
                continue
            own_th = _member_th(member)
            for attack in attacks:
                if not isinstance(attack, dict):
                    missing.append(f"第 {round_number} 轮玩家 {attacker_tag} 存在无效进攻记录")
                    continue
                recorded_attacker = canonical_tag(attack.get("attackerTag"))
                if recorded_attacker != attacker_tag:
                    missing.append(f"第 {round_number} 轮玩家 {attacker_tag} 的进攻 attackerTag 缺失或与成员不匹配")
                    continue
                try:
                    stars = int(decimal_value(attack.get("stars"), field_name="stars"))
                    destruction = decimal_value(attack.get("destructionPercentage"), field_name="destructionPercentage")
                except (AnalysisError, ValueError) as exc:
                    missing.append(f"第 {round_number} 轮玩家 {attacker_tag} 进攻数据不完整：{exc}")
                    continue
                if stars < 0 or stars > 3 or destruction < 0 or destruction > 100:
                    missing.append(f"第 {round_number} 轮玩家 {attacker_tag} 的星数或摧毁率超出来源字段范围")
                    continue
                defender_tag = canonical_tag(attack.get("defenderTag"))
                target = enemy_by_tag.get(defender_tag)
                target_th = _member_th(target) if isinstance(target, dict) else None
                if not defender_tag or target_th is None or own_th is None:
                    missing.append(f"第 {round_number} 轮玩家 {attacker_tag} 的进攻缺少攻击双方标签或大本快照")
                    continue
                detail = {
                    "attacker_tag": attacker_tag,
                    "defender_tag": defender_tag,
                    "stars": stars,
                    "destruction": destruction,
                    "th_diff": target_th - own_th,
                    "cup_score": attack_cups(stars, destruction),
                }
                totals[attacker_tag].attack_count += 1
                totals[attacker_tag].attack_stars += stars
                totals[attacker_tag].attack_cups += detail["cup_score"]
                totals[attacker_tag].attack_destruction += destruction
                totals[attacker_tag].attack_th_diff += detail["th_diff"]
                totals[attacker_tag].attack_by_round.setdefault(round_number, []).append(detail)

        for enemy_member in enemy_members:
            if not isinstance(enemy_member, dict):
                continue
            opponent_tag = canonical_tag(enemy_member.get("tag"))
            if "attacks" not in enemy_member:
                attacks = []
            else:
                attacks = enemy_member.get("attacks")
                if attacks is None:
                    attacks = []
            if not isinstance(attacks, list):
                missing.append(f"第 {round_number} 轮对手玩家 {opponent_tag} 的 attacks 字段格式无效")
                continue
            enemy_th = _member_th(enemy_member)
            for attack in attacks:
                if not isinstance(attack, dict):
                    missing.append(f"第 {round_number} 轮对手玩家 {opponent_tag} 存在无效进攻记录")
                    continue
                defender_tag = canonical_tag(attack.get("defenderTag"))
                if defender_tag not in totals:
                    continue
                target = our_by_tag.get(defender_tag)
                own_th = _member_th(target) if isinstance(target, dict) else None
                try:
                    stars = int(decimal_value(attack.get("stars"), field_name="stars"))
                    destruction = decimal_value(attack.get("destructionPercentage"), field_name="destructionPercentage")
                except (AnalysisError, ValueError) as exc:
                    missing.append(f"第 {round_number} 轮玩家 {defender_tag} 防御数据不完整：{exc}")
                    continue
                if stars < 0 or stars > 3 or destruction < 0 or destruction > 100:
                    missing.append(f"第 {round_number} 轮玩家 {defender_tag} 的防御星数或摧毁率超出来源字段范围")
                    continue
                attack_attacker_tag = canonical_tag(attack.get("attackerTag"))
                source_attacker = enemy_by_tag.get(attack_attacker_tag)
                source_attacker_th = _member_th(source_attacker) if isinstance(source_attacker, dict) else None
                if not opponent_tag or not attack_attacker_tag or attack_attacker_tag != opponent_tag or source_attacker_th is None or own_th is None:
                    missing.append(f"第 {round_number} 轮玩家 {defender_tag} 的防御缺少攻击者标签或大本快照")
                    continue
                detail = {
                    "attacker_tag": attack_attacker_tag,
                    "defender_tag": defender_tag,
                    "stars": stars,
                    "destruction": destruction,
                    "th_diff": source_attacker_th - own_th,
                    "cup_score": defense_cups(stars, destruction),
                }
                totals[defender_tag].defense_count += 1
                totals[defender_tag].defended_stars += max(0, 3 - stars)
                totals[defender_tag].defense_prevented_destruction += Decimal(100) - destruction
                totals[defender_tag].defense_th_diff += detail["th_diff"]
                totals[defender_tag].defense_by_round.setdefault(round_number, []).append(detail)

    season_complete = all_wars_terminal and not missing and all(_war_round_status(r) == "collected" for r in rounds)
    rankable = not missing
    total_rounds = len(rounds)
    result_rows: list[dict[str, Any]] = []
    for total in totals.values():
        # 每个有防御记录的轮次仅取该轮最低防御杯分，再按完整性规则补齐缺席轮次。
        round_defense_minima = [
            min(item["cup_score"] for item in attacks)
            for attacks in total.defense_by_round.values()
            if attacks
        ]
        actual_defense_cups = sum(round_defense_minima)
        final_defense = final_defense_cups(round_defense_minima, total_rounds) if season_complete else None
        scored_defense_cups = final_defense if final_defense is not None else actual_defense_cups
        row = {
            "tag": total.tag,
            "name": total.name,
            "roster_town_hall": total.roster_town_hall,
            "town_hall_snapshots": dict(sorted(total.town_hall_snapshots.items())),
            "attack_count": total.attack_count,
            "attack_stars": total.attack_stars,
            "attack_destruction_total": total.attack_destruction,
            "attack_destruction_mean": total.attack_destruction / total.attack_count if total.attack_count else None,
            "attack_cups": total.attack_cups,
            "attack_th_diff": total.attack_th_diff,
            "defense_count": total.defense_count,
            "defended_stars": total.defended_stars,
            "defense_prevented_destruction_total": total.defense_prevented_destruction,
            "defense_prevented_destruction_mean": total.defense_prevented_destruction / total.defense_count if total.defense_count else None,
            "defense_th_diff": total.defense_th_diff,
            "defense_actual_cups": actual_defense_cups,
            "defense_final_cups": final_defense,
            "total_cups": total.attack_cups + scored_defense_cups,
            "attack_by_round": total.attack_by_round,
            "defense_by_round": total.defense_by_round,
            "missing": list(total.errors),
        }
        result_rows.append(row)

    def value_for(row: dict[str, Any], key: str) -> Any:
        """取出单行对应排序代号的数值。

        参数:
            row: 单名成员统计行。
            key: A～F 排序代号。
        返回:
            可按该指标比较的累计值。
        """
        return {
            "A": row["attack_stars"],
            "B": row["attack_destruction_total"],
            "C": row["attack_th_diff"],
            "D": row["total_cups"],
            "E": row["defense_prevented_destruction_total"],
            "F": row["defense_th_diff"],
        }[key]

    # 先设定标签升序的最终平局顺序，再由低优先级向高优先级稳定排序。
    result_rows.sort(key=lambda row: row["tag"])
    for key in reversed(order):
        result_rows.sort(key=lambda row: value_for(row, key), reverse=True)
    if rankable:
        previous_values: tuple[Any, ...] | None = None
        current_rank: int | None = None
        for index, row in enumerate(result_rows, start=1):
            # 名次只比较用户选中的指标；标签仅稳定并列成员的显示顺序。
            selected_values = tuple(value_for(row, key) for key in order)
            if selected_values != previous_values:
                current_rank = index
                previous_values = selected_values
            row["rank"] = current_rank
    else:
        for row in result_rows:
            row["rank"] = None

    round_columns = []
    for index, round_record in enumerate(rounds, start=1):
        round_number = int(round_record.get("round_number") or index)
        war = round_record.get("war") if isinstance(round_record.get("war"), dict) else None
        state = str((war or {}).get("state") or "")
        round_columns.append({
            "round_number": round_number,
            "status": _war_round_status(round_record),
            "state": state,
            "terminal": state in TERMINAL_WAR_STATES,
        })
    return AnalysisResult(result_rows, round_columns, rankable, season_complete, missing, order)


def display_number(value: Any) -> str:
    """把可选 Decimal 数值转成 CSV 文本，缺失值显示为短横线。

    参数:
        value: 数值、None 或可转换数值对象。
    返回:
        不带多余尾随零的显示文本。
    """
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return str(int(value))
        return format(value.normalize(), "f")
    return str(value)


def display_signed(value: int) -> str:
    """格式化带显式正负号的整数大本差。

    参数:
        value: 目标大本等级减攻击方大本等级后的整数差。
    返回:
        带正负号的整数文本。
    """
    return f"{value:+d}"


def _town_hall_display(row: dict[str, Any]) -> str:
    """合并报名大本与各轮历史快照，显示成员参与期间的等级范围。

    参数:
        row: 已计算的成员统计行。
    返回:
        CSV 中的大本等级或逐轮变化文本。
    """
    snapshots = row["town_hall_snapshots"]
    distinct = sorted(set(snapshots.values()))
    roster_level = row["roster_town_hall"]
    if len(distinct) > 1:
        history = ", ".join(f"R{round_number}={level}" for round_number, level in snapshots.items())
        return f"报名={display_number(roster_level)}；战时变化={history}"
    if distinct:
        return f"{distinct[0]}（战时）"
    return display_number(roster_level)


def _status_for_round(round_record: dict[str, Any], row: dict[str, Any], round_number: int, clan_tag: str) -> tuple[str, str]:
    """生成成员在某轮的进攻和防御大本差单元格文本。

    参数:
        round_record: 归档中的单轮战争对象。
        row: 单名成员的统计行。
        round_number: 当前轮次编号。
        clan_tag: 目标部落标签。
    返回:
        (进攻差文本, 防御差文本)，含零差、无记录或数据缺失状态。
    """
    status = _war_round_status(round_record)
    if status == "future":
        return "尚未开始", "尚未开始"
    if status == "missing":
        return "数据缺失", "数据缺失"
    war = round_record.get("war") or {}
    try:
        our_side, _ = _side_by_tag(war, clan_tag)
    except AnalysisError:
        return "数据缺失", "数据缺失"
    selected = {canonical_tag(m.get("tag")) for m in (our_side.get("members") or []) if isinstance(m, dict)}
    if row["tag"] not in selected:
        return "未出场", "未出场"
    attack_items = row["attack_by_round"].get(round_number, [])
    defense_items = row["defense_by_round"].get(round_number, [])
    terminal = str(war.get("state") or "") in TERMINAL_WAR_STATES

    def format_diffs(items: list[dict[str, Any]], empty_text: str) -> str:
        """紧凑显示单轮差值；多条记录时附合计供 C/F 核对。

        参数:
            items: 本成员本轮同类的进攻或防御明细。
            empty_text: 无记录时按战争状态显示的文字。
        返回:
            单次差值、多次差值与合计，或无记录状态。
        """
        values = [display_signed(item["th_diff"]) for item in items]
        if len(values) == 1:
            return values[0]
        if len(values) > 1:
            total = sum(item["th_diff"] for item in items)
            return f"{', '.join(values)}（合计 {display_signed(total)}）"
        return empty_text

    # 单次进攻/防御只显示差值；多条记录保留明细和合计，避免单条也重复显示合计。
    attack_text = format_diffs(attack_items, "未进攻" if terminal else "暂无记录（进行中）")
    defense_text = format_diffs(defense_items, "未被攻击" if terminal else "暂无记录（进行中）")
    return attack_text, defense_text


def write_csvs(archive: dict[str, Any], result: AnalysisResult, output_dir: Path, ranking_path: Path, diff_path: Path) -> tuple[Path, Path]:
    """写出排名表和逐轮进攻、防御大本差核对表。

    参数:
        archive: 计算所用归档，用于赛季、来源和逐轮状态列。
        result: 与该归档及排序方案对应的计算结果。
        output_dir: CSV 输出目录；不存在时创建。
        ranking_path: 排名 CSV 的目标路径。
        diff_path: 大本差核对 CSV 的目标路径。
    返回:
        实际写入的 (排名路径, 核对路径)。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    clan = archive.get("clan") or {}
    season = archive.get("season") or {}
    clan_tag = canonical_tag(clan.get("tag"))
    source_names = sorted({str(r.get("chosen_source")) for r in archive.get("rounds", []) if r.get("chosen_source")})
    source_text = "+".join(source_names) if source_names else "无已取得战绩"
    completeness = "完整" if result.rankable and result.season_complete else ("进行中数据可排名" if result.rankable else "缺失，暂停排名")
    now = archive.get("collected_at") or ""
    season_id = str(season.get("id") or "")
    ranking_fields = [
        "名次", "玩家标签", "成员名称", "大本等级", "实际进攻次数", "进攻总星数", "进攻摧毁率累计", "进攻摧毁率均值",
        "进攻杯分", "进攻大本差合计", "实际防御次数", "防住星数", "防住摧毁率累计", "防住摧毁率均值",
        "实际防御杯分（逐轮最低）", "最终防御杯分（完整终局补偿）", "总杯分", "防御大本差合计",
        "赛季标识", "部落标签", "数据来源", "数据完整性", "排名状态", "排序代号", "排序指标", "采集时间", "缺失说明",
    ]
    rank_rows = []
    for row in result.rows:
        rank_rows.append({
            "名次": row["rank"] if row["rank"] is not None else "",
            "玩家标签": row["tag"],
            "成员名称": row["name"],
            "大本等级": _town_hall_display(row),
            "实际进攻次数": row["attack_count"],
            "进攻总星数": row["attack_stars"],
            "进攻摧毁率累计": display_number(row["attack_destruction_total"]),
            "进攻摧毁率均值": display_number(row["attack_destruction_mean"]),
            "进攻杯分": row["attack_cups"],
            "进攻大本差合计": row["attack_th_diff"],
            "实际防御次数": row["defense_count"],
            "防住星数": row["defended_stars"],
            "防住摧毁率累计": display_number(row["defense_prevented_destruction_total"]),
            "防住摧毁率均值": display_number(row["defense_prevented_destruction_mean"]),
            "实际防御杯分（逐轮最低）": row["defense_actual_cups"],
            "最终防御杯分（完整终局补偿）": display_number(row["defense_final_cups"]),
            "总杯分": row["total_cups"],
            "防御大本差合计": row["defense_th_diff"],
            "赛季标识": season_id,
            "部落标签": clan_tag,
            "数据来源": source_text,
            "数据完整性": completeness,
            "排名状态": "有效" if result.rankable else "暂停排名；已取得战绩仅供参考",
            "排序代号": "".join(result.order),
            "排序指标": " → ".join(ORDER_LABELS[key] for key in result.order),
            "采集时间": now,
            "缺失说明": "；".join(result.missing),
        })

    # 按用户核对习惯分成两组列：先全部进攻轮次，再全部防御轮次。
    attack_headers = [f"第{item['round_number']}轮进攻大本差" for item in result.round_columns]
    defense_headers = [f"第{item['round_number']}轮防御大本差" for item in result.round_columns]
    diff_fields = ["玩家标签", "成员名称", "大本等级"] + attack_headers + defense_headers + ["赛季标识", "部落标签", "数据来源", "数据完整性", "采集时间"]
    clan_tag = canonical_tag((archive.get("clan") or {}).get("tag"))
    rounds_by_number = {int(r.get("round_number") or i): r for i, r in enumerate(archive.get("rounds", []), start=1)}
    diff_rows = []
    for row in result.rows:
        output_row: dict[str, Any] = {
            "玩家标签": row["tag"],
            "成员名称": row["name"],
            "大本等级": _town_hall_display(row),
            "赛季标识": season_id,
            "部落标签": clan_tag,
            "数据来源": source_text,
            "数据完整性": completeness,
            "采集时间": now,
        }
        for column in result.round_columns:
            round_number = column["round_number"]
            attack_text, defense_text = _status_for_round(rounds_by_number[round_number], row, round_number, clan_tag)
            output_row[f"第{round_number}轮进攻大本差"] = attack_text
            output_row[f"第{round_number}轮防御大本差"] = defense_text
        diff_rows.append(output_row)

    _atomic_csv(ranking_path, ranking_fields, rank_rows)
    _atomic_csv(diff_path, diff_fields, diff_rows)
    return ranking_path, diff_path


def _atomic_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    """先写临时 CSV 再替换目标文件，避免中断留下半份结果。

    参数:
        path: 目标 CSV 路径。
        fieldnames: 输出列顺序。
        rows: 每行以列名为键的字典。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        file.flush()
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 对象，并验证顶层格式为对象。

    参数:
        path: JSON 文件路径。
    返回:
        解析后的 JSON 对象。
    """
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise AnalysisError(f"归档不是 JSON 对象：{path}")
    return payload
