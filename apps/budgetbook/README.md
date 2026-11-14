# BudgetBook 个人记账

纯 Python 3.11 标准库后端 + 原生 HTML/CSS/JS 前端的个人记账小程序，无第三方运行依赖。

## 启动

```bash
python server.py --data book.json --port 8765
```

启动后打印一行 JSON，例如 `{"url": "http://127.0.0.1:8765", "port": 8765}`，浏览器打开该地址即可使用。
`--port 0` 表示自动选择空闲端口；服务只绑定 `127.0.0.1`。

## 功能

- 录入金额（正数、最多两位小数）、分类、日期、备注
- 显示总支出、记录数、分类汇总
- 记录列表，确认后删除
- 按月份（YYYY-MM）与分类筛选
- 导出 CSV（列：id,amount,category,note,date）

## HTTP API

- `GET /` 返回网页
- `GET /api/entries?month=YYYY-MM&category=food` 记录数组
- `GET /api/summary?month=YYYY-MM` 汇总对象
- `POST /api/entries` JSON `{amount,category,note,date}`，成功 201
- `DELETE /api/entries/ID` 返回 `{deleted: bool}`
- `GET /api/export` 下载 CSV

非法请求返回 400 JSON `{error: ...}`，未知路由 404，请求体上限 16 KiB。

## 数据存储

数据保存在 `--data` 指定的 JSON 文件中（带版本号与 `next_id`）。文件不存在视为空账本并自动创建父目录；已存在的损坏或不兼容文件会抛 `ValueError`，不会被静默覆盖。写入采用同目录临时文件 + `os.replace` 原子替换。

## 限制

- 单用户、单进程使用，不保证并发写安全
- 无登录、无部署、无图表
- 金额以十进制字符串精确加总，不做货币换算

## 测试

```bash
python -m pytest tests/test_contract.py tests/test_extra.py
```
