# 适配包实测登记表

只有在本表登记了实测结果的适配包，才允许把 `adapter.json` 的 `verified` 改成 `true`
（开发文档 8.18）。未实测的适配包不会出现在 UI 可勾选列表里。

## 当前状态：无任何适配包通过实测

| 适配包 | 目标驱动族 | verified | 实测工程 | 实测日期 | 结论 |
|---|---|---|---|---|---|
| alientek | 正点原子 `LCD_ShowPicture` | false | — | — | 未实测 |

所以 UI 上「适配包」下拉框现在显示「暂无可用」。这是**规则生效**的表现，不是缺功能。

---

## 板卡实测记录（非适配包，用于证明链路本身可用）

| 日期 | 板卡 | 芯片 ID | Flash | 结论 |
|---|---|---|---|---|
| 2026-11-12 | 无屏空白 ESP32-S3（CH340，`/dev/cu.usbserial-2140`） | `98:88:e0:00:c4:dc` | 16 MB，全 `0xFF` | 整片备份 16,777,216 B 通过；注入 `display+led` 编译烧录后握手成功、灯态实时上报；一键回滚后读回全 `0xFF`、界面回到「缺少画面固件 / 缺少驱动」。**画面帧未产生（板子无屏）** |

---

## 实测必须记录的项目

1. 用户工程路径与 IDF 版本
2. 屏幕型号、分辨率、颜色格式（RGB565 / BGR565）
3. 实际调用链：哪个函数把像素送到屏（用于确认 wrap 点）
4. 首帧是否完整（有整屏校验，不是「看着像」）
5. 局部刷新是否正确（改一个矩形，只有该矩形变化）
6. 帧率实测值
7. 是否影响原工程显示（对比刷固件前后屏幕外观）

## 已知主流方案的采画面路径

| 用户工程刷屏方式 | 采集方式 | 状态 |
|---|---|---|
| `esp_lcd_panel_draw_bitmap` | 链接期 `--wrap`（已编译验证，真机未验） | 可用，待真机确认 |
| 自写 SPI + `LCD_ShowPicture`（正点原子） | 适配包 wrap 同名符号 | 未实测 |
| LVGL + `flush_cb` | 需适配包 wrap `flush_cb` 注册点 | 未实测 |
| TFT_eSPI（Arduino 生态） | 不支持（本项目不接 Arduino 工程） | 不支持 |
| RGB 直驱面板（`esp_lcd_rgb_panel`） | `draw_bitmap` 不走 panel_io，当前截不到 | 未支持，需另做帧缓冲读取 |

## 编译级验证记录（本机真实执行）

| 项目 | 结果 |
|---|---|
| IDF 版本 | v5.5.6（`/Users/xiaoyi/esp/v5.5.6/esp-idf`） |
| 目标 | esp32s3 |
| 夹具 | `tests/fixture_project` |
| 编译 | 通过，0 错误（1082 步），app 0x37a10 字节 |
| `--wrap` 生效 | 已验证：main 的绘制调用跳到 `__wrap_esp_lcd_panel_draw_bitmap`，壳内再调用真实的 `esp_lcd_panel_draw_bitmap` |
| 后端自测 | `tests/test_backend.py` 全部通过（CRC / 混合流解析 / 烧录清单 / 注入 / 导入 / 预检） |
| 真机烧录 | **未执行**（当时无 `/dev/cu.usbserial*` 设备） |

## 打包产物验证记录（2026-11-12 第二轮，真机 + 真板）

测试对象：`mac_bundle_packager/build/ESP32-S3 检修台.app`（PyInstaller onedir + Swift 壳），
数据目录 `~/Library/Application Support/ESP32-S3-Console-App/data`，端口 8765。

| 项目 | 操作 | 结果 |
|---|---|---|
| esptool 是否进包 | `find .app -iname "esptool*"` | `_internal/esptool`、`_internal/esptool-4.12.0.dist-info` 均在 |
| 芯片 ID | POST `/api/connect {"port":"/dev/cu.usbserial-2140"}` | `chip_id=98:88:e0:00:c4:dc`（修复前恒为空） |
| 备份可见性 | GET `/api/state` 的 `backup` | `whole=true`，`size=16777216`，`file` 指向数据目录副本（备份种子生效） |
| 「检测设备」按钮 | 界面真实点击 | 后端日志「扫描串口：1 个候选 → 打开串口 → 板子没有上报组件」，界面切「缺少画面固件」 |
| 「选择…」按钮 | 界面真实点击 | 系统 Open 面板弹出（`dialog Open, ID: open-panel`），含「选择 ESP-IDF 工程文件夹，或选择 zip 压缩包」 |
| 「1:1」按钮 | 界面真实点击 + 截图 | 板图放大、针脚文字变大 |
| 「适应窗口」按钮 | 界面真实点击 + 截图 | 回到基准缩放 |
| 失败诊断 | 芯片读取失败时日志 | 打印 `LAST_ERROR` 最后一行真实原因，不再是模糊话 |

