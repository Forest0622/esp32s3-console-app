# 更新日志

## v1.0.0 — 2026-11-17

macOS arm64 首个安装包。

### 安装包

- `ESP32-S3 检修台.dmg`，13 MB
- `.app` 26 MB，ad-hoc 签名，未做 Apple 公证
- 首次打开如被 Gatekeeper 拦截，右键选择“打开”

### 本次实现

- 原生 USB 连接从 USB 描述符 `iSerialNumber` 读取芯片 MAC，不打开串口、不复位芯片。
- 连接状态机覆盖 `idle`、`no_device`、`connecting`、`connected_no_fw`、`ready`、`busy`、`error`。
- 读取并显示开发板实时画面，支持适应窗口、1:1、滚轮缩放、拖拽。
- 读取板载 LED 的 GPIO 电平与 PWM 占空比，按 `active_low` 解释亮灭。
- 读取 KEY0–KEY3（XL9555 P17–P14），显示按下与松开。
- 支持 zip、tar、tar.gz、tgz、文件夹导入；识别最浅 ESP-IDF 工程根；拒绝 Arduino/PlatformIO。
- 组件注入到工作区副本的 `components/`，修改副本 `CMakeLists.txt` 的 `EXTRA_COMPONENT_DIRS`，原工程不改。
- 注入模式编译并烧录画面、指示灯、按键组件；原样烧录不要求备份，先整片擦除。
- 备份按芯片 MAC 隔离；快速备份只读到最后分区末尾；完整备份读整片；抽样复核首/中/末 32 KB。
- 回滚支持稀疏镜像，先整片擦除再写镜像；空片备份只擦除。
- 开发板拔出满 1 小时后删除该芯片备份；插回撤销；有任意串口设备在线时不执行删除。
- 备份完成后自动继续烧录，不需要再次点击“编译并烧录”。
- 打包为原生 macOS 窗口（NSWindow + WKWebView），带 App 图标与备份种子。

### 修复

- 原生 USB 连接后 `chip_id` 为空，导致备份、回滚、注入烧录被闸门锁死。
- 原生 USB 复位会停在 stub/下载模式；改为连接时不跑 esptool、不复位。
- 打包版 `esptool` 缺失、DMG 校验失败、图标未写入 `Info.plist`。

### 已知限制

- 检修台点击按键不会反控开发板；固件 `console_proto_set_cmd_cb()` 没有注册者。
- 只采集 KEY0–KEY3；RESET、BOOT 不采集。
- 画面依赖用户工程暴露 `ui_framebuffer()` 或经过 `esp_lcd_panel_draw_bitmap`；两者都没有时没有画面。
- 画面分辨率固定 320×240。
- 正点原子画面适配包 `verified=false`，未实测。
- 仅提供 macOS arm64 版本，没有 Windows 版本，也没有 Windows 截图。
