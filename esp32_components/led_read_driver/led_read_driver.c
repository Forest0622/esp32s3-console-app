/*
 * led_read_driver —— LED 硬件状态只读采集
 *
 * 硬约束（文档 8.1）：
 *   只读电平，**绝不调用 gpio_config**、不写 GPIO、不改上下拉/方向。
 *   原因：用户工程可能把该脚配成 LEDC 输出，一旦我们 gpio_config 成输入，
 *   他们的 LED 会直接失控。
 *
 * PWM 处理：
 *   LEDC 调光的脚电平高速翻转，单次采样必然抖动。
 *   这里用固定窗口密集采样统计高电平占比：接近 0/100% 判静态，否则报占空比。
 */

#include "led_read_driver.h"

#include "console_protocol.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "esp_system.h"
#include "esp_private/startup_internal.h"

#include <stdio.h>
#include <string.h>

#ifndef CONFIG_LED_READ_PIN
#define CONFIG_LED_READ_PIN 1
#endif
#ifndef CONFIG_LED_READ_IDLE_MS
#define CONFIG_LED_READ_IDLE_MS 100
#endif
#ifndef CONFIG_LED_READ_WINDOW_US
#define CONFIG_LED_READ_WINDOW_US 4000
#endif
#ifndef CONFIG_LED_READ_PWM_EPS_PCT
#define CONFIG_LED_READ_PWM_EPS_PCT 3
#endif

static int s_pin = -1;
static TaskHandle_t s_task = NULL;
/* 最后一次上报的状态，供主机问 console_info 时补发 */
static bool s_last_pwm = false;
static int  s_last_level = -1;
static int  s_last_duty = 0;
static bool s_has_state = false;

static void emit(int level, bool pwm, int duty)
{
    char buf[200];
    if (pwm) {
        snprintf(buf, sizeof(buf),
                 "{\"type\":\"led_status\",\"data\":{\"pins\":[{\"pin\":\"GPIO%d\","
                 "\"level\":%d,\"pwm\":true,\"duty_pct\":%d,\"active_low\":%d}]}}",
                 s_pin, level, duty, CONFIG_LED_READ_ACTIVE_LOW);
    } else {
        snprintf(buf, sizeof(buf),
                 "{\"type\":\"led_status\",\"data\":{\"pins\":[{\"pin\":\"GPIO%d\","
                 "\"level\":%d,\"pwm\":false,\"active_low\":%d}]}}",
                 s_pin, level, CONFIG_LED_READ_ACTIVE_LOW);
    }
    console_proto_send_line(buf);
}

/* 主机问 console_info 时补发一次当前灯态 */
static void led_report_now(void)
{
    if (!s_has_state || s_pin < 0) {
        return;
    }
    emit(s_last_level, s_last_pwm, s_last_duty);
}

static void led_task(void *arg)
{
    (void)arg;
    bool last_pwm = false;
    int  last_level = -1;
    int  last_duty = -1;
    bool first = true;

    for (;;) {
        /* 窗口内密集采样，统计高电平占比 */
        int64_t t0 = esp_timer_get_time();
        int highs = 0;
        int total = 0;
        while ((esp_timer_get_time() - t0) < CONFIG_LED_READ_WINDOW_US) {
            if (gpio_get_level((gpio_num_t)s_pin)) {
                highs++;
            }
            total++;
        }

        int ratio = total > 0 ? (highs * 100) / total : 0;
        bool pwm = (ratio > CONFIG_LED_READ_PWM_EPS_PCT) &&
                   (ratio < 100 - CONFIG_LED_READ_PWM_EPS_PCT);
        int level = pwm ? (ratio >= 50 ? 1 : 0) : (ratio >= 50 ? 1 : 0);
        int duty = pwm ? ratio : 0;

        if (!console_proto_report_enabled(1)) {
            vTaskDelay(pdMS_TO_TICKS(CONFIG_LED_READ_IDLE_MS));
            continue;
        }

        /* 只在变化时上报，避免刷屏占带宽 */
        if (first || pwm != last_pwm || level != last_level ||
            (pwm && (duty - last_duty >= 2 || last_duty - duty >= 2))) {
            emit(level, pwm, duty);
            s_last_pwm = last_pwm = pwm;
            s_last_level = last_level = level;
            s_last_duty = last_duty = duty;
            s_has_state = true;
            first = false;
        }
        vTaskDelay(pdMS_TO_TICKS(CONFIG_LED_READ_IDLE_MS));
    }
}

void led_read_driver_start(int gpio_num)
{
    if (gpio_num < 0) {
        return;
    }
    if (s_task != NULL) {
        return;
    }
    s_pin = gpio_num;
    console_proto_init();
    console_proto_register_component("led");
    console_proto_on_info(led_report_now);
    xTaskCreatePinnedToCore(led_task, "led_read", 4096, NULL, 4, &s_task, 0);
}


/* 自动启动：同上，注入只加组件目录，用户工程不会主动调用 start()。
   板载 LED 就是 GPIO 电平，默认 LED 脚由 Kconfig 给出（本板 GPIO1）。 */
void led_read_driver_autostart(void)
{
#if CONFIG_LED_READ_PIN >= 0
    led_read_driver_start(CONFIG_LED_READ_PIN);
#endif
}

/* 同 key 组件：constructor 在 IDF 里不执行，改用系统初始化钩子 + 任务。 */
static void led_boot_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(200));
    led_read_driver_autostart();
    vTaskDelete(NULL);
}

ESP_SYSTEM_INIT_FN(console_led_boot, SECONDARY, BIT(0), 1000)
{
#ifdef CONFIG_LED_READ_PIN
    xTaskCreate(led_boot_task, "led_boot", 3072, NULL, 4, NULL);
#endif
    return ESP_OK;
}
