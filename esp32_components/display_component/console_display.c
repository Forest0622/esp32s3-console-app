/*
 * display_component —— 画面只读采集
 *
 * 原理（文档 3.5.3 / 3.5.5.1）：
 *   ESP32-S3 没有「读回屏内容」的硬件能力，只能在数据送屏的路径上截一份副本。
 *   主拦截点是 esp_lcd_panel_draw_bitmap，用链接期 --wrap 实现：
 *   壳函数先调 __real_* 让原绘制照常发生，再把区域拷进快照，行为不变。
 *
 * 只传脏块：320x240 全屏 153600 B，@921600 需 1.68 s，全量发不可用；
 * 因此按 1024 像素一块做增量，只有变化的块才发。
 */

#include "console_display.h"

#include "console_protocol.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "esp_lcd_panel_ops.h"
#include "esp_private/startup_internal.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_err.h"
#include "esp_log.h"

#include <string.h>

#ifndef CONFIG_CONSOLE_DISPLAY_WIDTH
#define CONFIG_CONSOLE_DISPLAY_WIDTH 320
#endif
#ifndef CONFIG_CONSOLE_DISPLAY_HEIGHT
#define CONFIG_CONSOLE_DISPLAY_HEIGHT 240
#endif
#ifndef CONFIG_CONSOLE_DISPLAY_MAX_FPS
#define CONFIG_CONSOLE_DISPLAY_MAX_FPS 20
#endif

#define OP_END  0x00
#define OP_PART 0x05
#define BLOCK_PIXELS 1024

static uint16_t *s_snap = NULL;
static uint8_t  *s_dirty = NULL;
static int       s_blocks = 0;
static int       s_w = CONFIG_CONSOLE_DISPLAY_WIDTH;
static int       s_h = CONFIG_CONSOLE_DISPLAY_HEIGHT;
static TaskHandle_t s_task = NULL;
static bool s_seeded = false;

/* 首帧补采：组件先于用户 UI 起来，屏上的静态区域（标题以下那一大片）只在
   开机时画过一次，之后只重绘变化的小块，快照就永远缺它们。正点原子例程暴露
   整帧缓冲 ui_framebuffer()，取一份即可补齐；没有这个符号的工程直接跳过。 */
extern uint16_t *ui_framebuffer(void) __attribute__((weak));
void console_display_on_draw(int x_start, int y_start, int x_end, int y_end,
                             const void *color_data);
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
/* 整帧差分：拿用户工程自己的 framebuffer 当唯一真源，逐块 memcmp，
   变了的块拷进快照并标脏。这样无论用户 UI 是整屏刷还是局部刷，镜像都是全的。
   没有 ui_framebuffer 的工程仍然走 draw-bitmap 拦截那条路。 */
static void console_display_sync_ui(void)
{
    if (ui_framebuffer == NULL || s_snap == NULL || s_dirty == NULL) {
        return;
    }
    uint16_t *fb = ui_framebuffer();
    if (fb == NULL) {
        return;
    }
    for (int b = 0; b < s_blocks; b++) {
        size_t px = (size_t)b * BLOCK_PIXELS;
        int cnt = (px + BLOCK_PIXELS <= (size_t)s_w * s_h)
                      ? BLOCK_PIXELS : (s_w * s_h - (int)px);
        if (cnt <= 0) {
            continue;
        }
        size_t bytes = (size_t)cnt * 2;
        if (memcmp((uint8_t *)s_snap + px * 2, (uint8_t *)fb + px * 2, bytes) == 0) {
            continue;
        }
        memcpy((uint8_t *)s_snap + px * 2, (uint8_t *)fb + px * 2, bytes);
        taskENTER_CRITICAL(&s_lock);
        s_dirty[b] = 1;
        taskEXIT_CRITICAL(&s_lock);
    }
}

void console_display_try_seed(void)
{
    if (ui_framebuffer == NULL) {
        return;
    }
    uint16_t *fb = ui_framebuffer();
    if (fb == NULL) {
        return;
    }
    console_display_on_draw(0, 0, s_w, s_h, fb);
    s_seeded = true;
}


