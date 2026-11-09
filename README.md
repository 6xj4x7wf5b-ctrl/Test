# 利仓上位机

Windows 11 离线串口上位机，使用 Python 3.11、PySide6、PyQtGraph 和 SQLite；当前在 macOS 开发、运行模拟测试。**尚未生成 Windows EXE，也未完成实机验收。** 运行时不依赖原网页服务。

首版提供仓房/分机管理、多串口采集、探头登记、温湿度微调、超限声音报警、本地历史与备份。登录、园区管理、通风控制、注册码及授权激活未纳入首版。

## macOS 启动

已建立项目独立 Conda 环境：

```text
/Users/ly/Projects/WarehouseTemperatureSystem/.conda/warehouse-pc
```

可双击本目录的脚本：

- `run_macos.command`：正常模式，首次为空档案，连接现场串口。
- `demo_macos.command`：独立模拟模式，自动建立演示仓房和两路模拟分机；显示“模拟演示”标记。

也可在终端运行：

```bash
conda activate /Users/ly/Projects/WarehouseTemperatureSystem/.conda/warehouse-pc
cd /Users/ly/Projects/WarehouseTemperatureSystem/desktop
python -m warehouse_pc
python -m warehouse_pc --demo
```

自定义本地数据目录：

```bash
python -m warehouse_pc --data-dir /path/to/warehouse-data
python -m warehouse_pc --demo --data-dir /path/to/warehouse-data
```

模拟模式会在所选目录下追加 `demo/`，与正常数据库隔离。默认目录由系统决定，可在“系统维护 → 本地维护”查看并打开。同一数据目录只允许一个程序实例运行。

## Windows 11 环境与打包

### 没有 Windows 电脑：GitHub Actions 云端打包

本仓库已提供 `.github/workflows/windows-build.yml`，在 GitHub 的 Windows x64 环境构建，Mac 只负责上传源码和下载结果。云端构建需要可用的 GitHub Actions 额度。

1. 将 **`desktop/` 目录的内容作为仓库根目录**提交到自己的 GitHub 私有仓库；根目录应直接看到 `pyproject.toml`、`tools/` 和 `.github/`，不要再套一层 `desktop/`。无需上传分机固件、数据库或 `.conda/`。
2. 打开仓库的 **Actions → Windows Build → Run workflow**。工作流文件需要先存在于默认分支；该流程仅手动触发。
3. 构建成功后，从本次运行的 Artifacts 下载 `windows-x64-release`，解开外层下载包，得到版本 ZIP 和 SHA256 文件。
4. 将版本 ZIP 完整解压，运行其中的 `WarehouseTemperatureConsole.exe`；`Demo.cmd` 为模拟演示入口。

云端依次执行：创建独立 Python 环境 → 安装依赖 → 自动化测试 → PyInstaller 打包 → 检查 Windows x64 EXE 格式 → 启动正常/演示模式并检查截图和数据库 → 生成 ZIP 与校验值。日志、测试报告和启动截图可从 `windows-build-evidence` 下载。POSIX 伪终端测试在 Windows 下跳过，仍需用实际串口验收。

**工作流已配置不代表 EXE 已生成。当前尚未执行 Windows 云端构建，尚无验证通过的 Windows 发布包。** 云端结果以该次 Actions 运行和包内的 `build-info.json` 为准；项目根目录 `output/desktop/build-info.json` 是旧的 macOS 开发记录。

### 有 Windows 环境：本地打包

安装 Windows **x64 Python 3.11（或 3.12）**，在 PowerShell 中进入本目录：

```powershell
.\build_windows.ps1
# 也可直接运行，不需要 Conda：
python tools/build_windows.py
```

脚本会重建本目录下专用的 `.venv-windows/` 构建环境，执行与云端相同的测试和打包流程。构建需要联网安装依赖；客户电脑运行成品不需要联网或安装 Python。

成功输出：

- `dist/WarehouseTemperatureConsole/WarehouseTemperatureConsole.exe` 及整个同名运行目录。
- `dist/WarehouseTemperatureConsole-0.1.0-windows-x64.zip` 和对应 `.zip.sha256` 校验文件。

