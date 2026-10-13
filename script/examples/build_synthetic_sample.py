"""生成完全合成的离线联赛归档，用于说明输出格式和规则。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cwl_emergency.storage import atomic_write_json


# 样例输出固定写在 examples/，避免覆盖真实采集数据。
SAMPLE_PATH = Path(__file__).resolve().parent / "synthetic_cwl_2026_10_02.json"


def attack(attacker: str, defender: str, stars: int, destruction: int, order: int) -> dict:
    """构造样例战争中的一条合成进攻记录。

    参数:
        attacker: 合成攻击者标签。
        defender: 合成防御者标签。
        stars: 合成星数。
        destruction: 合成摧毁率百分比。
        order: 合成战争内攻击序号。
    返回:
        来源字段形状的攻击字典。
    """
    return {
        "attackerTag": attacker,
        "defenderTag": defender,
        "stars": stars,
        "destructionPercentage": destruction,
        "order": order,
        "duration": 180,
    }


def member(tag: str, name: str, town_hall: int, position: int, attacks: list[dict]) -> dict:
    """构造样例战争的一名合成成员。

    参数:
        tag: 合成玩家标签。
        name: 合成显示名。
        town_hall: 战时大本等级。
        position: 地图位置序号。
        attacks: 该成员的合成攻击列表。
    返回:
        来源成员对象形状的字典。
    """
    return {
        "tag": tag,
        "name": name,
        "townhallLevel": town_hall,
        "mapPosition": position,
        "attacks": attacks,
        "opponentAttacks": [],
        "bestOpponentAttack": None,
    }


def synthetic_archive() -> dict:
    """构造七轮、三名报名成员的脱敏合成完整终局归档。

    返回:
        可直接交给离线分析和 CSV 导出的 v1 归档字典。
    """
    clan_tag = "#CLAN1"
    clan_members = [
        ("#P1", "Sample One", 16),
        ("#P2", "Sample Two", 15),
        ("#P3", "No Attack Sample", 14),
    ]
    enemy_members = [("#O1", "Opponent One", 16), ("#O2", "Opponent Two", 15)]
    rounds = []
    start = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)

    # 逐轮构造独立战争；示例攻击量刻意较少，便于人工核对计算结果。
    for number in range(1, 8):
        own_attacks = {tag: [] for tag, _, _ in clan_members}
        enemy_attacks = {tag: [] for tag, _, _ in enemy_members}
        if number == 1:
            own_attacks["#P1"].append(attack("#P1", "#O1", 2, 70, 1))
            enemy_attacks["#O1"].append(attack("#O1", "#P1", 2, 92, 1))
            enemy_attacks["#O2"].append(attack("#O2", "#P1", 1, 46, 2))
        elif number == 2:
            own_attacks["#P1"].append(attack("#P1", "#O1", 2, 70, 1))
            enemy_attacks["#O1"].append(attack("#O1", "#P1", 2, 62, 1))
        elif number == 3:
            own_attacks["#P2"].append(attack("#P2", "#O2", 1, 99, 1))
            enemy_attacks["#O1"].append(attack("#O1", "#P1", 1, 46, 1))
        elif number == 4:
            own_attacks["#P2"].append(attack("#P2", "#O2", 1, 99, 1))
            enemy_attacks["#O1"].append(attack("#O1", "#P1", 2, 62, 1))

        our_side_members = [
            member(tag, name, th, position, own_attacks[tag])
            for position, (tag, name, th) in enumerate(clan_members, start=1)
        ]
        opponent_side_members = [
            member(tag, name, th, position, enemy_attacks[tag])
            for position, (tag, name, th) in enumerate(enemy_members, start=1)
        ]
        # 来源侧 attacks 是逐成员攻击列表的汇总计数，样例保持二者一致。
        our_attack_total = sum(len(item["attacks"]) for item in our_side_members)
        opponent_attack_total = sum(len(item["attacks"]) for item in opponent_side_members)
        war_start = start + timedelta(days=number - 1)
        war_tag = f"#SYNTH{number:02d}"
        war = {
            "state": "warEnded",
            "teamSize": 3,
            "attacksPerMember": 1,
            "battleModifier": "none",
            "preparationStartTime": (war_start - timedelta(hours=24)).strftime("%Y%m%dT%H%M%S.000Z"),
            "startTime": war_start.strftime("%Y%m%dT%H%M%S.000Z"),
            "endTime": (war_start + timedelta(hours=24)).strftime("%Y%m%dT%H%M%S.000Z"),
            "clan": {"tag": clan_tag, "name": "Sample Clan", "attacks": our_attack_total, "stars": 0, "destructionPercentage": 0, "members": our_side_members},
            "opponent": {"tag": "#ENEMY1", "name": "Sample Opponent", "attacks": opponent_attack_total, "stars": 0, "destructionPercentage": 0, "members": opponent_side_members},
            "warStartTime": war_start.strftime("%Y%m%dT%H%M%S.000Z"),
            "tag": war_tag,
            "season": "2026-10-02",
        }
        rounds.append({
            "round_number": number,
            "war_tag": war_tag,
            "status": "collected",
            "war": war,
            "chosen_source": "synthetic-example",
            "sources": {"synthetic-example": {"http_status": None, "request_ok": False, "payload": war}},
            "last_attempt": {},
        })

    return {
        "archive_format": "clanmetric-cwl-archive-v1",
        "clan": {"tag": clan_tag, "name": "Sample Clan"},
        "season": {
            "id": "2026-10-02",
            "month": "2026-10",
            "state": "ended",
            "warLeague": "Crystal League II",
            "warSize": 3,
            "index_item": {"season": "2026-10-02", "state": "ended", "warSize": 3, "warLeague": "Crystal League II"},
        },
        "roster": [
            {"tag": tag, "name": name, "townHallLevel": th}
            for tag, name, th in clan_members
        ],
        "roster_source": "synthetic-example",
        "rounds": rounds,
        "source_checks": {
            "clashking_index": {"http_status": None, "request_ok": False, "top_level_fields": []},
            "clashking_season": {"http_status": None, "request_ok": False, "top_level_fields": []},
            "official_group": {"http_status": None, "request_ok": False, "top_level_fields": []},
        },
        "discovery": {
            "target_month": "202610",
            "requested_season_id": "2026-10-02",
            "month_coverage": "synthetic-example",
        },
        "collection": {"status": "synthetic-example", "errors": []},
        "collected_at": "2026-10-13T15:00:00+08:00",
        "example_data": True,
        "example_note": "完全合成的离线示例，非真实部落或来源返回值。",
    }


if __name__ == "__main__":
    atomic_write_json(SAMPLE_PATH, synthetic_archive())
    print(f"已写入合成示例：{SAMPLE_PATH}")
