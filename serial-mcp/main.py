import atexit
import asyncio
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar, Union

import serial
import wlkatapython
from mcp.server.fastmcp import FastMCP

# Log to stderr so stdio MCP transport is not corrupted
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

IDLE_TIMEOUT = 120.0
HOMING_IDLE_TIMEOUT = 180.0
ERROR_PREFIX = "⚠️ error: "
OK = "✅ OK"

T = TypeVar("T")

mcp = FastMCP("wlkata_Mirobot_server", dependencies=["pyserial", "wlkatapython"])

ROBOT_DRIVERS = {
    "Mirobot": wlkatapython.Mirobot_UART,
    "E4/MT4": wlkatapython.E4_UART,
    "Haro380": wlkatapython.Harobot_UART,
}

WLKATA_STATUS_PATTERN = re.compile(
    r"^<\w+,Angle\(ABCDXYZ\):[\d.-]+,.*Cartesian coordinate\(XYZ RxRyRz\):"
)


class WlkataMirobotServer:
    def __init__(self):
        self.wlkata_robot = None
        self.serial1 = None
        self.robot_model: str | None = None
        self._io_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._current_command: str | None = None

    def is_connected(self) -> bool:
        return (
            self.wlkata_robot is not None
            and self.serial1 is not None
            and self.serial1.is_open
        )

    @staticmethod
    def _connection_error() -> str:
        return (
            f"{ERROR_PREFIX}Not connected. "
            "Call robot_connection_tool to check status, then robot_name_tool to connect."
        )

    @staticmethod
    def _busy_error() -> str:
        return (
            f"{ERROR_PREFIX}Robot busy executing a previous command. "
            "Wait for it to finish, poll robot_connection_tool, or call robot_stop_tool."
        )

    @staticmethod
    def _is_error(value) -> bool:
        return isinstance(value, str) and value.startswith(ERROR_PREFIX)

    @staticmethod
    def _identify_model(text) -> str | None:
        text_str = str(text)
        if "Mirobot" in text_str:
            return "Mirobot"
        if "Haro380" in text_str:
            return "Haro380"
        if "E4" in text_str or "MT4" in text_str:
            return "E4/MT4"
        if text_str.startswith("EXbox") or "EXbox" in text_str:
            return "Mirobot"
        return None

    def _drain_serial(self, ser: serial.Serial, duration: float = 0.5) -> None:
        deadline = time.time() + duration
        while time.time() < deadline:
            if ser.in_waiting:
                ser.readline()
            else:
                time.sleep(0.05)

    def _read_serial_lines(
        self,
        ser: serial.Serial,
        max_lines: int = 20,
        timeout: float = 5.0,
    ) -> list[str]:
        lines: list[str] = []
        deadline = time.time() + timeout
        while time.time() < deadline and len(lines) < max_lines:
            if ser.in_waiting:
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if line:
                    lines.append(line)
            else:
                time.sleep(0.05)
        return lines

    @staticmethod
    def _identify_from_status_line(line: str) -> str | None:
        if "Haro380" in line:
            return "Haro380"
        if WLKATA_STATUS_PATTERN.match(line):
            return "Mirobot"
        if line.startswith("<") and "Angle(ABCDXYZ)" in line:
            return "Mirobot"
        if line.startswith("<") and ("E4" in line or "MT4" in line):
            return "E4/MT4"
        return None

    def _probe_version(self, probe, ser: serial.Serial) -> tuple[str | None, list[str]]:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        probe.sendMsg("$V")

        all_lines: list[str] = []
        for _ in range(8):
            batch = self._read_serial_lines(ser, max_lines=4, timeout=1.5)
            all_lines.extend(batch)
            for line in batch:
                model = self._identify_model(line)
                if model:
                    return model, all_lines
            model = self._identify_model(" ".join(batch))
            if model:
                return model, all_lines
            time.sleep(0.1)

        return None, all_lines

    def _probe_status(self, probe, ser: serial.Serial) -> tuple[str | None, list[str]]:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        probe.sendMsg("?")
        lines = self._read_serial_lines(ser, max_lines=5, timeout=2.0)
        for line in lines:
            model = self._identify_from_status_line(line)
            if model:
                return model, lines
            model = self._identify_model(line)
            if model:
                return model, lines
        return None, lines

    def _probe_port(self, device: str) -> tuple[str | None, str | None]:
        try:
            with serial.Serial(device, 115200, timeout=1) as test_port:
                test_port.reset_input_buffer()
                test_port.reset_output_buffer()
                time.sleep(0.3)
                self._drain_serial(test_port, duration=1.0)

                probe = wlkatapython.Wlkata_UART(False, True)
                probe.init(test_port, -1)

                model, version_lines = self._probe_version(probe, test_port)
                if model:
                    logger.info("Identified %s on %s via $V", model, device)
                    return model, None

                model, status_lines = self._probe_status(probe, test_port)
                if model:
                    logger.info("Identified %s on %s via status query", model, device)
                    return model, None

                # $V returning "ok" is a Wlkata controller ack; default to Mirobot if status also fails
                if "ok" in version_lines:
                    logger.info("Assuming Mirobot on %s ($V returned ok)", device)
                    return "Mirobot", None

                all_lines = version_lines + status_lines
                return None, f"unknown device: {all_lines if all_lines else 'no response'}"
        except Exception as e:
            return None, str(e)

    def _open_connection(self, device: str, model: str) -> None:
        self.serial1 = serial.Serial(device, 115200, timeout=2.0)
        driver_cls = ROBOT_DRIVERS[model]
        self.wlkata_robot = driver_cls(False, False)
        self.wlkata_robot.init(self.serial1, -1)
        self.robot_model = model
        time.sleep(0.3)
        self._drain_serial(self.serial1, duration=0.5)

    def _close_connection(self) -> None:
        if self.serial1 and self.serial1.is_open:
            self.serial1.close()
        self.serial1 = None
        self.wlkata_robot = None
        self.robot_model = None

    @staticmethod
    def _parse_state_line(line: str) -> str | None:
        if line.startswith("<") and "," in line:
            return line[1:].split(",", 1)[0].strip()
        match = re.match(r"^<(\w+),", line)
        if match:
            return match.group(1)
        return None

    def _query_state_sync(self, attempts: int = 3) -> str:
        if not self.is_connected():
            return "error"
        for _ in range(attempts):
            try:
                self.serial1.reset_input_buffer()
                self.serial1.reset_output_buffer()
                self.serial1.write(b"?\r\n")
                time.sleep(0.2)
                deadline = time.time() + 2.5
                while time.time() < deadline:
                    line = self.serial1.readline().decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    state = self._parse_state_line(line)
                    if state:
                        return state
            except Exception as exc:
                logger.warning("State query failed: %s", exc)
            time.sleep(0.15)
        return "-1"

    async def _get_robot_state(self, retries: int = 5) -> str:
        if not self.is_connected():
            return "error"
        async with self._io_lock:
            for attempt in range(retries):
                state = await asyncio.to_thread(self._query_state_sync, 2)
                if state not in ("-1", "error", ""):
                    return state
                if attempt < retries - 1:
                    await asyncio.sleep(0.25)
        return "-1"

    @staticmethod
    def _is_valid_state(state: str) -> bool:
        return state not in ("-1", "error", "", " ")

    @staticmethod
    def _state_read_error() -> str:
        return (
            f"{ERROR_PREFIX}Unable to read robot state from serial. "
            "Try robot_name_tool with force_rescan=true, or check the USB cable."
        )

    async def _run_method(
        self,
        method_name: str,
        *args,
        require_connection: bool = True,
        **kwargs,
    ) -> T | str:
        if require_connection and not self.is_connected():
            return self._connection_error()
        async with self._io_lock:
            if require_connection and not self.is_connected():
                return self._connection_error()
            try:
                fn = getattr(self.wlkata_robot, method_name)
                return await asyncio.to_thread(fn, *args, **kwargs)
            except Exception as e:
                logger.exception("Robot operation failed")
                return f"{ERROR_PREFIX}{e}"

    async def _wait_until_idle(
        self,
        timeout: float = IDLE_TIMEOUT,
        poll_interval: float = 1.0,
    ) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        last_state = "-1"
        while True:
            state = await self._get_robot_state(retries=3)
            last_state = state
            if state == "Idle":
                return OK
            if self._is_error(state):
                return state
            if not self._is_valid_state(state):
                if asyncio.get_running_loop().time() >= deadline:
                    return self._state_read_error()
            elif asyncio.get_running_loop().time() >= deadline:
                return f"{ERROR_PREFIX}Timeout waiting for Idle (last state: {last_state})"
            await asyncio.sleep(poll_interval)

    @asynccontextmanager
    async def _command_session(self, command_name: str, *, exclusive: bool = True):
        if exclusive and self._command_lock.locked():
            raise CommandBusyError(self._current_command or "unknown")

        async with self._command_lock:
            self._current_command = command_name
            try:
                yield
            finally:
                self._current_command = None

    async def _execute_command(
        self,
        command_name: str,
        action: Callable[[], Awaitable[Any]],
        *,
        wait_idle: bool = False,
        require_idle: bool = True,
        exclusive: bool = True,
    ) -> Any:
        try:
            async with self._command_session(command_name, exclusive=exclusive):
                if not self.is_connected():
                    return self._connection_error()

                if require_idle:
                    state = await self._get_robot_state()
                    if not self._is_valid_state(state):
                        return self._state_read_error()
                    if self._is_error(state):
                        return state
                    if state != "Idle":
                        return (
                            f"{ERROR_PREFIX}Robot not idle (state: {state}). "
                            "Wait for the current motion to finish or call robot_stop_tool."
                        )

                result = await action()
                if self._is_error(result):
                    return result

                if wait_idle:
                    idle_result = await self._wait_until_idle()
                    if self._is_error(idle_result):
                        return idle_result
                    return OK

                if result is None:
                    return OK
                return result
        except CommandBusyError as exc:
            return (
                f"{ERROR_PREFIX}Robot busy executing '{exc.command}'. "
                "Wait for completion, poll robot_connection_tool, or call robot_stop_tool."
            )

    async def get_connection_status(self) -> dict:
        info: dict[str, Any] = {
            "connected": self.is_connected(),
            "model": self.robot_model,
            "port": self.serial1.port if self.is_connected() else None,
            "busy": self._command_lock.locked(),
            "current_command": self._current_command,
            "robot_state": None,
            "ready": False,
            "message": "",
        }

        if not info["connected"]:
            info["message"] = (
                "Not connected. Call robot_name_tool to scan/connect before motion commands."
            )
            return info

        if info["busy"]:
            info["message"] = (
                f"Connected ({info['model']} on {info['port']}). "
                f"Busy executing: {info['current_command']}"
            )
            return info

        state = await self._get_robot_state()
        if not self._is_valid_state(state):
            info["robot_state"] = None
            info["state_read_failed"] = True
            info["message"] = (
                f"Connected ({info['model']} on {info['port']}) but cannot read robot state. "
                "Check serial link or call robot_name_tool with force_rescan=true."
            )
            return info

        if self._is_error(state):
            info["robot_state"] = state
            info["message"] = f"Connected but failed to read state: {state}"
            return info

        info["robot_state"] = state
        info["ready"] = state == "Idle"
        if info["ready"]:
            info["message"] = (
                f"Ready. {info['model']} on {info['port']}, state=Idle. "
                "Safe to send the next command."
            )
        else:
            info["message"] = (
                f"Connected ({info['model']} on {info['port']}) but state={state}. "
                "Wait until Idle before sending a new command."
            )
        return info

    async def get_robot_name(
        self,
        force_rescan: bool = False,
        port: str | None = None,
        model: str | None = None,
    ) -> dict:
        if self._command_lock.locked():
            return {
                "error": (
                    f"Robot busy executing '{self._current_command}'. "
                    "Wait for completion before reconnecting."
                ),
                "busy": True,
                "current_command": self._current_command,
            }

        if not force_rescan and self.is_connected():
            return {
                "connected": self.serial1.port,
                "model": self.robot_model,
            }

        async with self._command_lock:
            if force_rescan:
                self._close_connection()

            if port and model:
                if model not in ROBOT_DRIVERS:
                    return {
                        "error": f"Unknown model: {model}. Valid: {list(ROBOT_DRIVERS.keys())}"
                    }
                try:
                    self._open_connection(port, model)
                    return {model: port, "connected": True}
                except Exception as e:
                    self._close_connection()
                    return {f"{model}_init_error": str(e)}

            if port:
                model, err = await asyncio.to_thread(self._probe_port, port)
                if model is None:
                    return {port: err or "probe failed"}
                try:
                    self._open_connection(port, model)
                    return {model: port, "connected": True}
                except Exception as e:
                    self._close_connection()
                    return {f"{model}_init_error": str(e)}

            port_list: dict = {}
            for entry in serial.tools.list_ports.comports():
                model, err = await asyncio.to_thread(self._probe_port, entry.device)
                if model:
                    port_list[model] = entry.device
                    break
                if err:
                    port_list[entry.device] = f"unknown error: {err}"

            for model in ("Mirobot", "E4/MT4", "Haro380"):
                if model not in port_list:
                    continue
                try:
                    self._open_connection(port_list[model], model)
                    port_list["connected"] = True
                except Exception as e:
                    self._close_connection()
                    port_list[f"{model}_init_error"] = str(e)
                break

            return port_list

    async def robot_reset_homing(self, restart: bool = False) -> str:
        async def action() -> None:
            if restart:
                result = await self._run_method("restart")
                if self._is_error(result):
                    raise CommandFailedError(result)
                await asyncio.sleep(2)
            result = await self._run_method("homing")
            if self._is_error(result):
                raise CommandFailedError(result)
            await asyncio.sleep(1)

        try:
            async with self._command_session("homing"):
                if not self.is_connected():
                    return self._connection_error()
                state = await self._get_robot_state()
                if not self._is_valid_state(state):
                    return self._state_read_error()
                if self._is_error(state):
                    return state
                if state != "Idle":
                    return (
                        f"{ERROR_PREFIX}Robot not idle (state: {state}). "
                        "Wait or call robot_stop_tool before homing."
                    )
                await action()
                return await self._wait_until_idle(
                    timeout=HOMING_IDLE_TIMEOUT, poll_interval=3
                )
        except CommandBusyError as exc:
            return (
                f"{ERROR_PREFIX}Robot busy executing '{exc.command}'. "
                "Wait for completion or call robot_stop_tool."
            )
        except CommandFailedError as exc:
            return str(exc)

    async def robot_sendMsg(self, num: str) -> str:
        async def action() -> None:
            result = await self._run_method("sendMsg", num)
            if self._is_error(result):
                raise CommandFailedError(result)
            await asyncio.sleep(1)

        return await self._execute_command("sendMsg", action, wait_idle=True)

    async def robot_runFile(self, file_path: str) -> str:
        async def action() -> None:
            result = await self._run_method("runFile", file_path)
            if self._is_error(result):
                raise CommandFailedError(result)
            await asyncio.sleep(1)

        return await self._execute_command("runFile", action, wait_idle=True)

    async def robot_stop(self) -> str:
        if not self.is_connected():
            return self._connection_error()
        result = await self._run_method("cancellation")
        return OK if not self._is_error(result) else result

    async def robot_gripper(self, num: int) -> str:
        return await self._execute_command(
            "gripper",
            lambda: self._run_method("gripper", num),
            wait_idle=True,
        )

    async def robot_pump(self, num: int) -> str:
        return await self._execute_command(
            "pump",
            lambda: self._run_method("pump", num),
            wait_idle=True,
        )

    async def robot_zero(self) -> str:
        return await self._execute_command(
            "zero",
            lambda: self._run_method("zero"),
            wait_idle=True,
        )

    async def robot_writecoordinate(
        self, x: float, y: float, z: float, a: float, b: float, c: float
    ) -> str:
        return await self._execute_command(
            "writecoordinate",
            lambda: self._run_method("writecoordinate", 0, 0, x, y, z, a, b, c),
            wait_idle=True,
        )

    async def robot_writeangle(
        self, x: float, y: float, z: float, a: float, b: float, c: float
    ) -> str:
        return await self._execute_command(
            "writeangle",
            lambda: self._run_method("writeangle", 0, x, y, z, a, b, c),
            wait_idle=True,
        )

    async def robot_writeexpand(self, num: int) -> str:
        return await self._execute_command(
            "writeexpand",
            lambda: self._run_method("writeexpand", 0, 0, num),
            wait_idle=True,
        )

    async def robot_restart(self) -> str:
        async def action() -> None:
            result = await self._run_method("restart")
            if self._is_error(result):
                raise CommandFailedError(result)
            await asyncio.sleep(3)

        return await self._execute_command(
            "restart", action, require_idle=False, wait_idle=False
        )

    async def robot_version(self) -> Union[tuple, str]:
        return await self._execute_command(
            "version",
            lambda: self._run_method("version"),
            require_idle=False,
            wait_idle=False,
        )

    async def robot_get_State(self) -> str:
        if self._command_lock.locked():
            return (
                f"{ERROR_PREFIX}Cannot query state while '{self._current_command}' "
                "is running. Use robot_connection_tool instead."
            )
        state = await self._get_robot_state()
        if not self._is_valid_state(state):
            return self._state_read_error()
        return state

    async def robot_get_Status(self) -> Union[dict, str]:
        if self._command_lock.locked():
            return (
                f"{ERROR_PREFIX}Cannot query status while '{self._current_command}' "
                "is running. Use robot_connection_tool instead."
            )
        return await self._run_method("getStatus")

    async def robot_getAngle(self, num: int) -> Union[float, str]:
        if self._command_lock.locked():
            return self._busy_error()
        return await self._run_method("getAngle", num)

    async def robot_getcoordinate(self, num: int) -> Union[float, str]:
        if self._command_lock.locked():
            return self._busy_error()
        return await self._run_method("getcoordinate", num)

    async def robot_getpump(self) -> str:
        if self._command_lock.locked():
            return self._busy_error()
        return await self._run_method("getpump")

    async def robot_getmode(self) -> str:
        if self._command_lock.locked():
            return self._busy_error()
        return await self._run_method("getmode")

    async def robot_read_file(self, program_id: str = "110") -> str:
        command = f"o{program_id}"

        async def action() -> str:
            result = await self._run_method("sendMsg", command)
            if self._is_error(result):
                raise CommandFailedError(result)
            content = await self._run_method("read_message")
            if self._is_error(content):
                raise CommandFailedError(content)
            logger.info("Read offline program %s", program_id)
            return content

        return await self._execute_command(
            "read_file", action, require_idle=False, wait_idle=False
        )

    async def robot_serial_close(self) -> str:
        if self._command_lock.locked():
            return (
                f"{ERROR_PREFIX}Cannot close serial while '{self._current_command}' "
                "is running. Wait for completion or call robot_stop_tool."
            )
        async with self._command_lock:
            if self.serial1 and self.serial1.is_open:
                self._close_connection()
                return "serial close"
            self._close_connection()
            return "serial already closed"


