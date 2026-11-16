/*
 * 正点原子（ALIENTEK）画面采集适配包
 *
 * 适用：用户工程自己写 SPI 刷屏、不走 esp_lcd，但调用的是 LCD_ShowPicture。
 * 约束（文档 8.17）：必须先调 __real_ 完成原绘制，再取副本；不改入参/返回值。
 *
 * 状态：未实测（adapter.json verified=false），默认不会被注入。
 */

#include "console_display.h"

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

/* 原始函数签名需与用户工程 lcd.h 一致；此处按正点原子常见定义声明 */
extern void __real_LCD_ShowPicture(uint16_t x, uint16_t y, uint16_t width,
                                   uint16_t height, const uint8_t *pic)
    __attribute__((weak));

void __wrap_LCD_ShowPicture(uint16_t x, uint16_t y, uint16_t width,
                            uint16_t height, const uint8_t *pic)
{
    if (__real_LCD_ShowPicture != NULL) {
        __real_LCD_ShowPicture(x, y, width, height, pic);
    }
    if (pic != NULL && width > 0 && height > 0) {
        console_display_on_draw((int)x, (int)y, (int)x + (int)width,
                                (int)y + (int)height, pic);
    }
}
