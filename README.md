# ESP32-S3 智能灌溉：本地预测 + 云端大模型

本项目是面向 2026 年全国大学生物联网设计竞赛乐鑫科技赛道的离线优先智能灌溉系统。ESP32-S3 采集空气温湿度、气压、土壤温湿度、双路风速和双路太阳辐射；电脑端完成多传感器融合、SQLite 存储、N-BEATS/LSTM 本地预测、实时网页展示，并通过火山引擎 OpenAI 兼容网关调用真实云端大模型。云端建议必须经过本地安全审核，默认还需人工确认，最终才会通过 USB 串口控制 GPIO11 继电器。

官方赛题原文：[2026 乐鑫科技赛道 PDF](https://iot.sjtu.edu.cn/ueditor/net/upload/file/20260329/6391039317778793056390288.pdf)。

## 赛题必备项对照

| 官方必备项 | 本项目对应实现 | 现场验收证据 |
| --- | --- | --- |
| 使用 ESP32-S3/C5/P4 之一 | ESP32-S3 采集固件 | Arduino 编译/烧录日志和串口启动信息 |
| 至少 1 种传感器数据融合 | 空气、气压、土壤、双风速、双太阳辐射融合为统一 `SensorSnapshot` | dashboard 实时卡片、`@TELEMETRY` JSON、SQLite 历史 |
| 对接至少 1 个云端大模型 | 火山引擎 OpenAI 兼容网关，实际发起 HTTPS 请求 | 页面“云端已启用/已配置”、调用时间、延迟、Token 和模型回复 |
| 上行或下行交互 | 上行：趋势/预测/异常/灌溉日志交给大模型；下行：模型建议经审核和确认后控制水阀 | `llm_calls`、`decisions`、`@COMMAND`、ESP32 `@ACK` 和继电器动作 |

项目同时实现了赛题加分方向中的边缘计算、多源融合和节水应用。需要注意：仓库默认关闭真实云调用，正式验收时必须配置真实 Key、模型/推理接入点并完成一次成功调用；只有假数据测试不能证明已满足“对接云端大模型”。

## 离线主干 + 云端增强架构

```mermaid
flowchart LR
  S["多路传感器"] --> E["ESP32-S3 采集与融合"]
  E -- "USB @TELEMETRY" --> P["电脑端 FastAPI + SQLite"]
  P --> M["本地 N-BEATS / LSTM"]
  P --> D["实时 Dashboard"]
  P -- "趋势、预测、异常、日志" --> C["云端大模型"]
  C -- "结构化灌溉建议" --> G["本地安全审核"]
  G --> H["网页人工确认"]
  H -- "@COMMAND" --> E
  E -- "@ACK" --> P
  E --> R["GPIO11 继电器 + 24 V 水阀"]
```

断网时，传感器采集、SQLite、网页、本地预测和已有本地安全逻辑继续工作；云端问答和非实时分析降级为不可用，云端失败不会直接开阀。云端模型永远不直接访问 GPIO。

## 仓库结构

- `firmware/esp32_s3_all_sensors/`：ESP32-S3 传感器与安全水阀固件。
- `dual_forecast/`：数据桥接、存储、预测、云端交互和本地安全审核。
- `artifacts/`：已训练的 N-BEATS/LSTM 权重及标准化器。
- `tests/`：接口、假云网关、安全规则和串口闭环测试。
- `runtime/`：本机 SQLite、日志和 PID 文件，不提交 Git。

## 第一次安装与一键启动

要求 Python 3.10 或更高版本，推荐 Python 3.11/3.12。仓库自带模型权重，新电脑无需先训练。烧录 ESP32 后保留烧录用 USB 线，并关闭 Arduino IDE 串口监视器——同一串口不能同时被监视器和接收器占用。

### Windows PowerShell

不使用云端时直接启动：

```powershell
cd C:\Users\你的用户名\Desktop\AIoT--ModelPredition
.\start_dashboard.cmd -EspSerialPort COM3
```

首次运行会创建 `.venv`、安装依赖、启动 FastAPI 与串口接收器，并用 Edge/Chrome 应用模式打开全屏页面。若电脑只有一个 USB 串口，可以省略 `-EspSerialPort COM3` 自动识别。停止后台服务：

```powershell
.\stop_dashboard.cmd
```

启用真实火山引擎云端大模型时，在同一个 PowerShell 窗口先设置环境变量再启动：

```powershell
$env:AIOT_LLM_ENABLED="1"
$env:VEI_API_KEY="替换为真实Key"
$env:VEI_BASE_URL="https://ai-gateway.vei.volces.com/v1"
$env:VEI_MODEL="替换为控制台给出的模型名称或推理接入点ID"
$env:AIOT_LLM_INTERVAL_MINUTES="15"
$env:AIOT_DEMO_AUTO_EXECUTE="0"
.\start_dashboard.cmd -EspSerialPort COM3
```

PowerShell 里不使用 `source .venv/bin/activate`；脚本直接调用 `.venv\Scripts\python.exe`，不会误用 Anaconda 或全局 Python。

### Windows CMD

```cmd
cd C:\Users\你的用户名\Desktop\AIoT--ModelPredition
set AIOT_LLM_ENABLED=1
set VEI_API_KEY=替换为真实Key
set VEI_BASE_URL=https://ai-gateway.vei.volces.com/v1
set VEI_MODEL=替换为控制台给出的模型名称或推理接入点ID
set AIOT_DEMO_AUTO_EXECUTE=0
start_dashboard.cmd -EspSerialPort COM3
```

### macOS / Linux

macOS 一键启动：

```bash
cd /你的路径/AIoT--ModelPredition
./start_dashboard.sh --serial-port /dev/cu.wchusbserial10
```

启用真实云端模型：

```bash
export AIOT_LLM_ENABLED=1
export VEI_API_KEY='替换为真实Key'
export VEI_BASE_URL='https://ai-gateway.vei.volces.com/v1'
export VEI_MODEL='替换为控制台给出的模型名称或推理接入点ID'
export AIOT_LLM_INTERVAL_MINUTES=15
export AIOT_DEMO_AUTO_EXECUTE=0
./start_dashboard.sh --serial-port /dev/cu.wchusbserial10
```

停止：

```bash
./stop_dashboard.sh
```

`.env.example` 只作为变量清单，启动脚本不会自动读取它，避免无意间加载密钥。真实 Key 只放在本机环境变量或未提交的密钥管理系统中，不要写进源码、截图、SQLite 或 Git。

## 手动启动与健康检查

需要排错时打开两个终端。终端 1：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m dual_forecast.cli serve --host 127.0.0.1 --port 8000
```

终端 2（端口替换为实际值）：

```bash
.venv/bin/python -m dual_forecast.cli receive-esp32-serial \
  --serial-port /dev/cu.wchusbserial10
```

Windows 将上面的 Python 路径写为 `.venv\Scripts\python.exe`，串口写为 `COM3`。检查服务：

```bash
curl http://127.0.0.1:8000/health
```

页面地址：`http://127.0.0.1:8000/dashboard`。实时卡片每 2 秒刷新；预测历史默认每 5 分钟入库一次。页面右上角不超过 5 秒且为绿色代表串口实时链路正常，超过 20 秒变红代表中断。

Windows 查看接收日志：

```powershell
Get-Content .\runtime\logs\esp32-receiver.out.log -Tail 40 -Wait
```

`-Wait` 会持续等待新日志，看似“卡住”是正常现象，按 Ctrl+C 退出。macOS 对应：

```bash
tail -f runtime/logs/esp32-receiver.out.log
```

## 云端分析、问答和安全灌溉

环境变量配置正确并重新启动服务后，dashboard 应显示云端“已启用、已配置”。

1. 点击“生成云端分析建议”，电脑将当前融合数据、最近 1/24 小时趋势、本地预测、异常和近 7 天灌溉统计发给大模型。
2. 在问答框输入“今天需要调整灌溉计划吗？”或“过去一周用了多少水？”，页面同时显示回答所依据的数据范围。
3. 若模型建议 `START_WATERING`，本地会先验证数据新鲜/完整、土壤阈值、置信度、过期时间、15 分钟冷却、单次 60 秒和每日 600 秒限制。
4. 默认状态是 `awaiting_confirmation`。点击“人工确认并下发”后，命令才进入 SQLite 队列。
5. 串口接收器发送 `@COMMAND`，ESP32 验证 Schema、TTL、动作白名单、持续时间和最新传感器状态，再把 GPIO11 拉高。
6. ESP32 返回 `@ACK`，网页显示 `OPEN`；达到持续时间后自动拉低 GPIO11并返回关闭 ACK。

`AIOT_DEMO_AUTO_EXECUTE=1` 只用于受控演示：它跳过网页确认点击，但不会绕过任何本地安全规则。正常使用务必保持为 `0`。

### 串口协议示例

设备上行：

```text
@TELEMETRY {"uptime_ms":123456,"wind":{...},"air":{...},"soil":{...},"solar":{...}}
```

电脑下行：

```text
@COMMAND {"schemaVersion":"1.0","requestId":"...","action":"START_WATERING","durationSeconds":30,"reasonCode":"SOIL_DRY","reason":"human-confirmed: ...","confidence":0.91,"expiresAt":"...","ttlSeconds":30}
```

设备确认：

```text
@ACK {"requestId":"...","accepted":true,"actualState":"OPEN","reason":"started","remainingSeconds":30}
```

电脑端验证绝对时间 `expiresAt`，并且只发送未过期命令；ESP32 不依赖联网校时，使用 1–30 秒的传输 TTL、独立的最长 60 秒关阀计时器和 8 秒主机心跳超时保护。重复 `requestId` 不会重复执行。

## 全部硬件接线

| 功能 | ESP32-S3 引脚 | 传感器/模块侧 | 参数 |
| --- | --- | --- | --- |
| 风速 1 | GPIO9 ADC | 模拟信号 OUT | 0–5 V 输出必须先分压到 0–3.3 V |
| 风速 2 | GPIO6 ADC | 模拟信号 OUT | 同上 |
| AHT20 | GPIO5 / GPIO8 | SDA / SCL | 3.3 V，地址 0x38 |
| HW-611 BMP280 | GPIO3 / GPIO4 | SDA / SCL | 3.3 V；CSB→3.3 V；SDO→GND 时地址 0x76 |
| 土壤 RS485 转换器 | GPIO18 / GPIO17 | RO / DI | 4800 8N1，土壤地址 0x03，独立总线 |
| 太阳 RS485 转换器 | GPIO16 / GPIO15 | RO / DI | 4800 8N1，两个探头地址 0x01/0x02 并联在此总线 |
| 继电器控制 | GPIO11 | IN | 3.3 V 一路继电器，高电平有效 |
| Feather I²C 电源控制 | GPIO7 | 不外接 | 固件自动拉高，禁止复用 |

AHT20：VCC→3.3 V，GND→GND，SDA→GPIO5，SCL→GPIO8。BMP280：VCC→3.3 V、GND→GND、SDA→GPIO3、SCL→GPIO4、CSB→3.3 V、SDO→GND；若 SDO 拉高则通常是 0x77，固件也会回退扫描。

土壤探头虽然只暴露 VCC、GND、A、B，但 A/B 是 RS485 差分信号，仍需 3.3 V 逻辑兼容的自动收发 RS485 转换器，不能把 A/B 作为普通 UART 长期直连 ESP32。探头 A/B→转换器 A/B，转换器 RO→GPIO18、DI→GPIO17。太阳辐射使用另一块转换器：两个探头 A 对 A、B 对 B 并联，转换器 RO→GPIO16、DI→GPIO15。A/B 无响应时先核对说明书定义，必要时只在断电后互换 A/B。传感器电源按铭牌独立供电，不能因接口名字相同就接 ESP32 3.3 V。

继电器低压控制侧：模块 DC+/VCC→符合模块要求的 3.3 V 电源，DC-/GND→ESP32 GND，IN→GPIO11。24 V 常闭水阀的触点侧：24 V 电源正极→COM，NO→水阀正极，水阀负极→24 V 电源负极。这样继电器未吸合时水阀无电并保持关闭。COM/NO/NC 是隔离触点，可以切换 24 V，但必须确认触点额定直流电压/电流高于水阀负载；直流水阀线圈建议并联合适的续流二极管或抑制器。24 V 不得接入 ESP32 GPIO 或继电器 IN。

更详细固件说明见 `firmware/esp32_s3_all_sensors/README.md`。当前串口屏方案已停用，GPIO11 专用于继电器。

## 评委现场演示流程

1. 烧录固件并展示芯片识别为 ESP32-S3、GPIO11 上电 LOW。
2. 关闭 Arduino 串口监视器，一键启动 dashboard 和 USB receiver。
3. 展示多路实时传感器和统一 `@TELEMETRY`，拔掉一个传感器展示异常记录，再恢复。
4. 展示 SQLite 历史、本地 N-BEATS/LSTM 状态；未满 288 个五分钟样本时可解释为约 24 小时预热，也可使用已有连续历史演示完整预测。
5. 设置真实火山引擎环境变量，重启后展示页面“已启用、已配置”。
6. 点击云端分析并进行自然语言问答，展示真实调用时间、延迟、Token、回复和数据范围。
7. 让模型产生灌溉建议，展示其先停留在人工确认状态；确认后观察 `@COMMAND`、继电器动作和 `@ACK`。
8. 不等待也可看到最长 60 秒后自动关阀；拔掉 USB/停止心跳时，8 秒内安全关阀。
9. 断开互联网，再展示本地采集、网页、SQLite 和预测不受影响，云端请求明确降级为 `NO_OP`。

建议正式演示前先不接 24 V 水阀，只观察继电器指示灯和万用表通断，确认全部保护逻辑后再接负载。

## 数据和本地预测说明

`AirPressure` 以 hPa 接收，内部转为 kPa。积累不足 288 个五分钟点时返回 `warming_up`；存在超过 15 分钟且无法插值的缺口时返回 `insufficient_data`。不完整数据仍用于实时安全判断和异常记录，但不会污染预测历史；下一条完整数据可继续入库。

完整预测接口为 `GET /v1/forecast/latest`，服务重启后从 `runtime/forecast.sqlite3` 恢复历史。若 BMP280 暂时上报 0，接收器默认临时使用 1013 hPa 作为预测回退值；安全审核仍以实时有效性为准。

模型训练为可选操作：

```bash
.venv/bin/python -m dual_forecast.cli preprocess '虹桥2018-2024逐小时气象数据.zip'
.venv/bin/python -m dual_forecast.cli train-all '虹桥2018-2024逐小时气象数据.zip' --epochs 35
```

虹桥数据不含真实土壤湿度，初始 LSTM 使用桶式水量平衡和虚拟灌溉事件生成代理序列，不能把该指标解释为真实土壤预测精度。积累至少 14 天有效土壤观测后可运行 `retrain-observed --epochs 35`，再调用 `POST /v1/models/reload` 热更新模型。

## 测试与固件编译

```bash
.venv/bin/python -m pytest -q
arduino-cli compile \
  --fqbn esp32:esp32:adafruit_feather_esp32s3_nopsram \
  --build-path /tmp/aiot-esp32-build \
  firmware/esp32_s3_all_sensors
```

仓库未提交原始气象 ZIP，因此测试中依赖该 ZIP 的两项数据处理测试会跳过，其余云网关、安全规则、API、串口和预测流程仍会运行。假网关测试不会访问互联网或消耗 Token，也不能替代正式验收中的真实云调用。