/* 必须声明为强引用（不能 weak）：
 * --wrap 会把对 __real_X 的引用解析成 X。若这里写 weak，链接器不会因为一个
 * 弱引用去抽取静态库成员，esp_lcd_panel_draw_bitmap 的实现就永远不会被拉进来，
 * __real 恒为 NULL —— 画面会被悄悄跳过（本组件开发时踩过这个坑）。
 * 代价：即使工程不用 esp_lcd，也会把它链进来（几 KB），换来的是确定性。
 */
extern esp_err_t __real_esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t panel,
                                                  int x_start, int y_start,
                                                  int x_end, int y_end,
                                                  const void *color_data);

esp_err_t __wrap_esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t panel,
                                           int x_start, int y_start,
                                           int x_end, int y_end,
                                           const void *color_data)
{
    /* 先让原绘制发生，再取副本：顺序不能反，否则用户屏会缺一帧/顺序错乱 */
    esp_err_t ok = __real_esp_lcd_panel_draw_bitmap(panel, x_start, y_start, x_end, y_end, color_data);
    console_display_on_draw(x_start, y_start, x_end, y_end, color_data);
    return ok;
}

void console_display_on_draw(int x_start, int y_start, int x_end, int y_end,
                             const void *color_data)
{
    if (s_snap == NULL || color_data == NULL) {
        return;
    }
    if (x_start < 0) x_start = 0;
    if (y_start < 0) y_start = 0;
    if (x_end > s_w) x_end = s_w;
    if (y_end > s_h) y_end = s_h;
    if (x_start >= x_end || y_start >= y_end) {
        return;
    }

    const uint8_t *src = (const uint8_t *)color_data;
    int row_bytes = (x_end - x_start) * 2;

    for (int y = y_start; y < y_end; y++) {
        size_t dst_off = ((size_t)y * s_w + x_start) * 2;
        memcpy((uint8_t *)s_snap + dst_off, src, (size_t)row_bytes);
        src += row_bytes;

        size_t first = (size_t)y * s_w + x_start;
        size_t last  = first + (size_t)(x_end - x_start) - 1;
        int b0 = (int)(first / BLOCK_PIXELS);
        int b1 = (int)(last / BLOCK_PIXELS);
        if (b1 >= s_blocks) {
            b1 = s_blocks - 1;
        }
        taskENTER_CRITICAL(&s_lock);
        for (int b = b0; b <= b1; b++) {
            s_dirty[b] = 1;
        }
        taskEXIT_CRITICAL(&s_lock);
    }
}

static void console_display_emit_info(void)
{
    char buf[64];
    snprintf(buf, sizeof(buf),
             "{\"type\":\"display_info\",\"w\":%d,\"h\":%d}", s_w, s_h);
    console_proto_send_line(buf);
}


/* USB 原生口带宽足够（约 12 Mbps），整帧 153 KB 只要 ~0.13 s。
   这里把所有块按 8 块一批发出去，只有最后一批带 OP_END，
   解析端会把这些 PART 拼成一整帧再回调，不会撕裂。 */
