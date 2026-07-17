# AIoT 双时序预测服务

本项目在电脑端接收 `SensorSnapshot`，使用 N-BEATS 预测下一小时 ET₀，并使用 LSTM 预测未来 12 个五分钟土壤湿度点。传感器通信和灌溉控制不在本模块范围内。

## 仓库结构

- `firmware/esp32_s3_all_sensors/`：ESP32-S3 全传感器采集固件，Arduino IDE 可直接打开。
- `dual_forecast/`：电脑端 TCP 接收、数据存储与预测模型服务。
- `artifacts/`：已训练的模型权重及标准化器。

## 克隆后直接运行

仓库已包含可用的模型权重 `artifacts/`，因此新电脑不需要训练即可接收 ESP32 数据并预测。
不要提交 `.venv/` 或 `runtime/`：前者可由依赖文件重建，后者是本机采集记录。

前提：Python 3.10 或更高版本，并能访问 PyPI 下载依赖。推荐 Python 3.11 或 3.12；
依赖会根据 Windows、macOS 或 Linux 自动选择兼容的安装包。

macOS / Linux 推荐直接执行：

```bash
git clone https://github.com/Shun-Zhan/AIoT--ModelPredition.git
cd AIoT--ModelPredition
make setup
make test
```

`make setup` 会创建 `.venv`，安装本项目和测试依赖。之后开两个终端：

```bash
# 终端 1：预测服务
make serve

# 终端 2：电脑已连 ESP32-S3-IOT 热点后启动接收桥
make receive
```

没有 `make` 时，任意系统可手动执行；Windows 将激活命令改为 `.venv\\Scripts\\Activate.ps1`：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

依赖范围统一定义在 `pyproject.toml`，安装时由 pip 为当前系统和 Python 版本选择兼容版本。
仓库未提交原始气象 ZIP，因此首次 `make test` 会跳过两项依赖该 ZIP 的训练数据测试；其余
接口、ESP32 桥接和预测流程测试仍会完整执行。把 `虹桥2018-2024逐小时气象数据.zip` 放到
仓库根目录后再运行 `make test`，即可额外执行这两项数据处理测试。

## 训练模型（可选）

```bash
dual-forecast preprocess '虹桥2018-2024逐小时气象数据.zip'
dual-forecast train-all '虹桥2018-2024逐小时气象数据.zip' --epochs 35
# 也可以分别训练：train-et0 或 train-soil-proxy
```

训练会生成 `artifacts/nbeats_et0.pt`、`artifacts/lstm_soil.pt`、各自的缩放器和指标元数据。只有同时优于“上一时刻不变”基线的模型才在元数据中标记为可用。虹桥数据不含土壤湿度，且其逐小时降水字段实际没有有效值，因此初始 LSTM 使用桶式水量平衡与“30%触发、补水至75%”的虚拟灌溉事件生成 `proxy` 序列；该指标不能解释为真实土壤预测精度。

N-BEATS 使用 MSE；LSTM 以 MSE 为主损失并加入 0.2 权重的 MAE 正则，减少普通缓变时段的小幅系统偏差。LSTM 输出采用相对当前湿度的残差形式。

> 数据质量提示：当前提供的压缩包中，名为 `虹桥2019.csv` 的文件内部年份是 2020，名为 `虹桥2020.csv` 的文件内部年份是 2021，实际缺少 2019 年。程序不会改写或伪造年份，会去除重复时间戳并在预处理报告中输出该警告。

## 快照接口

```bash
curl -X POST http://127.0.0.1:8000/v1/snapshots \
  -H 'Content-Type: application/json' \
  -d '{
    "uptimeMs": 300000,
    "windOk": true, "windVoltage": 1.2, "windSpeedMs": 2.3,
    "airOk": true, "air": {"temperatureC": 25.1, "humidityPercent": 63.2},
    "soilOk": true, "soil": {"temperatureC": 22.4, "moisturePercent": 51.7},
    "solar1Ok": true, "solarRadiation1Wm2": 410,
    "solar2Ok": true, "solarRadiation2Wm2": 430,
    "AirPressure": 1013
  }'
```

`AirPressure`按 hPa接收并在内部转为 kPa。也可发送带时区的 `receivedAt`；省略时使用电脑接收时间。积累不足 288 个五分钟点时返回 `warming_up`，存在超过 15 分钟且无法插值的缺口时返回 `insufficient_data`。

完整预测可通过 `GET /v1/forecast/latest`读取。服务重启后从 `runtime/forecast.sqlite3`恢复历史窗口。

## 接收 ESP32 TCP 数据并提交预测服务

烧录 [`firmware/esp32_s3_all_sensors/esp32_s3_all_sensors.ino`](firmware/esp32_s3_all_sensors/esp32_s3_all_sensors.ino) 后，当前固件默认作为
Windows 移动热点的 Wi-Fi 客户端，并在 TCP 3333 端口提供一行一条的 JSON 采集数据。Windows
移动热点请设置为 **2.4 GHz**，固件中的 `WIFI_USE_SOFT_AP` 保持为 `false`。
复制 `firmware/esp32_s3_all_sensors/wifi_credentials.h.example` 为
`wifi_credentials.h`，填写热点名和密码。真实凭据文件仅保存在本机，不会推送到 GitHub。

