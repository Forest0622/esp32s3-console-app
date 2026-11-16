idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES console_protocol led_read_driver key_read_driver display_component esp_lcd
)
