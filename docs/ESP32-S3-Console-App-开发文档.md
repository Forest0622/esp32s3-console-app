# ESP32-S3 检修台 开发文档

> 版本：2026-11-17  
> 代码根：`/Users/xiaoyi/个人/单片机/ESP-S3/Project/ESP32-S3-Console-App`  
> App：`ESP32-S3 检修台`，bundle id `com.xiaoyi.esp32s3console`，版本 `1.1.1`  
> 运行平台：macOS 12+，当前构建为 arm64  
> 文档依据：本文按当前仓库代码重写。旧文档中的“两模式都必须备份”“仅 `-D` 注入”“采集 RESET”等说法已废弃，不再作为实现依据。

## 目录

1. 系统构成
2. 运行状态机与数据流
3. 前端界面
4. 后端模块
5. HTTP API
6. 串口协议
7. 画面采集
8. LED 采集
9. 按键采集
10. 工程导入
11. 工作区与组件注入
12. 编译
13. 烧录
14. 备份、恢复与清理
15. 打包
16. 测试与验收
17. 已知限制
18. 本轮变更

---

## 1. 系统构成

### 1.1 交付形态

一个 macOS `.app`，内部包含：

| 层 | 文件 | 作用 |
|---|---|---|
| 原生窗口壳 | `mac_bundle_packager/ConsoleShell.swift` | `NSWindow` + `WKWebView`，启动后台进程，转发访达选择与备份目录打开 |
| 前端 | `frontend-ui/index.html`、`frontend-ui/assets/board.png` | 单页界面、板图、实时画面、状态与操作 |
| 后端 | `backend/*.py` | 串口、协议解析、导入、编译、烧录、备份、HTTP |
| 固件组件 | `esp32_components/*` | 注入到用户 ESP-IDF 工程的只读采集组件 |
| 打包资源 | `mac_bundle_packager/assets/AppIcon.icns` | App 图标 |
| 测试 | `tests/test_backend.py`、`tests/fixture_project` | 后端 7 组自测 |

后端由 PyInstaller 打成 `console_app`，窗口壳再以子进程启动它。窗口壳只负责窗口、WebView、原生文件面板与退出清理；业务全部在后端。

### 1.2 组件构成

| 目录 | 组件名 | 是否可选 | 职责 |
|---|---|---|---|
| `console_protocol` | 公共协议层 | 始终随任一组件的注入 | 串口初始化、JSON 行、二进制帧、CRC、命令分发、组件清单 |
| `display_component` | 画面 | 可选 | 截取 `esp_lcd_panel_draw_bitmap`，或在原生 USB 下读 `ui_framebuffer()`，上报 RGB565 帧 |
| `led_read_driver` | 指示灯 | 可选 | 只读 GPIO 电平，4 ms 窗口统计 PWM 占空比 |
| `key_read_driver` | 按键 | 可选 | 只读 KEY0–KEY3；ATK 板走 XL9555 P17–P14 |
| `screen_adapters/alientek` | 画面适配包 | 可选 | 正点原子自写 SPI 驱动的 wrap 适配；当前 `verified=false` |

功能组件只有 `display`、`led`、`key` 三个。`console_protocol` 是共享依赖，不是第四个功能组件。

### 1.3 当前实现状态

| 能力 | 状态 | 证据 |
|---|---|---|
| 原生 USB 读芯片 MAC | 已实现并真机验证 | `status=ready`、`chip_id=dc:b4:d9:16:b4:48`、`components=['led','key','display']` |
| 组件识别 | 已实现并真机验证 | `console_info` 返回三组件，LED 与 KEY 遥测持续上报 |
| 画面帧 | 已实现；当前真机 `fps≈1.5` | 原生 USB + `ui_framebuffer()` 路径；无屏工程是否出帧取决于用户工程 |
| 备份索引 | 已实现并真机验证 | `dcb4d916b448_20261116_1459_1600KB.bin`，`whole=true` |
| 注入烧录 | 已实现 | 组件复制 + 副本 CMake 修改；详见第 11、13 节 |
| 原样烧录 | 已实现 | `erase_flash` 后编译写入，不要求备份 |
| 回滚 | 已实现 | 按芯片 MAC 索引；支持稀疏镜像 |
| 检修台按键反控开发板 | **未实现** | 固件 `console_proto_set_cmd_cb()` 没有注册者 |
| RESET / BOOT 采集 | **未实现** | 只采集 KEY0–KEY3，`CONFIG_KEY_READ_RESET=-1` |
| Windows 版本 | **没有** | 当前只构建 macOS arm64 |
| 画面适配包 | **未验证** | `alientek` 的 `verified=false`，UI 不展示 |

---

## 2. 运行状态机与数据流

### 2.1 状态

`backend/app.py::ConsoleApp.state["status"]`：

| 状态 | 含义 |
|---|---|
| `idle` | 未连接 |
| `no_device` | 扫描不到串口，或链路断开 |
| `connecting` | 正在打开串口 |
| `connected_no_fw` | 串口已打开，但没有收到组件遥测 |
| `ready` | 已收到组件清单或 LED/按键遥测 |
| `busy` | 备份、编译、烧录、回滚、停止收尾 |
| `error` | 最近一次操作失败 |

`busy` 取值：`backup`、`deploy`、`restore`。

### 2.2 连接流程

```text
扫描串口
  → 原生 USB 优先
  → 设置 port
  → 原生 USB：从 USB 描述符 iSerialNumber 读 MAC
  → CH340/其它：esptool chip_id
  → 打开串口链路
  → 发送 ping + console_info + set_report(true,true,true)
  → 3.5 s 内每 0.4 s 重发 console_info
  → 收到 console_status / led_status / key_status 任一 → ready
  → 都未收到 → connected_no_fw
  → 若当前是 CH340 且存在原生 USB 口 → 自动切到原生 USB 重连一次
```

关键实现：

- `_native_usb_mac(device)` 遍历 `serial.tools.list_ports`，匹配目标 `device`，读取 `serial_number`，校验 `xx:xx:xx:xx:xx:xx` 格式后返回小写 MAC。
- 原生分支不再调用 esptool，不打开串口，不复位芯片。
- 非原生分支连续 3 次 `hard_release` + `get_chip_id`，每次失败退避 1 s。
- 连接成功后若拿到 chip，`cancel_purge(chip)` 撤销拔板清理登记。
- `link_start` 起一个 1.2 s 周期的 `console_info` 轮询线程，用于补发 LED 与按键快照。

### 2.3 数据流

```text
开发板组件 ──USB/UART──> ConsoleLink 读线程
                         ├─ JSON 行 → SerialDataParser._handle_line → ConsoleApp._on_telemetry
                         └─ A5 5A 帧 → SerialDataParser._apply_ops → 组帧 → ConsoleApp._on_frame
ConsoleApp.state ──/api/state──> 前端 poll() ──> DOM / canvas
ConsoleApp._last_frame ──/api/frame.bmp──> 前端 Image → canvas
```

### 2.4 运行目录与环境变量

开发态：

| 路径 | 来源 |
|---|---|
| 工程根 | `backend/app.py` 的上一级 |
| 备份 | `<工程根>/backups` |
| 工作区 | `<工程根>/workspace` |
| 导入解压 | `<工程根>/projects_imported` |

打包态由窗口壳注入：

| 变量 | 值 |
|---|---|
| `CONSOLE_APP_HOME` | `~/Library/Application Support/ESP32-S3-Console-App/data` |
| `CONSOLE_APP_FRONTEND` | `Contents/Resources/frontend-ui` |
| `CONSOLE_APP_COMPONENTS` | `Contents/Resources/esp32_components` |
| `CONSOLE_APP_SEED_BACKUPS` | `Contents/Resources/backups`，仅首次启动复制 |

