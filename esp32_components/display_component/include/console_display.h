#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 启动画面采集。分配快照缓冲并起编码任务。无 PSRAM 且内存不足时返回 false。 */
bool console_display_start(void);

/** 供 esp_lcd 包装函数与各适配包调用：把一块绘制区域拷入快照并标记脏块。 */
void console_display_on_draw(int x_start, int y_start, int x_end, int y_end,
                             const void *color_data);

#ifdef __cplusplus
}
#endif
