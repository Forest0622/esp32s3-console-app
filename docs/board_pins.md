# 板型引脚映射

本文件记录各板型实际使用的 LED / 按键，供注入组件时核对 Kconfig。
只写已核对的事实；未核对的一律标「待核实」。

## ATK-DNESP32S3（正点原子）

| 项目 | 实际连接 | 组件行为 |
|---|---|---|
| 板载红灯 | GPIO1，低电平点亮 | `CONFIG_LED_READ_PIN=1`、`CONFIG_LED_READ_ACTIVE_LOW=1`，只读 |
| KEY0 | XL9555 输入端口 1 的 P17 | 已实现只读采集 |
| KEY1 | XL9555 输入端口 1 的 P16 | 已实现只读采集 |
| KEY2 | XL9555 输入端口 1 的 P15 | 已实现只读采集 |
| KEY3 | XL9555 输入端口 1 的 P14 | 已实现只读采集 |
| RESET | EN 复位脚，不是普通 GPIO | `CONFIG_KEY_READ_RESET=-1`，不采集 |
| BOOT | GPIO0，拉低进下载模式 | 没有采集项，不采集 |
| 屏幕 | ST7789 类 SPI 屏；复位/背光挂 XL9555 | 走 `esp_lcd` 时可在 `esp_lcd_panel_draw_bitmap` 截帧；工程暴露 `ui_framebuffer()` 时可直接整帧读 |

XL9555 默认参数：

| 参数 | 默认 |
|---|---|
| SDA | GPIO41 |
| SCL | GPIO42 |
| 地址 | `0x20` |
| 端口 | `I2C_NUM_0` |
| 速率 | 400 kHz |
| KEY0..KEY3 位 | P17..P14 |

按键只读实现：

- 只在主机发 `console_info` 时读 XL9555 输入端口 1，不轮询，避免清掉用户工程中断。
- 不写配置寄存器，不改方向，不主动安装 I2C 驱动；用户工程没装总线时报 `dbg=1/2`，不伪造状态。
- 读不到或引脚未使能时报 `-1`，界面显示「未配置」。

## 通用 ESP32-S3 DevKit

| 项目 | 说明 |
|---|---|
| 板载 LED | 常见 GPIO48（RGB）、GPIO38/GPIO2（单色）；必须按实物或原理图确认 |
| BOOT | GPIO0 |
| RESET | EN |
| 用户键 | 由用户工程定义，无统一标准 |

使用 GPIO 模式时把 `CONFIG_KEY_READ_SOURCE_GPIO` 打开，并逐个填 `CONFIG_KEY_READ_KEY0..KEY3`；不确定的填 `-1`。

## 填写原则

1. 不猜引脚。填错会读到无关信号，界面会出现看似正常的假状态。
2. 组件只读不配：不调用 `gpio_config`，不改上下拉、方向、中断。
3. 读不到报 `-1`，不伪造「已松开」。
4. RESET/BOOT 当前不采集；界面卡片显示「未配置」是功能缺失，不是驱动异常。
