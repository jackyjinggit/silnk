/**
 * @file lv_games.c
 * @brief 思印客自助取件终端主界面（铜档 + 银档）
 *
 * 数据通道：UART0 逐行 JSON
 *   {"s":"idle","q":2,"p":180}
 *   s = idle | busy | pickup
 *   q = 排队人数
 *   p = 剩余纸张数
 * 超过 6 秒收不到整帧 -> 界面置灰，保留最后一帧内容
 *
 * @copyright Copyright (c) 2021-2024 Tuya Inc. All Rights Reserved.
 */

#include <stdio.h>
#include <string.h>

#include "tuya_cloud_types.h"

#include "tal_api.h"
#include "tkl_output.h"
#include "tkl_spi.h"
#include "tkl_system.h"

#include "lvgl.h"
#include "lv_vendor.h"
#include "board_com_api.h"
#include "lv_games.h"

#include "cJSON.h"

/***********************************************************
*************************micro define***********************
***********************************************************/

#define SY_UART_PORT            TUYA_UART_NUM_0
#define SY_UART_BAUDRATE        115200
#define SY_UART_RX_BUFSZ        1024
#define SY_LINE_MAX             512
#define SY_FRAME_TIMEOUT_MS     6000
#define SY_UI_TICK_MS           200
#define SY_RETRY_MS             3000
#define SY_HEARTBEAT_MS         10000
#define SY_PAPER_CAP            300

#define SY_CODE_MAX             8
#define SY_PICK_RETRY_MS        2000
#define SY_ACK_MS               1400
#define SY_RES_MS               4000

#define SY_COL_BG               0xF8F5F0
#define SY_COL_BRAND            0xFF7043
#define SY_COL_TXT              0x2B2724
#define SY_COL_MUTED            0x9A938B
#define SY_COL_TRACK            0xEDE7DD
#define SY_COL_TILE             0xFFFFFF
#define SY_COL_TILE_LINE        0xEDE7DD
#define SY_COL_IDLE             0x2E9E5B
#define SY_COL_IDLE_DK          0x1F6F41
#define SY_COL_BUSY             0xE8871E
#define SY_COL_PICKUP           0x2F6FD0
#define SY_COL_WARN             0xD9453B
#define SY_COL_OFF              0xB9B3AA
#define SY_COL_OFF_TXT          0xB4AEA5
#define SY_COL_PILL_ON_BG       0xE3F1E8
#define SY_COL_PILL_OFF_BG      0xE8E2D8

/***********************************************************
***********************typedef define***********************
***********************************************************/

typedef enum {
    SY_ST_IDLE = 0,
    SY_ST_BUSY,
    SY_ST_PICKUP,
} SY_STATE_E;

/***********************************************************
***********************variable define**********************
***********************************************************/

LV_FONT_DECLARE(sy_title)   /* 「思印客」 */
LV_FONT_DECLARE(sy_state)   /* 大号状态字 */
LV_FONT_DECLARE(sy_ui)      /* 小号信息字 */
LV_FONT_DECLARE(sy_tiny)    /* 角标 */
LV_FONT_DECLARE(sy_num_l)   /* 取件码大数字 */
LV_FONT_DECLARE(sy_num_m)   /* 磁贴数字 */

static volatile SY_STATE_E s_state = SY_ST_IDLE;
static volatile int32_t    s_queue = -1;
static volatile int32_t    s_paper = -1;
static volatile int32_t    s_price = -1;

static volatile uint32_t   s_frame_seq = 0;
static volatile uint32_t   s_frame_cnt = 0;
static volatile uint32_t   s_bad_cnt = 0;
static volatile uint32_t   s_rx_bytes = 0;
static volatile uint32_t   s_last_frame_ms = 0;
static volatile int        s_uart_ready = 0;

static THREAD_HANDLE       s_uart_thrd = NULL;

static char s_code[SY_CODE_MAX + 1] = {0};

static lv_obj_t *s_lbl_title = NULL;
static lv_obj_t *s_pill = NULL;
static lv_obj_t *s_pill_dot = NULL;
static lv_obj_t *s_pill_txt = NULL;
static lv_obj_t *s_card = NULL;
static lv_obj_t *s_mark_a = NULL;
static lv_obj_t *s_mark_b = NULL;
static lv_obj_t *s_lbl_state = NULL;
static lv_obj_t *s_lbl_code = NULL;
static lv_obj_t *s_lbl_sub = NULL;
static lv_obj_t *s_tile_q = NULL;
static lv_obj_t *s_tile_p = NULL;
static lv_obj_t *s_lbl_q_cap = NULL;
static lv_obj_t *s_lbl_q_num = NULL;
static lv_obj_t *s_lbl_q_unit = NULL;
static lv_obj_t *s_lbl_p_cap = NULL;
static lv_obj_t *s_lbl_p_num = NULL;
static lv_obj_t *s_lbl_p_unit = NULL;
static lv_obj_t *s_bar_paper = NULL;
static lv_obj_t *s_rule = NULL;
static lv_obj_t *s_lbl_price = NULL;
static lv_obj_t *s_lbl_badge = NULL;

