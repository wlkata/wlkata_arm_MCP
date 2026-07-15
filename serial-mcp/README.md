# serial-mcp

MCP server for Wlkata robot arms. Controls Mirobot / E4 / MT4 / Haro380 over serial, exposing tools for Cursor and other AI clients.

[中文文档](./README.zh-CN.md)

## Features

- Auto-scan serial ports and identify robot model
- Connection status queries (model, port, runtime state, readiness for next command)
- Command serialization: reject new motion/control commands until the previous one finishes
- Motion commands block until the robot returns to `Idle`
- Zero position, homing, Cartesian/joint motion, gripper, vacuum pump, G-code, and more

## Supported Devices

| Model | Driver | Description |
|-------|--------|-------------|
| Mirobot | `Mirobot_UART` | 6-axis desktop arm |
| E4 / MT4 | `E4_UART` | E4 series |
| Haro380 | `Harobot_UART` | Haro380 series |

Default serial settings: **115200 8N1**

## Requirements

- Python >= 3.12
- Windows / Linux / macOS
- Robot connected via USB and visible as a serial port (e.g. `COM8`, `/dev/ttyUSB0`)

## Installation

Recommended with [uv](https://github.com/astral-sh/uv):

```bash
git clone <your-repo-url>
cd serial-mcp
uv sync
```

Or with pip:

```bash
pip install -e .
```

## Cursor MCP Configuration

Add to your project or global MCP config (adjust paths as needed):

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

If using a virtual environment directly:

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

Restart Cursor after saving the config so the MCP server reloads.

## Manual Run

```bash
uv run main.py
```

The server communicates over **stdio** with MCP clients. Logs go to **stderr** — do not print to stdout.

## Recommended Workflow

When controlling the robot via AI or scripts, follow this order:

```
1. robot_connection_tool        # Check connection and readiness
2. robot_name_tool (if needed)  # Scan or connect to a specific port
3. robot_connection_tool        # Confirm ready=true
4. Send ONE motion/control cmd  # Wait for "✅ OK"
5. robot_connection_tool        # Confirm ready=true again
6. Next command…
7. robot_serial_close_tool      # Release the port when done
```

### `robot_connection_tool` Response Fields

| Field | Meaning |
|-------|---------|
| `connected` | Serial link established |
| `model` | Robot model |
| `port` | Serial port name |
| `robot_state` | Runtime state, e.g. `Idle`, `Run` |
| `busy` | A command is currently executing |
| `current_command` | Name of the running command |
| `ready` | **Safe to send the next command** (connected + `Idle` + not busy) |
| `message` | Human-readable summary |

**Only send new motion/control commands when `ready=true`.**

### Command Serialization Rules

- Motion/control tools hold a command lock for their full duration
- If a command is still running, new ones return: `Robot busy executing '...'`
- Emergency stop: use `robot_stop_tool` (no need to wait for `ready=true`)
- Some query tools are blocked while a command runs; poll with `robot_connection_tool` instead

## Tools

### Connection & Status

| Tool | Description |
|------|-------------|
| `robot_connection_tool` | **Start here**: connection, model, state, readiness |
| `robot_name_tool` | Scan/connect; supports `port`, `model`, `force_rescan` |
| `robot_serial_close_tool` | Close serial port and release connection |

**`robot_name_tool` parameters:**

```json
{
  "force_rescan": false,
  "port": "COM8",
  "model": "Mirobot"
}
```

- `port`: target serial port
- `model`: skip auto-detection (`Mirobot` / `E4/MT4` / `Haro380`)
- `force_rescan`: disconnect and rescan

### Motion & Control

| Tool | Description |
|------|-------------|
| `robot_zero_tool` | Move to zero position (`G00 X0 Y0 Z0...`) |
| `robot_homing_tool` | Homing sequence (`o105=8`); optional `restart=true` |
| `robot_writecoordinate_tool` | Cartesian move (x, y, z, a, b, c) |
| `robot_writeangle_tool` | Joint angle move (x, y, z, a, b, c) |
| `robot_writeexpand_tool` | Linear rail / extension axis |
| `robot_sendMsg_tool` | Send raw G-code or control commands |
| `robot_runFile_tool` | Run an offline program file |
| `robot_stop_tool` | Emergency stop / cancel current motion |

### End Effectors

| Tool | Parameter | Description |
|------|-----------|-------------|
| `robot_gripper_tool` | `num` | 0=open, 1=close |
| `robot_pump_tool` | `num` | 0=off, 1=on |

### Queries

| Tool | Description |
|------|-------------|
| `robot_get_State_tool` | Current state (Idle / Run) |
| `robot_get_Status_tool` | Full status dictionary |
| `robot_getAngle_tool` | Axis angle (num: 0–5) |
| `robot_getcoordinate_tool` | Coordinate component (0=X, 1=Y, 2=Z, 3=RX, 4=RY, 5=RZ) |
| `robot_getpump_tool` | Vacuum pump state |
| `robot_getmode_tool` | Control mode |
| `robot_version_tool` | Firmware version (not supported on Haro380) |
| `robot_read_file_tool` | Read offline program (`program_id`, default `"110"`) |

### MCP Prompt

| Prompt | Description |
|--------|-------------|
| `robot_workflow_prompt` | Guides the AI through safe robot operation |

## Response Format

- Success: `✅ OK`
- Failure: `⚠️ error: ...`

Common errors:

| Message | Cause | Action |
|---------|-------|--------|
| `Not connected` | No serial connection | Call `robot_name_tool` first |
| `Robot busy executing '...'` | Previous command still running | Wait or call `robot_stop_tool` |
| `Robot not idle (state: Run)` | Robot still moving | Wait for `Idle` before next command |
| `Unable to read robot state` | State query failed | See troubleshooting below |

## Examples

### Move to Zero

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
robot_connection_tool()   # confirm ready=true
robot_homing_tool({ "restart": false })
→ ✅ OK
```

## Troubleshooting

### 1. Port found but auto-detection fails

Symptom:

```json
{ "COM8": "unknown error: unknown device: ['[MSG: NVS ...]', 'ok']" }
```

The device is a Wlkata controller, but `$V` did not return full version lines. Force the model:

```json
{ "port": "COM8", "model": "Mirobot", "force_rescan": true }
```

### 2. `robot_state` is `-1`

`-1` means **state read failed**, not the robot's actual state. The server uses a reliable `?` status query; if it still fails:

1. Check USB cable and drivers
2. Close other apps using the COM port
3. Reconnect: `robot_name_tool({ "force_rescan": true, "port": "COM8" })`
4. Call `robot_connection_tool()` again

### 3. Code changes not applied

Restart Cursor or reload MCP config so `main.py` is picked up again.

### 4. Overlapping commands

Do not call multiple motion tools in parallel. Wait for `✅ OK` and `ready=true` from `robot_connection_tool` before each new command.

## Project Structure

```
serial-mcp/
├── main.py           # MCP server entry point and tool definitions
├── pyproject.toml    # Project dependencies
├── uv.lock           # Locked dependencies
├── README.md         # English documentation
└── README.zh-CN.md   # Chinese documentation
```

## Dependencies

- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework
- [pyserial](https://pythonhosted.org/pyserial/) — Serial communication
- [wlkatapython](https://pypi.org/project/wlkatapython/) — Official Wlkata Python driver

## Safety

- Clear the workspace before sending motion commands
- Use `robot_stop_tool` in abnormal situations
- `robot_sendMsg_tool` accepts arbitrary G-code — use with care
- Test with small moves first to verify direction and coordinates

## License

See the LICENSE file in the repository (if present).
