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
| DHT11 OUT | GPIO10 | `+` 接 3.3 V，`-` 接 GND；无上拉的模块需 OUT–3.3 V 加 4.7–10 kΩ |
| BMP280/BME280 SDA/SCL | GPIO3 / GPIO4 | VCC 接 3.3 V，CSB 接 3.3 V，SDO 接 GND（地址 0x76） |
| 土壤 RS485 模块 RO/DI | GPIO18 / GPIO17 | 传感器地址 0x03、4800 baud |
| 两个太阳辐射 RS485 模块 RO/DI | GPIO16 / GPIO15 | 地址依次为 0x01、0x02，4800 baud |
| AHT20 SDA/SCL（可选） | GPIO5 / GPIO8 | 当前默认使用 DHT11；切换时改固件中的 `AIR_SENSOR_TYPE` |

GPIO7 是 Feather 板上 I²C/STEMMA 电源控制脚，由程序自动拉高，不应接其他传感器。

## 与预测服务的通信

固件默认创建热点：

```text
SSID: ESP32-S3-IOT
密码: 12345678
TCP: 192.168.4.1:3333
```

每次采样会通过 TCP 发一条换行结尾 JSON。电脑端启动 `dual-forecast receive-esp32` 后，会
将 JSON 转成预测服务的 `SensorSnapshot` 并提交到本机 API。采集间隔是 2 秒；接收程序默认
每 5 分钟提交一次，匹配预测模型的时间步。

默认是 ESP32 热点模式。若要连路由器，修改草图顶部的 `WIFI_USE_SOFT_AP`、`ROUTER_SSID`
和 `ROUTER_PASSWORD` 后重新烧录。
