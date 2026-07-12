# AIoT 双时序预测服务

本项目在电脑端接收 `SensorSnapshot`，使用 N-BEATS 预测下一小时 ET₀，并使用 LSTM 预测未来 12 个五分钟土壤湿度点。传感器通信和灌溉控制不在本模块范围内。

## 安装与训练

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'

dual-forecast preprocess '虹桥2018-2024逐小时气象数据.zip'
dual-forecast train-all '虹桥2018-2024逐小时气象数据.zip' --epochs 35
# 也可以分别训练：train-et0 或 train-soil-proxy
dual-forecast serve --host 127.0.0.1 --port 8000
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

## 使用真实土壤数据重训

SQLite 中有效土壤观测跨度达到 14 天后运行：

```bash
dual-forecast retrain-observed --epochs 35
curl -X POST http://127.0.0.1:8000/v1/models/reload
```

新模型通过临时文件写入并原子替换，避免服务读到不完整权重。预测结果可用 `dual-forecast export-latest --output outputs/latest_forecast.csv`导出。