打包态实际数据目录：

```text
~/Library/Application Support/ESP32-S3-Console-App/data/
├── backups/
│   ├── index.json
│   ├── pending_purge.json
│   └── <mac>_<时间>_<数据量>KB.bin
├── workspace/<chip>/<工程名>_<时间戳>/
└── projects_imported/<时间戳>_<包名>/
```

`~/Library/Application Support/ESP32-S3-Console-App/port.txt` 保存后端实际监听端口。

---

## 3. 前端界面

### 3.1 布局

单页四区：

| 区域 | 内容 | 位置 |
|---|---|---|
| 板图 | 官方渲染底板、排针标注、屏幕位、按键热区、LED/PWR | 顶部，占首屏约六成 |
| 设备与日志 | 连接状态、端口、芯片 ID、固件、组件、提示、进度、备份状态、日志 | 下方左列 |
| 灯 / 键 | LED 状态、KEY0–KEY3、RESET、BOOT 卡片 | 下方中列 |
| 工程与烧录 | 工程路径、导入信息、烧录方式、组件勾选、适配包、备份/烧录/回滚 | 下方右列 |

### 3.2 板图

底图 `frontend-ui/assets/board.png` 为 1569×894。界面以 1046×596 为基准坐标，缩放比例 1.5。

排针标注分为 P1–P6：

| 组 | 位置/形状 | 说明 |
|---|---|---|
| P1 | 2×12 | 上排 12、下排 12 |
| P2 | 2×9 | 上排 9、下排 9 |
| P3 | 1×6 | ATK MODULE 扩展排母 |
| P4 | 2×3 | 6 针 |
| P5 | 1×4 | 4 针 |
| P6 | 2×3 | DC IN 区域 |

`pinAt(x, y, tx, ty, name, tone)` 处理“孔位落点 + 标签外移”。坐标常量不是通用板型数据，只对应这张 ATK-DNESP32S3 渲染图。

按键热区：

| 热区 | `data-n` | 是否上报状态 |
|---|---|---|
| RESET | 4 | 否，固件不采集 |
| BOOT | 5 | 否，固件不采集 |
| KEY2 | 2 | 是 |
| KEY1 | 1 | 是 |
| KEY0 | 0 | 是 |
| KEY3 | 3 | 是 |

点击行为：

- `.hot` 点击只切换右侧对应 `.key.active` 高亮，不发 `/api/key`。
- `.key` 卡片点击发 `POST /api/key {"idx":N}`；固件没有注册命令回调，所以当前不会驱动开发板。
- 实体按下时，`syncBoard()` 用 `state.key.state` 的 KEY0–KEY3 切换 `.hot.press`；RESET/BOOT 不参与。

### 3.3 画面窗口

`#screenbox` 内是 320×240 canvas。`/api/frame.bmp` 返回未压缩 24 位 BMP，前端用 `Image` 解码后绘制。支持：

- “适应”：按容器缩放；
- “1:1”：原始像素比例；
- 滚轮/触控板捏合缩放；
- 按住拖动平移；
- 普通双指滑动在放大态下平移，适应态下让页面滚动。

### 3.4 状态渲染

| 数据 | 渲染 |
|---|---|
| `status` | 顶部徽标与连接行 |
| `led` | `renderLed()`：PWM 显示 `PWM n%`；静态按 `active_low` 判断亮/灭 |
| `key` | `renderKeys()`：`-1` 或缺失显示“未配置”，否则“按下/松开” |
| `backup.whole` | `backupHint` 显示“已备份整片 N MB · 可一键恢复” |
| `purge_at` / `cleanup_at` | 显示“板已拔出：N 分钟后自动清理备份、缓存与导入工程（插回即撤销）” |
| `progress` / `progress_text` | 进度条与阶段文字 |
| `logs` | 最近 40 行 |
| `frame_seq` | 变化时拉一次 `/api/frame.bmp` |

覆盖层文案包括：未连接、正在检测、正在备份、正在处理、缺少画面固件、已连接暂无画面、出错。

### 3.5 弹窗与备份自动继续

`askModal()` 是自绘模态，替代 WKWebView 中不可用的 `alert/confirm`。

`requireBackupThen(what)`：

1. 弹出“需要备份”，文案说明“备份完成后自动继续”。
2. 点确认后 `POST /api/backup`。
3. 轮询 `/api/state`，直到 `busy !== "backup"`。
4. 若 `state.error` 非空，停止并提示；否则返回 `true`。
5. 调用方继续发 `/api/deploy`。

“编译并烧录”在注入模式且没有整片备份时会调用该函数；`/api/deploy` 的 `NO_BACKUP` 仍作为后端兜底。

### 3.6 工程默认路径

`index.html` 的 `#srcPath` 默认值为：

```text
/Users/xiaoyi/Desktop/ATK-DNESP32S3-Dashboard-Local
```

页面加载时若该值非空，会立即调用 `/api/import` 解析。换机器或换工程时需要在界面里改路径；这是当前写死的默认值。

---

## 4. 后端模块

`backend/` 共 18 个 `.py`：

| 模块 | 职责 |
|---|---|
| `app.py` | 状态、连接、备份、回滚、停止、部署、命令行入口、`--esptool-cli` 隐藏入口 |
| `app_data_cleanup.py` | 拔板计时、清理工作区副本、导入工程、旧版目录与分块临时文件 |
| `config.py` | 全局路径与少量旧常量；实际备份/烧录参数以各业务模块为准 |
| `log_setup.py` | 日志初始化 |
| `state_store.py` | 状态持久化辅助 |
| `ui_bridge.py` | `ThreadingHTTPServer`、静态文件、`/api/*`、RGB565→BMP |
| `serial_utils.py` | 串口枚举、硬释放、端口工具 |
| `serial_detect_handshake.py` | 端口扫描、打开、`ConsoleLink`、ping/console_info/set_report/key |
| `serial_data_parser.py` | 混合流解析、CRC、画面块组装 |
| `project_importer.py` | 解压、防越界、工程根 BFS、target/IDF 版本、画面路径扫描、适配包注册 |
| `screen_adapters.py` | 适配包解析 |
| `build_env_checker.py` | IDF 路径、版本、target、离线依赖、磁盘空间预检 |
| `workspace_manager.py` | 工作区副本、列表、按容量清理 |
| `source_injector.py` | 复制组件到副本、修改副本 CMake、返回复制目录 |
| `firmware_backup_manager.py` | 芯片 ID、备份、索引、抽样复核、回滚、拔板清理 |
| `flash_controller.py` | `flasher_args` 解析、烧录、擦除、二次校验、单 bin 直烧 |
| `idf_build_scheduler.py` | 激活 IDF、`idf.py build`、进度、日志尾部 |
| `cancel_token.py` | 全局取消标记 |
| `esptool_runner.py` | esptool 子进程辅助 |

---

## 5. HTTP API

服务只监听 `127.0.0.1`。默认端口 `8765`，被占用时在 `8765–8784` 内顺延；实际端口写入 `port.txt`。

### 5.1 GET

| 路径 | 返回 |
|---|---|
| `/api/state` | `{ok:true,state:{...}}` |
| `/api/ports` | `{ok:true,ports:[{device,description,hwid,serial}]}` |
| `/api/adapters` | `{ok:true,adapters:[...]}`，只返回 `verified=true` 的适配包 |
| `/api/frame.bmp` | 320×240 24 位 BMP；无帧时 `404 {ok:false,error:"no_frame"}` |
| `/`、`/index.html` | 前端页面 |
| 其它静态路径 | `frontend-ui/` 下文件；越界或不存在返回 404 |

