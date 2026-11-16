/*
 * 编译验证夹具（不是产品代码）
 *
 * 目的：证明三套组件能在真实 ESP-IDF 工程里编译并链接，
 *      且 --wrap=esp_lcd_panel_draw_bitmap 真的把调用重定向到了 __wrap_*。
 *
 * 验证方法：编译后检查 ELF 里同时存在 __wrap_esp_lcd_panel_draw_bitmap
 *          与 __real_esp_lcd_panel_draw_bitmap 两个符号。
 */

#include "console_protocol.h"
#include "console_display.h"
#include "led_read_driver.h"
#include "key_read_driver.h"

#include "esp_lcd_panel_ops.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include <stdbool.h>

/* volatile：防止编译器把这一支判断优化掉，从而丢掉对 esp_lcd 的符号引用 */
static volatile bool s_force_link = false;

static void touch_esp_lcd(void)
{
    if (s_force_link) {
        esp_lcd_panel_draw_bitmap(NULL, 0, 0, 1, 1, NULL);
    }
}

void app_main(void)
{
    console_proto_init();
    console_display_start();
    led_read_driver_start(CONFIG_LED_READ_PIN);
    key_read_driver_start(CONFIG_KEY_READ_KEY0, CONFIG_KEY_READ_KEY1,
                          CONFIG_KEY_READ_KEY2, CONFIG_KEY_READ_KEY3,
                          CONFIG_KEY_READ_RESET);

    touch_esp_lcd();

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