交付时需保留整个目录，包括 `_internal/`；这是免安装便携包，不是安装向导。目标为 Windows 11 Intel/AMD x64，串口驱动、声音、缩放和实际设备通信仍需在目标电脑验证。macOS 原生 Python 不能用此流程生成 Windows EXE，脚本会直接提示改用 Windows 或云端构建。

如需在 Windows 以源码运行，也可继续使用 Conda：

```powershell
conda env create -f environment.yml
conda activate warehouse-pc
python -m warehouse_pc
# 需要演示时：python -m warehouse_pc --demo
```

## 首次使用流程

1. **添加仓房**：展开“仓房管理 → 仓房信息管理”，点击“添加仓房”。
2. **添加串口**：进入“设备管理 → 串口连接”，填写连接名称、COM 口、波特率、校验位和停止位。
3. **添加分机**：在“测温分机管理”选择所属仓房和串口，设置地址、采集协议、粮温及仓温湿查询点数。
4. **连接与登记**：连接对应串口，在分机列表选择目标分机后点击“探头登记”。扫描完成后指定用途和编号，核对暂存表，点击“保存并核对”才写入分机。
5. **开始采集**：点击顶栏“开始采集”。首页显示概况，“数据管理 → 粮情总览/粮情详情”查看粮仓立体图、测点列表、平铺图和曲线；“调整布点”可填写线、层和安装位置。

查询点数是轮询范围，不能代替实际探头登记。扫描顺序无法确定物理位置，安装时需核对探头标签。登记期间暂停该串口的普通轮询，其他串口可继续采集；同一路串口由程序统一排队通信。

不同串口可使用相同分机地址，数据以串口连接和分机地址区分。正常模式与模拟模式的连接、档案和数据库分别使用，模拟数值不代表现场读数。

## 粮仓立体视图

粮情详情默认显示与网页相同形式的圆柱分层示意图，可切换层位和温度/点号/线号显示，点击测点查看数值、状态和布点信息。绿色为有效测点，红色为活动超限报警，灰色表示断线、过期、故障或尚无采样；仓温仓湿独立显示在上方环境数据中。

在“调整布点”登记测温线和层；也可填写实际每线层数，按“第1线各层 → 第2线各层”的点号顺序填充，再核对并保存。此配置仅改变本地视图，不改写分机编号或探头绑定。

尚未登记或线/层重复的点放入“未布点示意”，每组最多 24 点，不推测实际层数。已登记布点按层展示，同一测温线在各层保留相同示意位置；超过 24 条线时按线组切换。层序、圆环和线距均为示意，不能代替真实埋深或测量坐标。原有测点列表、按点号排列的测点平铺图和粮温曲线仍可切换查看。

## 协议与设备边界

- 当前协议针对本次 **GXHT3W 单总线固件**，支持 LC-M3 查询、Modbus RTU 查询和 `0x41` 私有登记命令。上位机选择采集协议；分机按每条请求识别并回复。
- 即使采集选择 LC-M3，登记仍在同一串口参数下使用 `0x41` 维护命令。旧固件不具备此登记功能。
- 当前固件台架参数为 **9600、8N1**；旧版本原为 1200、8N1。串口设置必须与实际固件一致，不能仅因都使用 LC-M3 就认为旧分机无需调整。
- 当前界面分机地址范围为 1～247，粮温最多 165 点、仓温仓湿最多 5 点。Modbus 粮温分批读取，每次不超过 125 个寄存器。
- 无效值和 `FF FF` 作为故障处理，不作为正常零值。时间显示为电脑接收时间；通信在线不等于每个传感器正常。

详细报文及未定事项见[分机协议草案](../board/LC-M3分机程序165点26.03.12/docs/protocol-draft.md)。旧板、旧适配器、现场线缆和甲方旧 Modbus 资料均需实机核对，当前不承诺全部兼容。主板引脚、供电和传感器手册版本仍按硬件联调资料确认。

## 微调、采集与报警