class CommandBusyError(Exception):
    def __init__(self, command: str):
        self.command = command
        super().__init__(command)


class CommandFailedError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


robot = WlkataMirobotServer()


def _cleanup() -> None:
    if robot.serial1 and robot.serial1.is_open:
        robot._close_connection()
        logger.info("Serial port closed on exit")


atexit.register(_cleanup)


@mcp.prompt()
def robot_workflow_prompt() -> str:
    """Guide the agent through safe Wlkata robot arm operation."""
    return """You control a Wlkata robot arm via MCP tools. Follow this workflow strictly:

1. ALWAYS call robot_connection_tool first to check:
   - connected: is the serial link up?
   - model: which arm (Mirobot / E4/MT4 / Haro380)?
   - ready: connected AND state=Idle AND not busy?

2. If not connected, call robot_name_tool (optionally with port/model), then robot_connection_tool again.

3. Only when ready=true, send ONE motion/control command at a time.
   Commands are serialized: if busy=true, do NOT send another command.
   Poll robot_connection_tool until ready=true.

4. Tool selection by task:
   - Return to zero position: robot_zero_tool
   - Full homing sequence: robot_homing_tool
   - Cartesian move: robot_writecoordinate_tool
   - Joint move: robot_writeangle_tool
   - Emergency abort: robot_stop_tool

5. Each motion tool blocks until the robot reaches Idle before returning.
   Wait for "✅ OK" before sending the next command.

6. When finished, call robot_serial_close_tool to release the port.
"""


