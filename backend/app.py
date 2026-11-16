"""应用编排（文档 2 章流程 / 5 章 Console-App-Core）。

状态机：
    idle → 检测端口 → connected_no_fw（缺组件）→ 备份 → 编译 → 烧录 → ready
                                                        └→ 回滚 → connected_no_fw

硬规则：注入模式必须先有整片备份；原样烧录（raw）不要求备份，但会先整片擦除。
"""

from __future__ import annotations

import json
import threading
import time

import cancel_token
from pathlib import Path

import build_env_checker
import firmware_backup_manager as backup_mod
import flash_controller
import idf_build_scheduler
import project_importer
import serial_data_parser
import serial_detect_handshake as handshake
import serial_utils
import source_injector

APP_ROOT = Path(__file__).resolve().parent.parent
FRONTEND = APP_ROOT / "frontend-ui"


class ConsoleApp:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.logs: list[str] = []
        self.parser = serial_data_parser.SerialDataParser(
            on_frame=self._on_frame, on_telemetry=self._on_telemetry)
        self.link: handshake.ConsoleLink | None = None

        self.state: dict = {
            "status": "idle",           # idle|no_device|connecting|connected_no_fw|ready|busy|error
            "port": "",
            "chip_id": "",
            "fw": "",
            "components": [],
            "report_port": "",
            "log_suppressed": False,
            "fps": 0.0,
            "frame_seq": 0,
            "led": None,
            "key": None,
            "error": "",
            "hint": "",
            "busy": "",
            "progress": 0.0,
            "progress_text": "",
            "backup": None,
            "adapters": [],
            "last_flash": None,
            "purge_at": None,          # 拔板满 1 小时自动删备份的时间点（UI 用来提示）
        }
        self._frame_times: list[float] = []
        self._last_frame: bytes | None = None

    # -------------------------------------------------------------- 日志与状态

    def log(self, msg: str) -> None:
        with self.lock:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(self.logs) > 300:
                self.logs = self.logs[-300:]

    def set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def snapshot(self) -> dict:
        with self.lock:
            s = dict(self.state)
            s["logs"] = self.logs[-40:]
            s["has_backup"] = bool(self.state.get("chip_id") and
                                   backup_mod.has_backup(self.state["chip_id"]))
            return s

    # -------------------------------------------------------------- 串口回调

    def _on_frame(self, fb: bytes) -> None:
        now = time.time()
        self._frame_times.append(now)
        self._frame_times = [t for t in self._frame_times if now - t < 2.0]
        with self.lock:
            self._last_frame = fb
            self.state["frame_seq"] += 1
            self.state["fps"] = round(len(self._frame_times) / 2.0, 1)

    def _on_telemetry(self, obj: dict) -> None:
        kind = obj.get("type", "")
        data = obj.get("data", {}) if isinstance(obj.get("data"), dict) else {}
        with self.lock:
            if kind == "console_status":
                self.state["fw"] = data.get("fw", "")
                self.state["components"] = data.get("components", [])
                self.state["report_port"] = data.get("report_port", "")
                self.state["log_suppressed"] = bool(data.get("log_suppressed"))
                self.state["status"] = "ready"
                self.state["hint"] = ""
            elif kind == "led_status":
                self.state["led"] = data.get("pins", [])
                self._learn_component("led")
            elif kind == "key_status":
                self.state["key"] = {"configured": data.get("configured", []),
                                     "state": data.get("state", {})}
                if data.get("dbg") is not None:
                    self.state["key"]["dbg"] = data.get("dbg")
                self._learn_component("key")

    def _learn_component(self, name: str) -> None:
        """收到某一路遥测就说明它已经真的在跑：补进组件清单，并把状态改成 ready。

        为什么不能只等 console_status：那一行只在开机/被问到时发一次，主机若在
        板子启动过程中开会话就会永久错过，界面就会一直显示「缺少三处固件」，
        尽管 key/led 其实已经在报数据（实测就是这样）。
        """
        comps = list(self.state.get("components") or [])
        if name not in comps:
            comps.append(name)
        self.state["components"] = comps
        if self.state.get("status") != "busy":
            self.state["status"] = "ready"
        self.state["hint"] = ""

    def latest_frame(self) -> bytes | None:
        with self.lock:
            return self._last_frame

    # -------------------------------------------------------------- 端口

    def scan_ports(self) -> list[dict]:
        ports = handshake.list_candidate_ports()
        if not ports:
            self.set(status="no_device", hint="没有检测到串口设备，请插好数据线并确认驱动")
            self.log("扫描串口：0 个候选")
        else:
            self.log(f"扫描串口：{len(ports)} 个候选")
            for p in ports:
                self.log(f"  {p['device']}  {p.get('description', '')}")
        return ports


    @staticmethod
    def _native_usb_mac(device: str) -> str:
        """从 USB 描述符的序列号取原生口 MAC。

        ESP32-S3 的 USB-Serial-JTAG 把芯片 MAC 写成 iSerialNumber，形如
        DC:B4:D9:16:B4:48；读它不需要打开串口，更不会复位芯片。
        """
        try:
            for p in handshake.list_candidate_ports():
                if p.get("device") != device:
                    continue
                sn = (p.get("serial") or "").strip().lower()
                if len(sn) == 17 and sn.count(":") == 5:
                    parts = sn.split(":")
                    if all(len(x) == 2 for x in parts):
                        return sn
        except Exception:
            return ""
        return ""

    @staticmethod
    def _native_usb_port() -> str:
        """返回原生 USB（usbmodem）端口；没有就返回空串。

        两块板子/两根线同时插着时，CH340 只能烧录，新固件的协议数据走
        USB-Serial-JTAG。检测设备若停在 CH340 上，界面就会报「缺少固件」。
        """
        try:
            from serial_utils import list_candidate_ports
            for p in list_candidate_ports():
                if "usbmodem" in p.lower():
                    return p
        except Exception:
            return ""
        return ""

    def connect(self, device: str) -> dict:
        self.disconnect(silent=True)
        # 备份/烧录失败后可能留下 busy 标记与半截进度，先把状态归零，
        # 否则界面会一直显示「备份 128/1600 KB」并拒绝后续操作。
        cancel_token.clear()
        self.set(status="connecting", port=device, hint="", busy="", progress=0.0,
                 progress_text="", error="")
        self.log(f"打开串口 {device}")

        # 芯片 ID 必须在建链路之前读：esptool 要独占串口，链路一开就抢不到。
        # 上一步 disconnect 只停了自己的 link，若还有残留 Serial 握着 fd，
        # esptool 必然报 port busy，chip_id 就会是空 —— 所以先硬释放再读。
        native = "usbmodem" in device.lower()
        chip = self.state.get("chip_id", "") if native else ""
        if native:
            if not chip:
                # 原生口不要跑 esptool：它在应用运行时既可能复位进 stub，
                # 也可能留下失效 fd。USB 描述符的 iSerialNumber 就是芯片 MAC，
                # 直接读端口信息即可，链路不会被碰。
                chip = self._native_usb_mac(device) or ""
                if chip:
                    self.log(f"原生 USB 读取芯片 ID：{chip}")
                else:
                    self.log("原生 USB 未提供 MAC 序列号，芯片 ID 留空")
            else:
                self.log(f"原生 USB 连接：沿用已知芯片 ID {chip}")
        else:
            for attempt in range(3):
                serial_utils.hard_release(device)
                chip = backup_mod.get_chip_id(device)
                if chip:
                    break
                time.sleep(1.0 + attempt)
        self.set(chip_id=chip or "")
        if not chip:
            tail = (getattr(backup_mod, "LAST_ERROR", "") or "").strip().splitlines()
            why = tail[-1][:160] if tail else "端口被占用，或板子不在下载模式"
            self.log(f"读取芯片 ID 失败：{why}")

        ok = self.link_start(device)
        if not ok:
            self.set(status="error", error="串口打开失败",
                     hint="端口被占用或驱动异常，拔插后重试")
            return {"ok": False, "error": "串口打开失败"}

        # 板子开机会复位、组件任务要等调度器起来（约 0.5-1 s）才开始上报；
        # 只发一次 console_info 必然太早，所以这里重试到 3.5 s，直到问出组件清单。
        deadline = time.time() + 3.5
        next_ask = time.time()
        while time.time() < deadline:
            # led/key 在 300 ms 就注册，display 要等到 600 ms 的懒初始化；
            # 只要看到 led 就 break，组件表里会永远缺 display，界面就报“缺固件”。
            comps = self.state.get("components") or []
            if comps and self.state.get("led") is not None and "display" in comps:
                break
            if time.time() >= next_ask:
                try:
                    self.link.send_console_info()      # 顺带触发 led/key 快照
                except Exception:
                    pass
                next_ask = time.time() + 0.4
            time.sleep(0.2)

        if self.state.get("components"):
            self.set(status="ready")
            self.log(f"已识别组件：{self.state['components']}")
        else:
            native = self._native_usb_port()
            if native and native != device and not getattr(self, "_usb_tried", False):
                self._usb_tried = True
                self.log(f"CH340 上没有协议数据，自动切到原生 USB：{native}")
                return self.connect(native)
            self.set(status="connected_no_fw",
                     hint="缺少画面固件，请刷录固件")
            self.log("板子没有上报组件：判定为缺少投屏固件")

        if chip:
            backup_mod.cancel_purge(chip)     # 插回来就撤销「拔板 1 小时删备份」
        info = backup_mod.backup_info(chip) if chip else None
        self.set(backup=info, purge_at=None)
        return {"ok": True, "state": self.snapshot()}

    def link_start(self, device: str) -> bool:
        link = handshake.ConsoleLink(device=device, parser=self.parser,
                                    on_disconnect=self._on_disconnect)
        if not link.start():
            return False
        self.link = link
        link.send_ping()
        link.send_report_cfg(True, True, True)
        # 组件是被动应答的：主机不主动问，灯态/按键就永远停在连接那一刻。
        # 每 1.2 s 问一次，既刷新 LED 实时状态，也不至于把总线占满。
        def _poller():
            while self.link is link:
                time.sleep(1.2)
                try:
                    link.send_console_info()
                except Exception:
                    break
        threading.Thread(target=_poller, name="console-poll", daemon=True).start()
        return True

    def _on_disconnect(self) -> None:
        if self.state.get("status") not in ("busy",):
            self.set(status="no_device", hint="设备已断开")
            self.log("串口读取线程结束（设备断开）")
        # 这里不登记「拔板清理」：esptool 独占串口、烧录后重连都会主动停链路，
        # 那不是用户拔线。真正的拔板由 _purge_loop 按「端口还在不在」判断。

    def _mark_unplugged(self) -> None:
        """开发板拔出：登记 1 小时后自动删除该芯片的备份（用户口径）。"""
        chip = self.state.get("chip_id", "")
        if not chip or not backup_mod.has_backup(chip):
            self.set(purge_at=None)
            return
        due = backup_mod.purge_deadline(chip) or (time.time() + backup_mod.PURGE_GRACE)
        backup_mod.schedule_purge(chip, at=due - backup_mod.PURGE_GRACE)
        self.set(purge_at=due)
        self.log(f"设备已拔出：{time.strftime('%H:%M', time.localtime(due))} "
                 f"自动删除 {chip} 的备份（插回即撤销）")

    def check_plug_state(self) -> None:
        """按「这个端口还在不在」判定插拔，避免把内部停止链路当成拔板。

        在也就撤销清理登记（用户口径：插回即撤销）；不在就登记 1 小时后清。
        """
        chip = self.state.get("chip_id", "")
        port = self.state.get("port", "")
        if not chip:
            return
        # 不能只看「原来那个端口名还在不在」：原生 USB 口在复位/重新枚举后
        # 名字会从 usbmodem21401 变成别的，旧判断会把插着的板子当成已拔出，
        # 一小时后把备份删掉。只要还有任何串口设备，就认为板子还插着。
        present = bool(serial_utils.list_candidate_ports())
        if present:
            if self.state.get("purge_at") or backup_mod.purge_deadline(chip):
                backup_mod.cancel_purge(chip)
                self.set(purge_at=None)
                self.log(f"{chip} 已插回，撤销自动清理")
        elif backup_mod.has_backup(chip):
            self._mark_unplugged()

    def disconnect(self, silent: bool = False) -> dict:
        if self.link:
            self.link.stop()
            self.link = None
        port = self.state.get("port", "")
        serial_utils.hard_release(port)
        with self.lock:
            self.state.update({"status": "idle", "port": "", "components": [],
                               "fw": "", "led": None, "key": None,
                               "fps": 0.0})
            self._last_frame = None
        if not silent:
            self.log("已断开")
        return {"ok": True}

    def send_key(self, idx: int) -> dict:
        if not self.link:
            return {"ok": False, "error": "未连接"}
        ok = self.link.send_key(idx)
        return {"ok": ok}

    # -------------------------------------------------------------- 备份 / 回滚

    def _release_port_for_esptool(self) -> None:
        """esptool 要独占串口：握手链必须先停，否则必然 port busy。

        实测症状：连接成功后直接点备份，第 1 块就报「读取失败（重试 3 次）」，
        根因不是读不出来，而是进程自己还握着 fd。
        """
        if self.link:
            self.link.stop()
            self.link = None
            time.sleep(0.6)   # 留时间让 CH340 释放独占锁

    def _guard_busy(self) -> dict | None:
        """同一时刻只允许一个编译/备份/烧录任务，避免两个任务抢同一个端口。"""
        busy = self.state.get("busy")
        if busy:
            return {"ok": False, "error": f"已有任务在进行：{busy}"}
        return None

    def start_backup(self, progress_cb=None, full: bool = False) -> dict:
        guard = self._guard_busy()
        if guard:
            return guard
        cancel_token.clear()             # 新任务开始：清掉上一次的「停止」标记
        self.set(error="", hint="")     # 上一轮的失败文案不要带进这一轮
        device = self.state.get("port", "")
        chip = self.state.get("chip_id", "")
        if not device:
            return {"ok": False, "error": "未连接设备"}
        if not chip:
            chip = backup_mod.get_chip_id(device) or ""
            self.set(chip_id=chip)
        if not chip:
            return {"ok": False, "error": "读不到芯片 ID，无法建立备份索引"}

        self.set(status="busy", busy="backup", progress=0.0, progress_text="准备备份")
        self._release_port_for_esptool()
        self.log("开始整片 Flash 备份")
        ok, path, err = backup_mod.backup_flash(device, chip, progress_cb=progress_cb,
                                                full=bool(full))
        if not ok:
            self.set(status="error", busy="", error=err, hint="备份失败，禁止烧录")
            self.log(f"备份失败：{err}")
            return {"ok": False, "error": err}

        info = backup_mod.backup_info(chip)
        self.set(status="connected_no_fw" if not self.state.get("components") else "ready",
                 busy="", backup=info, progress=1.0, progress_text="备份完成",
                 error="")
        self.log(f"备份完成：{path}")
        return {"ok": True, "backup": info}

    def rollback(self) -> dict:
        device = self.state.get("port", "")
        chip = self.state.get("chip_id", "")
        if not device or not chip:
            return {"ok": False, "error": "未连接设备"}
        if not backup_mod.has_backup(chip):
            return {"ok": False, "error": "该芯片没有备份，禁止回滚（跨设备不允许）"}

        self.set(status="busy", busy="restore", progress=0.0, progress_text="回烧原固件")
        if self.link:
            self.link.stop()
            self.link = None
        ok, err = backup_mod.restore_flash(device, chip)
        if not ok:
            self.set(status="error", busy="", error=err)
            return {"ok": False, "error": err}

        self.set(status="connected_no_fw", busy="", progress=1.0,
                 progress_text="已恢复原固件",
                 hint="缺少画面固件，请刷录固件", components=[], fw="", led=None, key=None)
        self.log("回滚完成，设备已恢复为接入时的原始固件")
        return {"ok": True}

    # -------------------------------------------------------------- 停止（取消 + 破坏性收尾）

    def stop(self) -> dict:
        """停止任何正在运行的任务。

        用户口径（2026-11-12）：
          * 停止烧录 ⇒ 芯片已被写了一半，必须清空开发板，不能留半截固件；
          * 停止备份 ⇒ 删掉这次备份出来的内容，不留半份镜像冒充「有备份」。
        停止是「先取消、再收尾」，收尾放在后台线程里等原任务退出，接口立刻返回。
        """
        busy = (self.state.get("busy") or "").strip()
        if not busy:
            # 上一轮已经失败/卡死时 busy 会被清空，但端口可能还被残留的 esptool
            # 握着。用户点「停止」的诉求是「把现在这一团收干净」，不能回一句
            # 「没有正在运行的任务」就当失败。
            cancel_token.request()
            port = self.state.get("port", "")
            if port:
                serial_utils.hard_release(port)
            self.set(status="error" if self.state.get("error") else "idle",
                     busy="", progress=0.0, progress_text="已停止",
                     error="", hint="")
            return {"ok": True, "stopping": "cleanup"}
        cancel_token.request()
        self.set(progress_text=f"正在停止 {busy}…")
        self.log(f"用户点了停止：{busy}")

        def finish() -> None:
            deadline = time.time() + 45
            while self.state.get("busy") and time.time() < deadline:
                time.sleep(0.3)
            port = self.state.get("port", "")
            chip = self.state.get("chip_id", "")
            # 收尾动作是新任务，必须先把「取消」旗子放下，否则 erase_flash 一进门
            # 看到 is_cancelled() 就直接返回「已停止」，芯片根本不会被清空
            # （实测：点了停止烧录，最后报「擦除失败：已停止」，板子里留着半截固件）。
            cancel_token.clear()
            if busy == "backup":
                removed = backup_mod.discard_incomplete(chip) if chip else 0
                self.set(status="connected_no_fw" if not self.state.get("components") else "ready",
                         busy="", progress=0.0, error="已停止备份",
                         progress_text="已停止备份（未完成的镜像已删除）",
                         backup=backup_mod.backup_info(chip) if chip else None)
                self.log(f"已停止备份，删除 {removed} 个未完成镜像")
                return
            # 烧录 / 回滚 / 编译：统一按「清空开发板」收尾
            if port and chip:
                self.set(progress_text="清空开发板（停止烧录的收尾动作）")
                ok, err = flash_controller.erase_flash(port)
                if not ok:
                    self.set(status="error", busy="", error=f"已停止，但擦除失败：{err}")
                    self.log(f"停止后擦除失败：{err}")
                    return
                self.set(status="connected_no_fw", busy="", progress=0.0,
                         error="已停止烧录", progress_text="已停止并清空开发板",
                         components=[], fw="", led=None, key=None)
                self.log("已停止烧录，芯片已整片擦除")
                try:
                    self.connect(port)
                except Exception:   # noqa: BLE001
                    pass
            else:
                self.set(busy="", error="已停止", progress_text="已停止")

        threading.Thread(target=finish, name="task-stop", daemon=True).start()
        return {"ok": True, "stopping": busy}

    # -------------------------------------------------------------- 部署（编译 + 烧录）

    def deploy(self, source: str, components: list[str], adapters: list[str],
               mode: str = "inject") -> dict:
        """mode="inject"：注入组件后编译烧录；mode="raw"：原样编译烧录。"""
        guard = self._guard_busy()
        if guard:
            return guard
        cancel_token.clear()
        self.set(error="", hint="")     # 上一轮的失败文案不要带进这一轮
        if not self.state.get("port"):
            # 断开后直接点烧录时，早先会拿空端口去起 esptool，用户看到的是
            # 「Could not open , the port is busy」这种残缺报错，等于没告诉他该干什么。
            return {"ok": False, "code": "NO_PORT",
                    "error": "还没有连接开发板，请先点「检测设备」再烧录"}
        if not self.state.get("chip_id"):
            return {"ok": False, "code": "NO_CHIP", "error": "未识别芯片，禁止烧录"}
        # 原样烧录 = 用户的明确意图是「覆盖」，不要求备份（用户口径 2026-11-12）。
        # 注入组件模式仍然强制备份：它要往用户工程里掺文件，必须留后路。
        if mode != "raw" and not backup_mod.has_backup(self.state["chip_id"]):
            return {"ok": False, "code": "NO_BACKUP",
                    "error": "注入组件前必须先做整片备份：没有备份就无法一键回滚",
                    "hint": "点「整片备份」后再烧录；或改用「原样烧录」（会清空芯片，不需要备份）"}

        # 用户选的是 .zip 时，这里必须先解压再取工程根：早先直接把 zip 的路径当
        # 目录丢给 prepare_copy，界面上只报「工程根不存在」。
        proj = project_importer.import_project(source)
        if proj.error or proj.kind != "idf":
            return {"ok": False, "code": "BAD_PROJECT",
                    "error": proj.error or "未找到 ESP-IDF 工程（缺少 CMakeLists.txt）"}
        root = proj.project_root or proj.extract_path
        env = build_env_checker.check(proj)
        if not env.ok:
            return {"ok": False, "error": "编译环境预检未通过",
                    "blocks": [b.to_dict() for b in env.blocks]}

        self.set(status="busy", busy="deploy", progress=0.0, progress_text="准备工作区")
        self._release_port_for_esptool()

        def prog(frac: float, text: str) -> None:
            self.set(progress=round(frac, 3), progress_text=text)

        import workspace_manager
        work = workspace_manager.prepare_copy(root, self.state["chip_id"])
        self.log(f"工作区副本：{work}")

        dirs: list[str] = []
        if mode == "inject":
            dirs = source_injector.inject_components(str(work), components, adapters)
            self.log(f"注入组件：{', '.join(Path(d).name for d in dirs)}")

        # 组件已复制进副本的 components/，不再用 EXTRA_COMPONENT_DIRS（实测被忽略）
        res = idf_build_scheduler.build_project(str(work), progress_cb=prog)
        if not res.ok:
            self.set(status="error", busy="", error=res.error,
                     hint="编译失败，未执行烧录")
            self.log(f"编译失败：{res.error}")
            return {"ok": False, "error": res.error, "log_tail": res.log_tail}

        if mode == "raw":
            self.set(progress_text="清空 Flash（原样烧录：直接覆盖）")
            self.log("原样烧录：先整片擦除，再写入编译产物")
            ok_e, err_e = flash_controller.erase_flash(self.state["port"])
            if not ok_e:
                self.set(status="error", busy="", error=err_e)
                self.log(f"擦除失败：{err_e}")
                return {"ok": False, "error": err_e}

        self.set(progress=0.0, progress_text="烧录中")
        ok, err = flash_controller.flash_bins(self.state["port"], res.bins,
                                              progress_cb=prog)
        if not ok:
            self.set(status="error", busy="", error=err)
            return {"ok": False, "error": err}

        same, verr = flash_controller.verify_same_chip(self.state["port"],
                                                       self.state["chip_id"])
        if not same:
            self.set(status="error", busy="", error=verr)
            return {"ok": False, "error": verr}

        self.set(busy="", progress=1.0, progress_text="烧录完成", error="",
                 last_flash={"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "workspace": str(work), "mode": mode,
                             "components": components, "adapters": adapters})
        self.log("烧录完成，重新握手")
        port = self._native_usb_port() or self.state.get("port") or ""
        if port:
            self._usb_tried = False
            self.connect(port)      # 内部会重读芯片、重建链路并复位 status
        else:
            self.set(status="idle")
        return {"ok": True}