### 5.2 POST

| 路径 | 请求体 | 行为 |
|---|---|---|
| `/api/scan` | 无 | 重新扫描串口，返回列表 |
| `/api/connect` | `{"port":"/dev/cu.usbmodem..."}` 或 `{"device":"..."}` | 连接 |
| `/api/disconnect` | 无 | 停链路、硬释放端口、清状态 |
| `/api/key` | `{"idx":0..5}` | 发 `{"cmd":"key","idx":N}`；当前固件无回调 |
| `/api/import` | `{"path":"..."}` | 导入工程或 bin |
| `/api/backup` | `{"full":false}` | 异步备份，立即返回 `{ok:true,started:"backup"}` |
| `/api/rollback` | 无 | 异步回滚 |
| `/api/open-backups` | 无 | `open` 备份目录 |
| `/api/stop` | 无 | 取消当前任务并执行收尾 |
| `/api/deploy` | `{"source":"...","components":["display","led","key"],"adapters":["..."],"mode":"inject"}` | 异步编译烧录 |

长任务统一由 `_run_async()` 起后台线程。任务返回 `ok:false` 时写入 `state.error`；用户主动停止的“已停止”分支不置为错误。

### 5.3 `/api/state` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | string | 第 2 节状态机 |
| `port` | string | 当前端口 |
| `chip_id` | string | 芯片 MAC，小写带冒号 |
| `fw` | string | 固件标识，组件固件为 `console-comp-1.0` |
| `components` | string[] | `display` / `led` / `key` |
| `report_port` | string | `usb_serial_jtag` 或 `uart0` |
| `log_suppressed` | bool | 是否压制日志 |
| `fps` | number | 最近 2 s 帧数除以 2 |
| `frame_seq` | int | 帧序号，前端据此拉图 |
| `led` | array/null | `[{pin,level,pwm,duty_pct,active_low}]` |
| `key` | object/null | `{configured:[...],state:{KEY0:0..},dbg?}` |
| `error` / `hint` | string | 错误与提示 |
| `busy` | string | 当前长任务 |
| `progress` | float | 0–1 |
| `progress_text` | string | 阶段文字 |
| `backup` | object/null | 索引条目 |
| `has_backup` | bool | 当前芯片是否有 `whole=true` 的备份 |
| `last_flash` | object/null | 最近一次烧录的时间、工作区、模式、组件、适配包 |
| `purge_at` | number/null | 拔板自动清理备份到期时间戳 |
| `cleanup_at` | number/null | 拔板满 1 小时清理缓存与旧工程的到期时间戳 |
| `logs` | string[] | 最近 40 行 |

---

## 6. 串口协议

### 6.1 物理层

| 项目 | 原生 USB-Serial-JTAG | CH340 / CP210x |
|---|---|---|
| 设备名 | `/dev/cu.usbmodem*` | `/dev/cu.usbserial*`、`/dev/cu.wchusbserial*` |
| 默认上报波特率 | 921600（USB 虚拟，不受晶振误差影响） | 921600；备份探速只用 230400 / 115200 |
| 芯片 ID 来源 | USB 描述符 `iSerialNumber` | esptool `chip_id` |
| 复位行为 | 连接不跑 esptool，不复位 | 连接前硬释放，esptool 读 ID |
| 日志 | 可保留 UART0 日志 | 若走上报，需压制日志 |

`ConsoleLink.open_port()` 打开串口后执行 `dtr=False, rts=True, 50 ms, rts=False, 350 ms`，再清空输入缓冲；这是给 UART 下载/复位用的时序。原生 USB 连接虽然不跑 esptool，但链路打开仍使用该时序。

### 6.2 混合流

解析器按首字节区分：

```text
0xA5 0x5A  → 二进制画面帧
其它字节   → JSON 文本行，遇到 \n 结束；\r 忽略
```

二进制帧：

| 偏移 | 长度 | 字段 |
|---|---|---|
| 0 | 2 | `A5 5A` |
| 2 | 4 | payload 长度，little-endian u32 |
| 6 | 2 | CRC16-CCITT，little-endian u16 |
| 8 | N | payload |

限制：

- `MAX_PAYLOAD = 1 MiB`；长度 0 或超限判坏帧。
- 单行最大 8192 字节；超长丢弃。
- CRC 算法：初值 `0xFFFF`，多项式 `0x1021`，不反射。
- 已验证标准向量：`CRC16("123456789") = 0x29B1`。

### 6.3 画面 payload

payload 是若干操作码的串联：

| op | 值 | 结构 | 说明 |
|---|---|---|---|
| `OP_END` | `0x00` | 1 字节 | 本帧结束，解析器回调整张 framebuffer |
| `OP_SKIP` | `0x01` | 3 字节 | 保留，解析器跳过，不绘制 |
| `OP_RLE` | `0x02` | 5 字节 | 保留，解析器跳过 |
| `OP_STATE` | `0x03` | 5 字节 | 保留，解析器跳过 |
| `OP_RAW` | `0x04` | 1 + N | 整段原始像素，覆盖 framebuffer 开头 |
| `OP_PART` | `0x05` | `op + idx(u8) + cnt(u16) + cnt*2 字节` | 1024 像素块 |

`OP_PART` 的 `idx` 是块号，`cnt` 是该块像素数，偏移为 `idx * 1024 * 2`。一块最多 1024 像素，即 2048 字节 RGB565。

固件 `display_send_full()` 每次把 8 个块拼进一个二进制帧，最后一批带 `OP_END`；`display_task` 在原生 USB + `ui_framebuffer()` 路径下每 200 ms 发一整帧。其它路径只发脏块，每帧最多 8 块，末尾带 `OP_END`。

### 6.4 主机 → 设备命令

均为一行 JSON，以 `\n` 结尾：

| 命令 | 示例 | 固件行为 |
|---|---|---|
| ping | `{"cmd":"ping"}` | 回 `{"resp":"pong"}` |
| console_info | `{"cmd":"console_info"}` | 回 `console_status`，并调用所有 `on_info` 钩子补发 LED/按键/画面信息 |
| set_report | `{"cmd":"set_report","display":true,"led":true,"key":true}` | 改三路上报开关，无回包 |
| key | `{"cmd":"key","idx":0}` | 交给 `s_cmd_cb`；当前没有注册者，**不驱动硬件** |

`cmd_handler` 用 `strstr` 匹配 `"ping"`、`"console_info"`、`"set_report"`，不是严格的 JSON 解析；字段名出现在任意字符串里都可能命中，这是当前实现。

### 6.5 设备 → 主机遥测

#### console_status

```json
{"type":"console_status","data":{"fw":"console-comp-1.0","components":["led","key","display"],"report_port":"usb_serial_jtag","log_suppressed":false}}
```

- `fw` 固定为 `console-comp-1.0`。
- `components` 是已注册组件，最多 4 个。
- `report_port` 为 `usb_serial_jtag` 或 `uart0`。
- `log_suppressed` 由 Kconfig 决定。

#### led_status

```json
{"type":"led_status","data":{"pins":[{"pin":"GPIO1","level":0,"pwm":false,"active_low":1}]}}
```

PWM 时：

```json
{"type":"led_status","data":{"pins":[{"pin":"GPIO1","level":1,"pwm":true,"duty_pct":37,"active_low":1}]}}
```

#### key_status

```json
{"type":"key_status","data":{"configured":["KEY0","KEY1","KEY2","KEY3"],"state":{"KEY0":0,"KEY1":0,"KEY2":0,"KEY3":0,"RESET":-1},"dbg":0}}
```