@mcp.tool()
async def robot_connection_tool() -> dict:
    """
    Check robot connection status. ALWAYS call this before any motion command.
    Returns: connected, model, port, robot_state, busy, current_command, ready, message.
    Only proceed when ready=true (connected, Idle, and not busy).
    """
    return await robot.get_connection_status()


@mcp.tool()
async def robot_name_tool(
    force_rescan: bool = False,
    port: str | None = None,
    model: str | None = None,
) -> dict:
    """
    Scan/connect to the robot. Call robot_connection_tool afterward to verify ready=true.
    Returns a dict: keys are typically device models (e.g. Mirobot/E4/MT4/Haro380), values are port names.
    If already connected, returns the current connection port info directly unless force_rescan is True.
    Optional port: connect to a specific serial port (e.g. COM3, /dev/ttyUSB0).
    Optional model: skip auto-detection and connect with a known model (e.g. port="COM8", model="Mirobot").
    """
    return await robot.get_robot_name(force_rescan=force_rescan, port=port, model=model)


@mcp.tool()
async def robot_homing_tool(restart: bool = False) -> str:
    """
    Perform homing on the robot.
    Prerequisite: robot_connection_tool shows ready=true.
    Blocks until homing completes. Rejects if another command is running.
    Set restart=True to restart the controller before homing (disrupts any running task).
    Returns "✅ OK" on success, otherwise an error message string.
    """
    return await robot.robot_reset_homing(restart=restart)


