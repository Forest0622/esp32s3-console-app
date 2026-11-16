idf_component_register(
    SRCS "console_display.c"
    INCLUDE_DIRS "include"
    REQUIRES console_protocol esp_lcd
)

# 链接期拦截 esp_lcd 的绘制调用（开发文档 3.5.3 主方案）
target_link_options(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=esp_lcd_panel_draw_bitmap"
)
