/* The dynamic Hungarian matching of `hbuilder.domain.joint_matching.JointMatching`, in C.
 *
 * A matching of socket rows to card groups is solved once (the bench) and kept with its dual
 * potentials; `hd_jm_solve` copies it, takes some socket rows out, gives them new weights and
 * puts them back with one augmenting path each (a team's rows). Square form, as in the Python:
 * every card unit is held by a socket row or by one of its group's own unused rows, a socket
 * without a card holds a unit of the open column (index m).
 *
 * BIT-IDENTITY IS THE CONTRACT, as in notewalk.c and connect.c: the augmentation is the Python
 * loop line for line on IEEE-754 doubles (same scan order, same strict comparisons, the rows of
 * a column kept in arrival order, a removal keeping the others' order), built with
 * -O2 -ffp-contract=off and without -ffast-math. tools/web/kernel/test_jointmatch.py holds it
 * to `==` on the assignment and on every potential.
 */
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define JM_MAX_COLS 256 /* groups + the open column */

typedef struct {
    int32_t n, m, rows;
    int32_t *caps;       /* m + 1 */
    double *cost;        /* n x (m + 1): the socket rows' costs (-weight; 0.0 on the open column) */
    int32_t *home;       /* rows: -1 a socket row, else the group of an unused row */
    double *u;           /* rows */
    double *v;           /* m + 1 */
    int32_t *start;      /* m + 1: where each column's rows begin in `held` */
    int32_t *count;      /* m + 1 */
    int32_t *held;       /* rows: each column's rows in arrival order, in its block */
    int32_t *col_of_row; /* rows */
} JM;

static void jm_alloc(JM *s, int n, int m, int rows) {
    s->n = n;
    s->m = m;
    s->rows = rows;
    s->caps = (int32_t *)calloc((size_t)(m + 1), sizeof(int32_t));
    s->cost = (double *)calloc((size_t)n * (size_t)(m + 1) + 1, sizeof(double));
    s->home = (int32_t *)calloc((size_t)rows + 1, sizeof(int32_t));
    s->u = (double *)calloc((size_t)rows + 1, sizeof(double));
    s->v = (double *)calloc((size_t)(m + 1), sizeof(double));
    s->start = (int32_t *)calloc((size_t)(m + 1), sizeof(int32_t));
    s->count = (int32_t *)calloc((size_t)(m + 1), sizeof(int32_t));
    s->held = (int32_t *)calloc((size_t)rows + 1, sizeof(int32_t));
    s->col_of_row = (int32_t *)calloc((size_t)rows + 1, sizeof(int32_t));
}

void hd_jm_free(JM *s) {
    if (!s) return;
    free(s->caps);
    free(s->cost);
    free(s->home);
    free(s->u);
    free(s->v);
    free(s->start);
    free(s->count);
    free(s->held);
    free(s->col_of_row);
    free(s);
}

static void col_remove(JM *s, int j, int32_t i) {
    int32_t *rows = s->held + s->start[j];
    int c = s->count[j];
    for (int k = 0; k < c; k++) {
        if (rows[k] == i) {
            memmove(rows + k, rows + k + 1, (size_t)(c - k - 1) * sizeof(int32_t));
            s->count[j] = c - 1;
            return;
        }
    }
}

/* The reduced-cost scan of row i over the unused columns, as the Python's loops. */
static inline void scan_row(const JM *s, int32_t i, const unsigned char *used, double *minv, int32_t *way_col,
                            int32_t *way_row, int32_t from_col) {
    const int m1 = s->m + 1;
    const double ui = s->u[i];
    if (s->home[i] < 0) {
        const double *ci = s->cost + (size_t)i * (size_t)m1;
        for (int j = 0; j < m1; j++) {
            if (used && used[j]) continue;
            double c = ci[j] - ui - s->v[j];
            if (c < minv[j]) {
                minv[j] = c;
                if (way_col) way_col[j] = from_col;
                way_row[j] = i;
            }
        }
    } else {
        const int cols[2] = {s->home[i], s->m};
        for (int k = 0; k < 2; k++) {
            int j = cols[k];
            if (used && used[j]) continue;
            double c = 0.0 - ui - s->v[j];
            if (c < minv[j]) {
                minv[j] = c;
                if (way_col) way_col[j] = from_col;
                way_row[j] = i;
            }
        }
    }
}

static void augment(JM *s, int32_t r) {
    const int m1 = s->m + 1;
    double minv[JM_MAX_COLS];
    int32_t way_col[JM_MAX_COLS], way_row[JM_MAX_COLS], visited[JM_MAX_COLS];
    unsigned char used[JM_MAX_COLS];
    int nvis = 0;
    for (int j = 0; j < m1; j++) {
        minv[j] = INFINITY;
        way_col[j] = -1;
        way_row[j] = -1;
        used[j] = 0;
    }
    scan_row(s, r, NULL, minv, NULL, way_row, -1);
    int j1;
    for (;;) {
        double delta = INFINITY;
        j1 = -1;
        for (int j = 0; j < m1; j++)
            if (!used[j] && minv[j] < delta) {
                delta = minv[j];
                j1 = j;
            }
        s->u[r] += delta;
        for (int k = 0; k < nvis; k++) {
            int j = visited[k];
            s->v[j] -= delta;
            const int32_t *rows = s->held + s->start[j];
            for (int q = 0; q < s->count[j]; q++) s->u[rows[q]] += delta;
        }
        for (int j = 0; j < m1; j++)
            if (!used[j]) minv[j] -= delta;
        used[j1] = 1;
        visited[nvis++] = j1;
        if (s->count[j1] < s->caps[j1]) break;
        const int32_t *rows = s->held + s->start[j1];
        for (int q = 0; q < s->count[j1]; q++) scan_row(s, rows[q], used, minv, way_col, way_row, j1);
    }
    int j = j1;
    for (;;) {
        int32_t i = way_row[j];
        int32_t pj = way_col[j];
        s->held[s->start[j] + s->count[j]] = i;
        s->count[j] += 1;
        s->col_of_row[i] = j;
        if (pj == -1) break;
        col_remove(s, pj, i);
        j = pj;
    }
}

