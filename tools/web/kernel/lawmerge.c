/* The merge of two run-score laws (`hbuilder.domain.song_distribution.merge_laws`), compiled.
 *
 * A law is a list of (relative score, probability) in the order its values were first added,
 * which is the order a Python dict keeps: the next merge adds them in that order, and when the
 * law is binned several values add into one grid point, so the order decides the last bits.
 * Every step is the Python's: law a then law b, each value's mass `p * share` put at
 * `value + shift` (split between the two grid points around it when binned, the upper share
 * `(absolute - grid) / bin_width`), a new value starting from `0.0 + mass`; past `cap` values
 * the law is binned again on a grid at least twice as wide, in insertion order. Bit-identical
 * to the Python (tools/web/kernel/test_lawmerge.py) under cbuild.py's flags. */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    int64_t n;
    int64_t *k;
    double *p;
} Law;

typedef struct {
    int64_t n;
    int64_t *k;
    double *p;
    int64_t *slot; /* index + 1 into k/p, 0 = empty */
    uint64_t mask;
} Out;

static int64_t floordiv(int64_t a, int64_t b) {
    int64_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) q--;
    return q;
}

static int out_init(Out *o, int64_t entries) {
    uint64_t size = 16;
    while (size < (uint64_t)(2 * entries + 2)) size <<= 1;
    o->n = 0;
    o->k = (int64_t *)malloc(sizeof(int64_t) * (size_t)(entries + 1));
    o->p = (double *)malloc(sizeof(double) * (size_t)(entries + 1));
    o->slot = (int64_t *)calloc((size_t)size, sizeof(int64_t));
    o->mask = size - 1;
    if (!o->k || !o->p || !o->slot) {
        free(o->k);
        free(o->p);
        free(o->slot);
        return 0;
    }
    return 1;
}

static void out_free(Out *o) {
    free(o->k);
    free(o->p);
    free(o->slot);
}

static inline uint64_t hash64(int64_t key) {
    uint64_t x = (uint64_t)key * 0x9E3779B97F4A7C15ull;
    return x ^ (x >> 31);
}

/* out[key] = out.get(key, 0.0) + mass */
static void add(Out *o, int64_t key, double mass) {
    uint64_t h = hash64(key) & o->mask;
    for (;;) {
        int64_t s = o->slot[h];
        if (s == 0) {
            o->k[o->n] = key;
            o->p[o->n] = 0.0 + mass;
            o->n++;
            o->slot[h] = o->n;
            return;
        }
        if (o->k[s - 1] == key) {
            o->p[s - 1] = o->p[s - 1] + mass;
            return;
        }
        h = (h + 1) & o->mask;
    }
}

/* song_distribution._put */
static void put(Out *o, int64_t key, double mass, int64_t base, int64_t bin_width) {
    if (bin_width == 1) {
        add(o, key, mass);
        return;
    }
    int64_t absolute = base + key;
    int64_t grid = floordiv(absolute, bin_width) * bin_width;
    double upper = (double)(absolute - grid) / (double)bin_width;
    int64_t low = grid - base;
    add(o, low, mass * (1.0 - upper));
    if (upper != 0.0) add(o, low + bin_width, mass * upper);
}

static Law *to_law(Out *o) {
    Law *law = (Law *)malloc(sizeof(Law));
    if (!law) {
        out_free(o);
        return NULL;
    }
    free(o->slot);
    law->n = o->n;
    law->k = o->k;
    law->p = o->p;
    return law;
}

Law *hd_law_new(const int64_t *k, const double *p, int64_t n) {
    Law *law = (Law *)malloc(sizeof(Law));
    if (!law) return NULL;
    law->n = n;
    law->k = (int64_t *)malloc(sizeof(int64_t) * (size_t)(n + 1));
    law->p = (double *)malloc(sizeof(double) * (size_t)(n + 1));
    if (!law->k || !law->p) {
        free(law->k);
        free(law->p);
        free(law);
        return NULL;
    }
    memcpy(law->k, k, sizeof(int64_t) * (size_t)n);
    memcpy(law->p, p, sizeof(double) * (size_t)n);
    return law;
}

int64_t hd_law_size(const Law *law) { return law->n; }

void hd_law_copy(const Law *law, int64_t *k, double *p) {
    memcpy(k, law->k, sizeof(int64_t) * (size_t)law->n);
    memcpy(p, law->p, sizeof(double) * (size_t)law->n);
}

void hd_law_free(Law *law) {
    if (!law) return;
    free(law->k);
    free(law->p);
    free(law);
}

/* `merge_laws(a, share_a, shift_a, b, share_b, shift_b, base, bin_width, cap)`: the new law,
 * and the bin width in *bin_width_io; NULL when memory runs out. */
Law *hd_law_merge(const Law *a, double share_a, int64_t shift_a, const Law *b, double share_b,
                  int64_t shift_b, int64_t base, int64_t *bin_width_io, int64_t cap) {
    int64_t bin_width = *bin_width_io;
    Out o;
    if (!out_init(&o, 2 * (a->n + b->n))) return NULL;
    for (int64_t i = 0; i < a->n; i++) put(&o, a->k[i] + shift_a, a->p[i] * share_a, base, bin_width);
    for (int64_t i = 0; i < b->n; i++) put(&o, b->k[i] + shift_b, b->p[i] * share_b, base, bin_width);
    if (o.n > cap) {
        int64_t lo = o.k[0], hi = o.k[0];
        for (int64_t i = 1; i < o.n; i++) {
            if (o.k[i] < lo) lo = o.k[i];
            if (o.k[i] > hi) hi = o.k[i];
        }
        int64_t wider = (hi - lo) / (cap / 2) + 1;
        bin_width = bin_width * 2 > wider ? bin_width * 2 : wider;
        Out r;
        if (!out_init(&r, 2 * o.n)) {
            out_free(&o);
            return NULL;
        }
        for (int64_t i = 0; i < o.n; i++) put(&r, o.k[i], o.p[i], base, bin_width);
        out_free(&o);
        o = r;
    }
    *bin_width_io = bin_width;
    return to_law(&o);
}
