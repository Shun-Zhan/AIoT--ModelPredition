# ESP32-S3 固件

该目录的固件是生产业务权威：它在 ESP32-S3 本机完成传感器采集、NTP 校时、离线 V2 历史、float32 N-BEATS/SoilLSTM 推理、灌溉安全审核、水阀控制，以及可选火山引擎云端大模型调用。电脑端仅接收和显示设备消息，并转发网页请求；电脑不参与生产预测、决策或云端调用。

## IDE 与依赖

使用 Arduino IDE 打开 `esp32_s3_all_sensors.ino`。板型选择 **Adafruit Feather ESP32-S3 No PSRAM**，Tools → Partition Scheme 选择 **Default (3MB APP/1.5MB SPIFFS)**。不要选 TinyUF2 FATFS 分区，否则 LittleFS 无法挂载。

通过 CH340 `/dev/cu.wchusbserial*` 或 Windows `COM*` 烧录时，设置 USB CDC On Boot → Disabled、Upload Mode → UART0 / Hardware CDC。

除 ESP32 Arduino Core 外，还需安装 **ArduinoJson 7.4.2**。模型权重由仓库根目录的 `scripts/export_esp32_models.py` 生成 `generated/model_data.h`，更新模型后必须重新烧录；不要手动修改生成文件。

```bash
.venv/bin/python scripts/export_esp32_models.py --check
arduino-cli compile \
  --fqbn 'esp32:esp32:adafruit_feather_esp32s3_nopsram:PartitionScheme=default_8MB,CDCOnBoot=default,UploadMode=default' \
  --build-path /tmp/aiot-esp32-build \
  firmware/esp32_s3_all_sensors
```

## 引脚与接线

| 功能 | ESP32-S3 | 外设侧 | 说明 |
| --- | --- | --- | --- |
| 风速（启用） | GPIO6 | OUT | ADC；0–5 V 信号先分压至 0–3.3 V |
| 风速预留 | GPIO9 | 不接 | 当前不采样 |
| AHT20 | GPIO5 / GPIO8 | SDA / SCL | 3.3 V，地址 0x38 |
| BMP280 / HW-611 | GPIO3 / GPIO4 | SDA / SCL | 3.3 V；CSB→3.3 V；SDO→GND 常为 0x76 |
| ZH-SOIL7 土壤 | GPIO18 / GPIO17 | TX / RX | TTL UART，4800 8N1，Modbus-RTU 地址 0x03 |
| SN-300AL 太阳辐射 | GPIO16 / GPIO15 | RS485 转换器 RO / DI | 4800 8N1，地址 0x01/0x02 |
| 水阀继电器 | GPIO11 | IN | 3.3 V、高电平有效；上电先置 LOW |
| Feather I²C 电源控制 | GPIO7 | 不外接 | 固件自动拉高，禁止复用 |

所有模块必须共地。本方案没有硬件 RTC：设备每次启动后先连接有互联网的 Wi-Fi，通过 NTP 校准 ESP32 系统时钟；Wi-Fi 连通后约 5 秒开始校时重试。校时成功前预测状态为 `clock_unset`，自动灌溉不会开启。校时成功后，在本次运行周期内即使暂时断网，设备仍可继续采集、预测和自动灌溉；断网重启则需再次 NTP 校时。

**土壤不是 RS485 电气层。** 该版本的 ZH-SOIL7 使用 TTL UART：传感器 TX→GPIO18（ESP32 RX），传感器 RX→GPIO17（ESP32 TX），共地。它的帧协议仍为 Modbus-RTU；协议不等于电气层，因此不要额外串接 RS485 转换器。

太阳辐射才需要 RS485 转换器：两个太阳探头 A 对 A、B 对 B 并联在独立太阳总线上，转换器 RO→GPIO16，DI→GPIO15。地址 0x01 是反射短波，0x02 是入射短波；净短波为 `max(入射 - 反射, 0)`。

继电器控制侧：DC+/VCC→模块要求的 3.3 V，DC-/GND→ESP32 GND，IN→GPIO11。24 V 常闭水阀的触点侧：24 V 正极→COM，NO→水阀正极，水阀负极→24 V 负极。24 V 不得接入 GPIO 或 IN；触点额定直流电压/电流应高于水阀负载。