- `configured` 只列实际读到的键；XL9555 模式为 KEY0–KEY3。
- `state` 取值：`0` 松开、`1` 按下、`-1` 未配置。
- `dbg`：`0` 正常、`1` 拿不到 I2C 总线、`2` 读不到扩展芯片；GPIO 模式不带 `dbg`。
- 主机 `_on_telemetry` 只保留 `configured`、`state`、`dbg`，其余字段丢弃。

### 6.6 时序与延迟

- 主机 ping/console_info 在连接时发送；之后每 1.2 s 发一次 `console_info`。
- 固件 `rx_task` 每 20 ms 读一次（无数据时 5 ms 退避）。
- LED 任务每 100 ms 采样一次。
- 按键任务按 `CONFIG_KEY_READ_POLL_MS` 轮询；Kconfig 默认 10 ms，代码在宏未定义时回退 30 ms。
- 主机看到物理按键变化最多延迟约 1.2 s + 一个轮询周期，因为 XL9555 的输入端口只在 `console_info` 时刷新。

---

## 7. 画面采集

### 7.1 两条路径

| 路径 | 触发条件 | 数据来源 | 适用 |
|---|---|---|---|
| framebuffer 整帧 | 原生 USB 且用户工程暴露 `uint16_t *ui_framebuffer(void)` | 用户工程自己的 UI framebuffer | 即使未接 LCD 也能显示；正点原子例程可用 |
| draw_bitmap 拦截 | 用户工程走 `esp_lcd_panel_draw_bitmap` | `--wrap` 壳函数复制绘制区域 | 常规 esp_lcd 屏 |
| 适配包 | 用户工程绕过 esp_lcd，命中已知驱动符号 | 适配包 wrap 同名函数 | 当前无已验证适配包 |

`ui_framebuffer` 是弱符号：

```c
extern uint16_t *ui_framebuffer(void) __attribute__((weak));
```

没有该符号时返回 `NULL`，组件退回 `esp_lcd` 脏块路径。

### 7.2 wrap 实现

`display_component/CMakeLists.txt`：

```cmake
idf_component_register(
    SRCS "console_display.c"
    INCLUDE_DIRS "include"
    REQUIRES console_protocol esp_lcd
)
target_link_options(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=esp_lcd_panel_draw_bitmap"
)
```

`console_display.c` 中 `__real_esp_lcd_panel_draw_bitmap` 必须是强引用，不能加 `weak`。否则链接器不会为它抽取静态库成员，`__real` 为空，壳函数跳过原绘制，画面静默丢失。

壳函数顺序固定：

```c
esp_err_t ok = __real_esp_lcd_panel_draw_bitmap(...);
console_display_on_draw(...);
return ok;
```

先让原绘制发生，再复制像素。复制区域会裁剪到 `s_w/s_h`，按行拷贝到快照，并标记覆盖到的块为脏。

### 7.3 缓冲与帧参数

| 参数 | 值 | 来源 |
|---|---|---|
| 宽 | 320 | `CONFIG_CONSOLE_DISPLAY_WIDTH` |
| 高 | 240 | `CONFIG_CONSOLE_DISPLAY_HEIGHT` |
| 最大帧率 | 20 | `CONFIG_CONSOLE_DISPLAY_MAX_FPS` |
| 块大小 | 1024 像素 | `BLOCK_PIXELS` |
| 块数 | 75 | `320*240/1024` 向上取整 |
| 快照 | 153600 B | 320×240×2 |
| payload 缓冲 | 8 块一批 + 32 B | 约 16 KB |
| 原生 USB 整帧周期 | 200 ms | `display_task` |
| 脏块路径周期 | `1000/max_fps` ms，默认 50 ms | `display_task` |

实测帧率不是固定值：当前原生 USB + 整帧路径约 1.5 fps；串口带宽、整帧块数、主机解析速度都会影响。

### 7.4 首帧补采与差分

- 组件启动任务延时 300 ms。
- 每约 5 秒尝试一次 `console_display_try_seed()`，最多 40 秒；拿到 `ui_framebuffer()` 就整帧拷贝并标记所有块。
- 原生 USB + `ui_framebuffer()` 路径下，每次循环 `console_display_sync_ui()` 逐块 `memcmp`，变了就拷贝到快照并标脏；随后 `display_send_fb_full()` 直接发整帧。
- 其它路径只发脏块。

### 7.5 限制

- `esp_lcd` 路径只能截到经过 `esp_lcd_panel_draw_bitmap` 的绘制；直接写 SPI、厂商私有驱动、RGB 直驱面板不走该函数时截不到。
- 没有 `ui_framebuffer()` 且没有 `esp_lcd` 绘制时，画面为空；界面显示“已连接，暂无画面”或“缺少画面固件”，不会伪造画面。
- 适配包需要真实工程验证后才能把 `verified` 改为 `true`；当前 `alientek` 未验证，不出现在下拉框。
- 画面尺寸固定 320×240；用户工程分辨率不同会导致裁剪/错位，当前没有自动协商分辨率。

---

## 8. LED 采集

### 8.1 默认参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `CONFIG_LED_READ_PIN` | 1 | ATK-DNESP32S3 板载红灯 GPIO1；`-1` 不采集 |
| `CONFIG_LED_READ_ACTIVE_LOW` | 1 | 低电平点亮 |
| `CONFIG_LED_READ_IDLE_MS` | 100 | 采样间隔 |
| `CONFIG_LED_READ_WINDOW_US` | 4000 | 每轮密集采样窗口 |
| `CONFIG_LED_READ_PWM_EPS_PCT` | 3 | 静态判定阈值 |

### 8.2 采样算法

1. 在 4 ms 窗口内尽可能密集地 `gpio_get_level()`。
2. 统计高电平占比 `ratio`。
3. `ratio <= 3%` 或 `ratio >= 97%` 判静态；否则判 PWM。
4. 静态时 `level = ratio >= 50 ? 1 : 0`，`duty_pct` 不填。
5. PWM 时 `level = ratio >= 50 ? 1 : 0`，`duty_pct = ratio`。
6. 只在首次、`pwm` 变化、`level` 变化、或 PWM 占空比变化 ≥2% 时上报。
7. 主机关闭 `set_report` 的 LED 路时，任务继续采样但不发送。

### 8.3 只读约束

`led_read_driver` 不调用 `gpio_config`，不写 GPIO，不改上下拉/方向/中断。它只读用户工程已经配置好的引脚电平。若用户工程没有把该脚配成输入，读数可能不可用；组件不会替用户改配置。

### 8.4 主机侧

- `/api/state` 的 `led` 是数组。
- 前端按 `active_low` 解释亮灭：`active_low=1` 时 `level=0` 为亮。
- PWM 显示 `PWM n%`，不按 `level` 猜亮灭。
- 板图 LED 热区也按同一规则切换 `on/pwm`。

---

## 9. 按键采集

### 9.1 来源

#### XL9555 模式（默认，ATK-DNESP32S3）

| 键 | XL9555 位 |
|---|---|
| KEY0 | P17 |
| KEY1 | P16 |
| KEY2 | P15 |
| KEY3 | P14 |

`CONFIG_KEY_READ_XL9555_BASE_BIT=4`，`KEY0 = base+3`，`KEY3 = base+0`。

I2C 默认：

| 参数 | 默认 |
|---|---|
| SDA | GPIO41 |
| SCL | GPIO42 |
| 地址 | `0x20` |
| 端口 | `I2C_NUM_0` |
| 速率 | 400 kHz |

读取方式：

