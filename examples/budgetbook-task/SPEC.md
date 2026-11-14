# BudgetBook 开发任务 v1

实现一个有浏览器界面的个人记账小程序，Python 3.11 标准库后端、原生 HTML/CSS/JS 前端，无第三方运行依赖。源码由模型编写。

## 文件

- `solution.py`：导出 `Book(path)`，path 可以是 str/Path。
- `storage.py`：可选的持久化辅助模块，供 solution.py 导入；可留空。
- `server.py`：可直接 `python server.py --data book.json --port 8765`。
- `index.html`：包含内联 CSS/JS 的中文网页，不依赖 CDN 或外部资源。
- `README.md`：中文说明、使用命令、限制。
- `tests/test_extra.py`：你补充的测试。
- `tests/test_contract.py`：只读公开验收测试，不得修改。

每个文件最多 8192 UTF-8 字节，总计最多 32768 字节。建议源码文件控制在 5000 字节以内，保持简洁，避免大量注释和冗长防御代码；solution.py 超长时将持久化拆到 storage.py。按文件分次 write_file，最多每批 4 个调用。

## Book 公共接口

1. `add(amount, category, note="", day=None) -> dict`。amount 是十进制字符串，必须有限、正数、最多两位小数；拒绝 0、负数、NaN、Infinity、三位小数。category 去两端空白后非空。note 原样保存。day 缺省为本地今天，否则必须是真实 YYYY-MM-DD。返回 `{id: int, amount: "12.30", category: str, note: str, date: "YYYY-MM-DD"}`。
2. `entries(*, month=None, category=None) -> list[dict]`。按 id 升序；支持 YYYY-MM 和精确分类过滤。非法月份抛 ValueError。
3. `delete(entry_id) -> bool`。存在则删除并落盘；不存在 False。id 递增且删除后不得复用，重启后继续。
4. `summary(*, month=None) -> dict`。返回 `{total: "0.00", count: int, by_category: {分类: "金额"}}`，十进制精确加总，空账本也是有效结果。
5. `export_csv(target) -> int`。UTF-8 CSV，列固定为 id,amount,category,note,date，导出全部记录，正确引用逗号/换行，返回条数。

持久化为带版本和 next_id 的 JSON；文件不存在表示空账本，自动创建父目录。已存在的损坏 JSON 或不兼容结构必须抛 ValueError，不能静默覆盖。写入先写同目录临时文件再 os.replace；本任务是单用户单进程，不要求并发写。

## HTTP 后端

只绑定 127.0.0.1，--data 和 --port 可配置，port=0 时自动选择空闲端口。启动后打印一行 JSON `{url: "http://127.0.0.1:端口", port: 端口}` 并 flush，便于测试启动。使用 ThreadingHTTPServer；从 __file__ 同目录读取 index.html。

- GET `/`：返回 HTML。
- GET `/api/entries?month=YYYY-MM&category=food`：返回记录数组，可不带过滤条件。
- GET `/api/summary?month=YYYY-MM`：返回汇总对象。
- POST `/api/entries`：JSON `{amount,category,note,date}`，date 可省略，转为 Book.add 的 day 参数，成功 201 并返回记录。
- DELETE `/api/entries/ID`：返回 `{deleted: bool}`。
- GET `/api/export`：返回 UTF-8 CSV，并设置下载文件名。
- 非法请求/金额/日期/损坏存储返回 400 JSON `{error: 友好错误}`，未知路由返回 404。限制请求体为 16 KiB，不开放任意文件读取。JSON 字符串 UTF-8 编码。

## 网页

中文响应式界面，浅色清爽风格、标题和简短说明。显示总支出、记录数、分类汇总；表单录入金额、分类、日期、备注；列表显示记录，支持确认后删除；月份与分类筛选；CSV 下载按钮。所有内容通过上面 HTTP API 保存，重启或刷新保留数据。显示加载/空列表/错误状态，用 textContent 等安全 DOM API 渲染用户输入，避免 innerHTML 拼接用户文本。用原生 fetch；不需要登录、部署、复杂图表、并发保障。

## 工作流程

先读取公开测试，编写后端、网页、补充测试和文档。通过 run_tests 运行 tests/test_contract.py 和 tests/test_extra.py，失败时自行修改后重测，最后给出简短交付说明。不要只在回答中贴代码，必须调用 write_file 写文件。可用工具只有 list_files/read_file/write_file/run_tests，没有 shell、联网或安装依赖权限。验证成功后停止。
