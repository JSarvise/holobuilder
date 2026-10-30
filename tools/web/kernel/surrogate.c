/* The SCREEN of the two-stage search: a cheap model of log(score) over member
 * 5-sets, fitted to the exact scores already taken and then evaluated on EVERY
 * legal set to choose which ones the exact scorer looks at next.
 *
 *     log(score) ~ bias + card[c] (x5) + gate[kind, count >= 2..5] + pair[ci, cj] (x10)
 *
 * UNLIKE notewalk.c and connect.c THIS IS NOT A BIT-IDENTICAL PORT OF ANYTHING
 * AND NOTHING IT COMPUTES IS EVER REPORTED. It only orders candidates; every
 * number shown comes from the exact leaf. It is deterministic (plain doubles,
 * fixed loop order), which is what the tests hold it to.
 */
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define MAX_CARDS 250     /* a set's key packs five card indices into 8 bits each */
#define MAX_KINDS 64      /* types + generations */
#define MAX_GENS_PER_CARD 6
#define THRESHOLDS 4      /* count >= 2, 3, 4, 5 */

typedef struct {
    int32_t n_cards, n_types, n_kinds;        /* kinds = types then generations */
    const int32_t *holomem;                    /* [n_cards] distinct-member rule */
    const int32_t *type_of;                    /* [n_cards] kind index of the type */
    const int32_t *gens;                       /* [n_cards][MAX_GENS_PER_CARD] kind index or -1 */
} Pool;

typedef struct {
    double bias;
    double *card;                              /* [n_cards] */
    double *gate;                              /* [THRESHOLDS][n_kinds] */
    double *pair;                              /* [n_cards][n_cards], i < j */
} Model;

/* The gate features one set switches on: for every kind with count c >= 2, the
 * features (threshold 2..min(c,5), kind). Returns how many; ids into `gate`. */
static int gate_features(const Pool *p, const uint8_t *set, int32_t *out) {
    int cnt[MAX_KINDS];
    memset(cnt, 0, sizeof(int) * (size_t)p->n_kinds);
    for (int k = 0; k < 5; k++) {
        int c = set[k];
        cnt[p->type_of[c]]++;
        for (int g = 0; g < MAX_GENS_PER_CARD; g++) {
            int kind = p->gens[c * MAX_GENS_PER_CARD + g];
            if (kind < 0) break;
            cnt[kind]++;
        }
    }
    int n = 0;
    for (int kind = 0; kind < p->n_kinds; kind++)
        for (int t = 2; t <= cnt[kind] && t <= 5; t++) out[n++] = (t - 2) * p->n_kinds + kind;
    return n;
}

/* Ridge regression by cyclic coordinate descent. One coordinate at a time, each
 * set to its exact minimiser given the others,
 *
 *     w = sum over the samples it touches of (residual + w_old)  /  (count + lambda)
 *
 * and the residuals updated at once -- so every step lowers the penalised squared
 * error and the fit cannot diverge whatever the pool size (a simultaneous update
 * of five card weights per sample does, on a small pool). `lambda` is the
 * shrinkage: a card pair seen twice is not believed. Coordinates are visited in a
 * fixed order (bias, cards, gates, pairs), `sweeps` times. Returns the mean
 * |residual|, or -1. */
