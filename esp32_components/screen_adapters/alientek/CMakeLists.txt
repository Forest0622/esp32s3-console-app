idf_component_register(
    SRCS "adapter.c"
    INCLUDE_DIRS "."
    REQUIRES display_component
)

target_link_options(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=LCD_ShowPicture"
)
