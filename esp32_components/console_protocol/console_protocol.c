/*
 * console_protocol —— 主机通讯公共层
 *
 * 设计依据：开发文档 6 章（修订后混合流）
 *   文本行：JSON，用于 ping/led/key/console_status 等低频遥测
 *   二进制帧：A5 5A | u32 len LE | u16 crc16 | payload，用于画面（base64 太慢，实测 0.06 fps）
 *
 * 硬约束：本组件不碰 GPIO、不碰用户业务；只负责收发。
 */

#include "console_protocol.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/portmacro.h"
#include "esp_attr.h"
#include "sdkconfig.h"

#if CONFIG_CONSOLE_REPORT_PORT_USB
#include "driver/usb_serial_jtag.h"
#else
#include "driver/uart.h"
#endif

#include <string.h>
#include <stdio.h>

#define RX_LINE_MAX 1024
#define TX_CHUNK    2048

static SemaphoreHandle_t s_tx_mutex = NULL;
static console_cmd_cb_t  s_cmd_cb = NULL;
static char   s_line[RX_LINE_MAX];
static size_t s_line_len = 0;
static bool   s_inited = false;
static bool   s_init_claimed = false;
static portMUX_TYPE s_init_mux = portMUX_INITIALIZER_UNLOCKED;

/* console_info 时补发快照的钩子 */
#define INFO_HOOK_MAX 4
static void (*s_info_hooks[INFO_HOOK_MAX])(void);
static int s_info_hook_count = 0;

void console_proto_on_info(void (*hook)(void))
{
    if (hook == NULL || s_info_hook_count >= INFO_HOOK_MAX) {
        return;
    }
    s_info_hooks[s_info_hook_count++] = hook;
}

/* 已启用组件与上报开关（主机可用 set_report 关闭某一路） */
#define MAX_COMPONENTS 4
static char s_components[MAX_COMPONENTS][16];
static int  s_component_count = 0;
static bool s_report[3] = { true, true, true };   /* 0=display 1=led 2=key */

static void send_line_raw(const char *line);

void console_proto_register_component(const char *name)
{
    if (name == NULL || s_component_count >= MAX_COMPONENTS) {
        return;
    }
    for (int i = 0; i < s_component_count; i++) {
        if (strncmp(s_components[i], name, sizeof(s_components[0]) - 1) == 0) {
            return;
        }
    }
    strncpy(s_components[s_component_count], name, sizeof(s_components[0]) - 1);
    s_components[s_component_count][sizeof(s_components[0]) - 1] = '\0';
    s_component_count++;
}

bool console_proto_report_enabled(int provider)
{
    if (provider < 0 || provider > 2) {
        return true;
    }
    return s_report[provider];
}

/* 内置命令处理：ping / console_info / set_report */
static void cmd_handler(const char *line)
{
    if (strstr(line, "\"ping\"") != NULL) {
        send_line_raw("{\"resp\":\"pong\"}");
        return;
    }
    if (strstr(line, "\"console_info\"") != NULL) {
        char buf[320];
        char list[80] = "";
        for (int i = 0; i < s_component_count; i++) {
            if (i > 0) {
                strncat(list, ",", sizeof(list) - strlen(list) - 1);
            }
            strncat(list, "\"", sizeof(list) - strlen(list) - 1);
            strncat(list, s_components[i], sizeof(list) - strlen(list) - 1);
            strncat(list, "\"", sizeof(list) - strlen(list) - 1);
        }
        snprintf(buf, sizeof(buf),
                 "{\"type\":\"console_status\",\"data\":{\"fw\":\"console-comp-1.0\","
                 "\"components\":[%s],\"report_port\":\"%s\",\"log_suppressed\":%s}}",
                 list, console_proto_port_name(),
                 console_proto_log_suppressed() ? "true" : "false");
        send_line_raw(buf);
        for (int i = 0; i < s_info_hook_count; i++) {
            s_info_hooks[i]();
        }
        return;
    }
    if (strstr(line, "\"set_report\"") != NULL) {
        if (strstr(line, "\"display\":false") != NULL) s_report[0] = false;
        if (strstr(line, "\"display\":true") != NULL)  s_report[0] = true;
        if (strstr(line, "\"led\":false") != NULL)     s_report[1] = false;
        if (strstr(line, "\"led\":true") != NULL)      s_report[1] = true;
        if (strstr(line, "\"key\":false") != NULL)     s_report[2] = false;
        if (strstr(line, "\"key\":true") != NULL)      s_report[2] = true;
        return;
    }
    if (s_cmd_cb) {
        s_cmd_cb(line);
    }
}