- 只读 XL9555 输入端口 1（寄存器 `0x01`）。
- 不写配置寄存器，不改方向。
- 不主动 `i2c_driver_install` 抢总线；用户工程已装则复用，读不到就报 `dbg=1/2`，不伪造数据。
- 启动后每 400 ms 重试，最多 30 秒（75 次），等用户工程把总线装好。

#### GPIO 模式

`CONFIG_KEY_READ_SOURCE_GPIO` 下使用 `CONFIG_KEY_READ_KEY0..KEY3` 的 GPIO 号；`-1` 表示不采集。当前 ATK 板不使用该模式。

### 9.2 只读约束

- 只 `gpio_get_level()`，不接管中断、不改 IOMUX、不改上下拉、不调用 `gpio_config`。
- 用 `pin_input_enabled()` 读 `FUN_IE` 判断输入路径是否使能；未使能报 `-1`。
- 不配置、不消费用户工程的中断；XL9555 只在主机查询时读端口，避免轮询清掉用户工程中断。

### 9.3 消抖与上报

- 轮询间隔：Kconfig 默认 10 ms；代码宏未定义时 30 ms。
- 消抖：同一候选状态累计 20 ms 才确认。
- 状态变化且 `set_report` 的 key 路开启时发 `key_status`。
- 主机 `console_info` 触发 `emit_all()`，补发当前快照。

### 9.4 RESET / BOOT

| 键 | 实际连接 | 组件行为 |
|---|---|---|
| RESET | EN 复位脚，不是普通 GPIO | `CONFIG_KEY_READ_RESET=-1`，不采集 |
| BOOT | GPIO0，拉低进下载模式 | 没有采集项，不上报 |

界面仍有 RESET/BOOT 卡片和板图热区，状态显示“未配置”。这不是驱动坏了，是当前固件没有这两个采集项。

### 9.5 反控未实现

主机可以发：

```json
{"cmd":"key","idx":0}
```

固件 `cmd_handler` 在 ping/console_info/set_report 都不匹配时会调用 `s_cmd_cb`。当前没有任何组件调用 `console_proto_set_cmd_cb()`，所以 `s_cmd_cb` 为 `NULL`，命令被丢弃。

因此：

- 检修台 `.key` 卡片点击不会让开发板按键动作。
- 板图 `.hot` 点击只做右侧高亮。
- 要实现反控，需要在固件侧注册回调，并决定用 GPIO 还是 XL9555 输出；当前没有做。

---

## 10. 工程导入

### 10.1 输入类型

| 输入 | 处理 |
|---|---|
| `.zip` | `ditto -x -k --rsrc`；无 `ditto` 时 Python `zipfile`，先做路径校验 |
| `.tar` / `.tar.gz` / `.tgz` / `.tar.bz2` / `.tbz2` | Python `tarfile`，拒绝符号链接/硬链接 |
| 文件夹 | 不复制到 `projects_imported`，记录源路径；编译时复制到工作区 |
| `.bin` | 不解压，`kind=bin`，`target=esp32s3` |

### 10.2 安全

- zip 成员以 `/` 开头或含 `..` 直接拒绝。
- tar 成员含链接或越界直接拒绝；Python 3.12+ 使用 `filter="data"`。
- 解压后再遍历一次，拒绝越界符号链接。
- 解压目录必须在 `projects_imported/` 内；打包态该目录在可写数据目录，不在只读 `.app` 内。

### 10.3 工程根识别

`find_project_root()` 从解压根开始 BFS：

1. 某目录有 `CMakeLists.txt`，且内容含 `include($ENV{IDF_PATH}`、`project.cmake`、`project(` 或 `idf_component_register`，判为 IDF 工程根。
2. 命中即停止当前层，返回最浅命中。
3. 同层多个命中 → `error="工程根不唯一，请指定：..."`。
4. 没有命中：
   - 有 `*.ino` → `kind=arduino`，`error="暂不支持该工程类型（Arduino / PlatformIO）"`；
   - 有 `platformio.ini` → `kind=platformio`，同一错误；
   - 否则 `kind=unknown`，`error="未找到 ESP-IDF 工程（缺少 CMakeLists.txt）"`。

### 10.4 元信息

| 字段 | 来源 |
|---|---|
| `target` | `sdkconfig` 的 `CONFIG_IDF_TARGET`；`sdkconfig.defaults`；`sdkconfig.defaults.esp32s3`；最后读根 `CMakeLists.txt` 的 `set(IDF_TARGET ...)` |
| `idf_version` | `CONFIG_IDF_INIT_VERSION` |
| `has_sdkconfig` | 根目录是否有 `sdkconfig` |
| `has_component_yml` | `main/idf_component.yml` 或根 `idf_component.yml` |
| `screen_scan` | 扫描 `.c/.h/.cc/.cpp/.hpp`，匹配 esp_lcd 符号、SPI 写入特征、适配包符号 |

`screen_scan.path` 三档：

| 值 | 条件 |
|---|---|
| `esp_lcd` | 命中 `esp_lcd_panel_draw_bitmap`、`esp_lcd_panel_io_tx_color` 或相关头文件 |
| `known_driver` | 命中适配包 `symbols` |
| `unknown` | 都没有 |

扫描是启发式，不展开宏、不编译。

### 10.5 单 bin

`import_project()` 对 `.bin` 返回 `kind=bin`，不找工程根。`deploy()` 当前只接受 `kind=idf`，所以单 bin 主要通过 `flash_controller.flash_image()` 直烧路径使用；UI 主流程是 ESP-IDF 工程。

---

## 11. 工作区与组件注入

### 11.1 工作区副本

`workspace_manager.prepare_copy(project_root, chip_id)`：

```text
workspace/<safe_chip>/<safe_project_name>_<YYYYmmdd_HHMMSS>/
```

- `ignore_patterns`: `build`、`.git`、`managed_components`、`.vscode`、`__pycache__`、`*.o`。
- `symlinks=False`，复制实体文件。
- 每次烧录使用新目录，不复用旧 `build/`。
- 原工程只读；复制失败的文件跳过（`OSError` / `SameFileError`）。

### 11.2 注入

`source_injector.inject_components(work_copy, components, adapters)`：

1. 校验 `components` 只能是 `display`、`led`、`key`。
2. 若有任一组件的选择，先加入 `console_protocol`。
3. 把组件目录复制到 `work_copy/components/<目录名>/`；已存在则先删。
4. 对 `work_copy/CMakeLists.txt` 调 `_patch_project_cmake()`。

`_patch_project_cmake()`：

- 若已有 `__CONSOLE_EXTRA_COMPONENTS__` 标记，跳过。
- 在 `include($ENV{IDF_PATH}/tools/cmake/project.cmake)` 之前插入：

```cmake
# __CONSOLE_EXTRA_COMPONENTS__ 由检修台注入，只存在于工作区副本
set(EXTRA_COMPONENT_DIRS "<work_copy>/components")
```

- 把 `idf_build_set_property(MINIMAL_BUILD ON)` 替换成注释，避免组件被裁掉。
- 只改副本，原工程一个字节不动。

### 11.3 不是 `-D` 注入

`idf_build_scheduler._build_env()` 和 `build_project()` 仍保留 `EXTRA_COMPONENT_DIRS` 环境变量与 `-DEXTRA_COMPONENT_DIRS` 参数，这是遗留路径；`deploy()` 当前不传 `extra_dirs`。实际注入以复制组件 + 修改副本 CMake 为准。

`source_injector.inject_args()` 仍存在，但当前主流程不调用。

### 11.4 适配包

`resolve_component_dirs()` 对 `adapters` 逐个检查 `esp32_components/screen_adapters/<id>/`，不存在报错。`alientek` 适配包：

