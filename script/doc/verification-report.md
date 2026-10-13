# 应急联赛脚本验证报告

日期：2026-10-13（Asia/Shanghai）

范围：Windows 本地实现、离线验证和只读 API 基本检查。VM 上的完整真实采集尚未执行。

## 运行环境

- Windows 11，Conda 环境 `ClanMetric`，Python 3.13.16。
- `httpx==0.28.1`、`pydantic==2.13.3`、`pydantic-settings==2.15.0`、`tenacity==9.2.1`。
- 通过 `script/requirements.txt` 安装依赖；没有安装正式服务或数据库组件。
- 本地设置读取检查通过：脚本从 `script/.env` 识别官方密钥与代理配置。报告、样例和 CSV 均不含配置值。

## 已执行验证

命令（工作目录为 `script/`；原解释器绝对路径脱敏为 `<CLANMETRIC_PYTHON>`，代表实际使用的 ClanMetric conda 环境内 Python；版本和其余参数照原记录保留，详见 [路径约定](../../doc/path-conventions.md)）：

```powershell
& "<CLANMETRIC_PYTHON>" -X utf8 -m unittest discover -s tests -v
& "<CLANMETRIC_PYTHON>" -X utf8 emergency_cwl.py --help
& "<CLANMETRIC_PYTHON>" -X utf8 emergency_cwl.py export examples\synthetic_cwl_2026_10_02.json --sort-order "A B D C F"
```

结果：13 项离线测试全部通过；帮助入口退出成功；样例离线导出退出成功，测试将 HTTP 客户端请求替换为立即失败的哨兵，确认导出路径没有调用外部接口。

覆盖内容：

- 2 星、70% 得进攻杯分 23、防御杯分 17；.5 边界按四舍五入向上；四个有防御轮次合计 80、七轮补偿为 140；整届无防御时不可估算。
- 三名报名成员均出现在结果；未进攻成员的均值为空值。缺少终局 attacks 字段会保留成员行并暂停排名；进行中的战争不触发终局补偿。
- A 与 B 改变优先顺序时能按各自指标降序排序；没有额外未选指标插入排序。
- 官方来源只在官方联赛组同轮列出精确 warTag、官方战争再次匹配目标部落后选用；ClashKing 和官方原始战争记录分别保留。模拟数据覆盖准备期和已结束战争，索引出现两个同月赛季也不会阻止精确 warTag 匹配。
- JSON 合并重复运行不重复增加战争轮次；逐轮采集回调会产生七个有序快照。
- 大本差 CSV 将七轮进攻和七轮防御分成两组共 14 列；单条差值只显示数值，多条保留明细与合计，可区分真实 `+0`、未进攻与未被攻击；文件名保留排序序列。
- 主入口 `--help` 不访问 API。
- 输入菜单只列 A～F 六项；非法优先级提示后要求重输；YYYYMM 与当前年月默认分支已核对。
- 对成员 `attacks` 缺键执行两种对账验证：侧级攻击总数与可见明细一致时排名有效；移除包含真实攻击的列表导致侧级总数不匹配时暂停排名且保留成员行。
- 官方请求连续服务错误时，控制台显示来源实际错误内容与重试次数；不会把耗尽重试后的内部状态码 0 误报为网络错误。

后续实现更新：排名相同的成员现在按用户选中的全部指标判断并列，显示竞赛名次（如 `1、2、2、2、5`）；标签升序仅决定并列成员的行顺序。本次更新尚未纳入上方 13 项离线测试，现有 CSV 也未重导。

生成的脱敏合成样例和 CSV：

- `script/examples/synthetic_cwl_2026_10_02.json`
- `script/results/Sample_Clan_CLAN1_2026_10_02_排名信息_ABDCF.csv`
- `script/results/Sample_Clan_CLAN1_2026_10_02_大本差详细信息_ABDCF.csv`

所有示例联赛、部落、成员和战争均为合成数据。

## 来源字段核对与实测边界

2026-10-13 检查了来源公开 OpenAPI 定义，并按用户确认的文档样本部落 `#GPQ00V9C` 通过本机已配置代理运行只读检查。基础接口检查分开保留在 `script/results/source_validation_*.json`。之后用户在 Windows 上实际运行了目标月采集，ClashKing 七轮战绩已写入 `script/data/傲世苍穹_GPQ00V9C_2026_10_02.json`；排名 CSV 替换时因文件被其他软件打开而失败，JSON 归档保留完整，CSV 可离线重导。

