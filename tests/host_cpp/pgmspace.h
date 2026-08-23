// Minimal desktop shim for AVR/ESP32 <pgmspace.h> so edge_model.cpp can be
// compiled with a host compiler for numerical golden testing. On the host,
// PROGMEM data is already in ordinary RAM, so all PROGMEM accessors collapse
// to plain copies/reads.
#ifndef AIOT_TEST_PGMSPACE_SHIM_H
#define AIOT_TEST_PGMSPACE_SHIM_H

#include <string.h>

#define PROGMEM
#define PGM_P const char *
#define PSTR(s) (s)

static inline void memcpy_P(void *dest, const void *src, size_t n) {
  memcpy(dest, src, n);
}

static inline char pgm_read_byte(const char *p) { return *p; }
static inline float pgm_read_float(const float *p) { return *p; }

#endif