- 默认每 **5 秒**轮询、每 **30 秒**保存一轮历史，可在“采集规则 → 测温分机采集规则”调整。实际频率受串口速度、点数和设备响应影响。
- “系统维护 → 温湿度微调”按分机和测点设置加减偏移；温度单位为 ℃，湿度偏移为 `%RH` 百分点。**仅在电脑端应用一次**，不会写入探头或分机标定区。
- 显示、历史和超限判断共用修正后的有效数值。历史保留原值、当时修正结果及配置版本，修改微调不会重新计算旧历史。
- 在“报警记录”页设置温度/湿度上下限；开启声音后，有活动且未确认的报警时每 **10 秒**调用系统提示音。确认或全部消音可停止相应提示，确认不代表测量值已恢复正常；电脑声音设置会影响实际可听效果。

## 历史与备份

历史永久保留，不自动过期删除。“历史记录”可按分机、类型、点号和时间分页查询，点击一行查看对应单测点曲线。曲线最多显示所选范围内最近 5000 条并标明限制；CSV/Excel 导出覆盖**完整筛选范围**，不只当前页。记录逐条保留模拟/现场来源。

“本地维护”提供三类操作：

| 操作 | 内容与影响 |
|---|---|
| 完整数据库备份/恢复 | 一致性快照，包含档案、历史、报警和设置。恢复会覆盖当前数据库，执行前自动留存安全副本；完成后采集保持暂停 |
| JSON 配置导出/导入 | 转移仓房、串口、分机、绑定、微调、阈值和设置，不含采集历史及报警记录；导入按端点合并，已有历史保留 |
| 手动历史清理 | 按分机和时间预统计，确认后删除该范围采集记录；保留档案、微调、绑定和报警记录 |

电脑数据库与分机 Flash 是两份配置：恢复电脑数据库或导入电脑 JSON 不会自动改写分机。探头登记窗口另有活动表 JSON 备份/恢复，恢复到暂存表后仍需“保存并核对”才能生效。

退出程序时会等待本地任务收尾；不要将仍在执行的导出、备份或保存误认为已经完成。

历史清理删除记录后，SQLite 空闲页可供之后采集复用；数据库文件大小不一定立即缩小。

## 界面来源与测试

桌面界面参照原网页的主蓝色 `#2f6fdf`、分组导航和页面结构。根据工控界面反馈，现采用深色外框、全宽深蓝标题栏和浅灰白工作区，控件与面板为 1～2 px 小圆角，去掉装饰性渐变，保留紧凑尺寸。样式实现位于 [warehouse_pc/style.py](warehouse_pc/style.py)。参照文件：

- [网页颜色变量](../../2026.3.13/vue/src/styles/tokens.css)、[网页主题](../../2026.3.13/vue/src/styles/theme.css)
- [网页侧栏](../../2026.3.13/vue/src/components/shell/AppSidebar.vue)、[页面框架](../../2026.3.13/vue/src/layouts/AppShellLayout.vue)
- [粮情总览](../../2026.3.13/vue/src/views/grain-overview/GrainOverviewView.vue)、[仓房管理](../../2026.3.13/vue/src/views/warehouse/WarehouseDesktopView.vue)
- [网页粮仓立体示意](../../2026.3.13/vue/src/components/grain/GrainSiloProfile.vue)

这些文件作为视觉与布局参照；本次桌面界面实现位于 `desktop/`，运行上位机无需修改或启动原网页。

调整前的紧凑版保存在本地 Git 标签 `desktop-before-industrial-ui-20261016`（提交 `2a68a58`），可用于比较或恢复。

字体层级为主标题 20 px、页面标题 16 px、卡片标题 14 px、正文 12 px；顶栏状态与操作控件统一为 28 px 高。下拉/增减箭头资源随程序一起打包。测点平铺图保留固定测点格子，按当前可见宽度自动换列。

在本目录、独立环境中运行测试：

```bash
conda activate /Users/ly/Projects/WarehouseTemperatureSystem/.conda/warehouse-pc
python -m pytest -q
```

测试包括协议编解码、多串口模拟、登记、数据库、历史导出和异步边界。模拟通过不等于 Windows、传感器实物、断电保存或现场通信已经验收；最终结果以硬件和目标电脑联调记录为准。
