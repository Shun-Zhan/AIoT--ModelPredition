# ESP32-S3 全传感器与安全水阀固件

用 Arduino IDE 打开 `esp32_s3_all_sensors.ino`，板型选择 `Adafruit Feather ESP32-S3 No PSRAM`。固件只依赖 ESP32 Arduino Core 自带组件，无需额外安装 Arduino 库。正常运行通过 Wi‑Fi 把传感器数据、心跳和经本机审核的水阀命令传给电脑；USB 保留烧录、查看日志和故障排查用途。

## 引脚总表

| 功能 | ESP32-S3 | 外设侧 | 说明 |
| --- | --- | --- | --- |
| 风速（当前启用） | GPIO6 | OUT | ADC；0–5 V 必须分压到 0–3.3 V |
| GPIO9 | 不接 | 保留给未来第二路风速；当前固件不采样 |
| AHT20 | GPIO5 / GPIO8 | SDA / SCL | 当前启用，3.3 V，地址 0x38 |
| DHT11 备用 | GPIO10 | OUT | 当前停用 |
| BMP280/BME280 | GPIO3 / GPIO4 | SDA / SCL | 3.3 V；CSB→3.3 V；SDO→GND（0x76） |
| 土壤 RS485 转换器 | GPIO18 / GPIO17 | RO / DI | 地址 0x03，4800 8N1，独立总线 |
| 太阳 RS485 转换器 | GPIO16 / GPIO15 | RO / DI | 地址 0x01/0x02，4800 8N1，共用太阳总线 |
| 水阀继电器 | GPIO11 | IN | 一路 3.3 V，高电平有效，上电默认 LOW |
| 板载 I²C 电源控制 | GPIO7 | 不外接 | 程序自动拉高，禁止复用 |

土壤探头 VCC/GND/A/B 中的 A/B 必须进入 RS485 转换器，转换器 RO→GPIO18、DI→GPIO17。两个太阳探头 A/B 可在太阳总线上并联，因为地址分别为 0x01 和 0x02；它们不能与土壤总线混用。转换器应为 3.3 V UART 逻辑兼容、自动收发方向型。

AHT20 与 BMP280 使用两组独立 I²C。AHT20：VCC→3.3 V、GND→GND、SDA→GPIO5、SCL→GPIO8。BMP280：VCC→3.3 V、GND→GND、SDA→GPIO3、SCL→GPIO4、CSB→3.3 V、SDO→GND。

## 继电器与 24 V 常闭水阀

低压控制侧：继电器 DC+/VCC→模块要求的 3.3 V，DC-/GND→ESP32 GND，IN→GPIO11。高压负载侧：24 V 正极→COM，NO→水阀正极，水阀负极→24 V 负极。COM/NO/NC 与继电器线圈电源相互独立，但触点额定直流电压和电流必须高于水阀负载。24 V 不得进入 ESP32 GPIO。

固件安全行为：

- `setup()` 初始化其他总线前先将 GPIO11 置 LOW。
- 只接受 `START_WATERING`、`STOP_WATERING`、`NO_OP`。
- START 持续时间只允许 1–60 秒，并要求最近一次融合采集完整。
- 设备维护独立关阀计时器，不依赖电脑再次发送 STOP。
- 8 秒收不到电脑 `@HEARTBEAT` 时提前关阀。
- 重复 `requestId` 只回复 ACK，不重复执行。

## Wi‑Fi 数据链路（当前默认）

烧录这版固件后，ESP32 加入已保存的 2.4 GHz 网络并拿到 DHCP IP 后，会启动本地 TCP 数据端点：

```text
esp32-sensors.local:3333
```

电脑端 Dashboard 通过该端点接收传感器 JSON、发送心跳、采样配置和经后端安全审核后的水阀命令。因此正常运行时可拔掉 USB 数据线；USB 只保留烧录、查看启动日志、发送 `@WIFI_RESET` 和故障排查用途。

同时 ESP32 每 3 秒向同一局域网广播一条 UDP 3334 发现消息。电脑端以 `--esp-host auto`（启动脚本的默认值）监听该消息并连接当前 TCP 3333 地址，因此 DHCP IP 变化、热点重启或 ESP32 重连后无需手工记 IP，也不依赖手机热点是否支持 `esp32-sensors.local` / mDNS。只有网络开启“客户端隔离”并阻止设备间 TCP 和 UDP 时，才需将 ESP32 串口打印的 `IP=...` 显式传给电脑端接收器。

电脑接收器连接到 ESP32 后，所有数据仍先进入本机 FastAPI、SQLite、预测模型和安全规则；手机网页只访问电脑的 Dashboard，不直接向 ESP32 或继电器发控制命令。

## USB 串口调试协议

波特率 115200。每次采样发送：

```text
@TELEMETRY {JSON}
```

电脑接收器发送：

```text
@HEARTBEAT
@COMMAND {JSON}
@CONFIG {"schemaVersion":"1.0","requestId":"config-...","samplingMode":"NORMAL_MONITORING","readIntervalMs":60000}
```

设备响应：