## 离线记录、预测与安全

上电默认是 `OFFLINE_LOGGING`，每 **5 分钟**尝试写入一个完整样本。任一必需传感器失败时，该样本不会写入；设备每 15 秒重试，跨越本 slot 后记录缺口，新的连续窗口从下一条完整样本开始。

- V2 记录包含版本、记录长度、序号、UTC epoch、五分钟 slot、有效传感器标志和 CRC。
- 从 LittleFS 的轮换文件恢复最新连续 288 条 V2 记录。288 条完整的五分钟样本等于 24 小时，可立即推理。
- V1 与 V2 不兼容。升级前先用 `dual-forecast offline-log --action export` 导出 CSV；确认文件可用后，才通过明确 `erase` 操作清除旧记录。固件不静默清空 V1。
- 导出 CSV 可通过 `dual-forecast offline-log --action import --input <csv>` 经 USB 回灌到 V2 预测历史。导入时设备强制关阀并关闭自动模式；电脑校验 CSV 的完整记录和五分钟连续性，选择最长连续段，最多 288 条。导入 288 条立即推理，少于 288 条则继续追加真实采样直到窗口满。
- 本方案不使用硬件 RTC。设备在 NTP 校时成功后可将系统时钟作为当前运行周期的可信时间源；断网重启后回到 `clock_unset`，直到再次 NTP 校时。时间无效、窗口未满、缺失数据或模型错误时，仅允许 `edge_prediction` 风险提示，禁止自动开阀。
- 自动模式只保存在 RAM，**每次重启默认关闭**。即使人工打开自动模式，也要满足完整传感器、预测就绪、冷却、单次 60 秒和每日 600 秒限额。
- 本地自动开阀不依赖电脑心跳；PC 发起的调试/人工开阀仍受传输心跳保护。任何状态都接受 STOP 并关阀。

## Wi-Fi、云端与配网

首次烧录、没有已保存网络或收到 `@WIFI_RESET` 时，设备会创建 `AIOT-SETUP-xxxxxx` 配置热点，密码 `12345678`，页面为 `http://192.168.4.1/`。支持 2.4 GHz WPA2 网络、手机热点和 Windows 移动热点；5 GHz、网页认证和 802.1X 校园网需额外适配。

配网页面保存 Wi-Fi 凭据，以及云端开关、模型名、农田档案和 API Key。云端 Key 只保存在 ESP32 NVS：页面只显示是否已配置，绝不回显、写日志、出现在遥测或提交到 Git。空 Key 更新保留旧 Key；清除是显式操作。

设备通过 HTTPS 直接调用火山引擎网关，并验证根证书；禁止使用 `setInsecure()`。云端网络/TLS/JSON/服务失败时只返回离线状态，采集、预测和本地安全闭环继续运行。正式部署前应作废历史聊天、终端或截图中泄露过的 Key。

## 与 Dashboard 的协议

设备上行：

```text
@TELEMETRY { ... }
@FORECAST { "schemaVersion":"2.0", ... }
@IRRIGATION_STATE { "schemaVersion":"2.0", ... }
@CLOUD_RESULT { "schemaVersion":"2.0", ... }
@UI_ACK { "schemaVersion":"2.0", ... }
```

电脑/网页下行：

```text
@UI_COMMAND { "action":"SET_AUTO_MODE" | "CONFIRM_WATERING" | "CANCEL" | "STOP_WATERING" | "CLOUD_ANALYZE" | "CLOUD_CHAT", ... }
```

设备对所有开阀请求重新执行完整安全审核。Dashboard 的按钮表示“已请求”，不是“已执行”；以 `@UI_ACK` 和 `@IRRIGATION_STATE` 中的设备状态为准。

Wi-Fi 模式下设备以 UDP 3334 广播、TCP 3333 提供遥测端点；电脑可自动发现。USB 波特率为 115200，Arduino 串口监视器不能与电脑接收器同时占用该端口。
