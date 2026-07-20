# ESP32-S3 全传感器与安全水阀固件

用 Arduino IDE 打开 `esp32_s3_all_sensors.ino`，板型选择 `Adafruit Feather ESP32-S3 No PSRAM`。固件只依赖 ESP32 Arduino Core 自带组件，无需额外安装 Arduino 库。当前使用烧录 USB 线传输数据和命令，Wi-Fi 与旧串口屏均关闭。

## 引脚总表

| 功能 | ESP32-S3 | 外设侧 | 说明 |
| --- | --- | --- | --- |
| 风速 1 | GPIO9 | OUT | ADC；0–5 V 必须分压到 0–3.3 V |
| 风速 2 | GPIO6 | OUT | ADC；同上 |
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

## USB 串口协议

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

动态配置仅存 RAM；上电或复位恢复 `DEBUG` / 2000 ms。水阀已打开时，固件拒绝大于 5000 ms 的周期和 `NIGHT_ECO`（原因 `valve_open_requires_fast_sampling`）。本项目不使用 Deep Sleep，因为必须持续保留继电器最长时长保护、8 秒心跳断开关阀和 USB 命令接收能力；这是一项安全设计，并非已测得的低功耗百分比。

Arduino 串口监视器和 `dual-forecast receive-esp32-serial` 不能同时打开。macOS 端口一般类似 `/dev/cu.wchusbserial10`，Windows 为 `COM3` 等。

## 编译

```bash
arduino-cli compile \
  --fqbn esp32:esp32:adafruit_feather_esp32s3_nopsram \
  --build-path /tmp/aiot-esp32-build \
  firmware/esp32_s3_all_sensors
```

若未来恢复 Wi-Fi TCP，可把 `WIFI_TELEMETRY_ENABLED` 改为 `true`，并使用未提交的 `wifi_credentials.h`。当前比赛闭环默认使用 USB，避免热点和网络切换影响现场演示。
