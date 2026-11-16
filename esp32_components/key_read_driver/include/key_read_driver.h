#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 启动按键只读采集。任一脚传 -1 表示该键不采集（上报为 -1 未配置）。 */
void key_read_driver_start(int key0, int key1, int key2, int key3, int reset);

/* XL9555 板载按键（ATK-DNESP32S3：KEY0~KEY3 = P17~P14） */
void key_read_driver_start_exio(void);

/* 开机自启（构造函数调用，不需要用户工程显式调用） */
void key_read_driver_autostart(void);

#ifdef __cplusplus
}
#endif