- `symbols`: `LCD_ShowPicture`、`lcd_color_fill`
- `pixel_offset_rule`: 行优先 RGB565，无行间 padding
- `byte_swap`: false
- `verified`: false

`/api/adapters` 只返回 `verified=true`，所以当前下拉框只有“适配包：无”。

---

## 12. 编译

`idf_build_scheduler.build_project(work_copy, progress_cb, extra_dirs, idf_path)`：

1. 检查工作区存在。
2. `build_env_checker.find_idf()` 找 IDF；默认候选：

```text
/Users/xiaoyi/esp/v5.5.6/esp-idf
~/esp/esp-idf
~/esp/v5.5.6/esp-idf
```

3. 环境变量：`IDF_PATH`、`IDF_TARGET=esp32s3`。
4. 命令：

```bash
/bin/bash -c '. "/Users/xiaoyi/.espressif/tools/activate_idf_v5.5.6.sh" >/dev/null 2>&1 \
  && cd "<work_copy>" && idf.py -DIDF_TARGET=esp32s3 build'
```

5. 逐行读 stdout/stderr，正则 `\[\s*(\d+)/(\d+)\]\s` 解析进度，回调 `progress_cb(min(done/total,0.99), "编译 done/total")`。
6. 保留最后 200 行日志。
7. 超时 900 秒；`cancel_token` 置位时 `proc.kill()`。
8. 失败返回 `ok=false`、`error`、`log_tail`。
9. 成功后用 `flash_controller.parse_flasher_args(work_copy/build)` 解析烧录清单；没有清单判失败。

预检 `build_env_checker.check(project)` 的阻断项：

| 代码 | 条件 |
|---|---|
| `E_PROJECT` | 工程不可用或不是 IDF |
| `E_NO_IDF` | 找不到 IDF |
| `E_IDF_BROKEN` | `idf.py --version` 失败或找不到 venv |
| `E_TARGET` | `target` 不是 `esp32s3` |
| `E_OFFLINE_DEPS` | 工程有 `idf_component.yml` 且离线 |
| `E_DISK` | 可用空间 < 2 GB |

告警项：工程未声明 target；工程 IDF 版本与本机前两位不一致。

---

## 13. 烧录

### 13.1 烧录清单

`parse_flasher_args(build_dir)` 优先级：

1. `build/flasher_args.json` 的 `flash_files`。
2. `build/flash_args` 每行 `0x... <file>`。
3. 标准回退：

```text
0x0     build/bootloader/bootloader.bin
0x8000  build/partition_table/partition-table.bin
0x10000 build/app.bin
```

文件不存在则跳过；最终为空报“没有可烧录的镜像”。

### 13.2 模式与闸门

| 模式 | 注入 | 备份要求 | 擦除 | 说明 |
|---|---|---|---|---|
| `inject` | 复制组件 + 改副本 CMake | **必须** `whole=true` 的整片备份 | 不额外擦除，直接写 | 没有备份返回 `NO_BACKUP` |
| `raw` | 不注入 | 不要求 | 先 `erase_flash` | 覆盖用户工程，烧完二次校验 |

`deploy()` 还要求：

- `port` 非空，否则 `NO_PORT`；
- `chip_id` 非空，否则 `NO_CHIP`；
- 工程能导入且 `kind=idf`，否则 `BAD_PROJECT`；
- 预检通过。

### 13.3 写入

`flash_controller.flash_bins(port, bins, baud=460800, progress_cb)`：

- 每个 `(offset, path)` 校验文件存在、offset 以 `0x` 开头。
- `settle(0.6)`。
- 按文件大小加权计算总进度。
- 依次尝试 `460800 → 230400 → 115200`。
- 每次 `run_streaming(["write_flash", "--port", port, "--baud", cur, offsets...])`，逐行解析 `( n % )`，按当前文件权重折算整体进度。
- `Wrote ...` 行用于切换到下一个文件的进度。
- 取消时返回“已停止烧录（芯片已被清空，可从备份回滚）”。
- 全部尝试失败返回最后一次输出。

### 13.4 校验

`verify_same_chip(port, expected_chip_id)`：

- 烧录后再次读芯片 ID。
- 读不到 → 失败；
- 与期望不一致 → 失败；
- 一致 → 成功。

`deploy()` 成功后：

- 写 `last_flash`（时间、work、mode、components、adapters）；
- 优先取原生 USB 口重连；
- 没有原生口则用当前端口重连；
- 重连会重新读芯片、重建链路、刷新状态。

### 13.5 停止

`POST /api/stop`：

- 无 `busy`：清取消标记、硬释放端口、清错误，返回 `{ok:true,stopping:"cleanup"}`。
- `busy=backup`：取消；等待任务退出最多 45 s；`cancel_token.clear()`；`discard_incomplete(chip)` 只删未登记的半份镜像；状态回到 `connected_no_fw` 或 `ready`。
- `busy=deploy` / `restore`：取消；等待退出；`cancel_token.clear()`；若端口和芯片存在，执行 `erase_flash` 整片擦除；状态置 `connected_no_fw`，清空组件/LED/按键；随后尝试重连。
- 停止烧录/回滚会清空开发板，这是破坏性收尾。

---

## 14. 备份、恢复与清理

### 14.1 芯片 ID

| 端口 | 来源 |
|---|---|
| 原生 USB | `serial.tools.list_ports` 的 `serial_number`，形如 `DC:B4:D9:16:B4:48`；小写保存 |
| CH340 / 其它 | `esptool chip_id`；`get_chip_id()` 先尝试 `usb_serial_mac()`，失败再走 esptool |

`get_chip_id()` 里的原生口兜底：

```text
--before no_reset --after watchdog_reset
```

该兜底在应用运行时仍可能失败；正常板子会先命中 USB 描述符，不会走到 esptool。

### 14.2 备份模式

| 模式 | 读取范围 | 适用 |
|---|---|---|
| 快速（默认） | 读 `0x8000` 分区表，找到最后一个分区末尾，按 64 KB 对齐 | 绝大多数 IDF 固件 |
| 完整（`full=true`） | `0x0` 到整片末尾 | 需要逐字节镜像 |
| 局部（`total_size`） | 指定大小 | 试跑/自检，不写索引 |

分区表 magic `0x50AA`，项偏移 `0x20`；找不到有效分区或全 `0xFF` 判空片：

- 空片写 0 字节文件；
- 索引 `data_size=0`、`sparse=true`；
- 回滚只整片擦除。

分区表读失败返回 `-1`，快速备份直接报错，提示重试或勾“完整备份”；不会偷偷跑 16 MB。

### 14.3 读取实现

| 场景 | 实现 |
|---|---|
| 原生 USB | `baud=921600`，先 `_read_whole()` 一次读 `span`；失败退回 `_read_chunked()` |
| CH340 | `_pick_baud()`：先预热，再 230400 / 115200 各试 3 次，成功即采用；默认 230400 |
| 块大小 | `CHUNK=0x10000`（64 KB）；230400/115200 都是 64 KB |
| 每块 | 独立 esptool 子进程，最多 3 次；失败释放 fd、退避 1.2–3.2 s |
| 整片读看门狗 | 75 秒无新输出就 `_drop_serial()` 让 esptool 退出，再回退分块 |
| 停止 | 删除未完成镜像，清临时文件，返回“已停止备份” |

### 14.4 抽样复核

`_verify_samples(port, dest, size, baud)`：

