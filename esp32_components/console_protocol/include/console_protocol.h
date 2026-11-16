#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*console_cmd_cb_t)(const char *line);

/** 初始化上报通道（UART0 或 USB-Serial-JTAG，由 Kconfig 决定）。可重复调用，第二次为空操作。 */
void console_proto_init(void);

/** 发送一行 JSON 遥测（自动补 \n）。与帧发送互斥，保证不交错。 */
bool console_proto_send_line(const char *line);

/** 发送一帧二进制负载：A5 5A | u32 len(LE) | u16 crc16-ccitt | payload */
bool console_proto_send_frame(const uint8_t *payload, uint16_t len);

/** 注册主机命令回调（收到一行 JSON 时回调，行内已去掉 \n）。 */
void console_proto_set_cmd_cb(console_cmd_cb_t cb);

/** 当前上报口名称："uart0" / "usb_serial_jtag" */
const char *console_proto_port_name(void);

/** 日志是否已被压制 */
bool console_proto_log_suppressed(void);

/** 注册一个已启用的上报组件名（"display"/"led"/"key"），用于应答 console_info。 */
void console_proto_register_component(const char *name);

/** 主机下发的上报开关；provider 取值 0=display 1=led 2=key。默认全开。 */
bool console_proto_report_enabled(int provider);

/** 注册「主机下发 console_info 时重发当前快照」的回调，最多 4 个。

    组件只在状态变化时上报，主机晚一步连上就只看到空面板；由协议转发这次
    握手请求，让 led/key 把当前状态补发一遍。
 */
void console_proto_on_info(void (*hook)(void));

uint16_t console_crc16(const uint8_t *data, uint16_t len);

#ifdef __cplusplus
}
#endif
