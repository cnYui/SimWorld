idf_component_register(SRCS "main.c" "params.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_driver_ledc esp_driver_gpio esp_partition esp_system esp_timer)