Windows 移动热点通常使用 `192.168.137.1` 作为网关；固件默认使用固定地址
`192.168.137.50`，避免部分 Windows 热点未及时分配 DHCP 地址的问题。ESP32 成功连接后
会打印该地址，同时注册为 `esp32-sensors.local`。

如需恢复 ESP32 自己创建热点的直连模式，将 `WIFI_USE_SOFT_AP` 改为 `true`；地址为
`192.168.4.1:3333`。

在电脑上启动本项目的预测服务：

```bash
dual-forecast serve --host 127.0.0.1 --port 8000
```

在另一个终端启动 TCP 接收桥接程序：

```bash
dual-forecast receive-esp32 --esp-host 192.168.137.50
```

桥接程序会将 ESP32 的下划线 JSON 字段转换为 `SensorSnapshot` 的字段名，并 POST 到
`/v1/snapshots`，由服务写入历史窗口和调用预测模型。默认每 300 秒提交一次，匹配模型的
五分钟时间步；调试时可用 `--min-interval-seconds 0` 每条都提交。

启动后用浏览器打开电脑端实时画面：

```text
http://127.0.0.1:8000/dashboard
```

桥接程序会每 2 秒转发实时数据给画面，同时仍只每 5 分钟将一个样本写入预测模型历史，避免
实时显示影响 24 小时预测窗口。画面会每 2 秒刷新空气温湿度、气压、风速、土壤数据、太阳辐射
和预测模型状态。预测入库时会跳过空包或任何关键传感器读数无效的包，并直接等待下一条完整数据。
也可以使用 `esp32-sensors.local`；如果电脑无法解析 `.local`，使用 ESP32 串口输出的 IP：

```bash
dual-forecast receive-esp32 --esp-host <ESP32_IP>
```

### Windows：直接复制运行

确认 Windows 移动热点已开启，且 ESP32 已连接到它。当前 Windows 热点固定地址配置下，ESP32
地址为 `192.168.137.50`。先在 **PowerShell** 测试 TCP 是否可达：

```powershell
Test-NetConnection 192.168.137.50 -Port 3333
```

#### 一键启动（推荐）

先在 Windows 上 `git pull` 获取最新代码。之后直接双击仓库根目录的 `start_dashboard.cmd`，或在
PowerShell 中运行：

```powershell
cd C:\Users\你的用户名\Desktop\AIoT--ModelPredition
.\start_dashboard.cmd
```

脚本会在首次运行时自动创建 `.venv` 并安装依赖，随后后台启动预测服务与 ESP32 接收器，最后以
没有地址栏和标签页的 Edge 全屏窗口打开 dashboard。按 `Esc` 可退出全屏；关闭 Edge 不会停止后台
服务。需要停止后台服务时双击 `stop_dashboard.cmd`，或运行：

```powershell
.\stop_dashboard.cmd
```

日志保存在 `runtime\logs\`。

若网页右上角显示“实时数据：超过 20 秒前”，用下面命令实时查看接收器日志：

```powershell
Get-Content .\runtime\logs\esp32-receiver.out.log -Tail 40 -Wait
```

正常时每约 10 秒会出现 `Live dashboard updated from ESP32 telemetry.`；若脚本提示 TCP 3333
不可达，先运行 `Test-NetConnection 192.168.137.50 -Port 3333` 检查热点连通性。

若电脑没有安装在默认位置的 Edge，脚本会自动尝试 Chrome；两者都未找到时会以普通浏览器打开。

#### 手动启动

看到 `TcpTestSucceeded : True` 后，打开两个 PowerShell 窗口。下面命令不依赖 `source`，也不会
误用 Anaconda 或全局 Python。

第一个窗口启动预测服务和网页：

```powershell
cd C:\Users\你的用户名\Desktop\AIoT--ModelPredition

if (!(Test-Path .venv)) {
  py -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\dual-forecast.exe serve --host 127.0.0.1 --port 8000
```

第二个窗口接收 ESP32 数据：

```powershell
cd C:\Users\你的用户名\Desktop\AIoT--ModelPredition
.\.venv\Scripts\dual-forecast.exe receive-esp32 --esp-host 192.168.137.50
```

最后在 Windows 浏览器打开：

```text
http://127.0.0.1:8000/dashboard
```

网页会实时显示传感器值；预测模型刚开始会显示 `warming_up`，因为需要累积 `288` 个五分钟样本
（约 24 小时）才会生成完整预测。实时显示不会改变预测模型的五分钟采样节奏。网页右上角会显示
“实时数据：N 秒前”：绿色且不超过 5 秒表示实时链路正常；超过 20 秒变红表示 ESP32 接收链路中断。

当前 ESP32 程序已支持 BMP280/BME280 气压传感器，正常时会发送真实的 `AirPressure`（hPa）。
若传感器暂未接好而上传 0，桥接程序会临时改用 1013 hPa，避免 0 hPa 进入蒸散预测；可通过
`--fallback-air-pressure-hpa` 修改该回退值。

## 使用真实土壤数据重训

SQLite 中有效土壤观测跨度达到 14 天后运行：

```bash
dual-forecast retrain-observed --epochs 35
curl -X POST http://127.0.0.1:8000/v1/models/reload
```

新模型通过临时文件写入并原子替换，避免服务读到不完整权重。预测结果可用 `dual-forecast export-latest --output outputs/latest_forecast.csv`导出。
