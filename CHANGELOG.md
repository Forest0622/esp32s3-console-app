# 更新日志

## v1.1.1 — 2026-11-17

### 修正

- `docs/board_pins.md` 补齐屏幕 SPI 引脚表与音频 ES8388 的 5 个 I2S 引脚。数据源为《DNESP32S3 硬件参考手册》表 1.2.2.1 IO 资源分配总表。
- 交叉核对依据：该表与该板 V1.3 `board.h` 的 SPI 与 I2C 共 8 项逐项一致（MOSI 11 / SCLK 12 / MISO 13 / CS 21 / DC 40 / SDA 41 / SCL 42 / LED 1），据此采信音频 5 项。
- I2S 引脚：MCLK=3、BCLK=46、LRCK=9、数据出=10、数据入=14。
- 标注 I2S 数据脚按 Codec 侧命名：`I2S_SDIN`（GPIO10）对 ESP32 是输出，`I2S_SDOUT`（GPIO14）对 ESP32 是输入。写反不报错但无声。
- 新增「不可用 IO」清单：33–37（Octal PSRAM，35/36/37 官方标勿用）、0（BOOT/XL9555 INT）、19/20（USB D-/D+）、43/44（UART0）、41/42（I2C0）。
- 标注 GPIO 38/39 实为触摸 IC 的 `CT_SCL`/`CT_SDA`；网传「38/39/40 为 I2S」属 DNESP32S3B3（BOX3）底板，不适用本板。
- 标注 GPIO 9/10/14/46 与 RGBLCD 的 G3/G2/R7/G4 复用，换 RGB 屏底板会冲突。
- 标注 GPIO1 板载 LED 电平极性冲突：本文件原记低电平点亮，`board.h` 记高电平点亮，现改为待实测，不再写死 `ACTIVE_LOW=1`。
- 标注 ES8388 I2C 地址 0x10 为待核实项，需上电扫 0x08–0x77 确认（应同时命中 0x10 与 0x20）。

### 安装包

- `ESP32-S3 检修台.dmg`，9,694,346 字节
- SHA-256 `b20f0572ba4f8095c9a6163121a5d94448f875b60f0b58120e027582312ee4cb`
- `.app` 16 MB，ad-hoc 签名，arm64，未公证
- `hdiutil verify` 通过

## v1.1.0 — 2026-11-17

### 变更

- 开发板拔出满 1 小时，除该芯片备份外，同时清理 `data/workspace` 构建副本、`data/projects_imported` 导入工程、旧版外层 `projects_imported` 和遗留分块临时文件。
- 插回开发板立即撤销清理；有任意串口设备在线时不执行。
- 清理只动 App 数据目录，不动用户原始工程、正式备份镜像、`index.json` 和 `port.txt`。
- 新增 `VERSION` 文件；`build_app.sh` 从该文件写入 `CFBundleVersion` 与 `CFBundleShortVersionString`。
- 每次改动递增版本并同步 GitHub；修 bug 用 patch，新增功能用 minor。

### 安装包

- `ESP32-S3 检修台.dmg`，9,694,864 字节
- SHA-256 `3dc2d23fd08531c4a804719468184dc2303718ec50d480ec10559366e3d657aa`
- `.app` 16 MB，ad-hoc 签名，arm64，未公证
- `hdiutil verify` 通过

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
