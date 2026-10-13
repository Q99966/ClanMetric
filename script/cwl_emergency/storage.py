"""应急联赛归档及结果文件的命名、原子写入与逐轮合并。"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .analysis import canonical_tag


def safe_filename(value: str) -> str:
    """规范文件名片段，移除 Windows 非法字符并收敛空白。

    参数:
        value: 部落名、标签或其他来源标识。
    返回:
        可安全用于 Windows 文件名的 NFC 字符串。
    """
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalized)
    normalized = re.sub(r"\s+", "_", normalized)
    return normalized.rstrip(" .") or "unknown"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """以 UTF-8 写临时 JSON 并原子替换目标文件。

    参数:
        path: JSON 目标路径。
        payload: 要持久化的归档或验证报告对象。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=False)
        file.write("\n")
        file.flush()
    temporary.replace(path)


def read_archive(path: Path) -> dict[str, Any]:
    """读取并检查 ClanMetric 应急归档版本标记。

    参数:
        path: 归档 JSON 文件路径。
    返回:
        顶层格式受支持的归档对象。
    异常:
        文件无法读取、JSON 无效或版本不支持时抛出异常。
    """
    with path.open("r", encoding="utf-8") as file:
        archive = json.load(file)
    if not isinstance(archive, dict) or archive.get("archive_format") != "clanmetric-cwl-archive-v1":
        raise ValueError(f"不是受支持的 ClanMetric 联赛归档：{path}")
    return archive


def find_archive(data_dir: Path, clan_tag: str, season_id: str) -> Path | None:
    """按部落标签和完整赛季标识查找已有归档，供同届更新复用。

    参数:
        data_dir: 归档目录。
        clan_tag: 目标部落标签。
        season_id: 来源完整赛季标识。
    返回:
        第一个匹配 JSON 路径；未找到时为 None。
    """
    expected_tag = canonical_tag(clan_tag)
    for candidate in sorted(data_dir.glob("*.json")):
        try:
            with candidate.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue
        clan = payload.get("clan") if isinstance(payload, dict) else None
        season = payload.get("season") if isinstance(payload, dict) else None
        if (
            isinstance(clan, dict)
            and isinstance(season, dict)
            and canonical_tag(clan.get("tag")) == expected_tag
            and season.get("id") == season_id
        ):
            return candidate
    return None


def canonical_archive_path(data_dir: Path, clan_name: str, clan_tag: str, season_id: str) -> Path:
    """构造使用来源完整赛季日期的正式 JSON 文件路径。

    参数:
        data_dir: 归档目录。
        clan_name: 已验证的部落名称。
        clan_tag: 已验证的部落标签。
        season_id: 来源完整日期赛季标识。
    返回:
        正式归档文件路径。
    """
    date_part = season_id.replace("-", "_")
    return data_dir / f"{safe_filename(clan_name)}_{safe_filename(canonical_tag(clan_tag).lstrip('#'))}_{date_part}.json"


def pending_archive_path(data_dir: Path, clan_name: str, clan_tag: str, target: str) -> Path:
    """为日期/身份尚未核实的采集结果构造待识别文件名。

    参数:
        data_dir: 归档目录。
        clan_name: 可用的部落显示名；缺失时调用方提供说明名称。
        clan_tag: 已知的部落标签。
        target: 未核实赛季标识或目标月份。
    返回:
        带“待识别”标记的归档路径。
    """
    return data_dir / f"{safe_filename(clan_name)}_{safe_filename(canonical_tag(clan_tag).lstrip('#'))}_待识别_{safe_filename(target)}.json"