@mcp.tool()
async def robot_sendMsg_tool(num: str) -> str:
    """
    Send raw G-code or control commands to the robot.
    Prerequisite: robot_connection_tool shows ready=true.
    Blocks until the robot is Idle. Rejects if another command is running.
    Parameter num: e.g. "$H", "G0 X150 Y0 Z80".
    Returns "✅ OK" when the command completes, otherwise an error message.
    """
    return await robot.robot_sendMsg(num)


@mcp.tool()
async def robot_runFile_tool(file_path: str) -> str:
    """
    Run an offline program file on the robot.
    Parameter file_path: name of the offline program.
    Returns "✅ OK" when execution completes, otherwise an error message.
    """
    return await robot.robot_runFile(file_path)


@mcp.tool()
async def robot_stop_tool() -> str:
    """
    Immediately cancel the current robot motion (emergency stop / abort current task).
    Can be called while another command is running. Does not require ready=true.
    Returns "✅ OK" or an error message.
    """
    return await robot.robot_stop()


@mcp.tool()
async def robot_gripper_tool(num: int) -> str:
    """
    Control gripper open/close state.
    Parameter num: 0=open, 1=close (per wlkatapython driver convention).
    Returns "✅ OK" or an error message.
    """
    return await robot.robot_gripper(num)