/* A matching of `n` socket rows (`weights`, n x m) to `m` groups of `caps`, solved: the unused
   rows first, then the socket rows in order. NULL when a size is out of range. */
JM *hd_jm_new(int n, int m, const double *weights, const int32_t *caps) {
    if (n < 0 || m < 0 || m + 1 > JM_MAX_COLS) return NULL;
    int units = 0;
    for (int j = 0; j < m; j++) {
        if (caps[j] < 0) return NULL;
        units += caps[j];
    }
    JM *s = (JM *)calloc(1, sizeof(JM));
    if (!s) return NULL;
    jm_alloc(s, n, m, n + units);
    const int m1 = m + 1;
    int at = 0;
    for (int j = 0; j < m; j++) {
        s->caps[j] = caps[j];
        s->start[j] = at;
        at += caps[j];
    }
    s->caps[m] = n;
    s->start[m] = at;
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < m; j++) s->cost[(size_t)i * m1 + j] = -weights[(size_t)i * m + j];
        s->cost[(size_t)i * m1 + m] = 0.0;
        s->home[i] = -1;
    }
    int r = n;
    for (int j = 0; j < m; j++)
        for (int k = 0; k < caps[j]; k++) s->home[r++] = j;
    for (int i = 0; i < s->rows; i++) s->col_of_row[i] = -1;
    for (int i = n; i < s->rows; i++) augment(s, i);
    for (int i = 0; i < n; i++) augment(s, i);
    return s;
}

/* `out` (a matching of the same sizes, or NULL to allocate one) = `base` with the socket
   rows `rows[0..k)` (ascending) given `weights` (k x m) and re-solved: taken out in order,
   then put back one augmentation each, in order. Returns `out`, or NULL on a bad row. */
JM *hd_jm_solve(const JM *base, JM *out, int k, const int32_t *rows, const double *weights) {
    const int n = base->n, m = base->m, m1 = m + 1, R = base->rows;
    for (int q = 0; q < k; q++)
        if (rows[q] < 0 || rows[q] >= n) return NULL;
    if (!out) {
        out = (JM *)calloc(1, sizeof(JM));
        if (!out) return NULL;
        jm_alloc(out, n, m, R);
    } else if (out->n != n || out->m != m || out->rows != R) {
        return NULL;
    }
    memcpy(out->caps, base->caps, (size_t)m1 * sizeof(int32_t));
    memcpy(out->cost, base->cost, ((size_t)n * (size_t)m1) * sizeof(double));
    memcpy(out->home, base->home, (size_t)R * sizeof(int32_t));
    memcpy(out->u, base->u, (size_t)R * sizeof(double));
    memcpy(out->v, base->v, (size_t)m1 * sizeof(double));
    memcpy(out->start, base->start, (size_t)m1 * sizeof(int32_t));
    memcpy(out->count, base->count, (size_t)m1 * sizeof(int32_t));
    memcpy(out->held, base->held, (size_t)R * sizeof(int32_t));
    memcpy(out->col_of_row, base->col_of_row, (size_t)R * sizeof(int32_t));
    for (int q = 0; q < k; q++) {
        int32_t r = rows[q];
        int col = out->col_of_row[r];
        if (col >= 0) {
            col_remove(out, col, r);
            out->col_of_row[r] = -1;
        }
        double *cr = out->cost + (size_t)r * m1;
        for (int j = 0; j < m; j++) cr[j] = -weights[(size_t)q * m + j];
        cr[m] = 0.0;
    }
    for (int q = 0; q < k; q++) augment(out, rows[q]);
    return out;
}

/* The socket rows holding a group at a positive weight, ascending: `out_row[t]`, `out_col[t]`.
   Returns how many. */
int hd_jm_assignment(const JM *s, int32_t *out_row, int32_t *out_col) {
    const int m1 = s->m + 1;
    int t = 0;
    for (int i = 0; i < s->n; i++) {
        int j = s->col_of_row[i];
        if (j >= 0 && j < s->m && s->cost[(size_t)i * m1 + j] < 0) {
            out_row[t] = i;
            out_col[t] = j;
            t++;
        }
    }
    return t;
}

/* The state, for the bit-identity test: u (rows), v (m + 1), col_of_row (rows). */
int hd_jm_state(const JM *s, double *u, double *v, int32_t *col_of_row) {
    memcpy(u, s->u, (size_t)s->rows * sizeof(double));
    memcpy(v, s->v, (size_t)(s->m + 1) * sizeof(double));
    memcpy(col_of_row, s->col_of_row, (size_t)s->rows * sizeof(int32_t));
    return s->rows;
}