结论：开发环境能跑通不等于打包版能跑通。esptool（第三方库）、数据目录（备份迁移）、
系统对话框（访达面板）三类功能必须在 `.app` 上单独验收。

## 第三轮验证记录（2026-11-12 19:00–19:45，备份提速 + 四条硬规则）

测试对象：开发后端 `python backend/app.py --port 8798` 与打包版
`mac_bundle_packager/build/ESP32-S3 检修台.app`（端口 8765），同一块板
`/dev/cu.usbserial-2140`（MAC `98:88:e0:00:c4:dc`，Flash 16 MB，板上为 `hello_world`）。

| 项目 | 操作 | 结果 |
|---|---|---|
| 快速备份（有固件） | POST `/api/backup {"full":false}` | 1088 KB / 100 s / 10.9 KB/s，`baud=230400`，`data_size=1114112` |
| 打包版快速备份 | 同上，走 `.app` 8765 | 1088 KB / **108.5 s**；证明冻结版 `--esptool-cli` 子进程路径可用 |
| 原样烧录免备份 | 清空索引后 POST `/api/deploy {"mode":"raw"}` | 编译 → `erase_flash` → 写入 → `verify_same_chip` → 重连，`last_flash.mode="raw"`，`error=""` |
| 注入闸门（dev） | 清空索引后 POST `/api/deploy {"mode":"inject"}` | `status=error`、`error=注入组件前必须先做整片备份…`、`hint=点「整片备份」后再烧录；或改用「原样烧录」`；日志 `deploy 被拦下（NO_BACKUP）` |
| 注入闸门（打包版） | 同上，`.app` 8765 | 同上（冻结版同样拦截），索引已还原 |
| 一键回滚（稀疏镜像） | POST `/api/rollback` | `busy=restore` → 擦除 + 写 1088 KB → `connected_no_fw`，`progress_text=已恢复原固件` |
| 拔板清理（模块级） | 伪造 `TE:ST:C0:FF:EE` + 临时文件，`schedule_purge(at=now-PURGE_GRACE+3)` | 未到期不删；3 s 后 `purge_expired()` 返回该芯片，文件与索引条目均被删；`purge_deadline` = +3600 s；`cancel_purge` 后为 None |
| 备份完整性策略 | `_verify_samples` 改动后 | 首/中/末 32 KB 各复核一次；链路抖动导致复核没做成时**不再作废整份备份**（每块本身过 esptool MD5） |
| 注入未备份弹窗 | 用真实 `frontend-ui/index.html` + 无头 Chrome 渲染 `askModal` | 标题「还没有整片备份」、橙框步骤提示、按钮「立即备份，然后再烧录」/「取消」，见 `docs/images/验收_注入未备份弹窗.png` |

已知未验：`/api/disconnect` 后 App 仍持 2 个 fd（串口未真正释放）；真人拔插重连；第二块板回滚隔离。

## 第四轮验证记录（2026-11-12 20:20–20:54，打包版逐项功能验收）

测试对象：重新打包的 `mac_bundle_packager/build/ESP32-S3 检修台.app`（端口 8765），
板 `/dev/cu.usbserial-1140`（CH340，MAC `dc:b4:d9:16:b4:48`，16 MB），空白板注入构建。
截图：`docs/images/验收_打包版_空白板组件识别.png`、`docs/images/验收_停止与烧录后的就绪态.png`。