@mcp.tool()
async def robot_pump_tool(num: int) -> str:
    """
    Control vacuum pump (suction cup) on/off state.
    Parameter num: 0=off, 1=on (per wlkatapython driver convention).
    Returns "✅ OK" or an error message.
    """
    return await robot.robot_pump(num)


@mcp.tool()
async def robot_zero_tool() -> str:
    """
    Move the robot back to the initial zero position.
    Unlike homing, this returns to the configured start position.
    Prerequisite: robot_connection_tool shows ready=true.
    Blocks until motion completes. Rejects if another command is running.
    Returns "✅ OK" or an error message.
    """
    return await robot.robot_zero()


@mcp.tool()
async def robot_writecoordinate_tool(
    x: float, y: float, z: float, a: float, b: float, c: float
) -> str:
    """
    Move the robot using Cartesian coordinates.
    Parameters x/y/z/a/b/c: target pose (units and meaning follow device protocol).
    Returns "✅ OK" or an error message.
    """
    return await robot.robot_writecoordinate(x, y, z, a, b, c)


@mcp.tool()
async def robot_writeangle_tool(
    x: float, y: float, z: float, a: float, b: float, c: float
) -> str:
    """
    Move the robot using joint angles.
    Parameters x/y/z/a/b/c: target angle for each axis (per device axis definition).
    Returns "✅ OK" or an error message.
    """
    return await robot.robot_writeangle(x, y, z, a, b, c)


