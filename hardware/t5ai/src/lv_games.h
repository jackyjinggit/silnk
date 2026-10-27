/**
 * @file lv_games.h
 * @brief 思印客取件屏的 LVGL 版本兼容层
 *
 * 上游示例工程基于 LVGL v8 编写，TuyaOpen 默认使用 v9。
 * 这里只保留让 v8 风格代码在 v9 下编译通过所需的宏，不引入任何游戏模块。
 */

#ifndef LV_GAMES_H
#define LV_GAMES_H

#ifdef __cplusplus
extern "C" {
#endif

#include "lvgl.h"

// LVGL v8 to v9 compatibility macros
#if LVGL_VERSION_MAJOR == 9
#if LV_COLOR_DEPTH == 32 && LV_COLOR_16_SWAP == 0

#ifndef LV_IMG_PX_SIZE_ALPHA_BYTE
#define LV_IMG_PX_SIZE_ALPHA_BYTE 4
#endif

#ifndef LV_COLOR_SIZE
#define LV_COLOR_SIZE 32
#endif

#ifndef LV_IMG_CF_TRUE_COLOR
#define LV_IMG_CF_TRUE_COLOR LV_COLOR_FORMAT_XRGB8888
#endif

#ifndef LV_IMG_CF_TRUE_COLOR_ALPHA
#define LV_IMG_CF_TRUE_COLOR_ALPHA LV_COLOR_FORMAT_ARGB8888
#endif

#define lv_obj_del_delayed lv_obj_delete_delayed

#else

#ifndef LV_IMG_PX_SIZE_ALPHA_BYTE
#define LV_IMG_PX_SIZE_ALPHA_BYTE 3
#endif

#ifndef LV_COLOR_SIZE
#define LV_COLOR_SIZE LV_COLOR_DEPTH
#endif

#ifndef LV_IMG_CF_TRUE_COLOR
#define LV_IMG_CF_TRUE_COLOR LV_COLOR_FORMAT_NATIVE
#endif

#ifndef LV_IMG_CF_TRUE_COLOR_ALPHA
#define LV_IMG_CF_TRUE_COLOR_ALPHA LV_COLOR_FORMAT_ARGB8888
#endif

#define lv_obj_del_delayed lv_obj_delete_delayed

#endif /* LV_COLOR_DEPTH == 32 && LV_COLOR_16_SWAP == 0 */
#endif /* LVGL_VERSION_MAJOR == 9 */

#ifdef __cplusplus
}
#endif

#endif /* LV_GAMES_H */
