# AIoT 智能灌溉项目交接文档

> 项目目录：`/Users/jiahaoruan/Work/IOT/AIoT--ModelPredition`  
> 最近整理：2026-07-23  
> 交接目标：让下一位开发者能在不依赖聊天记录的前提下，继续完成、验证和提交本项目。

## 1. 当前项目做什么

这是一个 ESP32-S3 智能灌溉系统：ESP32-S3 采集环境数据，通过 Wi-Fi 或 USB 串口发送到电脑；电脑端保存数据、运行本地预测、提供实时 Dashboard、调用可选的火山引擎云端大模型，并在本地安全规则审核后控制 ESP32 GPIO11 上的继电器水阀。

系统采用“离线主干 + 云端增强”设计：局域网断开互联网时，采集、电脑网页、SQLite、本地预测、边缘风险分析、人工确认开关阀仍可使用；云端大模型分析和云端 AI 自动灌溉不可用，且不会因云端失败而开阀。

## 2. 重要目录

| 位置 | 作用 |
| --- | --- |
| `firmware/esp32_s3_all_sensors/esp32_s3_all_sensors.ino` | ESP32-S3 主固件：采集、Wi-Fi 配网/TCP、USB 协议、继电器、设备侧安全保护 |
| `firmware/esp32_s3_all_sensors/README.md` | 最准确的固件接线、烧录、Wi-Fi 和协议说明 |
| `dual_forecast/esp32_receiver.py` | Wi-Fi TCP / USB 串口接收器，接收 `@TELEMETRY`、转发命令并等待 ACK |
| `dual_forecast/service.py` | FastAPI 接口和内嵌 Dashboard 前端 |
| `dual_forecast/irrigation.py` | 云端建议、本地边缘风险、本地安全审核、命令入队 |
| `dual_forecast/cloud.py` | 火山引擎 OpenAI 兼容网关适配与结构化回答解析 |
| `dual_forecast/config.py` | `.env` 加载和运行配置 |
| `dual_forecast/storage.py` | SQLite 历史、预测、事件、决策、指令和 ACK |
| `start_dashboard.sh` / `scripts/start_dashboard.ps1` | macOS/Linux、Windows 一键启动 |
| `README.md` | 面向使用者的总说明、比赛对照、运行流程 |
| `tests/` | API、云端模拟、边缘风险和水阀安全逻辑测试 |

## 3. 已完成的功能

### ESP32-S3 与传感器

- 多传感器统一 `SensorSnapshot`，上报 `@TELEMETRY` JSON。
- AHT20 空气温湿度，BMP280/BME280 气压，双路风速，土壤 RS485，双路太阳辐射 RS485；Solar 1 为反射短波 Rs↑，Solar 2 为入射短波 Rs↓，ET₀ 使用净短波 Rns。
- AHT20 / DHT11 可选，当前推荐 AHT20；DHT11 是备用方案。
- 首次或执行 `@WIFI_RESET` 后开启配网热点；保存凭据到 ESP32 NVS，后续重启会自动重连。
- ESP32 成功入网后开启 TCP 3333，并每 3 秒 UDP 3334 广播发现消息；电脑用自动发现，无需固定 IP。
- USB 串口调试保留，波特率 115200。
- 设备侧包含命令 TTL、重复 requestId 去重、最长 60 秒开阀、8 秒无主机心跳提前关阀、上电默认关阀。
- ESP32 还会生成轻量、可解释的断网降级预测 `edge_prediction`；它只提示风险，不能直接开阀。

### 电脑端本地能力

- FastAPI Dashboard：实时传感器、历史、预测、边缘风险、事件、报告、云端分析和水阀状态。
- SQLite 保存实时样本、预测、环境事件、云端调用、灌溉决策、命令和设备 ACK。
- 本地 N-BEATS / LSTM 预测，并有 288 样本预热逻辑。
- 多源数据融合、数据新鲜度校验、传感器失败事件、干旱候选和高蒸散风险判断。
- 本地动态采样建议与独立 `@CONFIG`/`@CONFIG_ACK` 协议；不能误触发水阀命令。
- 节水、日报、事件时间线及本地风险说明。
- macOS/Linux 与 Windows 的一键启动；支持 USB 模式和 Wi-Fi 自动发现模式。

