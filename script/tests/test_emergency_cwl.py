"""离线验证应急联赛计算、来源关联和文件保全规则。"""

from __future__ import annotations

import copy
import csv
import io
import unittest
from decimal import Decimal
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from cwl_emergency import cli
from cwl_emergency.analysis import (
    AnalysisError,
    analyze_archive,
    attack_cups,
    defense_cups,
    final_defense_cups,
    parse_order,
)
from cwl_emergency.collector import collect_season, official_no_current_league
from cwl_emergency.sources import FetchResult, RuntimeSettings
from cwl_emergency.storage import merge_archives, result_paths
from examples.build_synthetic_sample import SAMPLE_PATH, synthetic_archive


# 测试目录中的归档和结果均相对当前脚本树定位。
SCRIPT_DIR = Path(__file__).resolve().parents[1]


class OfficialWarStub:
    """返回预置战争对象的官方客户端替身。

    属性:
        wars: 以 warTag 为键的测试战争对象。
        requested: 记录被请求的标签，用于确认没有探测无关战争。
    """

    def __init__(self, wars: dict[str, dict]):
        """初始化预置战争与请求记录。

        参数:
            wars: 按标签索引的测试响应对象。
        """
        self.wars = wars
        self.requested: list[str] = []

    def get_cwl_war(self, war_tag: str) -> FetchResult:
        """记录请求并返回标签对应的深拷贝战争响应。"""
        self.requested.append(war_tag)
        return FetchResult(200, payload=copy.deepcopy(self.wars[war_tag]))