PORT_FILE = Path.home() / "Library" / "Application Support" / "ESP32-S3-Console-App" / "port.txt"


def _watch_parent() -> None:
    """父进程（.app 外壳）被强杀时，本进程会被 system 收养成 1，必须自行退出。

    没有这段，用 Cmd+Q 之外的方式结束外壳会留下一个还在监听端口的孤儿进程，
    下次启动就会抢不到端口。
    """
    import os
    origin = os.getppid()

    def loop() -> None:
        while True:
            time.sleep(2)
            if os.getppid() != origin:
                os._exit(0)

    threading.Thread(target=loop, name="parent-watch", daemon=True).start()


def _purge_loop(app: "ConsoleApp") -> None:
    """每 30 秒扫一次：拔出满 1 小时的设备，自动删掉它的整片备份。

    只做「到期才删」，不会因为 App 重启就丢登记——登记写在
    backups/pending_purge.json，下次启动先 purge_expired() 一次。
    """
    while True:
        time.sleep(30)
        try:
            app.check_plug_state()
        except Exception as exc:  # noqa: BLE001
            app.log(f"插拔检测失败：{type(exc).__name__}: {exc}")
        try:
            removed = backup_mod.purge_expired()
        except Exception as exc:  # noqa: BLE001
            app.log(f"自动清理备份失败：{type(exc).__name__}: {exc}")
            continue
        for chip in removed:
            app.log(f"开发板已拔出满 1 小时：自动删除 {chip} 的备份")
            if app.state.get("chip_id", "").lower() == chip.lower():
                app.set(backup=None, purge_at=None,
                        hint="该设备的备份已按规则自动清理，重新烧录需先重新备份")