static int32_t  s_shown_state = -1;
/* 链路状态：0 = 等待首帧，1 = 在线，2 = 离线（超过 6 秒无整帧） */
static int32_t  s_shown_link = -1;
static int32_t  s_shown_queue = -999;
static int32_t  s_shown_paper = -999;
static int32_t  s_shown_price = -999;
static char     s_shown_code[SY_CODE_MAX + 1] = {0};
static uint32_t s_beat_ms = 0;
static uint32_t s_pick_ms = 0;
static int32_t  s_pick_sent = 0;
static uint32_t s_ack_ms = 0;
static uint32_t s_res_ms = 0;
static int32_t  s_res_shown = 0;
static char     s_res_txt[48] = {0};

/***********************************************************
***********************function define**********************
***********************************************************/

static int32_t sy_state_from_str(const char *str)
{
    if (str == NULL) {
        return -1;
    }
    if (strcmp(str, "idle") == 0) {
        return SY_ST_IDLE;
    }
    if (strcmp(str, "busy") == 0) {
        return SY_ST_BUSY;
    }
    if (strcmp(str, "pickup") == 0) {
        return SY_ST_PICKUP;
    }
    return -1;
}

static void sy_handle_line(const char *line, uint32_t len)
{
    cJSON *root = NULL;
    cJSON *item = NULL;

    if (len == 0) {
        return;
    }

    root = cJSON_ParseWithLength(line, (size_t)len);
    if (root == NULL) {
        s_bad_cnt++;
        PR_DEBUG("SY: bad line (drop)");
        return;
    }

    item = cJSON_GetObjectItemCaseSensitive(root, "s");
    if (cJSON_IsString(item) && item->valuestring != NULL) {
        int32_t st = sy_state_from_str(item->valuestring);
        if (st >= 0) {
            s_state = (SY_STATE_E)st;
        }
    }

    item = cJSON_GetObjectItemCaseSensitive(root, "c");
    if (cJSON_IsString(item) && item->valuestring != NULL) {
        char     tmp[SY_CODE_MAX + 1];
        uint32_t n = 0;
        const char *src = item->valuestring;

        memset(tmp, 0, sizeof(tmp));
        for (; *src != '\0' && n < SY_CODE_MAX; src++) {
            if (*src >= '0' && *src <= '9') {
                tmp[n++] = *src;
            }
        }
        memcpy(s_code, tmp, sizeof(s_code));
    }

    item = cJSON_GetObjectItemCaseSensitive(root, "q");
    if (cJSON_IsNumber(item)) {
        s_queue = item->valueint;
    }

    item = cJSON_GetObjectItemCaseSensitive(root, "p");
    if (cJSON_IsNumber(item) && item->valueint >= -1) {
        s_paper = item->valueint;
    }

    item = cJSON_GetObjectItemCaseSensitive(root, "u");
    if (cJSON_IsNumber(item) && item->valueint >= -1 && item->valueint < 100000) {
        s_price = item->valueint;
    }

    /* 取件结果回显：{"r":"pick","ok":1,"m":"已取件 请拿好纸张"} */
    item = cJSON_GetObjectItemCaseSensitive(root, "r");
    if (cJSON_IsString(item) && item->valuestring != NULL &&
        strcmp(item->valuestring, "pick") == 0) {
        cJSON *ok  = cJSON_GetObjectItemCaseSensitive(root, "ok");
        cJSON *msg = cJSON_GetObjectItemCaseSensitive(root, "m");
        int32_t good = (cJSON_IsNumber(ok) && ok->valueint != 0) ? 1 : 0;

        memset(s_res_txt, 0, sizeof(s_res_txt));
        if (cJSON_IsString(msg) && msg->valuestring != NULL) {
            strncpy(s_res_txt, msg->valuestring, sizeof(s_res_txt) - 1);
        }
        if (s_res_txt[0] == '\0') {
            strncpy(s_res_txt, good ? "已取件" : "取件失败 请重试",
                    sizeof(s_res_txt) - 1);
        }
        s_res_shown = 0;
        s_res_ms = (uint32_t)tal_system_get_millisecond();
        if (good == 0) {
            /* 失败时允许立刻重试，不必再等 2 秒 */
            s_pick_sent = 0;
        }
    }

    cJSON_Delete(root);

    s_frame_cnt++;
    s_last_frame_ms = (uint32_t)tal_system_get_millisecond();
    s_frame_seq++;
}