### 云端大模型与控制闭环

- 对接火山引擎 VEI 的 OpenAI 兼容 `/v1/chat/completions` 接口。
- 上行上下文包含当前有效数据、趋势、本地预测、异常、灌溉日志和可选农田资料；未配置天气数据时显式提示模型不得臆造天气或降雨。
- 要求模型返回结构化 `START_WATERING`、`STOP_WATERING` 或 `NO_OP` 建议，再由电脑端本地安全层复核。
- 默认需要网页长按确认，才会进入 ESP32 命令队列；网页、云端模型均不直接操作 GPIO。
- 已增加“显式开启的 AI 自动灌溉”安全闭环，默认关闭。自动开阀仍必须同时满足：完整且新鲜的 ESP32 数据、`IRRIGATION_CANDIDATE` 风险、本地预测就绪、AI 置信度阈值、土壤阈值、冷却时间、单次时长和每日上限。
- 云端不可达、模型输出不合法、数据缺失/陈旧、预测未就绪等任何一种情况都会保持 `NO_OP` 或保留建议，不会开阀。

## 4. 当前固件接线（交给硬件组）

| 功能 | ESP32-S3 引脚 | 对外模块接法 / 注意事项 |
| --- | --- | --- |
| 风速（当前启用） | GPIO6 | 接传感器 OUT；0–5 V 输出必须先分压至 0–3.3 V |
| GPIO9 | 不接 | 预留给未来第二路风速；当前固件不采样 |
| AHT20 | GPIO5 / GPIO8 | SDA / SCL；VCC→3.3 V，GND 共地，地址 0x38 |
| DHT11 备用 | GPIO10 | OUT；当前固件默认不使用 |
| BMP280/BME280（HW-611） | GPIO3 / GPIO4 | SDA / SCL；VCC→3.3 V，GND；CSB→3.3 V，SDO→GND 使用地址 0x76 |
| 土壤传感器 | GPIO18 / GPIO17 | 经独立、3.3 V UART 兼容的自动收发 RS485 转换器：转换器 RO→GPIO18，DI→GPIO17；地址 0x03，4800 8N1 |
| Solar 1（反射 Rs↑）+ Solar 2（入射 Rs↓） | GPIO16 / GPIO15 | 经另一独立 RS485 转换器：RO→GPIO16，DI→GPIO15；两个探头 A/B 可并联，地址依次为 0x01、0x02，4800 8N1；计算净短波 `max(Solar2 - Solar1, 0)` |
| 水阀继电器输入 | GPIO11 | 3.3 V 一路继电器，当前配置为高电平有效；模块 GND 必须和 ESP32 共地 |
| 板载 I2C 电源控制 | GPIO7 | 程序使用，禁止外接或复用 |

24 V 常闭水阀的负载侧：`24V+ → COM`，`NO → 水阀正极`，`水阀负极 → 24V-`。继电器低压控制侧和 24 V 负载侧电气隔离，但触点额定直流电压/电流必须覆盖水阀负载。24 V 绝不能接入 ESP32 GPIO。

## 5. 日常运行流程

### ESP32 一次性配网

1. 烧录主固件并打开 USB 串口监视器（115200）。
2. 首次烧录、没有已保存网络、或串口发送 `@WIFI_RESET` 时，手机连接临时配网热点。
3. 打开固件串口输出中提示的配网页面，输入当前 2.4 GHz Wi-Fi/手机热点名称和密码。
4. 看到 `Connected. SSID=... IP=...` 后，凭据已经存入 NVS；普通 reset 和断电重启不需要再次配置。

### macOS/Linux Dashboard

