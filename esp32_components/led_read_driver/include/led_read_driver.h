#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 启动 LED 硬件状态只读采集。pin < 0 时不启动（UI 显示缺少驱动）。 */
void led_read_driver_start(int gpio_num);

#ifdef __cplusplus
}
#endif