@mcp.tool()
async def robot_writeexpand_tool(num: int) -> str:
    """
    Control linear rail (extension axis) movement.
    Parameter num: target position or displacement value (per device protocol).
    Returns "✅ OK" or an error message.
    """
    return await robot.robot_writeexpand(num)


@mcp.tool()
async def robot_restart_tool() -> str:
    """
    Restart the robot controller.
    After restart, query status again and run homing if needed.
    Returns "✅ OK" or an error message.
    """
    return await robot.robot_restart()


@mcp.tool()
async def robot_version_tool() -> Union[tuple, str]:
    """
    Get firmware/device version info. Not supported on Haro380.
    Returns a version tuple on success, or an error message string on failure.
    """
    return await robot.robot_version()


@mcp.tool()
async def robot_get_State_tool() -> str:
    """
    Get the robot's current runtime state (e.g. Idle, Run).
    Prefer robot_connection_tool for pre-flight checks.
    Blocked while a motion command is executing; use robot_connection_tool instead.
    """
    return await robot.robot_get_State()


@mcp.tool()
async def robot_get_Status_tool() -> Union[dict, str]:
    """
    Get full robot status information.
    Returns a status dict on success, or an error message string on failure.
    """
    return await robot.robot_get_Status()


@mcp.tool()
async def robot_getAngle_tool(num: int) -> str:
    """
    Query the current angle of a specified axis.
    Parameter num: axis index (0-5 for six axes).
    Returns the angle value on success, or an error message string on failure.
    """
    result = await robot.robot_getAngle(num)
    return str(result) if not isinstance(result, str) else result