static void display_send_full(uint8_t *payload)
{
    int sent = 0;
    for (int b = 0; b < s_blocks; b += 8) {
        uint16_t len = 0;
        int upto = (b + 8 <= s_blocks) ? b + 8 : s_blocks;
        for (int i = b; i < upto; i++) {
            int px = (i * BLOCK_PIXELS + BLOCK_PIXELS <= s_w * s_h)
                         ? BLOCK_PIXELS : (s_w * s_h - i * BLOCK_PIXELS);
            if (px <= 0) {
                continue;
            }
            payload[len++] = OP_PART;
            payload[len++] = (uint8_t)i;
            payload[len++] = (uint8_t)(px & 0xFF);
            payload[len++] = (uint8_t)((px >> 8) & 0xFF);
            memcpy(payload + len, s_snap + (size_t)i * BLOCK_PIXELS, (size_t)px * 2);
            len += (uint16_t)(px * 2);
            sent++;
        }
        if (upto >= s_blocks) {
            payload[len++] = OP_END;
        }
        console_proto_send_frame(payload, len);
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    taskENTER_CRITICAL(&s_lock);
    memset(s_dirty, 0, s_blocks);
    taskEXIT_CRITICAL(&s_lock);
}


/* 把用户工程的 framebuffer 当成一块「虚拟屏」整帧发出去：
   即使板子上没插 LCD，只要 ui.c 往 s_fb 里画过，电脑端就能当第二块屏显示。
   不依赖 draw_bitmap 拦截，也不依赖脏块累积，画面天然完整。 */
static void display_send_fb_full(uint16_t *fb, uint8_t *payload)
{
    for (int b = 0; b < s_blocks; b += 8) {
        uint16_t len = 0;
        int upto = (b + 8 <= s_blocks) ? b + 8 : s_blocks;
        for (int i = b; i < upto; i++) {
            int px = (i * BLOCK_PIXELS + BLOCK_PIXELS <= s_w * s_h)
                         ? BLOCK_PIXELS : (s_w * s_h - i * BLOCK_PIXELS);
            if (px <= 0) {
                continue;
            }
            payload[len++] = OP_PART;
            payload[len++] = (uint8_t)i;
            payload[len++] = (uint8_t)(px & 0xFF);
            payload[len++] = (uint8_t)((px >> 8) & 0xFF);
            memcpy(payload + len, fb + (size_t)i * BLOCK_PIXELS, (size_t)px * 2);
            len += (uint16_t)(px * 2);
        }
        if (upto >= s_blocks) {
            payload[len++] = OP_END;
        }
        console_proto_send_frame(payload, len);
        vTaskDelay(pdMS_TO_TICKS(2));
    }
}

static void display_task(void *arg)
{
    (void)arg;
    uint8_t *payload = (uint8_t *)heap_caps_malloc(8 * (4 + BLOCK_PIXELS * 2) + 32,
                                                   MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (payload == NULL) {
        payload = (uint8_t *)heap_caps_malloc(8 * (4 + BLOCK_PIXELS * 2) + 32,
                                             MALLOC_CAP_8BIT);
    }
    if (payload == NULL) {
        s_task = NULL;
        vTaskDelete(NULL);
        return;
    }

    /* 原生 USB 的驱动在 300ms 时可能还没装好，早先只在 start 里 init 一次，
       失败就永远不上报（实测切到 USB 后 display 直接消失、0 帧）。
       这里在任务里补一次 init/注册，确保 USB 就绪后组件能接上。 */
    vTaskDelay(pdMS_TO_TICKS(600));
    console_proto_init();
    console_proto_register_component("display");
    ESP_LOGI("disp", "start: snap=%p blocks=%d", (void *)s_snap, s_blocks);
    console_proto_on_info(console_display_emit_info);
    console_display_emit_info();
    ESP_LOGI("disp", "task up, blocks=%d snap=%p", s_blocks, (void *)s_snap);

    const int period = 1000 / (CONFIG_CONSOLE_DISPLAY_MAX_FPS > 0
                                   ? CONFIG_CONSOLE_DISPLAY_MAX_FPS : 10);

    int seed_tries = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(
            strcmp(console_proto_port_name(), "usb_serial_jtag") == 0 ? 200 : period));
        /* 用户工程的 UI 很可能比我们晚初始化（WiFi 起来后才建 framebuffer），
           300ms 时还是 NULL。这里每秒再试一次，拿到就补一整帧；之后就靠整帧差分。 */
        if (seed_tries >= 0 && (++seed_tries % 100) == 0) {   /* 约每 5 秒 */
            console_display_try_seed();
            if (seed_tries > 800) {                            /* 40 秒后收手 */
                seed_tries = -1;
            }
        }
        console_display_sync_ui();

        if (strcmp(console_proto_port_name(), "usb_serial_jtag") == 0 &&
            ui_framebuffer != NULL) {
            uint16_t *fb = ui_framebuffer();
            if (fb != NULL) {
                display_send_fb_full(fb, payload);
                continue;
            }
        }

        if (!console_proto_report_enabled(0)) {
            /* 主机关了画面上报：清掉脏标记，避免恢复后一次性补发一大堆 */
            taskENTER_CRITICAL(&s_lock);
            memset(s_dirty, 0, s_blocks);
            taskEXIT_CRITICAL(&s_lock);
            continue;
        }

        int picked[8];
        int picked_n = 0;
        taskENTER_CRITICAL(&s_lock);
        for (int b = 0; b < s_blocks && picked_n < 8; b++) {
            if (s_dirty[b]) {
                picked[picked_n++] = b;
            }
        }
        taskEXIT_CRITICAL(&s_lock);
        if (picked_n == 0) {
            continue;
        }

        uint16_t len = 0;
        for (int i = 0; i < picked_n; i++) {
            int b = picked[i];
            int px = (b * BLOCK_PIXELS + BLOCK_PIXELS <= s_w * s_h)
                         ? BLOCK_PIXELS : (s_w * s_h - b * BLOCK_PIXELS);
            if (px <= 0) {
                continue;
            }
            payload[len++] = OP_PART;
            payload[len++] = (uint8_t)b;
            payload[len++] = (uint8_t)(px & 0xFF);
            payload[len++] = (uint8_t)((px >> 8) & 0xFF);
            memcpy(payload + len, s_snap + (size_t)b * BLOCK_PIXELS, (size_t)px * 2);
            len += (uint16_t)(px * 2);
        }
        payload[len++] = OP_END;

        console_proto_send_frame(payload, len);

        taskENTER_CRITICAL(&s_lock);
        for (int i = 0; i < picked_n; i++) {
            s_dirty[picked[i]] = 0;
        }
        taskEXIT_CRITICAL(&s_lock);
    }
}

