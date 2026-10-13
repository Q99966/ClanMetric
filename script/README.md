# ClanMetric 应急联赛脚本

最后更新：2026-10-13。本文说明首次准备、常用命令、输出文件和故障排查。脚本的来源策略、字段要求及计算规则见 [`doc/`](doc/)；启动和输出以其中最新设计为准。

此工具用于手动查询、归档、补采与离线导出。它不启动常驻服务、不连接 ClanMetric 业务数据库，也不改动正式平台。

## 第一次运行前

### 1. 创建官方 API Key

1. 打开 [Clash of Clans 官方开发者门户](https://developer.clashofclans.com/) 并注册或登录账号。
2. 在 **My Account** 页面创建 API Key，填写名称和描述，并设置允许访问该 Key 的公网 IP 地址。
3. 允许 IP 必须是脚本请求实际到达官方 API 时使用的公网出口地址。直连时填写运行机器的公网出口 IP；通过代理访问时，填写代理的公网出口 IP。在不同机器或网络上运行时，要确保 Key 的白名单覆盖实际会使用的出口地址。回环地址（例如 `127.0.0.1`）不是公网白名单地址。
4. 创建后复制 API Key，只放入 `script/.env`，不要贴到命令行、报告或聊天中。

API Key 的 IP 白名单和下面 `.env` 中的 `IP` 是两项不同配置：门户里的白名单是公网出口 IP；`.env` 的 `IP` 是脚本使用的代理 URL。

### 2. 配置 `script/.env`

在 `script/` 目录创建或编辑 `.env`。默认不配置代理：

```dotenv
COC_API_KEY=YOUR_OFFICIAL_API_KEY
CLAN_TAG="#部落标签"
```

- `COC_API_KEY`：官方 API Key。脚本也兼容旧变量名 `COC_API_TOKEN`。
- `IP`：可选的 HTTP(S) 代理 URL，必须包含协议和端口。只有需要代理时才添加，例如 `IP=http://127.0.0.1:7890`；请换成当前机器上代理实际监听的地址和端口。脚本不假设所有人的代理端口相同。
- `CLAN_TAG`：可选的默认部落标签。也可以每次在命令行使用 `--clan-tag` 指定。

没有代理需求时，保持 `.env` 中不设置 `IP`。若系统设置了 `HTTP_PROXY`、`HTTPS_PROXY` 等环境变量，HTTPX 仍可能使用这些代理；要确保直连，请在网络命令中加入 `--no-proxy`。该选项会忽略 `.env` 中的 `IP` 和系统代理环境变量。若要使用 `.env` 配置的代理，请不要加此选项。

`.env` 已加入 Git 忽略规则。不要提交或分享包含真实 Key 的文件。Key 创建后若仍遇到官方 API 鉴权错误，请检查 Key 的 IP 白名单是否与当前网络出口一致，并确认已选的连接方式可用。

### 3. 准备 Python 环境

Windows 本地开发和离线验证使用 Python 3.13 的 Conda 环境 `ClanMetric`。若环境已经创建，直接激活；首次使用时才需要创建：

```powershell
conda create -n ClanMetric python=3.13
conda activate ClanMetric
python -m pip install -r requirements.txt
```

后续打开新的终端，先进入 `script/` 并激活环境：

```powershell
Set-Location .\script
conda activate ClanMetric
```

检查命令行帮助：

```powershell
python emergency_cwl.py --help
```

## 常用运行方式

### 查询一个年月

指定 `YYYYMM` 后，脚本会查找该月能发现的全部联赛，每届独立保存和导出。可加 `--no-proxy` 强制直连。PowerShell 中部落标签请加引号；`#` 未加引号时可能被当作注释，导致 `--clan-tag` 收不到参数。

```powershell
python emergency_cwl.py 202610 --clan-tag "#部落标签" --no-proxy
```

也可预先在 `.env` 设置 `CLAN_TAG`，省略命令行标签：

```powershell
python emergency_cwl.py 202610
```

省略年月时按 `Asia/Shanghai` 使用当前年月，并打印指定年月的用法：

```powershell
python emergency_cwl.py --clan-tag "#部落标签"
```

省略年月和标签时，脚本使用当前年月及 `.env` 中的 `CLAN_TAG`：

```powershell
python emergency_cwl.py
```

### 排序选择

交互运行时，脚本只显示以下六项，并提示输入一次排序优先级。选入的排序指标按降序比较；B、E 是累计值。直接回车采用 `A B D C F`。也可用 `--sort-order` 固定排序，方便无人值守或复现结果：

| 代号 | 排序项 |
| --- | --- |
| A | 进攻总星数 |
| B | 进攻摧毁率累计 |
| C | 进攻大本差合计 |
| D | 总杯分 |
| E | 防住摧毁率累计 |
| F | 防御大本差合计 |

```powershell
python emergency_cwl.py 202610 --clan-tag "#部落标签" --sort-order "A B D C F"
```

上例为强制直连。若 `.env` 中已设置 `IP` 且本次要使用该代理，请删除 `--no-proxy`。若希望强制直连，可运行：

```powershell
python emergency_cwl.py 202610 --clan-tag "#部落标签" --sort-order "A B D C F" --no-proxy
```

只选择 `A B C` 时就只按这三项降序比较。所有已选项都相同的玩家显示竞赛并列名次，例如 `1、2、2、2、5`；玩家标签升序只决定并列行的显示顺序。

## 补采、离线导出和来源检查

以下命令均在 `script/` 目录运行。文件路径可按实际归档位置调整。

```powershell
# 从已有 JSON 离线重算两份 CSV，不访问网络
python emergency_cwl.py export "data\归档.json" --sort-order "A B D C F"

# 按 JSON 中的部落和赛季信息补采，保留已取得的轮次
python emergency_cwl.py backfill "data\归档.json" --sort-order "A B D C F"

# 查询 ClashKing 返回的完整历史赛季标识
python emergency_cwl.py season 2026-10-02 --clan-tag "#部落标签"

# 单独检查官方 API；可选核验已知战争标签
python emergency_cwl.py check-official --clan-tag "#部落标签"
python emergency_cwl.py check-official --clan-tag "#部落标签" --war-tag "#战争标签"

# 单独检查 ClashKing，可指定完整赛季标识
python emergency_cwl.py check-clashking --clan-tag "#部落标签" --season-id 2026-10-02
```

`export` 只读取本地 JSON，可在无网络时运行。补采和来源检查会访问对应 API。来源检查报告写入 `results/`；不得将基本接口检查等同于整月采集完成。`--no-proxy` 也可用于 `backfill`、`season`、`check-official` 和 `check-clashking` 网络命令，例如：

```powershell
python emergency_cwl.py check-official --clan-tag "#部落标签" --no-proxy
```

官方战争记录在有效且能匹配目标部落与 `warTag` 时优先采用，包括准备期和已结束的战争。官方接口明确返回“当前未参战”时，脚本会提示并切换 ClashKing；HTTP 404、网络错误、鉴权错误或限流都不能证明目标月份没有联赛。ClashKing 月份索引完整性无法确认时，报告会注明覆盖状态未核实。

## 输出位置

所有相对路径均以 `script/` 为基准：

- 完整 JSON 归档：`data/部落名称_部落标签_YYYY_MM_DD.json`
- 排名 CSV：`results/部落名称_部落标签_YYYY_MM_DD_排名信息_排序代号.csv`
- 大本差核对 CSV：`results/部落名称_部落标签_YYYY_MM_DD_大本差详细信息_排序代号.csv`

文件名中的日期采用来源返回的完整赛季标识；同届再次运行会更新对应归档和 CSV，不同届分别保存。排名并列按用户选择的排序项计算。大本差核对表将每轮进攻和防御拆成不同列。CSV 从 JSON 归档生成，因此发现 CSV 写入问题时，可以关闭占用该 CSV 的表格软件后重新运行，也可用 `export` 离线重导。

## 在 Azure VM 上运行

真实采集在 VM 上执行时，同样需要 Python 3.13、`requirements.txt`、VM 可读取的 `script/.env` 和可访问的代理（如需代理）。门户 API Key 的 IP 白名单应允许 VM 的实际公网出口地址。Linux 示例：

```bash
cd script
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python emergency_cwl.py 202610 --clan-tag '#部落标签' --no-proxy
```

在 VM 的 `.env` 中配置 `COC_API_KEY` 和可选默认部落 `CLAN_TAG`。如果 VM 需要代理，再设置 VM 可访问的代理 `IP`，并使用实际端口；不要直接复制本机代理地址。无需代理时可省略 `IP`，或在命令中加入 `--no-proxy` 强制直连。

## 离线样例与验证

离线样例使用合成数据，不代表真实部落或来源记录。可重新生成样例并导出：

```powershell
python examples/build_synthetic_sample.py
python emergency_cwl.py export "examples\synthetic_cwl_2026_10_02.json" --sort-order "A B D C F"
```

样例 JSON 与示例 CSV 位于 `examples/` 和 `results/`。离线验收、只读 API 检查、已取得数据及尚未执行的 VM 全量采集记录见 [`doc/verification-report.md`](doc/verification-report.md)。

## 常见问题

- **`--clan-tag: expected one argument`**：在 PowerShell 中将 `#` 标签放进引号，例如 `--clan-tag "#GPQ00V9C"`。
- **官方 API 鉴权或访问被拒绝**：确认 `.env` 里的 Key 没有多余空格，Key 仍有效，并检查开发者门户的允许 IP 是否匹配请求的公网出口；使用代理时检查代理出口 IP。
- **不需要代理或代理端口不同**：不需要代理就从 `.env` 删除 `IP`；需要强制直连时加 `--no-proxy`。使用代理时按本机或 VM 实际代理端口填写 `IP`，不要照抄示例端口。
- **显示 HTTP 404、503 或网络错误**：这是来源请求失败信息，不表示目标年月没有联赛。脚本会继续按来源策略处理，并保留错误原因。
- **写 CSV 时出现 `PermissionError`**：先关闭正在 Excel、WPS 等软件中打开的目标 CSV，再重试或执行 `export` 离线重导。
- **提示缺少部落标签**：在命令行加入 `--clan-tag "#部落标签"`，或在 `script/.env` 设置 `CLAN_TAG`。