@mcp.tool()
async def robot_getcoordinate_tool(num: int) -> str:
    """
    Query a specified coordinate component value.
    Parameter num: coordinate index (0=X, 1=Y, 2=Z, 3=RX, 4=RY, 5=RZ per device protocol).
    Returns the numeric value on success, or an error message string on failure.
    """
    result = await robot.robot_getcoordinate(num)
    return str(result) if not isinstance(result, str) else result


@mcp.tool()
async def robot_getpump_tool() -> str:
    """
    Get current vacuum pump (suction cup) state.
    Commonly used to verify grasp state before and after tasks.
    """
    return await robot.robot_getpump()


@mcp.tool()
async def robot_getmode_tool() -> str:
    """
    Get the robot's current control mode.
    Useful to confirm the robot is ready to accept commands.
    """
    return await robot.robot_getmode()


@mcp.tool()
async def robot_read_file_tool(program_id: str = "110") -> str:
    """
    Read offline program content from the controller (raw protocol response).
    Parameter program_id: offline program slot number (default "110").
    Returns program text or raw response on success, or an error message on failure.
    """
    return await robot.robot_read_file(program_id)


@mcp.tool()
async def robot_serial_close_tool() -> str:
    """
    Close the serial port and release the robot object.
    Call after a task session to avoid holding the device connection.
    """
    return await robot.robot_serial_close()


if __name__ == "__main__":
    mcp.run(transport="stdio")
