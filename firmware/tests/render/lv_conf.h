// LVGL 9 configuration of the host render (firmware/tests/render/render.sh); options not set here
// take the lv_conf_internal.h defaults.
#ifndef LV_CONF_H
#define LV_CONF_H

#define LV_COLOR_DEPTH 16
#define LV_USE_STDLIB_MALLOC LV_STDLIB_CLIB
#define LV_USE_OS LV_OS_NONE
#define LV_USE_LOG 0
#define LV_USE_SNAPSHOT 1
#define LV_FONT_FMT_TXT_LARGE 1
#define LV_DRAW_SW_COMPLEX 1

#endif
