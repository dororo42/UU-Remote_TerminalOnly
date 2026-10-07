#ifndef UURB_PUBLIC_CONFIG_H
#define UURB_PUBLIC_CONFIG_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define UURB_PUBLIC_CONFIG_MAX 4096
#define UURB_PUBLIC_PATH_MAX 780

typedef struct uurb_public_config {
    char nonce[65];
    char bootstrap[65];
    char broker_ready[UURB_PUBLIC_PATH_MAX];
    char source_exe[UURB_PUBLIC_PATH_MAX];
    char log[UURB_PUBLIC_PATH_MAX];
    uint32_t broker_pid;
    uint64_t broker_start;
} uurb_public_config;

static int uurb_config_hex(const char *text)
{
    if (strlen(text) != 64)
        return 0;
    for (size_t i = 0; i < 64; i++)
        if (!((text[i] >= '0' && text[i] <= '9') ||
              (text[i] >= 'a' && text[i] <= 'f')))
            return 0;
    return 1;
}

static int uurb_config_path(const char *text)
{
    if (strlen(text) < 4 || text[0] != 'C' || text[1] != ':' || text[2] != '\\')
        return 0;
    const char *component = text + 3;
    for (const char *p = component; ; p++) {
        unsigned char value = (unsigned char)*p;
        if (value && (value < 32 || value == 127 || value == '/' || value == ':'))
            return 0;
        if (value == '\\' || value == 0) {
            size_t n = (size_t)(p - component);
            if (!n || (n == 1 && component[0] == '.') ||
                (n == 2 && component[0] == '.' && component[1] == '.'))
                return 0;
            if (!value)
                return 1;
            component = p + 1;
        }
    }
}

static int uurb_config_decimal(const char *text, uint64_t limit, uint64_t *out)
{
    uint64_t value = 0;
    if (!*text)
        return 0;
    for (; *text; text++) {
        if (*text < '0' || *text > '9')
            return 0;
        uint64_t digit = (uint64_t)(*text - '0');
        if (value > (limit - digit) / 10)
            return 0;
        value = value * 10 + digit;
    }
    if (!value)
        return 0;
    *out = value;
    return 1;
}

static int uurb_config_line(char **cursor, const char *key, char *out, size_t cap)
{
    char *line = *cursor;
    char *end = strchr(line, '\n');
    size_t key_size = strlen(key);
    if (!end || (size_t)(end - line) < key_size || strncmp(line, key, key_size))
        return 0;
    size_t size = (size_t)(end - line) - key_size;
    if (!size || size >= cap)
        return 0;
    memcpy(out, line + key_size, size);
    out[size] = 0;
    *cursor = end + 1;
    return 1;
}

static int uurb_public_config_parse(const char *input, size_t size,
                                    uurb_public_config *out)
{
    char data[UURB_PUBLIC_CONFIG_MAX];
    char pid[24], start[24];
    char *cursor = data;
    uint64_t number;
    uurb_public_config parsed;
    if (!input || !out || !size || size >= sizeof(data) ||
        memchr(input, 0, size) || memchr(input, '\r', size))
        return 0;
    memcpy(data, input, size);
    data[size] = 0;
    memset(&parsed, 0, sizeof(parsed));
    if (strncmp(data, "UURB_PUBLIC_SESSION_V1\n", 23))
        return 0;
    cursor += 23;
    if (!uurb_config_line(&cursor, "nonce=", parsed.nonce, sizeof(parsed.nonce)) ||
        !uurb_config_line(&cursor, "bootstrap=", parsed.bootstrap, sizeof(parsed.bootstrap)) ||
        !uurb_config_line(&cursor, "broker_ready=", parsed.broker_ready, sizeof(parsed.broker_ready)) ||
        !uurb_config_line(&cursor, "source_exe=", parsed.source_exe, sizeof(parsed.source_exe)) ||
        !uurb_config_line(&cursor, "log=", parsed.log, sizeof(parsed.log)) ||
        !uurb_config_line(&cursor, "broker_pid=", pid, sizeof(pid)) ||
        !uurb_config_line(&cursor, "broker_start=", start, sizeof(start)) || *cursor ||
        !uurb_config_hex(parsed.nonce) || !uurb_config_hex(parsed.bootstrap) ||
        !uurb_config_path(parsed.broker_ready) || !uurb_config_path(parsed.source_exe) ||
        !uurb_config_path(parsed.log) || !uurb_config_decimal(pid, UINT32_MAX, &number))
        return 0;
    parsed.broker_pid = (uint32_t)number;
    if (!uurb_config_decimal(start, UINT64_MAX, &parsed.broker_start))
        return 0;
    *out = parsed;
    return 1;
}
#endif
