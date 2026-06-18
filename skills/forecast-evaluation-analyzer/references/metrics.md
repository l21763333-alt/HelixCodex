# 指标规则

## 定义

- `error = prediction - actual`
- `WAPE = sum(abs(error)) / sum(actual)`
- `MAPE = mean(abs(error) / abs(actual))`，跳过 `actual = 0` 的行
- `Bias = sum(error) / sum(actual)`
- `MAE = mean(abs(error))`
- `RMSE = sqrt(mean(error^2))`

## 解释规则

- 不在自然语言中手算指标。
- 不修改脚本生成的指标值。
- 没有可用对照预测时，不做对比结论。
- `Bias > 0` 表示整体偏高估，`Bias < 0` 表示整体偏低估，具体以工具定义为准。
