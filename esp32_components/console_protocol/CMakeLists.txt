idf_component_register(
    SRCS "console_protocol.c"
    INCLUDE_DIRS "include"
    REQUIRES driver esp_driver_uart
)