- [Clash of Clans API 文档](https://developer.clashofclans.com/)：当前联赛组路由 `/clans/{clanTag}/currentwar/leaguegroup` 和战争路由 `/clanwarleagues/wars/{warTag}`。脚本用目标部落标签、同轮 warTag、响应战争标签及月份进行核对；401／403、429、网络和字段错误都不会转为“没有联赛”。
- [ClashKing OpenAPI JSON](https://api.clashk.ing/openapi.json)：赛季索引路由 `/v2/cwl/{clanTag}/seasons` 返回 `items`，定义了 `season`、`state`、`warSize`、`warLeague` 等字段；定义没有索引总数或分页游标，故月覆盖率不能确认。旧赛季详情路由 `/cwl/{clan_tag}/{season}` 定义 `clans` 和 `rounds[].warTags`；轮次元素可能是带双方成员和攻击记录的战争对象，也可能只有 `{tag}` 占位符。只有战争对象明确匹配部落和标签才会直接采用，不会对只含标签的分组逐个探测其它部落战争。

本次真实只读接口结果：

- 官方部落接口 HTTP 200，返回标签与 `#GPQ00V9C` 匹配。官方当前联赛组接口返回 HTTP 404；报告保留为 404，没有解释成 `notInWar` 或断言当前无联赛。另以 ClashKing 目标战争记录提供的 `warTag` 检查官方战争端点，HTTP 200，响应双方中有目标部落。战争响应字段包含 `clan`、`opponent`、`state`、时间和人数等，但未返回 `tag`、`season`；脚本允许缺少这两个回显字段，仍要求调用上下文的精确 warTag 关联和战争双方目标校验。
- ClashKing 赛季索引 HTTP 200，返回 12 个索引项。目标 `2026-10-02` 详情 HTTP 200，目标部落关联有效，报名名单 41 人、7 轮、7 场目标战争、420 个双方成员记录、340 次攻击记录。该检查读取详情用于字段与目标关联核验，没有把这些真实战绩保存成归档或 CSV。
- 完整月覆盖仍未验证：OpenAPI 没有总数或分页游标。官方当前联赛组的 HTTP 404 只说明当前组资源未找到，不代表目标月份没有历史联赛；成功响应明确为 `state=notInWar` 时也只表示当前未参战。

对应记录：`source_validation_official_20261013T160633.json`（部落、当前联赛组、warTag 三项）、`source_validation_clashking_20261013T160550.json`（索引与赛季详情），以及 `source_validation_official_20261013T160537.json`（早先单独检查记录）。

用户随后一次实际采集中，归档记录官方当前联赛组请求在 3 次尝试后收到 HTTP 503；旧版客户端将重试耗尽记为内部状态码 0，提示因此显示“HTTP 网络错误”。这不表示确认没有联赛；ClashKing 该届 7 轮均已采集。客户端现保留最后一次 HTTP 状态码，并在提示中显示状态和尝试次数。仅凭经代理收到的 HTTP 503 不能区分响应由官方服务还是代理网关返回。

2026-10-13 17:09（Asia/Shanghai）再次通过本机代理运行只读官方检查：部落详情接口 HTTP 200，返回标签匹配；当前联赛组接口 HTTP 404、尝试 1 次，错误对象为 `reason=notFound`。官方文档将该接口的 404 定义为“请求资源未找到”。由于同次检查的部落详情端点成功且标签匹配，这次缺失的是当前联赛组资源；它不说明目标月份没有历史联赛。脚本现在会在控制台和验证报告中显示该原因，并继续查询 ClashKing。记录：`source_validation_official_20261013T170900.json`。

ClashKing OpenAPI 将成员 `attacks` 定义为可选数组、战争侧 `attacks` 定义为数值汇总。本次目标赛季的 14 个战争侧汇总均与成员明细总数吻合；420 个双方成员对象中，340 个含 `attacks`、80 个省略该字段，未发现空数组或非数组值。官方样本第一轮 60 名成员中，47 个含字段、13 个省略；双方汇总数 21 和 26 均与明细一致。因此实现只在侧级总数与可见成员攻击列表合计完全相等时，将缺键解释为零次；侧级汇总缺失、非法或不匹配时暂停排名。显式 `null` 或非数组仍视为数据不完整。

VM 上完整真实采集尚待平台侧运行和确认；当前报告只覆盖 Windows 本地离线验证与两源只读基本检查，不把烟测响应写成真实归档或 CSV。平台实采后需核对完整归档、每轮所选来源与两份 CSV。