bool console_display_start(void)
{
    if (s_task != NULL) {
        return true;
    }

    size_t bytes = (size_t)s_w * s_h * 2;
    s_snap = (uint16_t *)heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_snap == NULL) {
        ESP_LOGE("disp", "snap alloc failed");
        s_snap = (uint16_t *)heap_caps_malloc(bytes, MALLOC_CAP_8BIT);
    }
    if (s_snap == NULL) {
        return false;
    }
    memset(s_snap, 0, bytes);

    s_blocks = (s_w * s_h + BLOCK_PIXELS - 1) / BLOCK_PIXELS;
    s_dirty = (uint8_t *)heap_caps_calloc((size_t)s_blocks, 1, MALLOC_CAP_8BIT);
    if (s_dirty == NULL) {
        ESP_LOGE("disp", "dirty alloc failed");
        heap_caps_free(s_snap);
        s_snap = NULL;
        return false;
    }

    console_proto_init();
    console_proto_register_component("display");
    console_proto_on_info(console_display_emit_info);
    console_display_emit_info();

    /* 首帧补采：组件在 300ms 才起来，用户工程的第一屏往往在那之前就画完了。
       之后屏上只重绘变化的小块，快照里就永远缺那些静态区域（实测镜像里只剩
       标题和 CHIP/MAC 两行，下面全黑）。用户工程若暴露整帧缓冲（正点原子
       例程的 ui_framebuffer()），这里直接取一份全屏，弱符号，没有就跳过。 */
    console_display_try_seed();
    xTaskCreatePinnedToCore(display_task, "console_disp", 6144, NULL, 3, &s_task, 1);
    if (s_task == NULL) {
        return false;
    }
    return true;
}

/* 启动钩子：注入的工程里没有任何地方会调用 console_display_start（文档假设
   「组件自动接管」，实际漏了这一步，结果屏在跑、主机永远收不到帧）。 */
static void display_boot_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(300));
    console_display_start();
    vTaskDelete(NULL);
}

ESP_SYSTEM_INIT_FN(console_display_boot, SECONDARY, BIT(0), 1000)
{
    xTaskCreate(display_boot_task, "disp_boot", 3072, NULL, 4, NULL);
    return ESP_OK;
}
