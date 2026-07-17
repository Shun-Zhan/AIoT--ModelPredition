# ESP32-S3 整合采集固件

这个目录是电脑端预测服务配套的 ESP32-S3 固件。请用 Arduino IDE 直接打开
`esp32_s3_all_sensors.ino`，选择 `Adafruit Feather ESP32-S3 No PSRAM` 后烧录。

它只使用 ESP32 Arduino Core 自带的 `Wire`、`WiFi` 和 `HardwareSerial`，不需要另装
Arduino 库。

## 采集与引脚

| 功能 | ESP32-S3 引脚 | 说明 |
| --- | --- | --- |
| 风速 1 模拟输出 | GPIO9 | ADC；若风速传感器输出 0–5 V，必须先分压至 0–3.3 V |
| 风速 2 模拟输出 | GPIO6 | ADC；同上 |
| AHT20 SDA/SCL | GPIO5 / GPIO8 | 当前启用；VCC 接 3.3 V，GND 接 GND，SDA 接 GPIO5，SCL 接 GPIO8 |
| DHT11 OUT（备用） | GPIO10 | 当前未启用；`+` 接 3.3 V，`-` 接 GND；无上拉的模块需 OUT–3.3 V 加 4.7–10 kΩ |
| BMP280/BME280 SDA/SCL | GPIO3 / GPIO4 | VCC 接 3.3 V，CSB 接 3.3 V，SDO 接 GND（地址 0x76） |
| 土壤 RS485 模块 RO/DI | GPIO18 / GPIO17 | 传感器地址 0x03、4800 baud |
| 两个太阳辐射 RS485 模块 RO/DI | GPIO16 / GPIO15 | 地址依次为 0x01、0x02，4800 baud |

GPIO7 是 Feather 板上 I²C/STEMMA 电源控制脚，由程序自动拉高，不应接其他传感器。

## 与预测服务的通信

若将草图顶部的 `WIFI_USE_SOFT_AP` 改为 `true`，固件会创建热点：

```text
SSID: ESP32-S3-IOT
密码: 12345678
TCP: 192.168.4.1:3333（默认 SoftAP 模式）
```

每次采样会通过 TCP 发一条换行结尾 JSON。电脑端启动 `dual-forecast receive-esp32` 后，会
将 JSON 转成预测服务的 `SensorSnapshot` 并提交到本机 API。采集间隔是 2 秒；接收程序默认
每 5 分钟提交一次，匹配预测模型的时间步。

## 可选 M 系列串口屏

固件已包含一个不依赖 VisualTFT 控件 ID 的 800×480 总览页：当前空气、土壤、平均风速、
平均光照，以及电脑模型回传的“下一小时 ET₀”和“一小时后土壤湿度”。

当前显示屏功能已关闭。后续接入新显示方案前，再把草图中的 `DISPLAY_ENABLED` 改为 `true`：

```text
ESP32 GPIO12 (TX) -> 串口屏 RX
ESP32 GPIO11 (RX) <- 串口屏 TX（仅需触摸回传时接）
GND                -> 串口屏 GND 与独立屏幕电源 GND
```

替换后的 M070 屏幕工程使用 **19200 baud**，固件已按此值配置。此前农业数据页面使用的是
9600 baud。串口波特率由屏幕内已烧录的 VisualTFT 工程决定；更换屏幕工程或修改其串口参数后，需
同步修改 `DISPLAY_BAUD` 并重新烧录。

屏幕 VCC 通常使用独立 5 V 供电，不能接 Feather 的 3.3 V。若屏幕 TX 输出 5 V，必须经
电平转换或分压后才可接 GPIO11。首次只显示时，可以不接屏幕 TX；GPIO12 到屏幕 RX 若屏幕
不接受 3.3 V 高电平，也应使用 UART 电平转换模块。

显示功能使用 ESP32 的 UART0。若当前串口监视器端口名称类似 `cu.wchusbserial...`，它很可能
也是 UART0；启用显示后只能看到 ROM 启动信息、看不到后续应用日志是预期现象。请在 Arduino
IDE 的 **Tools → USB CDC On Boot** 中启用 USB CDC，并改用 ESP32 原生 USB 出现的
`cu.usbmodem...` 端口查看日志。若板子没有原生 USB CDC，则关闭 `DISPLAY_ENABLED` 后才能继续
使用这个 USB 转串口端口调试。

当前仓库默认是 Windows 热点 Station 模式。若要恢复 ESP32 热点模式，将草图顶部的
`WIFI_USE_SOFT_AP` 改为 `true` 后重新烧录。
## 连接电脑共享 Wi-Fi

Windows 的“移动热点”请设置为 **2.4 GHz**。将代码顶部的 `WIFI_USE_SOFT_AP` 改为 `false`，
然后把 `wifi_credentials.h.example` 复制为 `wifi_credentials.h`，并填写：

```cpp
static const char *ROUTER_SSID = "电脑热点名称";
static const char *ROUTER_PASSWORD = "电脑热点密码";
```

`wifi_credentials.h` 被 Git 忽略，不会将真实密码推送到仓库。Windows 移动热点通常使用
当前电脑热点使用 `10.98.128.1` 作为网关；固件默认将 ESP32 固定为 `10.98.128.50`，避免 Windows
热点 DHCP 未及时分配地址。其他电脑请用 `ipconfig` 查看热点对应“本地连接*”的 IPv4 地址，并在
本机 `wifi_credentials.h` 中相应修改网关和 ESP32 IP。ESP32 加入热点后，串口每 10 秒会输出连接状态，并尝试注册
`esp32-sensors.local`。电脑端接收命令为：

```bash
dual-forecast receive-esp32 --esp-host 10.98.128.50
```

如果 `.local` 解析失败，使用串口打印的 IP 地址作为 `--esp-host`。