```text
@ACK {"requestId":"...","accepted":true,"actualState":"OPEN","reason":"started","remainingSeconds":30}
@CONFIG_ACK {"requestId":"config-...","accepted":true,"samplingMode":"NORMAL_MONITORING","readIntervalMs":60000}
```

`@CONFIG` 只调整传感器读取周期，和控制 GPIO11 的 `@COMMAND` 分开处理。固件白名单为：`DEBUG` 固定 2000 ms、`IRRIGATION_MONITORING` 为 2000–5000 ms、`NORMAL_MONITORING` 为 30000–120000 ms、`NIGHT_ECO` 为 300000–900000 ms。无效模式/范围会被拒绝并 ACK 原有安全值。

每 5 分钟，ESP32 还会在本地根据空气温湿度、气压、土壤湿度、净短波辐射和可用风速生成一个轻量趋势估计，并在 telemetry JSON 中增加：

```json
"edge_prediction": {
  "valid": true,
  "mode": "edge_fallback",
  "predicted_soil_moisture_30m_pct": 36.4,
  "drying_rate_pct_per_h": 0.580,
  "risk_level": "ATTENTION",
  "reason": "rapid_drying"
}
```

它不是电脑端 N-BEATS/LSTM 的移植版，而是断网时仍可运行的低算力、可解释降级预测；只提示风险，不能直接打开水阀。

太阳辐射语义固定为：RS485 地址 `0x01` 的 Solar 1 是反射短波 Rs↑，地址 `0x02` 的 Solar 2 是入射短波 Rs↓。ESP32 使用 `max(Solar2 - Solar1, 0)` 作为净短波；反射探头失败而入射探头正常时，以 `0.77 × Solar2` 作为默认反照率 α=0.23 回退。只有 Solar 2 正常的样本才可用于预测。

动态配置仅存 RAM；上电或复位恢复 `DEBUG` / 2000 ms。水阀已打开时，固件拒绝大于 5000 ms 的周期和 `NIGHT_ECO`（原因 `valve_open_requires_fast_sampling`）。本项目不使用 Deep Sleep，因为必须持续保留继电器最长时长保护、8 秒心跳断开关阀和 USB 命令接收能力；这是一项安全设计，并非已测得的低功耗百分比。

USB 模式中，Arduino 串口监视器和 `dual-forecast receive-esp32-serial` 不能同时打开。macOS 端口一般类似 `/dev/cu.wchusbserial10`，Windows 为 `COM3` 等。

## 手机 Wi-Fi 配网（普通热点/路由器）

固件不会把 Wi-Fi 名称或密码写在源码中。首次烧录、ESP32 未保存网络、或通过 USB 发送 `@WIFI_RESET` 后，设备会在串口打印类似：

```text
----- Wi-Fi setup portal -----
Connect phone to: AIOT-SETUP-12AB34
Setup password: 12345678
Open: http://192.168.4.1/
```

操作步骤：

1. 手机连接串口中显示的 `AIOT-SETUP-xxxxxx`，统一密码为 `12345678`。
2. 在手机浏览器打开 `http://192.168.4.1/`。
3. 填入当前网络的 SSID 与密码，提交后等待约 20 秒。
4. ESP32 会用 DHCP 自动获取 IP，并在 USB 串口打印 `Connected. SSID=... IP=...`。

已保存网络会在后续上电时自动尝试连接；连接失败约 20 秒后会开启并持续保持 `AIOT-SETUP-xxxxxx`，让手机重配。真实凭据只保存在该 ESP32 的 NVS 闪存中，不会进入 Git，也不会在串口或网页回显保存的密码。

支持范围：普通 **2.4 GHz** WPA2 Wi-Fi、手机热点、Windows 移动热点、家用路由器。ESP32-S3 不支持 5 GHz；需要网页跳转、扫码、验证码的校园网通常无法直接稳定接入；802.1X 学号/密码校园网需要另行按学校 EAP 认证方式适配。不要尝试绕过学校网络认证或设备接入策略。

这项配网功能只让 ESP32 加入当前局域网；它不会把 Dashboard 或开阀控制暴露到公网。当前默认主链路为：

```text
ESP32 → Wi-Fi TCP → 电脑接收器 / 本地预测 / Dashboard →（可选）云端大模型
```

若使用电脑热点供手机查看 Dashboard，电脑用 `-Lan` / `--lan` 启动 Dashboard，手机和电脑连同一个热点后访问电脑 IPv4 的 `/dashboard`；这与 ESP32 是否已完成配网是两件独立的事。

## 编译

```bash
arduino-cli compile \
  --fqbn esp32:esp32:adafruit_feather_esp32s3_nopsram \
  --build-path /tmp/aiot-esp32-build \
  firmware/esp32_s3_all_sensors
```

`WIFI_TELEMETRY_ENABLED` 当前为 `true`。它与手机配网共用保存在 ESP32 NVS 的凭据，不使用 `wifi_credentials.h`，也不设置静态 IP。若现场网络不可用，电脑端可临时用 USB 串口接收模式继续演示；水阀仍保留最长 60 秒和 8 秒无心跳自动关阀保护。