| 项目 | 操作 | 结果 |
|---|---|---|
| 组件真的进构建 | `source_injector.inject_components` 复制组件 + 副本 `CMakeLists` 写**绝对路径** `EXTRA_COMPONENT_DIRS`、注释 `MINIMAL_BUILD ON` | `build_components` 出现 `console_protocol/led_read_driver/key_read_driver`；map 中 `key_read_driver_autostart` 5 处 |
| 组件开机自启 | `ESP_SYSTEM_INIT_FN` 建任务（延时 200/260 ms 后启驱动） | 921600 收到 `pong`、`console_status`、`key_status`、`led_status` |
| 双任务并发初始化 | `console_proto_init` 临界区抢占 | 修掉重复 install UART 导致的 `LoadProhibited` 重启 |
| App 识别组件（打包版） | POST `/api/connect {"port":"…-1140"}` | `status=ready`、`components=["led","key"]`、`fw=console-comp-1.0`、`led=GPIO1`、`key=KEY0~KEY3` |
| zip 导入（打包版） | POST `/api/import {"path":"~/Desktop/ATK-DNESP32S3-Dashboard-Photo.zip"}` | `ok=true`、`kind=idf`、`target=esp32s3`、解压到 `Application Support/…/data/projects_imported/…`（可写目录，dmg 只读卷问题已消除） |
| ① 进度条 | POST `/api/deploy {"source":"/tmp/hw_raw","mode":"raw"}` 轮询 `/api/state` | `0.03→0.18→0.32→0.46→0.69→0.86→0.99`（编译）→ `0.00 烧录中` → `0.30` → `0.99` → `1.00 烧录完成`，不再恒 100% |
| ④ 停止备份 | POST `/api/backup`，6 s 后 POST `/api/stop` | 返回 `{"ok":true,"stopping":"backup"}`；状态收尾为 `已停止备份（未完成的镜像已删除）`，`has_backup=false`，索引无该芯片条目 |
| ④ 停止烧录 | POST `/api/deploy` 后在「烧录中」阶段 POST `/api/stop` | `已停止并清空开发板`、`芯片已整片擦除`、`status=connected_no_fw`；`read_flash 0x10000 4KB` 全 `0xFF`（真擦了） |
| ⑤ 回滚隔离（真实数据） | App 索引中保留旧板 `98:88:e0:00:c4:dc` 的整片备份；当前板 `dc:b4:d9:16:b4:48` | 当前板 `has_backup=false`、`backup_info=null`，回滚被 `该芯片没有备份，禁止回滚（跨设备不允许）` 拦下；`discard_backup` 只删本芯片 |
| 注入闸门（打包版复验） | 无备份时 POST `/api/deploy {"mode":"inject"}` | `NO_BACKUP`：`注入组件前必须先做整片备份` |
| 恢复可用状态 | POST `/api/deploy {"source":"/tmp/hw_inject","mode":"raw"}` | 编译→擦除→烧录→重连，`status=ready`、`components=["led","key"]` |

本轮验收中修掉的三个 bug：
1. `stop()` 收尾擦除被自己设的取消旗子拦下（`erase_flash` 进门即 `已停止`）——板子根本没清空。改为收尾前 `cancel_token.clear()`。
2. 用户主动停止被记成 `ERROR`（界面一片红）。改为 `已停止` 分支，不置 `status=error`。
3. 断开后直接点烧录时拿空端口起 esptool，报 `Could not open , the port is busy`；新增 `NO_PORT` 闸门：`还没有连接开发板，请先点「检测设备」再烧录`。`/api/connect` 同时接受 `port` 与 `device` 字段。

仍未验：屏幕画面帧（本板无 LCD，display 组件未注入）、真人按键物理触发（键值目前全 0）、双板同时插的实机对比（系统仍只枚举到一个 CH340）。

## 第五轮验证记录（2026-11-12 21:10–21:45，zip 刷入 + 画面/灯/按键同步）

用户反馈三处：刷完固件后红灯、LCD 画面、实体按键都不同步；zip 工程刷入报「工程根不存在」。

| 项目 | 根因 | 修复 / 实测 |
|---|---|---|
| zip 刷入 | `deploy()` 把 `.zip` 路径直接交给 `prepare_copy()`，它要求目录，于是抛 `工程根不存在`（`workspace_manager.py:32`） | `deploy()` 先 `project_importer.import_project()` 解压并取 `project_root`；打包版 `POST /api/deploy {"source":"~/Desktop/ATK-DNESP32S3-Dashboard-Photo.zip","components":["display","led","key"],"mode":"inject"}` → 编译 1124 步、烧录 1%→99%、`烧录完成` |
| LCD 画面 | `display_component` 编进去了但**没有任何地方调用 `console_display_start()`**；即使启动，用户第一屏在 300 ms 前就画完，之后只重绘小块，静态区域永远进不了快照 | 加 `ESP_SYSTEM_INIT_FN` 启动钩子；再按 5 s 周期从用户工程 `ui_framebuffer()` 补一整帧（弱符号，工程没有就跳过）；实测 `frame_seq` 从 0 涨到 1000+，`/api/frame.bmp` 能拿到 SYSTEM STATUS / CHIP / MAC 画面 |
| 实体按键 | 组件 260 ms 就启动，用户工程 `board_init` 里 `i2c_driver_install` 还没跑；且先调 `i2c_param_config`，在驱动已装时返回的不是 `INVALID_STATE`，被当成「拿不到总线」永久放弃（诊断字段 `dbg=1`） | 改为**先直接读** XL9555，失败再 install；启动后每 400 ms 重试最多 30 s。实测 `key={"configured":["KEY0","KEY1","KEY2","KEY3"],"state":{...0}}` |
| 红灯状态 | 板载红灯低电平点亮（GPIO1），界面按 `level==1` 判「亮」，方向相反 | 组件上报 `active_low`（Kconfig 默认 y），前端与板图都用它换算；实测 `led=[{pin:GPIO1,level:0,active_low:1}]` → 界面「GPIO1 · 亮」 |