class EmergencyCWLTests(unittest.TestCase):
    """围绕合成归档验证应急脚本的可复现业务行为。"""

    @classmethod
    def setUpClass(cls) -> None:
        """准备共享合成归档；仅在样例文件缺失时创建它。"""
        cls.archive = synthetic_archive()
        if not SAMPLE_PATH.exists():
            from cwl_emergency.storage import atomic_write_json

            atomic_write_json(SAMPLE_PATH, cls.archive)

    def test_scoring_rounding_and_defense_compensation(self) -> None:
        """校验杯分、半数进位与终局防御补偿公式。"""
        self.assertEqual(attack_cups(2, 70), 23)
        self.assertEqual(defense_cups(2, 70), 17)
        self.assertEqual(attack_cups(2, Decimal("51.5")), 17)
        self.assertEqual(final_defense_cups([10, 20, 30, 20], 7), 140)
        self.assertIsNone(final_defense_cups([], 7))
        with self.assertRaises(AnalysisError):
            final_defense_cups([10, 20], 1)

    def test_sample_roster_terminal_totals_and_round_minimum(self) -> None:
        """校验报名名单保留、均值空值和逐轮最低防御杯分。"""
        result = analyze_archive(self.archive)
        self.assertTrue(result.rankable)
        self.assertTrue(result.season_complete)
        self.assertEqual(len(result.rows), 3)
        by_tag = {row["tag"]: row for row in result.rows}
        p1 = by_tag["#P1"]
        p3 = by_tag["#P3"]
        self.assertEqual(p1["defense_actual_cups"], 80)
        self.assertEqual(p1["defense_final_cups"], 140)
        self.assertEqual(p3["attack_count"], 0)
        self.assertIsNone(p3["attack_destruction_mean"])
        self.assertEqual([column["round_number"] for column in result.round_columns], list(range(1, 8)))

    def test_priority_order_is_descending_and_uses_only_selected_items(self) -> None:
        """校验排序按指定优先级降序且只使用所选指标。"""
        self.assertEqual(parse_order("B A D"), ("B", "A", "D"))
        by_a = analyze_archive(self.archive, "A")
        by_b = analyze_archive(self.archive, "B")
        self.assertEqual(by_a.rows[0]["tag"], "#P1")
        self.assertEqual(by_b.rows[0]["tag"], "#P2")
        self.assertEqual([row["rank"] for row in by_a.rows], [1, 2, 3])

    def test_missing_attack_field_suspends_rank_but_keeps_rows(self) -> None:
        """侧级攻击总数不能与明细核对时暂停排名并保留成员行。"""
        incomplete = copy.deepcopy(self.archive)
        # P1 有一条进攻，删除其列表后成员合计将与来源侧级总数不一致。
        del incomplete["rounds"][0]["war"]["clan"]["members"][0]["attacks"]
        result = analyze_archive(incomplete)
        self.assertFalse(result.rankable)
        self.assertFalse(result.season_complete)
        self.assertTrue(all(row["rank"] is None for row in result.rows))
        self.assertEqual(len(result.rows), 3)

    def test_omitted_optional_attacks_field_is_zero_only_when_aggregate_matches(self) -> None:
        """成员 attacks 缺键仅在来源侧级攻击总数核对一致时视为零次。"""
        valid_omission = copy.deepcopy(self.archive)
        # P3 本轮本来没有攻击，来源侧 attacks=1 与显式成员记录总数仍一致。
        del valid_omission["rounds"][0]["war"]["clan"]["members"][2]["attacks"]
        result = analyze_archive(valid_omission)
        self.assertTrue(result.rankable)
        self.assertTrue(result.season_complete)

    def test_running_war_does_not_apply_terminal_bonus(self) -> None:
        """进行中的战争仍可按当前数据排名，但不执行终局防御补偿。"""
        active = copy.deepcopy(self.archive)
        active["rounds"][0]["war"]["state"] = "inWar"
        result = analyze_archive(active)
        self.assertTrue(result.rankable)
        self.assertFalse(result.season_complete)
        self.assertIsNone(next(row for row in result.rows if row["tag"] == "#P1")["defense_final_cups"])

    def test_official_source_requires_exact_target_war_tag_and_keeps_both_sources(self) -> None:
        """官方记录须通过同轮精确 warTag 与目标标签校验，并保存双源证据。"""
        ck_detail = {
            "season": "2026-10-02",
            "state": "ended",
            "clans": [{"tag": "#CLAN1", "name": "Sample Clan", "members": self.archive["roster"]}],
            "rounds": [],
        }
        official_group_rounds = []
        official_wars: dict[str, dict] = {}
        for index, archive_round in enumerate(self.archive["rounds"], start=1):
            ck_war = copy.deepcopy(archive_round["war"])
            official_war = copy.deepcopy(ck_war)
            official_war["officialMarker"] = True
            if index == 1:
                ck_war["state"] = "preparation"
                official_war["state"] = "preparation"
            ck_detail["rounds"].append({"warTags": [ck_war]})
            official_group_rounds.append({"warTags": [ck_war["tag"]]})
            official_wars[ck_war["tag"]] = official_war

        group = {
            "state": "inWar",
            "season": "2026-10",
            "clans": [{"tag": "#CLAN1", "name": "Sample Clan", "members": self.archive["roster"]}],
            "rounds": official_group_rounds,
        }
        official_stub = OfficialWarStub(official_wars)
        progress: list[dict] = []
        collected = collect_season(
            clan_tag="#CLAN1",
            season_id="2026-10-02",
            discovery_item={"season": "2026-10-02", "state": "ended"},
            index_result=FetchResult(200, payload={"items": [{"season": "2026-10-02"}, {"season": "2026-10-18"}]}),
            season_result=FetchResult(200, payload=ck_detail),
            official_group_result=FetchResult(200, payload=group),
            official_client=official_stub,
            index_month_count=2,
            target_month="202610",
            on_progress=progress.append,
        )
        self.assertEqual(len(progress), 7)
        self.assertEqual(len(official_stub.requested), 7)
        self.assertEqual(set(official_stub.requested), {war["war"]["tag"] for war in self.archive["rounds"]})
        self.assertTrue(all(item["chosen_source"] == "official" for item in collected["rounds"]))
        self.assertEqual(collected["rounds"][0]["war"]["state"], "preparation")
        self.assertTrue(collected["rounds"][0]["sources"]["clashking"]["payload"])
        self.assertTrue(collected["rounds"][0]["sources"]["official"]["payload"]["officialMarker"])

    def test_no_league_requires_explicit_successful_state(self) -> None:
        """只有成功响应明确返回 notInWar 才算确认无当前联赛。"""
        self.assertTrue(official_no_current_league(FetchResult(200, {"state": "notInWar"})))
        self.assertFalse(official_no_current_league(FetchResult(403, {"state": "notInWar"})))
        self.assertFalse(official_no_current_league(FetchResult(200, {"state": "preparation"})))

    def test_official_service_error_shows_status_and_retry_details(self) -> None:
        """重试耗尽时展示来源的真实错误，不把 HTTP 503 写成网络错误。"""
        output = io.StringIO()
        result = FetchResult(0, error="来源返回 HTTP 503。 已尝试 3 次。", attempts=3)
        with redirect_stdout(output):
            cli._describe_official_status(result, "202610", "202610")
        self.assertIn("HTTP 503", output.getvalue())
        self.assertIn("已尝试 3 次", output.getvalue())
        self.assertNotIn("HTTP 网络错误", output.getvalue())

    def test_month_input_and_six_item_menu_retry_invalid_order(self) -> None:
        """校验年月默认值、六项菜单和非法排序后的重新输入。"""
        month, was_defaulted = cli.parse_month("202610")
        self.assertEqual(month, "202610")
        self.assertFalse(was_defaulted)
        current_month, was_defaulted = cli.parse_month(None)
        self.assertRegex(current_month, r"^\d{6}$")
        self.assertTrue(was_defaulted)
        output = io.StringIO()
        with patch("builtins.input", side_effect=["A A", "B E"]), redirect_stdout(output):
            order = cli.choose_order(None, interactive=True)
        self.assertEqual(order, ("B", "E"))
        menu_items = [line for line in output.getvalue().splitlines() if line[:2] in {f"{key}：" for key in "ABCDEF"}]
        self.assertEqual(len(menu_items), 6)
        self.assertIn("排序输入无效", output.getvalue())

    def test_env_secrets_and_proxy_load_without_rendering_values(self) -> None:
        """验证本地密钥和代理可加载为隐藏配置。"""
        settings = RuntimeSettings()
        self.assertIsNotNone(settings.coc_api_token)
        proxy = settings.safe_proxy_url()
        self.assertIsNotNone(proxy)
        self.assertIsNotNone(settings.proxy_url)

    def test_offline_export_writes_named_csvs_without_constructing_http_client(self) -> None:
        """验证离线重算生成两份命名 CSV 且不创建外部请求。"""
        from cwl_emergency.storage import atomic_write_json

        atomic_write_json(SAMPLE_PATH, self.archive)
        with patch("cwl_emergency.sources.JsonHTTPClient.get_json", side_effect=AssertionError("unexpected network request")):
            status = cli.main(["export", str(SAMPLE_PATH), "--sort-order", "A B D C F"])
        self.assertEqual(status, 0)
        ranking, diff = result_paths(cli.RESULTS_DIR, "Sample Clan", "#CLAN1", "2026-10-02", ("A", "B", "D", "C", "F"))
        self.assertTrue(ranking.is_file())
        self.assertTrue(diff.is_file())
        with diff.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            diff_headers = [field for field in reader.fieldnames or [] if field.endswith("大本差")]
            self.assertEqual(
                diff_headers,
                [f"第{number}轮进攻大本差" for number in range(1, 8)]
                + [f"第{number}轮防御大本差" for number in range(1, 8)],
            )
            p1 = next(row for row in rows if row["玩家标签"] == "#P1")
            p3 = next(row for row in rows if row["玩家标签"] == "#P3")
            self.assertEqual(p1["第1轮进攻大本差"], "+0")
            self.assertEqual(p1["第1轮防御大本差"], "+0, -1（合计 -1）")
            self.assertEqual(p3["第1轮进攻大本差"], "未进攻")
            self.assertEqual(p3["第1轮防御大本差"], "未被攻击")

    def test_archive_merge_is_idempotent_and_filename_keeps_priority_suffix(self) -> None:
        """验证同届归档合并幂等，CSV 文件名包含实际排序代号。"""
        merged = merge_archives(copy.deepcopy(self.archive), self.archive)
        self.assertEqual(len(merged["rounds"]), 7)
        self.assertEqual(len(merged["rounds"][0]["war"]["clan"]["members"][0]["attacks"]), 1)
        rank, diff = result_paths(Path("results"), "Sample Clan", "#CLAN1", "2026-10-02", ("D", "A", "B"))
        self.assertTrue(rank.name.endswith("_排名信息_DAB.csv"))
        self.assertTrue(diff.name.endswith("_大本差详细信息_DAB.csv"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
