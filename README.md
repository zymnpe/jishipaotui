# 校园集市跑腿新单提醒

一个 Windows 桌面提醒工具：使用**个人登录**后导出的 `/task/list` 请求，每隔一段时间查询一次大厅，按 `task_id` 去重，发现新单后弹出置顶窗口，也可以通过 Bark 推送到 iPhone。

## 功能

- 图形界面启动，不需要命令行窗口
- 可设置轮询间隔
- 新单按 `task_id` 去重
- 每个新单单独弹窗
- 可选 Bark 推送
- 本地 SQLite 保存已提醒的订单 ID
- 支持通过 Fiddler 代理调试

## 使用前提

本项目不包含登录功能，也不包含任何个人凭证。使用者需要通过自己的个人登录流程获取一条 `/task/list` 的 Raw Request，并保存为：

```text
task_list_raw.txt
```

放在本项目目录下，或者在程序界面中选择它。

请勿把以下内容提交到 GitHub：

- `task_list_raw.txt`
- `paotui_seen.sqlite3`
- Bark key
- Cookie、Token、Session、OpenID 等个人登录信息

## 使用方法

1. 完成个人登录并从 Fiddler 导出 `/task/list` 请求。
2. 将请求保存为项目目录下的 `task_list_raw.txt`。
3. 双击 `校园集市跑腿提醒.pyw`。
4. 点击“测试一次”确认返回 `status=200`。
5. 设置轮询间隔并点击“开始监控”。

第一次轮询只建立当前大厅的基线，之后发现新的 `task_id` 才会提醒。

## Bark

在 Bark App 中获取自己的推送地址。在程序里勾选“同时推送到 Bark”，只填自己的 key 地址，例如：

```text
https://api.day.app/你的个人key
```

不要把真实 key 写入代码或提交到仓库。

## 依赖

```text
Python 3.9+
PyQt5
```

安装：

```powershell
pip install -r requirements.txt
```

## 安全与合规

本项目仅用于个人登录后、本人有权访问的数据提醒。请遵守平台用户协议和当地法律，控制轮询频率，不要分享个人凭证，不要抓取或公开他人的敏感信息。

项目不保证平台接口长期兼容。若请求失效，请重新完成个人登录并导出新的请求文件。
