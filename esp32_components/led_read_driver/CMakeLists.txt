idf_component_register(
    SRCS "led_read_driver.c"
    INCLUDE_DIRS "include"
    REQUIRES console_protocol driver esp_driver_gpio esp_timer
)

# 强制把 autostart 那个目标文件拉进链接（静态库没人引用就会被丢掉，
# constructor 也就永远不会执行 —— 表现为「烧进去但主机收不到任何包」）。
target_link_options(${COMPONENT_LIB} INTERFACE "-Wl,-u,led_read_driver_autostart")
