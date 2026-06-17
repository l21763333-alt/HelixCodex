# 代码模式

## 入口文件

训练入口常见命名：

- `train.py`
- `main.py`
- `run.py`
- `run_train.py`

评估入口常见命名：

- `evaluate.py`
- `eval.py`
- `metrics.py`

## 指标函数

关注这些关键词：

- `wape`
- `mape`
- `bias`
- `mae`
- `rmse`

## 特征与模型

特征构建关键词：

- `feature`
- `build_feature`
- `transform`

模型训练关键词：

- `fit`
- `train`
- `lightgbm`
- `xgboost`
- `sklearn`
- `prophet`

## 输出线索

预测或指标输出常见关键词：

- `to_csv`
- `save`
- `prediction`
- `forecast`
- `metrics`

## 解释规则

代码中出现某个关键词，只能说明存在相关逻辑。是否本次运行实际执行，需要结合日志和产物。
