/*
 * key_read_driver —— KEY0..KEY3 硬件状态只读采集（RESET/BOOT 不采集）
 *
 * 硬约束（文档 8.2）：
 *   只调 gpio_get_level()，**不接管中断、不改 IOMUX、不改上下拉、不调 gpio_config**。
 *   原因：改配置会破坏用户工程原有按键逻辑（含其消抖参数与中断处理）。
 *
 * 「未配置」判定：
 *   读该脚 IO_MUX 的 FUN_IE（输入使能位）。若输入路径未使能，读数恒为 0，
 *   此时上报 -1 表示未配置，避免伪造成「已松开」。
 */

#include "key_read_driver.h"

#include "console_protocol.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "esp_system.h"
#include "esp_private/startup_internal.h"

#include "soc/soc.h"

#if CONFIG_KEY_READ_SOURCE_XL9555
#include "driver/i2c.h"
/* XL9555（ATK-DNESP32S3 板载 I2C IO 扩展）：按键 KEY0~KEY3 挂在 P17~P14。
   这里只读输入寄存器，绝不写配置寄存器 —— 写配置会改掉用户工程设的输入/输出方向。 */
static bool     s_exio        = false;
static uint16_t s_exio_bits   = 0xFFFF;
static int      s_exio_shift[5] = { -1, -1, -1, -1, -1 };
static int      s_dbg_stage   = 0;   /* 0=ok 1=拿不到总线 2=读不到扩展芯片 */
#endif
#include "soc/io_mux_reg.h"

#include <stdio.h>
#include <string.h>

#ifndef CONFIG_KEY_READ_ACTIVE_LOW
#define CONFIG_KEY_READ_ACTIVE_LOW 1
#endif
#ifndef CONFIG_KEY_READ_POLL_MS
#define CONFIG_KEY_READ_POLL_MS 30
#endif
#ifndef CONFIG_KEY_READ_DEBOUNCE_MS
#define CONFIG_KEY_READ_DEBOUNCE_MS 20
#endif

#define KEY_COUNT 5

static const char *const KEY_NAMES[KEY_COUNT] = { "KEY0", "KEY1", "KEY2", "KEY3", "RESET" };
static int  s_pins[KEY_COUNT]     = { -1, -1, -1, -1, -1 };
static bool s_configured[KEY_COUNT] = { false, false, false, false, false };
static int  s_state[KEY_COUNT]    = { -1, -1, -1, -1, -1 };
static TaskHandle_t s_task = NULL;
static bool exio_read_port1(uint16_t *out);

static bool pin_input_enabled(int gpio)
{
#if defined(IO_MUX_GPIO0_REG) && defined(FUN_IE)
    uint32_t reg = REG_READ(IO_MUX_GPIO0_REG + 4 * gpio);
    return (reg & FUN_IE) != 0;
#else
    return true;
#endif
}

static void emit_all(void)
{
    char buf[320];
#if CONFIG_KEY_READ_SOURCE_XL9555
    /* 只在主机查询时读一次：XL9555 的 INT 会因「任何一次端口读」而被清除，
       我们若 30 ms 轮询一次，用户工程永远等不到中断，实体按键就整片失灵。
       读放在这里，既能让检修台拿到状态，又不会持续抢总线。 */
    if (s_exio) {
        uint16_t fresh = 0;
        if (exio_read_port1(&fresh)) {
            s_exio_bits = fresh;
        }
    }
#endif
    char conf[64] = "";
    char state[96] = "";

    for (int i = 0; i < KEY_COUNT; i++) {
        if (s_configured[i]) {
            if (conf[0] != '\0') {
                strncat(conf, ",", sizeof(conf) - strlen(conf) - 1);
            }
            strncat(conf, "\"", sizeof(conf) - strlen(conf) - 1);
            strncat(conf, KEY_NAMES[i], sizeof(conf) - strlen(conf) - 1);
            strncat(conf, "\"", sizeof(conf) - strlen(conf) - 1);
        }
        if (state[0] != '\0') {
            strncat(state, ",", sizeof(state) - strlen(state) - 1);
        }
        char kv[24];
        snprintf(kv, sizeof(kv), "\"%s\":%d", KEY_NAMES[i], s_state[i]);
        strncat(state, kv, sizeof(state) - strlen(state) - 1);
    }

    snprintf(buf, sizeof(buf),
             "{\"type\":\"key_status\",\"data\":{\"configured\":[%s],\"state\":{%s},\"dbg\":%d}}",
             conf, state, s_dbg_stage);
    console_proto_send_line(buf);
}