static void sy_rx_feed(uint8_t byte, char *line, uint32_t *idx, uint8_t *overflow)
{
    if (byte == '\n') {
        if (*overflow == 0 && *idx > 0) {
            line[*idx] = '\0';
            sy_handle_line(line, *idx);
        }
        *idx = 0;
        *overflow = 0;
        return;
    }

    if (byte == '\r') {
        return;
    }

    if (*overflow != 0) {
        return;
    }

    if (*idx >= (SY_LINE_MAX - 1)) {
        *overflow = 1;
        *idx = 0;
        return;
    }

    line[*idx] = (char)byte;
    (*idx)++;
}

static OPERATE_RET sy_uart_setup(void)
{
    TAL_UART_CFG_T cfg;
    OPERATE_RET    rt;

    memset(&cfg, 0, sizeof(cfg));
    cfg.rx_buffer_size         = SY_UART_RX_BUFSZ;
    cfg.open_mode              = 0;
    cfg.base_cfg.baudrate      = SY_UART_BAUDRATE;
    cfg.base_cfg.databits      = TUYA_UART_DATA_LEN_8BIT;
    cfg.base_cfg.stopbits      = TUYA_UART_STOP_LEN_1BIT;
    cfg.base_cfg.parity        = TUYA_UART_PARITY_TYPE_NONE;
    cfg.base_cfg.flowctrl      = TUYA_UART_FLOWCTRL_NONE;

    rt = tal_uart_init(SY_UART_PORT, &cfg);
    if (rt == OPRT_OK) {
        return OPRT_OK;
    }

    PR_WARN("SY: uart%d init fail (%d), retry after deinit", (int)SY_UART_PORT, (int)rt);
    (void)tal_uart_deinit(SY_UART_PORT);

    return tal_uart_init(SY_UART_PORT, &cfg);
}

static void sy_uart_thread(void *arg)
{
    static char line[SY_LINE_MAX];
    uint8_t     chunk[128];
    uint32_t    idx = 0;
    uint8_t     overflow = 0;
    uint32_t    last_try = 0;

    (void)arg;

    for (;;) {
        if (s_uart_ready == 0) {
            uint32_t now = (uint32_t)tal_system_get_millisecond();
            if ((uint32_t)(now - last_try) >= SY_RETRY_MS) {
                last_try = now;
                if (sy_uart_setup() == OPRT_OK) {
                    s_uart_ready = 1;
                    PR_NOTICE("SY: uart%d open %d 8N1", (int)SY_UART_PORT, SY_UART_BAUDRATE);
                } else {
                    PR_ERR("SY: uart%d open failed", (int)SY_UART_PORT);
                }
            }
            tal_system_sleep(200);
            continue;
        }

        int32_t avail = tal_uart_get_rx_data_size(SY_UART_PORT);
        if (avail > 0) {
            int32_t want = (avail > (int32_t)sizeof(chunk)) ? (int32_t)sizeof(chunk) : avail;
            int32_t got = tal_uart_read(SY_UART_PORT, chunk, (uint32_t)want);

            if (got > 0) {
                s_rx_bytes += (uint32_t)got;
                for (int32_t i = 0; i < got; i++) {
                    sy_rx_feed(chunk[i], line, &idx, &overflow);
                }
                continue;
            }

            if (got < 0) {
                PR_ERR("SY: uart read err %d", (int)got);
                tal_system_sleep(500);
            }
            continue;
        }

        tal_system_sleep(10);
    }
}
#define SY_Q_DOTS               5

static lv_obj_t *s_dot_card = NULL;
static lv_obj_t *s_q_dot[SY_Q_DOTS] = {0};

static lv_obj_t *sy_make_box(lv_obj_t *parent, int32_t w, int32_t h, int32_t r, uint32_t color)
{
    lv_obj_t *o = lv_obj_create(parent);

    lv_obj_remove_style_all(o);
    lv_obj_set_size(o, w, h);
    lv_obj_set_style_radius(o, r, 0);
    lv_obj_set_style_bg_color(o, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_clear_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    return o;
}

static lv_obj_t *sy_make_label(lv_obj_t *parent, const char *text, const lv_font_t *font, uint32_t color)
{
    lv_obj_t *l = lv_label_create(parent);

    lv_label_set_text(l, text);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    return l;
}

static void sy_anim_opa_cb(void *obj, int32_t v)
{
    lv_obj_set_style_bg_opa((lv_obj_t *)obj, (lv_opa_t)v, 0);
}

static void sy_pulse(lv_obj_t *obj, uint32_t period_ms, int32_t hi, int32_t lo, int32_t on)
{
    lv_anim_t a;

    lv_anim_delete(obj, sy_anim_opa_cb);
    lv_obj_set_style_bg_opa(obj, (lv_opa_t)hi, 0);

    if (on == 0) {
        return;
    }

    lv_anim_init(&a);
    lv_anim_set_var(&a, obj);
    lv_anim_set_exec_cb(&a, sy_anim_opa_cb);
    lv_anim_set_values(&a, hi, lo);
    lv_anim_set_duration(&a, period_ms);
    lv_anim_set_playback_duration(&a, period_ms);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_in_out);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&a);
}