double hd_sur_fit(const Pool *p, Model *m, int n_samples, const uint8_t *sets,
                  const double *log_score, int sweeps,
                  double lam_card, double lam_gate, double lam_pair) {
    int N = p->n_cards, F = THRESHOLDS * p->n_kinds;
    if (N > MAX_CARDS || p->n_kinds > MAX_KINDS || n_samples <= 0) return -1.0;
    int n_feat = N + F + N * N;                 /* cards, then gates, then pairs */
    int per_sample = 5 + 10 + 5 * (1 + MAX_GENS_PER_CARD);
    double *res = malloc(sizeof(double) * (size_t)n_samples);
    int32_t *feat = malloc(sizeof(int32_t) * (size_t)n_samples * per_sample);
    int32_t *foff = malloc(sizeof(int32_t) * ((size_t)n_samples + 1));
    int32_t *start = calloc((size_t)n_feat + 1, sizeof(int32_t));
    int32_t *fill = malloc(sizeof(int32_t) * ((size_t)n_feat + 1));
    int32_t *rows = NULL;
    if (!res || !feat || !foff || !start || !fill) {
        free(res); free(feat); free(foff); free(start); free(fill);
        return -1.0;
    }
    /* each sample's features, and from them the samples of each feature */
    int total = 0;
    double mean = 0.0;
    for (int s = 0; s < n_samples; s++) {
        const uint8_t *set = sets + 5 * s;
        foff[s] = total;
        for (int a = 0; a < 5; a++) feat[total++] = set[a];
        int ng = gate_features(p, set, feat + total);
        for (int k = 0; k < ng; k++) feat[total + k] += N;
        total += ng;
        for (int a = 0; a < 5; a++)
            for (int b = a + 1; b < 5; b++) feat[total++] = N + F + set[a] * N + set[b];
        mean += log_score[s];
    }
    foff[n_samples] = total;
    for (int k = 0; k < total; k++) start[feat[k] + 1]++;
    for (int f = 0; f < n_feat; f++) start[f + 1] += start[f];
    rows = malloc(sizeof(int32_t) * (size_t)(total > 0 ? total : 1));
    if (!rows) { free(res); free(feat); free(foff); free(start); free(fill); return -1.0; }
    memcpy(fill, start, sizeof(int32_t) * ((size_t)n_feat + 1));
    for (int s = 0; s < n_samples; s++)
        for (int k = foff[s]; k < foff[s + 1]; k++) rows[fill[feat[k]]++] = s;

    m->bias = mean / n_samples;
    memset(m->card, 0, sizeof(double) * (size_t)N);
    memset(m->gate, 0, sizeof(double) * (size_t)F);
    memset(m->pair, 0, sizeof(double) * (size_t)N * N);
    for (int s = 0; s < n_samples; s++) res[s] = log_score[s] - m->bias;

    for (int it = 0; it < sweeps; it++) {
        double shift = 0.0;                      /* the bias: unpenalised */
        for (int s = 0; s < n_samples; s++) shift += res[s];
        shift /= n_samples;
        m->bias += shift;
        for (int s = 0; s < n_samples; s++) res[s] -= shift;
        for (int f = 0; f < n_feat; f++) {
            int lo = start[f], hi = start[f + 1];
            if (lo == hi) continue;
            double *w = f < N ? &m->card[f] : f < N + F ? &m->gate[f - N] : &m->pair[f - N - F];
            double lam = f < N ? lam_card : f < N + F ? lam_gate : lam_pair;
            double sum = 0.0;
            for (int k = lo; k < hi; k++) sum += res[rows[k]];
            double count = (double)(hi - lo);
            double next = (sum + count * *w) / (count + lam);
            double delta = next - *w;
            *w = next;
            for (int k = lo; k < hi; k++) res[rows[k]] -= delta;
        }
    }
    double mae = 0.0;
    for (int s = 0; s < n_samples; s++) mae += fabs(res[s]);
    free(res); free(feat); free(foff); free(start); free(fill); free(rows);
    return mae / n_samples;
}

/* ---- the scan: every legal set, the best K by the model, minus the seen ones ---- */

/* five ascending card indices as one integer whose order is the sets'
 * lexicographic order */
static uint64_t set_key(int a, int b, int c, int d, int e) {
    return (uint64_t)a << 32 | (uint64_t)b << 24 | (uint64_t)c << 16 | (uint64_t)d << 8 | (uint64_t)e;
}

static uint64_t mix(uint64_t x) {
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
    return x;
}

typedef struct { double pred; uint64_t key; } Item;

/* a min-heap on (pred, then LARGER key first) so that among equal predictions the
 * lexicographically earlier set is the one kept -- a total order, hence deterministic */
static int worse(const Item *x, const Item *y) {
    return x->pred < y->pred || (x->pred == y->pred && x->key > y->key);
}

static void sift_down(Item *h, int n, int i) {
    for (;;) {
        int l = 2 * i + 1, r = l + 1, s = i;
        if (l < n && worse(&h[l], &h[s])) s = l;
        if (r < n && worse(&h[r], &h[s])) s = r;
        if (s == i) return;
        Item t = h[i]; h[i] = h[s]; h[s] = t; i = s;
    }
}

static void sift_up(Item *h, int i) {
    while (i > 0) {
        int parent = (i - 1) / 2;
        if (!worse(&h[i], &h[parent])) return;
        Item t = h[i]; h[i] = h[parent]; h[parent] = t; i = parent;
    }
}

static int item_cmp(const void *x, const void *y) {
    const Item *a = x, *b = y;
    return worse(a, b) ? 1 : worse(b, a) ? -1 : 0;   /* best first */
}

/* Writes up to `want` sets (5 card indices each, best prediction first) and their
 * predictions; `seen` are keys (`set_key` of ascending indices) to leave out.
 * Returns how many were written, `*n_legal` = how many legal sets exist; -1 on error. */