def main() -> int:
    import argparse
    import sys
    import ui_bridge

    # 打包版只有这一个可执行文件：备份/烧录要「每次起一个干净 esptool 进程」
    # （进程内连续调用实测每 3 次挂 1 次），没有独立 python 可用，所以走
    # 自己这个二进制的隐藏入口，argv 直接透传给 esptool。
    if "--esptool-cli" in sys.argv:
        import esptool
        argv = [a for a in sys.argv[1:] if a != "--esptool-cli"]
        try:
            esptool.main(argv)
            return 0
        except SystemExit as exc:
            return int(exc.code or 0)

    ap = argparse.ArgumentParser(description="ESP32-S3 Console App 后台")
    ap.add_argument("--port", type=int, default=None, help="本地界面端口，默认自动选择")
    args = ap.parse_args()

    app = ConsoleApp()
    _watch_parent()

    # 启动先清一次过期备份（App 关着的时候到期的也要删掉），再起常驻清扫线程
    try:
        for chip in backup_mod.purge_expired():
            app.log(f"启动清理：开发板拔出已满 1 小时，删除 {chip} 的备份")
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=_purge_loop, args=(app,), name="purge", daemon=True).start()
    port = ui_bridge.serve(app, args.port)
    try:
        PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError:
        pass
    print(f"PORT={port}", flush=True)

    app.scan_ports()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        app.disconnect(silent=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