/*__SY_CHUNK_2__*/

static void sy_ui_apply_pill(int32_t link)
{
    int32_t  online = (link == 1) ? 1 : 0;
    int32_t  waiting = (link == 0) ? 1 : 0;
    uint32_t dot_col = online ? SY_COL_IDLE : (waiting ? SY_COL_BUSY : SY_COL_OFF);
    uint32_t txt_col = online ? SY_COL_IDLE_DK : (waiting ? SY_COL_BUSY : SY_COL_MUTED);
    const char *txt = online ? "在线" : (waiting ? "连接中" : "离线");

    lv_obj_set_style_bg_color(s_pill, lv_color_hex(online ? SY_COL_PILL_ON_BG : SY_COL_PILL_OFF_BG), 0);
    lv_obj_set_style_bg_color(s_pill_dot, lv_color_hex(dot_col), 0);
    lv_obj_set_style_text_color(s_pill_txt, lv_color_hex(txt_col), 0);
    lv_label_set_text(s_pill_txt, txt);
    /* 在线缓慢呼吸，等待首帧时加快呼吸提示「正在连接」，离线常暗 */
    sy_pulse(s_pill_dot, waiting ? 520 : 900, 255, 90, (online || waiting) ? 1 : 0);
    sy_pulse(s_mark_a, 2600, 60, 24, 1);
}

static void sy_pickup_cb(lv_event_t *event)
{
    char     line[64];
    uint32_t now = (uint32_t)tal_system_get_millisecond();
    int32_t  len;
    int32_t  sent = 0;

    if (lv_event_get_code(event) != LV_EVENT_CLICKED || s_uart_ready == 0 ||
        s_state != SY_ST_PICKUP || s_shown_state != SY_ST_PICKUP ||
        s_shown_link != 1 ||
        s_frame_seq == 0 || (uint32_t)(now - s_last_frame_ms) > SY_FRAME_TIMEOUT_MS ||
        s_shown_code[0] == '\0' || strcmp(s_shown_code, s_code) != 0) {
        return;
    }

    if (s_pick_sent && (uint32_t)(now - s_pick_ms) < SY_PICK_RETRY_MS) {
        return;
    }

    s_pick_ms = now;
    s_pick_sent = 1;
    len = snprintf(line, sizeof(line), "{\"a\":\"pick\",\"c\":\"%s\"}\n", s_shown_code);
    if (len <= 0 || len >= (int32_t)sizeof(line)) {
        return;
    }

    while (sent < len) {
        int32_t n = tal_uart_write(SY_UART_PORT, (const uint8_t *)line + sent, (uint32_t)(len - sent));
        if (n <= 0 || n > len - sent) {
            PR_ERR("SY: pickup uart write failed (%d/%d)", (int)sent, (int)len);
            return;
        }
        sent += n;
    }

    /* 点击后立即给出反馈，避免用户以为没点中而反复戳屏 */
    s_ack_ms = now;
    lv_label_set_text(s_lbl_sub, "已确认 正在处理");
}