int hd_sur_scan(const Pool *p, const Model *m, int n_seen, const uint64_t *seen,
                int want, uint8_t *out_sets, double *out_pred, int64_t *n_legal) {
    int N = p->n_cards, K = p->n_kinds;
    if (N > MAX_CARDS || K > MAX_KINDS || want <= 0) return -1;
    uint64_t cap = 16;
    while (cap < (uint64_t)n_seen * 2 + 1) cap <<= 1;
    uint64_t *table = malloc(sizeof(uint64_t) * cap);
    Item *heap = malloc(sizeof(Item) * (size_t)want);
    if (!table || !heap) { free(table); free(heap); return -1; }
    memset(table, 0xff, sizeof(uint64_t) * cap);          /* all-ones = empty */
    for (int s = 0; s < n_seen; s++) {
        uint64_t slot = mix(seen[s]) & (cap - 1);
        while (table[slot] != UINT64_MAX && table[slot] != seen[s]) slot = (slot + 1) & (cap - 1);
        table[slot] = seen[s];
    }
    const int32_t *hol = p->holomem, *type_of = p->type_of, *gens = p->gens;
    const double *card = m->card, *gate = m->gate, *pair = m->pair;
    int cnt[MAX_KINDS];
    memset(cnt, 0, sizeof cnt);
    int filled = 0;
    int64_t legal = 0;
    int idx[4];
    double part[5];
    part[0] = m->bias;

    /* add card c at depth d (0..3): counts go up, the partial sum takes its card,
     * its pairs with the earlier cards and the gate features it newly switches on */
#define PUSH(d, c) do { \
        double v = part[d] + card[c]; \
        for (int q = 0; q < (d); q++) v += pair[idx[q] * N + (c)]; \
        int kind = type_of[c]; \
        if (++cnt[kind] >= 2) v += gate[(cnt[kind] - 2) * K + kind]; \
        for (int g = 0; g < MAX_GENS_PER_CARD; g++) { \
            kind = gens[(c) * MAX_GENS_PER_CARD + g]; \
            if (kind < 0) break; \
            if (++cnt[kind] >= 2) v += gate[(cnt[kind] - 2) * K + kind]; \
        } \
        idx[d] = (c); part[(d) + 1] = v; \
    } while (0)
#define POP(c) do { \
        cnt[type_of[c]]--; \
        for (int g = 0; g < MAX_GENS_PER_CARD; g++) { \
            int kind = gens[(c) * MAX_GENS_PER_CARD + g]; \
            if (kind < 0) break; \
            cnt[kind]--; \
        } \
    } while (0)

    for (int a = 0; a < N; a++) {
        PUSH(0, a);
        for (int b = a + 1; b < N; b++) {
            if (hol[b] == hol[a]) continue;
            PUSH(1, b);
            for (int c = b + 1; c < N; c++) {
                if (hol[c] == hol[a] || hol[c] == hol[b]) continue;
                PUSH(2, c);
                for (int d = c + 1; d < N; d++) {
                    if (hol[d] == hol[a] || hol[d] == hol[b] || hol[d] == hol[c]) continue;
                    PUSH(3, d);
                    double base = part[4];
                    for (int e = d + 1; e < N; e++) {
                        int he = hol[e];
                        if (he == hol[a] || he == hol[b] || he == hol[c] || he == hol[d]) continue;
                        legal++;
                        double v = base + card[e] + pair[a * N + e] + pair[b * N + e]
                                   + pair[c * N + e] + pair[d * N + e];
                        int kind = type_of[e];
                        if (cnt[kind] + 1 >= 2) v += gate[(cnt[kind] - 1) * K + kind];
                        for (int g = 0; g < MAX_GENS_PER_CARD; g++) {
                            kind = gens[e * MAX_GENS_PER_CARD + g];
                            if (kind < 0) break;
                            if (cnt[kind] + 1 >= 2) v += gate[(cnt[kind] - 1) * K + kind];
                        }
                        Item it = { v, set_key(a, b, c, d, e) };
                        if (filled == want && !worse(&heap[0], &it)) continue;
                        uint64_t slot = mix(it.key) & (cap - 1);
                        int hit = 0;
                        while (table[slot] != UINT64_MAX) {
                            if (table[slot] == it.key) { hit = 1; break; }
                            slot = (slot + 1) & (cap - 1);
                        }
                        if (hit) continue;
                        if (filled < want) { heap[filled] = it; sift_up(heap, filled); filled++; }
                        else { heap[0] = it; sift_down(heap, filled, 0); }
                    }
                    POP(d);
                }
                POP(c);
            }
            POP(b);
        }
        POP(a);
    }
#undef PUSH
#undef POP
    qsort(heap, (size_t)filled, sizeof(Item), item_cmp);
    for (int i = 0; i < filled; i++) {
        uint64_t key = heap[i].key;
        for (int q = 0; q < 5; q++) out_sets[5 * i + q] = (uint8_t)(key >> (8 * (4 - q)));
        out_pred[i] = heap[i].pred;
    }
    *n_legal = legal;
    free(table); free(heap);
    return filled;
}