在项目根目录运行：

```bash
./start_dashboard.sh --wifi --lan
```

`--wifi` 使用 UDP 3334 自动发现 ESP32，再连 TCP 3333；`--lan` 让手机可以访问电脑网页。电脑浏览器访问 `http://127.0.0.1:8000/dashboard`。手机和电脑在同一局域网时，访问页面显示的电脑局域网地址或二维码。

### Windows Dashboard

在 PowerShell 中进入项目根目录运行：

```powershell
.\start_dashboard.cmd --wifi --lan
```

首次运行会创建 `.venv` 并安装依赖。不要在 PowerShell 中使用 macOS/Linux 的 `source .venv/bin/activate`；如果需要手动激活，命令是：

```powershell
.\.venv\Scripts\Activate.ps1
```

### USB 降级模式

Wi-Fi 不可用时，保留 USB 数据线，关闭 Arduino Serial Monitor 后运行：

```bash
./start_dashboard.sh --serial-port /dev/cu.wchusbserial10 --lan
```

Windows 则替换为实际 COM 口，例如：

```powershell
.\start_dashboard.cmd --serial-port COM5 --lan
```

## 6. 云端配置与当前网络状态解释

真实 Key 只应放入项目根目录、已被 Git 忽略的 `.env`，不得写进 README、源码、测试、提交记录或交接文档。参考 `.env.example`：

```dotenv
AIOT_LLM_ENABLED=1
VEI_API_KEY=你的火山引擎密钥
VEI_BASE_URL=https://ai-gateway.vei.volces.com/v1
VEI_MODEL=doubao-1.5-thinking-pro
AIOT_AUTO_IRRIGATION_ENABLED=0
```

启动脚本会在 Key 缺失/被拒绝时提示输入并保存到 `.env`。当前实现中：

- “云端：已启用”表示本机已配置并打开云端功能开关，不等价于此刻互联网可达。
- 启动输出 `Cloud gateway is temporarily unreachable`、`URLError` 或页面 `gateway_error` 表示当前无法访问火山引擎。
- 无互联网但 ESP32、电脑、手机位于同一局域网时，Wi-Fi telemetry 和 Dashboard 仍然正常，这是设计目标。
- 云端不可达时不能获得新的云端 AI 判断，也不能走云端 AI 自动开阀；本地人工确认与设备侧安全保护仍可工作。

## 7. 当前 Git 状态（必须注意）

仓库最近已提交的基础提交包括：

```text
2aebac9 忽略本地编译产物与临时固件
cedf795 完善 Wi-Fi 自动接收与跨平台一键启动
a865cc5 统一 Wi-Fi 配网热点密码
568a9ce 增加 ESP32 手机 Wi-Fi 配网
e838098 完善边缘事件与移动巡检能力
1953788 实现云端大模型增强与安全灌溉闭环
```

但当前工作区有一批**尚未提交**的增强：`.env` 自动加载、启动时云端 Key 引导、农田资料注入、云端响应校验、显式 AI 自动灌溉安全门、Dashboard 云端/水阀信息优化，以及对应测试。涉及的修改文件为：

```text
.env.example
README.md
dual_forecast/cli.py
dual_forecast/cloud.py
dual_forecast/config.py
dual_forecast/irrigation.py
dual_forecast/schemas.py
dual_forecast/service.py
scripts/start_dashboard.ps1
scripts/start_dashboard.sh
tests/test_cloud.py
tests/test_edge_and_reports.py
tests/test_irrigation.py
tests/test_service.py
```

已有验证结果：

```text
.venv/bin/python -m pytest -q
58 passed, 2 skipped

git diff --check
通过
```

接手者在理解并复核后，应将这批修改作为单独提交提交；不要把 `.env`、`runtime/`、虚拟环境、SQLite、日志、Arduino 缓存或真实 API Key 加入 Git。

## 8. 推荐的下一步工作

### P0：完成当前未提交改动的验收与提交