static void sy_ui_apply_state(int32_t state, int32_t link, const char *code)
{
    uint32_t    color;
    const char *text;
    const char *sub;
    int32_t     online = (link == 1) ? 1 : 0;
    int32_t     waiting = (link == 0) ? 1 : 0;
    int32_t     has_code = ((state == SY_ST_PICKUP) && (code[0] != '\0')) ? 1 : 0;

    switch (state) {
        case SY_ST_BUSY:
            color = SY_COL_BUSY;
            text  = "打印中";
            sub   = "正在出纸 请稍候";
            break;
        case SY_ST_PICKUP:
            color = SY_COL_PICKUP;
            text  = "请取件";
            sub   = has_code ? "轻触屏幕确认取件" : "请到出口取件";
            break;
        default:
            color = SY_COL_IDLE;
            text  = "可打印";
            sub   = "欢迎使用自助打印";
            break;
    }

    /* 首帧未到不等于在线：明确显示「等待连接」，不拿默认状态冒充真实数据 */
    if (waiting) {
        color = SY_COL_OFF;
        text  = "等待连接";
        sub   = "正在连接打印服务";
    } else if (!online) {
        color = SY_COL_OFF;
        sub   = has_code ? "连接中断 取件码已保留" : "连接中断 请稍候";
    }

    lv_obj_set_style_bg_color(s_card, lv_color_hex(color), 0);
    lv_obj_set_style_bg_grad_color(s_card, lv_color_darken(lv_color_hex(color), 40), 0);
    lv_obj_set_style_shadow_color(s_card, lv_color_hex(color), 0);

    if (online && has_code) {
        lv_obj_add_flag(s_card, LV_OBJ_FLAG_CLICKABLE);
    } else {
        lv_obj_clear_flag(s_card, LV_OBJ_FLAG_CLICKABLE);
    }

    lv_label_set_text(s_lbl_state, text);
    lv_label_set_text(s_lbl_sub, sub);

    if (has_code) {
        lv_label_set_text(s_lbl_code, code);
        lv_obj_clear_flag(s_lbl_code, LV_OBJ_FLAG_HIDDEN);
        lv_obj_align(s_lbl_state, LV_ALIGN_TOP_MID, 0, 16);
        lv_obj_align(s_lbl_code, LV_ALIGN_CENTER, 0, 6);
        lv_obj_align(s_lbl_sub, LV_ALIGN_BOTTOM_MID, 0, -18);
    } else {
        lv_obj_add_flag(s_lbl_code, LV_OBJ_FLAG_HIDDEN);
        lv_obj_align(s_lbl_state, LV_ALIGN_CENTER, 0, -14);
        lv_obj_align(s_lbl_sub, LV_ALIGN_BOTTOM_MID, 0, -42);
    }

    sy_pulse(s_dot_card, (state == SY_ST_IDLE) ? 1600 : 650, 255, 60,
             (online || waiting) ? 1 : 0);
}

static void sy_ui_apply_tiles(int32_t queue, int32_t paper, int32_t online)
{
    uint32_t ink = online ? SY_COL_TXT : SY_COL_OFF_TXT;
    uint32_t cap = online ? SY_COL_MUTED : SY_COL_OFF_TXT;
    uint32_t on  = online ? SY_COL_BRAND : SY_COL_OFF;
    int32_t  filled = (queue < 0) ? 0 : ((queue > SY_Q_DOTS) ? SY_Q_DOTS : queue);
    int32_t  pct = 0;
    int32_t  i;

    if (queue >= 0) {
        lv_label_set_text_fmt(s_lbl_q_num, "%d", (int)queue);
    } else {
        lv_label_set_text(s_lbl_q_num, "--");
    }

    if (paper >= 0) {
        lv_label_set_text_fmt(s_lbl_p_num, "%d", (int)paper);
    } else {
        lv_label_set_text(s_lbl_p_num, "--");
    }

    lv_obj_align_to(s_lbl_q_unit, s_lbl_q_num, LV_ALIGN_OUT_RIGHT_BOTTOM, 5, -4);
    lv_obj_align_to(s_lbl_p_unit, s_lbl_p_num, LV_ALIGN_OUT_RIGHT_BOTTOM, 5, -4);

    for (i = 0; i < SY_Q_DOTS; i++) {
        lv_obj_set_style_bg_color(s_q_dot[i], lv_color_hex((i < filled) ? on : SY_COL_TRACK), 0);
    }

    if (paper > 0) {
        pct = (paper >= SY_PAPER_CAP) ? 100 : (paper * 100 / SY_PAPER_CAP);
        if (pct > 100) {
            pct = 100;
        }
    }
    lv_bar_set_value(s_bar_paper, pct, LV_ANIM_ON);
    lv_obj_set_style_bg_color(s_bar_paper,
                              lv_color_hex(online ? (((paper >= 0) && (pct <= 20)) ? SY_COL_WARN : SY_COL_IDLE)
                                                  : SY_COL_OFF),
                              LV_PART_INDICATOR);

    lv_obj_set_style_text_color(s_lbl_q_num, lv_color_hex(ink), 0);
    lv_obj_set_style_text_color(s_lbl_p_num, lv_color_hex(ink), 0);
    lv_obj_set_style_text_color(s_lbl_q_cap, lv_color_hex(cap), 0);
    lv_obj_set_style_text_color(s_lbl_p_cap, lv_color_hex(cap), 0);
    lv_obj_set_style_text_color(s_lbl_q_unit, lv_color_hex(cap), 0);
    lv_obj_set_style_text_color(s_lbl_p_unit, lv_color_hex(cap), 0);
    lv_obj_set_style_text_color(s_lbl_price, lv_color_hex(cap), 0);
}

