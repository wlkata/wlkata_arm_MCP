# serial-mcp

Wlkata 机械臂 MCP 服务器。通过串口控制 Mirobot / E4 / MT4 / Haro380，为 Cursor 等 AI 客户端提供 MCP 工具接口。

[English README](./README.md)

## 功能概览

- 自动扫描串口并识别机械臂型号
- 连接状态查询（型号、端口、运行状态、是否可执行下一条指令）
- 指令串行化：上一条指令未完成前，拒绝新的运动/控制指令
- 运动类指令阻塞，直到机械臂回到 `Idle`
- 支持回零、Homing、笛卡尔/关节运动、夹爪、吸盘、G 代码等

## 支持的设备

| 型号 | 驱动类 | 说明 |
|------|--------|------|
| Mirobot | `Mirobot_UART` | 六轴桌面机械臂 |
| E4 / MT4 | `E4_UART` | E4 系列 |
| Haro380 | `Harobot_UART` | Haro380 系列 |

默认串口参数：**115200 8N1**

## 环境要求

- Python >= 3.12
- Windows / Linux / macOS
- 机械臂已通过 USB 连接，并被系统识别为串口（如 `COM8`、`/dev/ttyUSB0`）

## 安装

推荐使用 [uv](https://github.com/astral-sh/uv)：

```bash
git clone <your-repo-url>
cd serial-mcp
uv sync
```

或使用 pip：

```bash
pip install -e .
```

## Cursor MCP 配置

在项目或全局 MCP 配置中加入（路径请按实际情况修改）：

```json
{
  "mcpServers": {
    "wlkata": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "D:/WLKATA/serial-mcp",
        "main.py"
      ]
    }
  }
}
```

若直接使用虚拟环境：

```json
{
  "mcpServers": {
    "wlkata": {
      "command": "D:/WLKATA/serial-mcp/.venv/Scripts/python.exe",
      "args": ["D:/WLKATA/serial-mcp/main.py"]
    }
  }
}
```

保存配置后重启 Cursor，使 MCP 服务重新加载。

## 手动运行

```bash
uv run main.py
```

服务通过 **stdio** 与 MCP 客户端通信，日志输出到 **stderr**，请勿向 stdout 打印内容。

## 推荐操作流程

通过 AI 或脚本控制机械臂时，请严格按以下顺序操作：

```
1. robot_connection_tool        # 查看连接与就绪状态
2. robot_name_tool（如未连接）   # 扫描或指定端口连接
3. robot_connection_tool        # 确认 ready=true
4. 发送一条运动/控制指令         # 等待返回 "✅ OK"
5. robot_connection_tool        # 再次确认 ready=true
6. 下一条指令…
7. robot_serial_close_tool      # 任务结束，释放串口
```

### `robot_connection_tool` 返回字段

| 字段 | 含义 |
|------|------|
| `connected` | 是否已建立串口连接 |
| `model` | 机械臂型号 |
| `port` | 串口号 |
| `robot_state` | 运行状态，如 `Idle`、`Run` |
| `busy` | 是否有指令正在执行 |
| `current_command` | 当前执行的指令名 |
| `ready` | **是否可发送下一条指令**（已连接 + `Idle` + 非 busy） |
| `message` | 可读说明 |

**只有 `ready=true` 时，才应发送新的运动/控制指令。**

### 指令串行规则

- 运动/控制类工具在执行期间会占用命令锁
- 上一条未完成时，新指令返回：`Robot busy executing '...'`
- 急停可使用 `robot_stop_tool`，无需等待 `ready=true`
- 指令执行期间，部分查询工具会被拒绝，请用 `robot_connection_tool` 轮询

## 工具列表

### 连接与状态

| 工具 | 说明 |
|------|------|
| `robot_connection_tool` | **首选**：查询连接、型号、状态、是否就绪 |
| `robot_name_tool` | 扫描串口并连接；支持 `port`、`model`、`force_rescan` |
| `robot_serial_close_tool` | 关闭串口，释放连接 |

**`robot_name_tool` 参数：**

```json
{
  "force_rescan": false,
  "port": "COM8",
  "model": "Mirobot"
}
```

- `port`：指定串口
- `model`：跳过自动识别，直接按型号连接（`Mirobot` / `E4/MT4` / `Haro380`）
- `force_rescan`：强制断开并重扫

### 运动与控制

| 工具 | 说明 |
|------|------|
| `robot_zero_tool` | 回到零位（`G00 X0 Y0 Z0...`） |
| `robot_homing_tool` | Homing 回零（`o105=8`）；可选 `restart=true` |
| `robot_writecoordinate_tool` | 笛卡尔坐标运动（x, y, z, a, b, c） |
| `robot_writeangle_tool` | 关节角度运动（x, y, z, a, b, c） |
| `robot_writeexpand_tool` | 滑台/扩展轴 |
| `robot_sendMsg_tool` | 发送原始 G 代码或控制指令 |
| `robot_runFile_tool` | 执行离线程序文件 |
| `robot_stop_tool` | 急停/取消当前动作 |

### 末端执行器

| 工具 | 参数 | 说明 |
|------|------|------|
| `robot_gripper_tool` | `num` | 0=张开，1=闭合 |
| `robot_pump_tool` | `num` | 0=关闭，1=开启 |

### 查询

| 工具 | 说明 |
|------|------|
| `robot_get_State_tool` | 当前状态（Idle / Run） |
| `robot_get_Status_tool` | 完整状态字典 |
| `robot_getAngle_tool` | 指定轴角度（num: 0–5） |
| `robot_getcoordinate_tool` | 坐标分量（0=X, 1=Y, 2=Z, 3=RX, 4=RY, 5=RZ） |
| `robot_getpump_tool` | 吸盘状态 |
| `robot_getmode_tool` | 控制模式 |
| `robot_version_tool` | 固件版本（Haro380 不支持） |
| `robot_read_file_tool` | 读取离线程序（`program_id`，默认 `"110"`） |

### MCP Prompt

| Prompt | 说明 |
|--------|------|
| `robot_workflow_prompt` | 引导 AI 按安全流程操作机械臂 |

## 返回格式

- 成功：`✅ OK`
- 失败：`⚠️ error: ...`

常见错误：

| 错误信息 | 原因 | 处理 |
|----------|------|------|
| `Not connected` | 未连接 | 先调用 `robot_name_tool` |
| `Robot busy executing '...'` | 上一条指令未完成 | 等待或调用 `robot_stop_tool` |
| `Robot not idle (state: Run)` | 机械臂仍在运动 | 等待 `Idle` 后再发指令 |
| `Unable to read robot state` | 状态读取失败 | 见下方故障排查 |

## 使用示例

### 回零

```
robot_connection_tool()
→ ready=false, connected=false

robot_name_tool({ "port": "COM8", "model": "Mirobot" })
→ { "Mirobot": "COM8", "connected": true }

robot_connection_tool()
→ ready=true, robot_state="Idle"

robot_zero_tool()
→ ✅ OK
```

### Homing

```
robot_connection_tool()   # 确认 ready=true
robot_homing_tool({ "restart": false })
→ ✅ OK
```

## 故障排查

### 1. 扫描到 COM 口但自动识别失败

现象：

```json
{ "COM8": "unknown error: unknown device: ['[MSG: NVS ...]', 'ok']" }
```

说明：设备是 Wlkata 控制器，但 `$V` 未返回完整版本行。可强制指定型号：

```json
{ "port": "COM8", "model": "Mirobot", "force_rescan": true }
```

### 2. `robot_state` 为 `-1`

`-1` 表示**状态读取失败**，不是机械臂的真实状态。当前版本已改用可靠的 `?` 状态查询；若仍失败：

1. 确认 USB 连接与驱动
2. 关闭占用 COM 口的其他程序
3. 重新连接：`robot_name_tool({ "force_rescan": true, "port": "COM8" })`
4. 再次调用 `robot_connection_tool()`

### 3. 修改代码后未生效

需**重启 Cursor** 或重新加载 MCP 配置，才会加载新的 `main.py`。

### 4. 多指令冲突

不要并行调用多个运动工具。每次等待 `✅ OK`，且 `robot_connection_tool` 显示 `ready=true` 后再发下一条。

## 项目结构

```
serial-mcp/
├── main.py           # MCP 服务入口与全部工具定义
├── pyproject.toml    # 项目依赖
├── uv.lock           # 依赖锁定
├── README.md         # 英文文档
└── README.zh-CN.md   # 中文文档
```

## 依赖

- [FastMCP](https://github.com/jlowin/fastmcp) — MCP 服务器框架
- [pyserial](https://pythonhosted.org/pyserial/) — 串口通信
- [wlkatapython](https://pypi.org/project/wlkatapython/) — Wlkata 官方 Python 驱动

## 安全提示

- 发送运动指令前，确认工作空间内无障碍物
- 异常情况下优先使用 `robot_stop_tool`
- `robot_sendMsg_tool` 可发送任意 G 代码，请谨慎使用
- 测试时建议先小幅度移动，确认方向与坐标正确

## License

见项目仓库中的 LICENSE 文件（如有）。