- 运行完整测试与 `git diff --check`，核对 Dashboard 及 API 没有破坏已有 USB/Wi-Fi 链路。
- 用真实联网环境执行一次 `cloud-check` 和一次真实云端分析，记录成功响应、模型、延迟和结构化建议，作为比赛验收证据。
- 提交当前改动，提交信息建议：`完善云端大模型配置与自动灌溉安全闭环`。

### P1：修正 Dashboard 云端状态文案

当前页面“云端：已启用”容易被理解为“已经联网”。建议在 `/v1/cloud/status` 和 Dashboard 中拆分为：

- `未配置`：没有 Key 或 `AIOT_LLM_ENABLED=0`。
- `已配置，当前不可达`：保存了 Key，但最近检查/调用是 `URLError`、超时或 HTTP 失败；显示“离线主干运行”。
- `已连接`：最近一次真实调用成功；显示最近成功时间、模型、耗时。

状态检测应使用最近调用结果或短时间缓存，不要让 Dashboard 每次轮询都发真实云端请求。

### P1：现场端到端验收

- ESP32 在真实 2.4 GHz 热点下重新上电，确认自动入网、UDP 发现、TCP telemetry、Dashboard “实时数据新鲜”。
- 断互联网但保留局域网，确认本地 Dashboard、预测、SQLite 和人工开关阀仍可用；确认云端分析安全地显示不可达而不开阀。
- 恢复互联网，确认云端分析有真实成功记录；不要把 API Key 或含 Key 的日志截图公开。
- 先断开 24 V 水阀，仅观察继电器 LED、`@COMMAND`、`@ACK`、网页状态；再接真实水阀验证 NO/COM 线路。
- 校准风速 ADC 分压和换算系数；当前 0.0 风速、0.0 土壤湿度、0 W/m² 可能是实际传感器/接线值，不应在软件中伪造有效数据。

### P2：可选功能扩展（需明确授权后再做）

- 如需在无互联网时也自动灌溉，应单独实现并默认关闭 `AIOT_OFFLINE_IRRIGATION_ENABLED`：只能由本地预测 + 阈值规则 + 相同安全门触发，绝不能把它伪装成云端 AI 决策。
- 如要增加天气预报、作物知识或地区信息，必须接入可信数据源，并在云端提示中标注其采集时间与缺失状态。
- 如要再加显示屏，重新选定并验证模块协议/供电/电平转换后再做；此前 M 系列串口屏试验已停止，当前 GPIO11 专用于水阀，不应复用。
- 如要加入语音/手机交互，仅能作为“请求分析/人工确认”的输入方式，不能绕过后端安全审核直接控制继电器。

## 9. 常用排查命令

```bash
# 查看当前改动与最近提交
git status --short
git log --oneline -8

# Python 测试
.venv/bin/python -m pytest -q

# macOS/Linux 观察接收器日志；持续等待新输出是正常现象，Ctrl+C 退出
tail -f runtime/logs/esp32-receiver.out.log

# 健康检查与最新 Dashboard 数据
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/dashboard/latest
curl http://127.0.0.1:8000/v1/cloud/status
```

Windows PowerShell：

```powershell
Get-Content .\runtime\logs\esp32-receiver.out.log -Tail 40 -Wait
Invoke-WebRequest http://127.0.0.1:8000/health
```

## 10. 安全与演示底线

- 任何 API Key、Wi-Fi 密码、数据库和运行日志都不提交 Git。
- 云端模型只能建议，不能直接开阀；电脑本地安全层和 ESP32 双重校验不可绕过。
- 自动灌溉必须显式打开，默认关闭；首次验收先断开真实水阀负载。
- 任何云端错误、传感器缺失、数据陈旧、预测预热不足或通信 ACK 超时，都应进入保守状态而不是继续开阀。
- 比赛演示应准备联网、仅局域网、USB 三种链路的演示说明，清楚表述各自能做和不能做的事情。