static void sy_ui_refresh(void)
{
    uint32_t seq = s_frame_seq;
    uint32_t last = s_last_frame_ms;
    uint32_t now = (uint32_t)tal_system_get_millisecond();
    int32_t  link;
    int32_t  online;
    int32_t  state = (int32_t)s_state;
    int32_t  queue = s_queue;
    int32_t  paper = s_paper;
    int32_t  price = s_price;
    int32_t  link_chg;
    char     code[SY_CODE_MAX + 1];

    memcpy(code, s_code, sizeof(code));

    /* 三档链路状态：0 等待首帧 / 1 在线 / 2 超过 6 秒无整帧 */
    if (seq == 0) {
        link = 0;
    } else {
        link = ((uint32_t)(now - last) <= SY_FRAME_TIMEOUT_MS) ? 1 : 2;
    }
    online = (link == 1) ? 1 : 0;
    link_chg = (s_shown_link != link) ? 1 : 0;

    if (link_chg || (s_shown_state != state) || (strcmp(code, s_shown_code) != 0)) {
        s_shown_state = state;
        s_shown_link = link;
        s_ack_ms = 0;
        s_res_ms = 0;
        s_res_shown = 0;
        memcpy(s_shown_code, code, sizeof(s_shown_code));
        sy_ui_apply_state(state, link, code);
        sy_ui_apply_pill(link);
    }

    if (link_chg || (s_shown_queue != queue) || (s_shown_paper != paper)) {
        s_shown_queue = queue;
        s_shown_paper = paper;
        sy_ui_apply_tiles(queue, paper, online);
    }

    if (link_chg || (s_shown_price != price)) {
        s_shown_price = price;
        if (price > 0) {
            lv_label_set_text_fmt(s_lbl_price, "单价 %d.%02d 元/张", (int)(price / 100), (int)(price % 100));
        } else {
            lv_label_set_text(s_lbl_price, "单价 -- 元/张");
        }
        lv_obj_set_style_text_color(s_lbl_price, lv_color_hex(online ? SY_COL_MUTED : SY_COL_OFF_TXT), 0);
    }

    /* 点击确认的临时文案到期后恢复提示；若期间收到新帧，会由上面的分支重绘 */
    if (s_ack_ms && (uint32_t)(now - s_ack_ms) >= SY_ACK_MS) {
        s_ack_ms = 0;
        if (s_res_ms == 0 && link == 1 && state == SY_ST_PICKUP && code[0] != '\0') {
            lv_label_set_text(s_lbl_sub, "轻触屏幕确认取件");
        }
    }

    /* 桥接回写的取件结果：成功让位给随后的状态帧，失败停留 4 秒 */
    if (s_res_ms && (uint32_t)(now - s_res_ms) >= SY_RES_MS) {
        s_res_ms = 0;
        s_res_shown = 0;
        if (s_ack_ms == 0 && link == 1 && state == SY_ST_PICKUP && code[0] != '\0') {
            lv_label_set_text(s_lbl_sub, "轻触屏幕确认取件");
        }
    } else if (s_res_ms && s_res_shown == 0 && s_res_txt[0] != '\0') {
        s_res_shown = 1;
        lv_label_set_text(s_lbl_sub, s_res_txt);
    }

    if ((uint32_t)(now - s_beat_ms) >= SY_HEARTBEAT_MS) {
        s_beat_ms = now;
        PR_DEBUG("SY: frame=%u bad=%u rx=%u online=%d q=%d p=%d u=%d c=%s",
                 (unsigned)s_frame_cnt, (unsigned)s_bad_cnt, (unsigned)s_rx_bytes,
                 (int)online, (int)queue, (int)paper, (int)price, code);
    }
}

static void sy_ui_timer_cb(lv_timer_t *tmr)
{
    (void)tmr;
    sy_ui_refresh();
}