uint16_t console_crc16(const uint8_t *data, uint16_t len)
{
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (uint8_t b = 0; b < 8; b++) {
            if (crc & 0x8000) {
                crc = (uint16_t)((crc << 1) ^ 0x1021);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
    }
    return crc;
}

static void tx_bytes(const uint8_t *data, size_t len)
{
#if CONFIG_CONSOLE_REPORT_PORT_USB
    usb_serial_jtag_write_bytes(data, len, pdMS_TO_TICKS(2000));
#else
    uart_write_bytes(UART_NUM_0, data, (int)len);
#endif
}

static int rx_read(uint8_t *buf, int max, int timeout_ms)
{
#if CONFIG_CONSOLE_REPORT_PORT_USB
    return (int)usb_serial_jtag_read_bytes(buf, max, pdMS_TO_TICKS(timeout_ms));
#else
    return uart_read_bytes(UART_NUM_0, buf, max, pdMS_TO_TICKS(timeout_ms));
#endif
}

static void rx_task(void *arg)
{
    (void)arg;
    uint8_t buf[256];
    for (;;) {
        int n = rx_read(buf, sizeof(buf), 20);
        for (int i = 0; i < n; i++) {
            char c = (char)buf[i];
            if (c == '\n' || c == '\r') {
                if (s_line_len > 0) {
                    s_line[s_line_len] = '\0';
                    cmd_handler(s_line);
                    s_line_len = 0;
                }
            } else if (s_line_len < RX_LINE_MAX - 1) {
                s_line[s_line_len++] = c;
            } else {
                s_line_len = 0;   /* 超长行丢弃，避免溢出 */
            }
        }
        if (n <= 0) {
            vTaskDelay(pdMS_TO_TICKS(5));
        }
    }
}

void console_proto_init(void)
{
    /* led 与 key 两个组件在各自任务里同时调用本函数：必须原子地「抢」初始化权，
       否则两个任务都看到 s_inited=false，会重复 install UART 驱动，第二次返回
       错误后队列为 NULL，紧接着写串口就 panic（实测就是这个 LoadProhibited 重启）。 */
    bool claimed = false;
    portENTER_CRITICAL(&s_init_mux);
    if (!s_init_claimed) {
        s_init_claimed = true;
        claimed = true;
    }
    portEXIT_CRITICAL(&s_init_mux);
    if (!claimed) {
        while (!s_inited) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        return;
    }

#if CONFIG_CONSOLE_SUPPRESS_LOG
    /* 与协议帧混流时会丢帧：压制日志是必要的代价，UI 上会标注「日志已压制」 */
    esp_log_level_set("*", ESP_LOG_NONE);
#endif

    s_tx_mutex = xSemaphoreCreateMutex();

#if CONFIG_CONSOLE_REPORT_PORT_USB
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    /* 默认 TX 缓冲只有 64 字节，帧块一次写 2 KB 会被截断丢弃：
       实测 JSON（几十字节）能通，画面帧 0 帧，就是这个原因。 */
    cfg.tx_buffer_size = 8192;
    cfg.rx_buffer_size = 4096;
    usb_serial_jtag_driver_install(&cfg);
#else
    uart_config_t ucfg = {
        .baud_rate  = CONFIG_CONSOLE_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    /* 不设置引脚：UART0 的 TX/RX 由 ROM/bootloader 已配好，重设会打架 */
    uart_driver_install(UART_NUM_0, 8192, 8192, 0, NULL, 0);
    uart_param_config(UART_NUM_0, &ucfg);
#endif

    xTaskCreatePinnedToCore(rx_task, "console_rx", 4096, NULL, 5, NULL, 0);
    s_inited = true;
}

void console_proto_set_cmd_cb(console_cmd_cb_t cb)
{
    s_cmd_cb = cb;
}

bool console_proto_send_line(const char *line)
{
    if (!s_inited || line == NULL) {
        return false;
    }
    send_line_raw(line);
    return true;
}

static void send_line_raw(const char *line)
{
    size_t len = strlen(line);
    if (len == 0 || len > 512 || s_tx_mutex == NULL) {
        return;
    }
    char buf[520];
    memcpy(buf, line, len);
    buf[len++] = '\n';

    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    tx_bytes((const uint8_t *)buf, len);
    xSemaphoreGive(s_tx_mutex);
}

bool console_proto_send_frame(const uint8_t *payload, uint16_t len)
{
    if (!s_inited || payload == NULL || len == 0) {
        return false;
    }
    uint8_t header[8];
    header[0] = 0xA5;
    header[1] = 0x5A;
    header[2] = (uint8_t)(len & 0xFF);
    header[3] = (uint8_t)((len >> 8) & 0xFF);
    header[4] = 0;
    header[5] = 0;
    uint16_t crc = console_crc16(payload, len);
    header[6] = (uint8_t)(crc & 0xFF);
    header[7] = (uint8_t)((crc >> 8) & 0xFF);

    /* 头部与负载必须连续，中间不允许插入遥测行，故全程持锁 */
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    tx_bytes(header, sizeof(header));
    size_t sent = 0;
    while (sent < len) {
        uint16_t part = (uint16_t)((len - sent) > TX_CHUNK ? TX_CHUNK : (len - sent));
        tx_bytes(payload + sent, part);
        sent += part;
    }
    xSemaphoreGive(s_tx_mutex);
    return true;
}

const char *console_proto_port_name(void)
{
#if CONFIG_CONSOLE_REPORT_PORT_USB
    return "usb_serial_jtag";
#else
    return "uart0";
#endif
}

bool console_proto_log_suppressed(void)
{
#if CONFIG_CONSOLE_SUPPRESS_LOG
    return true;
#else
    return false;
#endif
}
