from __future__ import annotations

import torch
from torch import nn


class NBeatsBlock(nn.Module):
    def __init__(self, input_size: int, hidden_size: int, output_size: int):
        super().__init__()
        self.body = nn.Sequential(
            nn.Linear(input_size, hidden_size), nn.ReLU(),
            nn.Linear(hidden_size, hidden_size), nn.ReLU(),
            nn.Linear(hidden_size, hidden_size), nn.ReLU(),
        )
        self.backcast = nn.Linear(hidden_size, input_size)
        self.forecast = nn.Linear(hidden_size, output_size)

    def forward(self, x):
        h = self.body(x)
        return self.backcast(h), self.forecast(h)


class NBeatsET0(nn.Module):
    def __init__(self, input_size: int = 24, hidden_size: int = 128, blocks: int = 4, output_size: int = 1):
        super().__init__()
        self.blocks = nn.ModuleList([NBeatsBlock(input_size, hidden_size, output_size) for _ in range(blocks)])

    def forward(self, x):
        residual = x
        forecast = torch.zeros((x.shape[0], 1), dtype=x.dtype, device=x.device)
        for block in self.blocks:
            backcast, block_forecast = block(residual)
            residual = residual - backcast
            forecast = forecast + block_forecast
        return forecast


class SoilLSTM(nn.Module):
    def __init__(self, feature_count: int, hidden_size: int = 64, layers: int = 2, output_steps: int = 12):
        super().__init__()
        self.lstm = nn.LSTM(feature_count, hidden_size, num_layers=layers, batch_first=True, dropout=0.15 if layers > 1 else 0.0)
        self.head = nn.Sequential(nn.Linear(hidden_size, hidden_size), nn.ReLU(), nn.Linear(hidden_size, output_steps))

    def forward(self, x):
        output, _ = self.lstm(x)
        # The first feature is standardized current soil moisture. Predicting a
        # residual around persistence makes slow drying stable while retaining
        # capacity for threshold-triggered recharge events.
        return x[:, -1, 0:1] + self.head(output[:, -1, :])