- 取样偏移：`0`、`size/2` 对齐到 32 KB、`size-0x8000`。
- 每块最多 32 KB，按当前 baud、230400、115200 依次试 2 次。
- 读回内容与本地文件对应区间比较。
- 只在不一致时返回 `False`，备份作废；链路抖动导致一块都没复核成功时返回 `True` + 本地 SHA-256，备份保留。
- 每块读取本身已过 esptool 的 MD5 digest 校验。

### 14.5 索引

`backups/index.json`：

```json
{
  "boards": {
    "dcb4d916b448": {
      "chip_id": "dc:b4:d9:16:b4:48",
      "file": "/.../dcb4d916b448_20261116_1459_1600KB.bin",
      "size": 16777216,
      "data_size": 1638400,
      "sparse": true,
      "whole": true,
      "sha256": "...",
      "baud": 921600,
      "seconds": 27.1,
      "created_at": "2026-11-16 14:59:43"
    }
  }
}
```

`backup_info(chip)` 校验文件存在且大小等于 `data_size`（没有则 `size`）。`has_backup(chip)` 还要求 `whole=true`。

### 14.6 恢复

`restore_flash(port, chip_id)`：

1. 取索引；没有返回“该芯片没有可用备份，禁止回烧”。
2. `settle(0.6)`。
3. `data_size`；`sparse` 或 `data_size=0` 时先 `erase_flash`（`BACKUP_BAUD=230400`，最多 2 次）。
4. 空片到此结束，进度“回滚完成（空片）”。
5. 非空片再 `write_flash --no-progress 0x0 <file>`，`RESTORE_BAUD=230400`，最多 2 次。
6. 进度“回烧完成”。

`app.rollback()` 在恢复前停链路；恢复成功后置 `connected_no_fw`，清空组件、LED、按键，提示“缺少画面固件，请刷录固件”。

### 14.7 拔板清理

`backups/pending_purge.json` 保存按芯片的备份清理登记：

```json
{
  "dcb4d916b448": {
    "chip_id": "dc:b4:d9:16:b4:48",
    "unplug_at": 1763,
    "due_at": 1763
  }
}
```

`data/cleanup_state.json` 保存全局插拔计时：

```json
{
  "last_present_at": 1763,
  "cleanup_due_at": 1763,
  "last_cleanup_at": null,
  "last_result": null
}
```

- 插入时 `cancel_purge(chip)`，撤销该芯片的备份清理登记；`cleanup_state.json` 的 `last_present_at` 在下一轮刷新，撤销缓存清理。
- `check_plug_state()` 每 30 秒执行：只要系统里还有任何 `usbmodem`、`usbserial`、`wchusbserial` 设备，就认为板子还插着，不登记、不删除。
- 没有任何串口设备且当前芯片已知 → `schedule_purge`，`due_at=now+3600`，同时写 `purge_at`。
- `purge_expired()` 到期后删除该芯片的镜像与索引条目。
- `app_data_cleanup.maybe_cleanup()` 到期后删除：
  - `data/workspace/` 下的全部构建副本；
  - `data/projects_imported/` 下的全部解压工程；
  - 旧版留在 `Application Support/ESP32-S3-Console-App/projects_imported/` 下的目录；
  - `data/backups/.chunk_*.tmp*` 遗留分块。
- 清理不动用户原始工程，不动 `backups/` 里的正式镜像与 `index.json`，不动 `port.txt`。
- `maybe_cleanup()` 在同一轮拔板事件里只执行一次，避免每 30 秒重复扫描；重新插回后从新的 `last_present_at` 重新计时。
- 正在备份、编译或烧录时跳过清理，等 `busy` 清空后再判断，避免删掉正在使用的工作区。
- `purge_expired()` 与 `maybe_cleanup()` 都受同一个总闸门约束：只要还有任何串口设备，直接返回，不删除。

### 14.8 备份速度

| 场景 | 实测 |
|---|---|
| 16 MB 空片快速备份 | 16.3 s（只写 0 字节镜像） |
| 1088 KB 有固件，CH340 230400 | 约 100 s，10.9 KB/s |
| 1600 KB 稀疏镜像，原生 USB 921600 | 27.1 s（`dcb4d916b448_20261116_1459_1600KB.bin`） |
| 完整 16 MB，CH340 | 约 25–30 分钟 |

速度取决于端口与转接芯片；原生 USB 的 921600 是虚拟串口，不受 CH340 晶振误差限制。

---

## 15. 打包

### 15.1 脚本

`mac_bundle_packager/build_app.sh`：

| 步 | 动作 |
|---|---|
| 1 | `rm -rf mac_bundle_packager/build`，重建 |
| 2 | PyInstaller：`--onedir --console --collect-all esptool`，入口 `backend/app.py` |
| 3 | 组装 `.app`：`Contents/MacOS`、`Contents/Resources/backend` |
| 4 | 复制 `frontend-ui`、`esp32_components`、`AppIcon.icns`、备份种子 |
| 5 | `swiftc` 编译 `ConsoleShell.swift`，链接 Cocoa/WebKit |
| 6 | 写 `Info.plist`、`xattr -cr`、`codesign --force --deep --sign -` |
| 7 | `hdiutil create -format UDZO` 生成桌面 DMG |

版本来源：

- 根目录 `VERSION` 保存唯一版本号，格式 `x.y.z`。
- `build_app.sh` 启动时读取并校验；文件缺失或格式错误直接退出。
- `CFBundleVersion` 与 `CFBundleShortVersionString` 都写入同一个版本号。
- 每次改动必须递增版本：修 bug 用 patch，新增功能用 minor；提交并推送 `main` 后重新打包、发布 `vVERSION` Release。

备份种子：

- 只复制 `index.json` 与索引登记的、非零大小的镜像。
- 首次启动 `_seed_from_bundle()` 把种子复制到数据目录，并改写索引中的绝对路径。
- 索引已存在时跳过，不再覆盖用户现有备份。

### 15.2 Info.plist

| 键 | 值 |
|---|---|
| `CFBundleName` / `CFBundleDisplayName` | `ESP32-S3 检修台` |
| `CFBundleIdentifier` | `com.xiaoyi.esp32s3console` |
| `CFBundleVersion` / `ShortVersion` | `VERSION` 文件的值（当前 `1.1.1`） |
| `CFBundleExecutable` | `console_shell` |
| `CFBundleIconFile` | `AppIcon` |
| `LSMinimumSystemVersion` | `12.0` |
| `NSHighResolutionCapable` | true |

### 15.3 窗口壳

- 创建 `NSWindow`，标题“ESP32-S3 检修台”，最小 940×620，自动保存窗口尺寸。
- `WKWebView` 加载后端 `http://127.0.0.1:<port>`。
- 注入 `openPanel` 消息处理器，弹 `NSOpenPanel`，把路径回传 `window.__pickedPath()`。
- 注入 `openBackups` 消息处理器，打开备份目录。
- 启动 `Contents/Resources/backend/console_app`，设置四个环境变量。
- 读 `port.txt`，最多等约 12 秒。
- 父进程退出时后台通过 `_watch_parent()` 自行退出，避免孤儿进程占端口。

### 15.4 打包前检查

1. 退出正在运行的 `ESP32-S3 检修台.app`；脚本会删除 `build/`。
2. 确认 `mac_bundle_packager/assets/AppIcon.icns` 存在。
3. 确认 `backups/index.json` 与镜像路径有效。
4. 运行 `tests/test_backend.py`。
5. 打包后检查：
   - `.app/Contents/Resources/backend/_internal/esptool/` 存在；
   - `Info.plist` 有 `CFBundleIconFile=AppIcon`；
   - `codesign -dv` 显示 ad-hoc；
   - `hdiutil verify` 校验 DMG；
   - 启动 App 后 `/api/state` 正常、`port.txt` 有端口、退出后进程归零。