def result_paths(results_dir: Path, clan_name: str, clan_tag: str, season_id: str, order: tuple[str, ...]) -> tuple[Path, Path]:
    """按届和实际排序序列生成排名与大本差核对 CSV 路径。

    参数:
        results_dir: CSV 输出目录。
        clan_name: 已验证的部落名称。
        clan_tag: 已验证的部落标签。
        season_id: 来源完整赛季标识。
        order: 实际采用的排序代号序列。
    返回:
        (排名 CSV 路径, 大本差核对 CSV 路径)。
    """
    base = f"{safe_filename(clan_name)}_{safe_filename(canonical_tag(clan_tag).lstrip('#'))}_{season_id.replace('-', '_')}"
    suffix = "".join(order)
    return (
        results_dir / f"{base}_排名信息_{suffix}.csv",
        results_dir / f"{base}_大本差详细信息_{suffix}.csv",
    )


def merge_archives(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """合并同部落、同届新旧归档，更新已取轮次并保留旧证据。

    参数:
        existing: 已有归档；首次写入时为 None。
        incoming: 本轮采集得到的当前快照。
    返回:
        按轮次编号合并后的新归档，不修改输入对象。
    异常:
        部落标签或完整赛季标识不一致时拒绝合并。
    """
    if existing is None:
        return incoming
    old_clan = existing.get("clan") or {}
    new_clan = incoming.get("clan") or {}
    old_season = existing.get("season") or {}
    new_season = incoming.get("season") or {}
    if canonical_tag(old_clan.get("tag")) != canonical_tag(new_clan.get("tag")):
        raise ValueError("归档部落标签不一致，拒绝覆盖。")
    if old_season.get("id") != new_season.get("id"):
        raise ValueError("归档完整赛季标识不一致，拒绝覆盖。")

    merged = deepcopy(existing)
    if new_clan.get("name"):
        merged.setdefault("clan", {})["name"] = new_clan["name"]
    if incoming.get("roster"):
        merged["roster"] = incoming["roster"]
        merged["roster_source"] = incoming.get("roster_source") or merged.get("roster_source")
    merged["season"] = {**merged.get("season", {}), **new_season}
    merged["discovery"] = incoming.get("discovery", merged.get("discovery", {}))
    merged["source_checks"] = incoming.get("source_checks", merged.get("source_checks", {}))
    merged["collection"] = incoming.get("collection", merged.get("collection", {}))
    merged["collected_at"] = incoming.get("collected_at") or merged.get("collected_at")

    # 用轮次编号作为合并键，断点重采不会重复追加同一轮。
    by_number = {int(item.get("round_number", index)): item for index, item in enumerate(merged.get("rounds", []), start=1)}
    for index, new_round in enumerate(incoming.get("rounds", []), start=1):
        number = int(new_round.get("round_number", index))
        old_round = by_number.get(number)
        if old_round is None:
            by_number[number] = deepcopy(new_round)
            continue
        combined = deepcopy(old_round)
        combined["round_number"] = number
        old_sources = combined.setdefault("sources", {})
        for name, new_source in (new_round.get("sources") or {}).items():
            old_source = old_sources.get(name, {})
            if isinstance(new_source, dict) and isinstance(old_source, dict):
                merged_source = {**old_source, **{k: v for k, v in new_source.items() if k != "payload" or v is not None}}
                if new_source.get("payload") is None and old_source.get("payload") is not None:
                    merged_source["payload"] = old_source["payload"]
                old_sources[name] = merged_source
            else:
                old_sources[name] = new_source
        if isinstance(new_round.get("war"), dict):
            combined["war"] = new_round["war"]
            combined["war_tag"] = new_round.get("war_tag") or combined.get("war_tag")
            combined["status"] = "collected"
            combined["chosen_source"] = new_round.get("chosen_source") or combined.get("chosen_source")
        elif combined.get("status") != "collected":
            combined["status"] = new_round.get("status", combined.get("status", "missing"))
        combined["last_attempt"] = new_round.get("last_attempt", {})
        combined["conflict"] = new_round.get("conflict", combined.get("conflict", {}))
        by_number[number] = combined

    merged["rounds"] = [by_number[key] for key in sorted(by_number)]
    merged["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return merged
