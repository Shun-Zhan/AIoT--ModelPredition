# AIoT 双时序预测服务

本项目在电脑端接收 `SensorSnapshot`，使用 N-BEATS 预测下一小时 ET₀，并使用 LSTM 预测未来 12 个五分钟土壤湿度点。传感器通信和灌溉控制不在本模块范围内。

## 克隆后直接运行

仓库已包含可用的模型权重 `artifacts/`，因此新电脑不需要训练即可接收 ESP32 数据并预测。
不要提交 `.venv/` 或 `runtime/`：前者可由依赖文件重建，后者是本机采集记录。

前提：Python 3.10 或更高版本，并能访问 PyPI 下载依赖。

macOS / Linux 推荐直接执行：

```bash
git clone https://github.com/Shun-Zhan/AIoT--ModelPredition.git
cd AIoT--ModelPredition
make setup
make test
```

`make setup` 会创建 `.venv`，按 `requirements.lock` 安装已经验证的精确版本，并安装本项目。之后开两个终端：

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
python -m pip install --no-build-isolation --no-deps -e .
python -m pytest -q
```

`requirements.lock` 是当前测试通过的精确依赖集合；升级依赖时应先完整测试，再同步更新它。
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

烧录 `../all_sensors/all_sensors.ino` 后，ESP32 默认创建 `ESP32-S3-IOT`
热点，并在 `192.168.4.1:3333` 提供一行一条的 JSON 采集数据。电脑连接该热点后，
在一个终端启动本项目的预测服务：

```bash
dual-forecast serve --host 127.0.0.1 --port 8000
```

在另一个终端启动 TCP 接收桥接程序：

```bash
dual-forecast receive-esp32
```

桥接程序会将 ESP32 的下划线 JSON 字段转换为 `SensorSnapshot` 的字段名，并 POST 到
`/v1/snapshots`，由服务写入历史窗口和调用预测模型。默认每 300 秒提交一次，匹配模型的
五分钟时间步；调试时可用 `--min-interval-seconds 0` 每条都提交。

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