### 15.5 签名与 Gatekeeper

当前是 ad-hoc 签名，没有 Developer ID、没有公证。换机器或从网络下载后，Gatekeeper 可能拦截；右键“打开”可以放行。分发时要说明这一点。

---

## 16. 测试与验收

### 16.1 后端 7 组

`tests/test_backend.py`：

| 测试 | 内容 |
|---|---|
| `test_crc` | `CRC16("123456789") == 0x29B1` |
| `test_parser` | 混合流：JSON、CRC 正确/错误、`OP_PART`、`OP_END`、帧组装 |
| `test_flasher_args` | `flasher_args.json` / `flash_args` / 标准偏移 |
| `test_injector` | 复制组件、副本 CMake 写入、原工程不动 |
| `test_importer` | zip slip、工程根、Arduino 拒绝 |
| `test_env` | IDF 预检 |
| `test_cleanup` | 拔板 1 小时计时、构建副本/导入工程/旧版目录清理、备份保留 |

运行：

```bash
/Users/xiaoyi/.espressif/tools/python/v5.5.6/venv/bin/python tests/test_backend.py
```

### 16.2 真机验收（2026-11-17）

对象：`/dev/cu.usbmodem21301`，芯片 `dc:b4:d9:16:b4:48`，固件 `console-comp-1.0`。

| 项目 | 结果 |
|---|---|
| `ConsoleApp.connect()` | `ok=True` |
| 状态 | `ready` |
| 芯片 ID | `dc:b4:d9:16:b4:48` |
| 组件 | `['led','key','display']` |
| 备份 | `dcb4d916b448_20261116_1459_1600KB.bin`，`whole=true`，`size=16777216`，`data_size=1638400`，`baud=921600` |
| 画面 | `fps≈1.5` |
| 后端测试 | 7 组全过 |

### 16.3 打包验收（2026-11-17）

| 项目 | 结果 |
|---|---|
| `.app` | 26 MB |
| DMG | 13 MB，`hdiutil verify` 通过 |
| `esptool` 进包 | `Contents/Resources/backend/_internal/esptool/` 存在 |
| 签名 | ad-hoc，`Identifier=com.xiaoyi.esp32s3console` |
| `Info.plist` | `CFBundleIconFile=AppIcon`，版本 1.0.0 |

### 16.4 未验收 / 不能声称通过

- 检修台点击按键反控开发板：未实现，不能作为验收项。
- 物理按键实时性：当前只验证了 KEY0–KEY3 被识别为 configured；按下/松开到界面的端到端延迟未单独量化。
- RESET / BOOT 采集：未实现。
- 适配包：`alientek` 未实测。
- 无 LCD 工程的画面：依赖 `ui_framebuffer()`；未对所有用户工程验证。
- Windows：无构建、无截图。

---

## 17. 已知限制

1. **按键反控缺失**：`/api/key` 发得出去，固件没有回调，硬件不会动作。界面 `.hot` 点击只做高亮。
2. **RESET / BOOT 不采集**：固件只有 KEY0–KEY3；`RESET=-1`，BOOT 没有配置项。
3. **物理按键延迟**：XL9555 输入端口只在 `console_info` 时刷新，主机 1.2 s 轮询一次，状态最多延迟约 1.2 s。
4. **画面路径限制**：无 `ui_framebuffer()` 且不走 `esp_lcd_panel_draw_bitmap` 的工程没有画面；适配包未验证。
5. **分辨率固定**：320×240；用户工程其它分辨率会裁剪或错位。
6. **单 bin 主流程不完整**：`deploy()` 只接受 IDF 工程；单 bin 主要走直烧 API。
7. **原样烧录是破坏性的**：先整片擦除；停止烧录/回滚也会整片擦除。
8. **注入模式必须整片备份**：`whole=true`；快速备份的空片镜像也算整片可回滚（回滚为擦除）。
9. **打包签名**：ad-hoc，未公证；首次打开可能需要右键。
10. **仅 macOS arm64**：没有 Windows/Linux 版本，也没有双系统截图。
11. **配置遗留**：`backend/config.py` 的 `BACKUP_BAUD=115200`、`BACKUP_TOTAL`、`BACKUP_CHUNK` 是旧常量，实际不参与备份；有效值在 `firmware_backup_manager.py`。`idf_build_scheduler` 的 `extra_dirs` 与 `source_injector.inject_args()` 是遗留路径，主流程不用。
12. **UI 是 WKWebView**：不是 AppKit 逐控件原生绘制；但窗口、图标、进程都是 App 自身，不经过浏览器地址栏。

---

## 18. 本轮变更

### v1.1.1 — 2026-11-17 文档修正

- `docs/board_pins.md` 补齐屏幕 SPI 引脚表与 ES8388 的 5 个 I2S 引脚。
- 数据源：《DNESP32S3 硬件参考手册》表 1.2.2.1 IO 资源分配总表。该表与本板 V1.3 `board.h` 的 SPI / I2C 共 8 项逐项一致（11/12/13/21/40/41/42/1），据此采信音频 5 项。
- I2S：MCLK=3、BCLK=46、LRCK=9、数据出=10、数据入=14。数据脚方向按 Codec 侧命名：`I2S_SDIN`(GPIO10) 对 ESP32 是输出，`I2S_SDOUT`(GPIO14) 对 ESP32 是输入；写反不报错但无声。
- 新增「不可用 IO」清单：33–37（Octal PSRAM）、0（BOOT/XL9555 INT）、19/20（USB D-/D+）、41/42（I2C0）、43/44（UART0）、38/39（触摸 I2C）。
- 澄清 GPIO 38/39/40 误传：实为 `CT_SCL` / `CT_SDA` / `LCD_DC`；网传「38/39/40 为 I2S」属 DNESP32S3B3（BOX3）底板。
- 标注 GPIO 9/10/14/46 与 RGBLCD 的 G3/G2/R7/G4 复用，换 RGB 屏底板会冲突。
- 标注 GPIO1 LED 电平极性两份文档冲突（本文件低电平、`board.h` 高电平），改为待实测，不再写死 `ACTIVE_LOW=1`。
- 标注 ES8388 I2C 地址 `0x10` 为待核实项，需上电扫 `0x08–0x77` 确认。
- 版本 `1.1.0` → `1.1.1`；重新打包并发布 `v1.1.1` Release。

### 2026-11-17

- `backend/app.py` 新增 `_native_usb_mac()`：原生 USB 从描述符 `iSerialNumber` 读芯片 MAC，不再跑 esptool，不复位芯片。
- `backend/firmware_backup_manager.py` 新增 `usb_serial_mac()`；`get_chip_id()` 优先返回 USB 描述符 MAC，拿不到才走 esptool；原生兜底参数改为 `--before no_reset --after watchdog_reset`。
- 真机验证：`status=ready`、`chip_id=dc:b4:d9:16:b4:48`、三组件、备份索引、`fps≈1.5`。
- 重新打包 `.app` 与 DMG；核对 `esptool`、图标、签名、DMG 校验。
- README 与本文按当前代码重写；`docs/board_pins.md`、`docs/verification.md`、`docs/维护进度-20261116.md` 同步现状。

### 历史回退

- 2026-11-16 的虚拟按键实验已回退：`frontend-ui` 不再通过 `.hot` 发 `/api/key`，固件不再注册 `console_proto_set_cmd_cb()`，`source_injector` 不再给 `main/app_main.c` 打弱符号补丁。`docs/rollback-backup/*.virtualkeys` 只是回退备份，不是当前实现。
- 因此“检修台按键反控开发板”仍是未实现项。