static int read_pressed(int i)
{
    if (!s_configured[i]) {
        return -1;
    }
#if CONFIG_KEY_READ_SOURCE_XL9555
    if (s_exio) {
        int raw = (s_exio_bits >> s_exio_shift[i]) & 1;
#if CONFIG_KEY_READ_ACTIVE_LOW
        return raw ? 0 : 1;
#else
        return raw ? 1 : 0;
#endif
    }
#endif
    int raw = gpio_get_level((gpio_num_t)s_pins[i]);
#if CONFIG_KEY_READ_ACTIVE_LOW
    return raw ? 0 : 1;
#else
    return raw ? 1 : 0;
#endif
}

static void key_task(void *arg)
{
    (void)arg;
    int cand[KEY_COUNT];
    int cand_ms[KEY_COUNT];
    for (int i = 0; i < KEY_COUNT; i++) {
        cand[i] = read_pressed(i);
        cand_ms[i] = 0;
    }
    emit_all();

    for (;;) {
        bool changed = false;
        for (int i = 0; i < KEY_COUNT; i++) {
            int now = read_pressed(i);
            if (now < 0) {
                if (s_state[i] != -1) {
                    s_state[i] = -1;
                    changed = true;
                }
                continue;
            }
            if (now == s_state[i]) {
                cand_ms[i] = 0;
                continue;
            }
            if (now == cand[i]) {
                cand_ms[i] += CONFIG_KEY_READ_POLL_MS;
            } else {
                cand[i] = now;
                cand_ms[i] = 0;
            }
            if (cand_ms[i] >= CONFIG_KEY_READ_DEBOUNCE_MS) {
                s_state[i] = now;
                cand_ms[i] = 0;
                changed = true;
            }
        }
        if (changed && console_proto_report_enabled(2)) {
            emit_all();
        }
        vTaskDelay(pdMS_TO_TICKS(CONFIG_KEY_READ_POLL_MS));
    }
}

void key_read_driver_start(int key0, int key1, int key2, int key3, int reset)
{
    if (s_task != NULL) {
        return;
    }
    s_pins[0] = key0;
    s_pins[1] = key1;
    s_pins[2] = key2;
    s_pins[3] = key3;
    s_pins[4] = reset;

    int usable = 0;
    for (int i = 0; i < KEY_COUNT; i++) {
        if (s_pins[i] < 0) {
            s_configured[i] = false;
            s_state[i] = -1;
            continue;
        }
        s_configured[i] = pin_input_enabled(s_pins[i]);
        s_state[i] = s_configured[i] ? read_pressed(i) : -1;
        if (s_configured[i]) {
            usable++;
        }
    }
    if (usable == 0) {
        /* 全都没配置：不启动，让主机显示「缺少驱动」，不产生假数据 */
        return;
    }

    console_proto_init();
    console_proto_register_component("key");
    console_proto_on_info(emit_all);
    xTaskCreatePinnedToCore(key_task, "key_read", 4096, NULL, 2, &s_task, 1);
}


#if CONFIG_KEY_READ_SOURCE_XL9555
/* 读 XL9555 输入端口 1（P10~P17）。驱动已在运行时直接复用，不重装、不改配置。 */
static bool exio_read_port1(uint16_t *out)
{
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    if (cmd == NULL) {
        return false;
    }
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (CONFIG_KEY_READ_XL9555_ADDR << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, 0x01, true);            /* 输入端口 1 */
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (CONFIG_KEY_READ_XL9555_ADDR << 1) | I2C_MASTER_READ, true);
    uint8_t b = 0;
    i2c_master_read_byte(cmd, &b, I2C_MASTER_NACK);
    i2c_master_stop(cmd);
    esp_err_t err = i2c_master_cmd_begin(I2C_NUM_0, cmd, pdMS_TO_TICKS(50));
    i2c_cmd_link_delete(cmd);
    if (err != ESP_OK) {
        return false;
    }
    *out = (uint16_t)b;
    return true;
}

/* 只 install 一次；若用户工程已经装过这个端口，安装会返回 INVALID_STATE，直接复用。 */
static bool exio_ensure(void)
{
    static bool inited = false;
    if (inited) {
        return true;
    }
    i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = CONFIG_KEY_READ_XL9555_SDA,
        .scl_io_num = CONFIG_KEY_READ_XL9555_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = 400000,
    };
    esp_err_t err = i2c_param_config(I2C_NUM_0, &conf);
    if (err == ESP_OK) {
        err = i2c_driver_install(I2C_NUM_0, conf.mode, 0, 0, 0);
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
            return false;      /* 端口被别的驱动（如 i2c_master）占了：不抢，让主机显示未配置 */
        }
    } else if (err != ESP_ERR_INVALID_STATE) {
        return false;
    }
    inited = true;
    return true;
}
#endif