最终状态：`status=ready`、`components=['led','key','display']`、`fps≈5`、按键全部配置完成。
截图：`docs/images/验收_画面_灯_按键同步.png`。

仍未验：画面镜像里静态下半屏是否与实物完全一致（此刻板子页面只画到 CHIP/MAC 两行）；真人按键物理按下时的 `按下/松开` 变化；WiFi 未连时用户工程自身不刷新下半屏。

以上第 1–5 轮是历史记录，日期、端口、板卡与当时结论按原文保留；不代表 2026-11-17 的现状。

## 第六轮验证记录（2026-11-17，原生 USB 主链路 + 最终打包）

测试对象：`/dev/cu.usbmodem21301`（原生 USB-Serial-JTAG），芯片 `dc:b4:d9:16:b4:48`，Flash 16 MB，当前固件 `console-comp-1.0`。

| 项目 | 操作 | 结果 |
|---|---|---|
| 原生 USB 读芯片 MAC | `ConsoleApp.connect('/dev/cu.usbmodem21301')` | `ok=True`；`chip_id=dc:b4:d9:16:b4:48`，取自 USB 描述符 `iSerialNumber`，未打开串口、未复位芯片 |
| 连接状态 | GET `/api/state` | `status=ready`，`fw=console-comp-1.0`，`components=['led','key','display']` |
| 画面 | `/api/state.fps` 与 `/api/frame.bmp` | `fps≈1.5`；BMP 320×240 可读 |
| 备份索引 | GET `/api/state.backup` | `dcb4d916b448_20261116_1459_1600KB.bin`，`whole=true`，`size=16777216`，`data_size=1638400`，`baud=921600`，`seconds=27.1` |
| 无链路读芯片 | `firmware_backup_manager.get_chip_id('/dev/cu.usbmodem21301')` | 直接返回 `dc:b4:d9:16:b4:48` |
| 后端自测 | `python -m pytest tests/test_backend.py -q` | 6 组全部通过 |
| 打包 | `mac_bundle_packager/build_app.sh` | 六步完成；`.app` 26 MB；DMG 13 MB |
| DMG 校验 | `hdiutil verify ~/Desktop/ESP32-S3 检修台.dmg` | VALID |
| esptool 进包 | 检查 `_internal/esptool/` | 存在 |
| 签名 | `codesign -dv` | ad-hoc，arm64，`com.xiaoyi.esp32s3console` |
| Info.plist | `plutil -p` | `CFBundleIconFile=AppIcon`，版本 1.0.0 |

本轮修复的真实缺陷：

| 现象 | 根因 | 修复 |
|---|---|---|
| 原生 USB 连接后 `chip_id` 为空，`has_backup` 恒假，备份/回滚/注入烧录全部被闸门锁死 | 原生分支跳过 esptool，没有读芯片 ID 的路径 | `app.py` 新增 `_native_usb_mac()`，从 USB 描述符 `iSerialNumber` 读 MAC；`firmware_backup_manager` 新增 `usb_serial_mac()`，`get_chip_id()` 优先使用 |
| 原生口 `hard_reset` 后板子停在 stub/下载模式；`usb_reset` 在应用运行时读出 `Invalid head of packet` | 原生 USB-Serial-JTAG 的复位语义与 UART 不同 | 原生连接不跑 esptool、不复位；兜底才用 `--before no_reset --after watchdog_reset` |

仍未实现/未验证：

- 检修台按键反控开发板：`console_proto_set_cmd_cb()` 没有注册者，`/api/key` 不驱动硬件。
- RESET / BOOT 采集：固件只采 KEY0–KEY3。
- 正点原子适配包：`verified=false`，未实测。
- Windows 版本与双系统截图：不存在。