static void sy_ui_create(void)
{
    lv_obj_t *scr = lv_scr_act();
    int32_t   i;

    lv_obj_clean(scr);
    lv_obj_set_style_bg_color(scr, lv_color_hex(SY_COL_BG), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    /* ---- 页眉：品牌 + 在线状态药丸 ---- */
    s_lbl_title = sy_make_label(scr, "思印客", &sy_title, SY_COL_BRAND);
    lv_obj_align(s_lbl_title, LV_ALIGN_TOP_LEFT, 16, 10);

    s_pill = sy_make_box(scr, 84, 28, 14, SY_COL_PILL_ON_BG);
    lv_obj_align(s_pill, LV_ALIGN_TOP_RIGHT, -16, 20);

    s_pill_dot = sy_make_box(s_pill, 8, 8, 4, SY_COL_IDLE);
    lv_obj_align(s_pill_dot, LV_ALIGN_LEFT_MID, 11, 0);

    s_pill_txt = sy_make_label(s_pill, "在线", &sy_tiny, SY_COL_IDLE_DK);
    lv_obj_align(s_pill_txt, LV_ALIGN_LEFT_MID, 26, 1);

    /* ---- 主卡片：颜色随三态变化 ---- */
    s_card = lv_obj_create(scr);
    lv_obj_remove_style_all(s_card);
    lv_obj_set_size(s_card, 288, 188);
    lv_obj_align(s_card, LV_ALIGN_TOP_MID, 0, 72);
    lv_obj_set_style_radius(s_card, 26, 0);
    lv_obj_set_style_bg_color(s_card, lv_color_hex(SY_COL_IDLE), 0);
    lv_obj_set_style_bg_grad_color(s_card, lv_color_hex(SY_COL_IDLE), 0);
    lv_obj_set_style_bg_grad_dir(s_card, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_opa(s_card, LV_OPA_COVER, 0);
    lv_obj_set_style_shadow_width(s_card, 24, 0);
    lv_obj_set_style_shadow_offset_y(s_card, 8, 0);
    lv_obj_set_style_shadow_opa(s_card, LV_OPA_20, 0);
    lv_obj_set_style_shadow_color(s_card, lv_color_hex(SY_COL_IDLE), 0);
    lv_obj_set_style_clip_corner(s_card, true, 0);
    lv_obj_clear_flag(s_card, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_card, sy_pickup_cb, LV_EVENT_CLICKED, NULL);

    /* 水印圆：让大片纯色有层次，不抢字 */
    s_mark_a = sy_make_box(s_card, 168, 168, 84, 0xFFFFFF);
    lv_obj_align(s_mark_a, LV_ALIGN_TOP_RIGHT, 52, -38);
    lv_obj_set_style_bg_opa(s_mark_a, 40, 0);

    s_mark_b = sy_make_box(s_card, 112, 112, 56, 0xFFFFFF);
    lv_obj_align(s_mark_b, LV_ALIGN_BOTTOM_LEFT, -38, 44);
    lv_obj_set_style_bg_opa(s_mark_b, 32, 0);

    s_lbl_state = sy_make_label(s_card, "可打印", &sy_state, 0xFFFFFF);
    lv_obj_align(s_lbl_state, LV_ALIGN_CENTER, 0, -14);

    s_lbl_code = sy_make_label(s_card, "", &sy_num_l, 0xFFFFFF);
    lv_obj_align(s_lbl_code, LV_ALIGN_CENTER, 0, 6);
    lv_obj_add_flag(s_lbl_code, LV_OBJ_FLAG_HIDDEN);

    s_lbl_sub = sy_make_label(s_card, "欢迎使用自助打印", &sy_ui, 0xFFFFFF);
    lv_obj_set_style_text_opa(s_lbl_sub, LV_OPA_80, 0);
    lv_obj_clear_flag(s_mark_a, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(s_mark_b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(s_lbl_state, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(s_lbl_code, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(s_lbl_sub, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_align(s_lbl_sub, LV_ALIGN_BOTTOM_MID, 0, -40);

    /* 卡片内的呼吸点，与文案同步节奏 */
    s_dot_card = sy_make_box(s_card, 12, 12, 6, 0xFFFFFF);
    lv_obj_align(s_dot_card, LV_ALIGN_TOP_RIGHT, -16, 16);
    lv_obj_set_style_bg_opa(s_dot_card, 200, 0);

    lv_obj_clear_flag(s_mark_a, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(s_mark_b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(s_dot_card, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(s_lbl_state, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(s_lbl_code, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_clear_flag(s_lbl_sub, LV_OBJ_FLAG_CLICKABLE);

    /* ---- 数据磁贴 1：排队人数 ---- */
    s_tile_q = sy_make_box(scr, 136, 106, 20, SY_COL_TILE);
    lv_obj_align(s_tile_q, LV_ALIGN_TOP_LEFT, 16, 280);
    lv_obj_set_style_border_width(s_tile_q, 1, 0);
    lv_obj_set_style_border_color(s_tile_q, lv_color_hex(SY_COL_TILE_LINE), 0);

    s_lbl_q_cap = sy_make_label(s_tile_q, "排队人数", &sy_tiny, SY_COL_MUTED);
    lv_obj_align(s_lbl_q_cap, LV_ALIGN_TOP_LEFT, 14, 12);

    s_lbl_q_num = sy_make_label(s_tile_q, "--", &sy_num_m, SY_COL_TXT);
    lv_obj_align(s_lbl_q_num, LV_ALIGN_TOP_LEFT, 14, 34);

    s_lbl_q_unit = sy_make_label(s_tile_q, "位", &sy_tiny, SY_COL_MUTED);
    lv_obj_align_to(s_lbl_q_unit, s_lbl_q_num, LV_ALIGN_OUT_RIGHT_BOTTOM, 5, -4);

    for (i = 0; i < SY_Q_DOTS; i++) {
        s_q_dot[i] = sy_make_box(s_tile_q, 12, 4, 2, SY_COL_TRACK);
        lv_obj_align(s_q_dot[i], LV_ALIGN_BOTTOM_LEFT, 14 + i * 16, -14);
    }

    /* ---- 数据磁贴 2：纸量 ---- */
    s_tile_p = sy_make_box(scr, 136, 106, 20, SY_COL_TILE);
    lv_obj_align(s_tile_p, LV_ALIGN_TOP_RIGHT, -16, 280);
    lv_obj_set_style_border_width(s_tile_p, 1, 0);
    lv_obj_set_style_border_color(s_tile_p, lv_color_hex(SY_COL_TILE_LINE), 0);

    s_lbl_p_cap = sy_make_label(s_tile_p, "纸量", &sy_tiny, SY_COL_MUTED);
    lv_obj_align(s_lbl_p_cap, LV_ALIGN_TOP_LEFT, 14, 12);

    s_lbl_p_num = sy_make_label(s_tile_p, "--", &sy_num_m, SY_COL_TXT);
    lv_obj_align(s_lbl_p_num, LV_ALIGN_TOP_LEFT, 14, 34);

    s_lbl_p_unit = sy_make_label(s_tile_p, "张", &sy_tiny, SY_COL_MUTED);
    lv_obj_align_to(s_lbl_p_unit, s_lbl_p_num, LV_ALIGN_OUT_RIGHT_BOTTOM, 5, -4);

    s_bar_paper = lv_bar_create(s_tile_p);
    lv_obj_remove_style_all(s_bar_paper);
    lv_obj_set_size(s_bar_paper, 108, 6);
    lv_obj_align(s_bar_paper, LV_ALIGN_BOTTOM_LEFT, 14, -16);
    lv_obj_set_style_radius(s_bar_paper, 3, 0);
    lv_obj_set_style_bg_color(s_bar_paper, lv_color_hex(SY_COL_TRACK), 0);
    lv_obj_set_style_bg_opa(s_bar_paper, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(s_bar_paper, 3, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s_bar_paper, lv_color_hex(SY_COL_IDLE), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s_bar_paper, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_bar_set_range(s_bar_paper, 0, 100);
    lv_bar_set_value(s_bar_paper, 0, LV_ANIM_OFF);

    /* ---- 页脚 ---- */
    s_rule = sy_make_box(scr, 288, 1, 0, SY_COL_TRACK);
    lv_obj_align(s_rule, LV_ALIGN_TOP_MID, 0, 402);

    s_lbl_price = sy_make_label(scr, "单价 -- 元/张", &sy_ui, SY_COL_MUTED);
    lv_obj_align(s_lbl_price, LV_ALIGN_TOP_LEFT, 16, 408);

    s_lbl_badge = sy_make_label(scr, "T5AI \xC2\xB7 LVGL", &sy_tiny, SY_COL_MUTED);
    lv_obj_align(s_lbl_badge, LV_ALIGN_TOP_RIGHT, -16, 414);
}


/**
 * @brief user_main
 *
 * @param[in] param:Task parameters
 * @return none
 */
void user_main(void)
{
    THREAD_CFG_T thrd_param = {4096, 4, "sy_uart"};

    tal_log_init(TAL_LOG_LEVEL_DEBUG, 4096, (TAL_LOG_OUTPUT_CB)tkl_log_output);

    board_register_hardware();

    lv_vendor_init(DISPLAY_NAME);

    sy_ui_create();

    lv_timer_create(sy_ui_timer_cb, SY_UI_TICK_MS, NULL);

    tal_thread_create_and_start(&s_uart_thrd, NULL, NULL, sy_uart_thread, NULL, &thrd_param);

    PR_NOTICE("SY: boot ok, json feed on uart%d @%d", (int)SY_UART_PORT, SY_UART_BAUDRATE);

    lv_vendor_start(5, 1024 * 8);
}

#if OPERATING_SYSTEM == SYSTEM_LINUX
void main(int argc, char *argv[])
{
    user_main();

    while (1) {
        tal_system_sleep(500);
    }
}
#else

/* Tuya thread handle */
static THREAD_HANDLE ty_app_thread = NULL;

/**
 * @brief  task thread
 *
 * @param[in] arg:Parameters when creating task
 * @return none
 */
static void tuya_app_thread(void *arg)
{
    (void) arg;

    user_main();

    tal_thread_delete(ty_app_thread);
    ty_app_thread = NULL;
}

void tuya_app_main(void)
{
    THREAD_CFG_T thrd_param = {1024 * 4, 4, "tuya_app_main"};
    tal_thread_create_and_start(&ty_app_thread, NULL, NULL, tuya_app_thread, NULL, &thrd_param);
}
#endif