void key_read_driver_start_exio(void)
{
#if CONFIG_KEY_READ_SOURCE_XL9555
    if (s_task != NULL) {
        return;
    }
    /* 先直接读：用户工程通常已经把 I2C_NUM_0 装好了（正点原子 board.c 里
       i2c_driver_install）。早先无条件先 i2c_param_config，那个调用在驱动已装时
       返回的不是 INVALID_STATE，于是被当成「拿不到总线」永久放弃（dbg=1）。 */
    /* 只读、不装总线。用户工程 board.c 自己会 i2c_driver_install；
       我们若抢先在 300 ms 装上另一个驱动/速率，用户的按键读取会整片失效
       （实测：注入后实体键完全无反应）。读不到就老实报未配置，绝不改总线。 */
    if (exio_read_port1(&s_exio_bits)) {
        s_dbg_stage = 0;
    } else {
        s_dbg_stage = 2;
        return;
    }
    s_exio = true;
    int base = CONFIG_KEY_READ_XL9555_BASE_BIT;   /* 默认 4：P14..P17 */
    s_exio_shift[0] = base + 3;                    /* KEY0 = P17 */
    s_exio_shift[1] = base + 2;                    /* KEY1 = P16 */
    s_exio_shift[2] = base + 1;                    /* KEY2 = P15 */
    s_exio_shift[3] = base + 0;                    /* KEY3 = P14 */
    s_exio_shift[4] = -1;                          /* RESET 不在扩展上 */
    for (int i = 0; i < 4; i++) {
        s_configured[i] = true;
        s_state[i] = read_pressed(i);
    }
    console_proto_init();
    console_proto_register_component("key");
    console_proto_on_info(emit_all);
    xTaskCreatePinnedToCore(key_task, "key_read", 4096, NULL, 2, &s_task, 1);
#endif
}

/* 自动启动：注入后不需要用户改任何代码，开机就把按键状态报给主机。
   之所以要这个入口：注入只加组件目录，用户工程里本来就没有任何地方调用
   key_read_driver_start()，结果就是「组件编进去了、板上却一个包都不发」。 */
void key_read_driver_autostart(void)
{
#if CONFIG_KEY_READ_SOURCE_XL9555
    key_read_driver_start_exio();
#elif CONFIG_KEY_READ_KEY0 >= 0 || CONFIG_KEY_READ_KEY1 >= 0 || \
      CONFIG_KEY_READ_KEY2 >= 0 || CONFIG_KEY_READ_KEY3 >= 0
    key_read_driver_start(CONFIG_KEY_READ_KEY0, CONFIG_KEY_READ_KEY1,
                          CONFIG_KEY_READ_KEY2, CONFIG_KEY_READ_KEY3,
                          CONFIG_KEY_READ_RESET);
#endif
}

/* 为什么不用 __attribute__((constructor))：ESP-IDF 的启动流程不会在 app_main
   之前跑 .init_array，构造函数实测根本没执行（板子烧完一个包都不发）。改用 IDF
   自己的系统初始化钩子，它一定会被调用；真正干活放到任务里，等调度器起来后
   （延时 200ms）再去装 I2C/UART 驱动，避免在调度器前调用驱动 API。 */
static void key_boot_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(260));
    /* 用户工程通常在自己的 board_init 里才 i2c_driver_install（正点原子例程在
       app_main 后半段）。早先只试一次，读不到就永久放弃，界面一直是「未配置」。
       这里改成每 400ms 重试，最多 30s，板子把总线装好后的下一次就接管。 */
    for (int i = 0; i < 75; i++) {
        key_read_driver_autostart();
        if (s_task != NULL) {
            break;
        }
        /* 还没接管：把失败原因（1=总线 2=读扩展芯片）报给主机，否则界面永远只有
           「未配置」，看不出卡在哪一步。 */
        if ((i % 4) == 0) {
            console_proto_init();
            char buf[180];
            snprintf(buf, sizeof(buf),
                     "{\"type\":\"key_status\",\"data\":{\"configured\":[],"
                     "\"state\":{\"KEY0\":-1,\"KEY1\":-1,\"KEY2\":-1,\"KEY3\":-1},"
                     "\"dbg\":%d}}", s_dbg_stage);
            console_proto_send_line(buf);
        }
        vTaskDelay(pdMS_TO_TICKS(400));
    }
    vTaskDelete(NULL);
}

ESP_SYSTEM_INIT_FN(console_key_boot, SECONDARY, BIT(0), 1000)
{
#ifdef CONFIG_KEY_READ_SOURCE_XL9555
    xTaskCreate(key_boot_task, "key_boot", 3072, NULL, 4, NULL);
#endif
    return ESP_OK;
}
