# 日志模式

## 异常关键词

- `error`
- `exception`
- `traceback`
- `failed`
- `nan`
- `missing`

## 风险关键词

- `warning`
- `deprecated`
- `skipped`
- `empty`
- `zero rows`

## 训练信号

- `loss`
- `metric`
- `wape`
- `mape`
- `mae`
- `rmse`
- `rows`
- `samples`
- `finished`
- `success`

## 解释规则

日志关键词只能作为证据线索。不要把 warning 直接写成根因，也不要把没有 error 写成训练一定成功。
